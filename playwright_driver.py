"""Playwright-backed browser driver — drop-in replacement for TMWebDriver.

Used in headless / container environments where the userscript+WebSocket model
(TMWebDriver) can't work (no real desktop browser to connect back). GA's ga.py /
simphtml.py only touch this public surface:

    execute_js(code, timeout=15, session_id=None)
        -> {'data': <value>}                      # success
        -> {'data': <value>, 'newTabs': [...]}    # success + new tabs opened
        -> {'result': <msg>, 'closed': 1}         # page navigated/reloaded mid-exec
        raises Exception(message)                 # script threw
    get_all_sessions()  -> [{'id', 'url', 'title'}, ...]   (active tabs only)
    get_session_dict()  -> {id: url}
    default_session_id / latest_session_id        (plain attributes, read+write)

Design notes:
- Playwright's *sync* API has thread affinity (objects must be used on the thread
  that created them). GA calls execute_js from agent worker threads, so ALL
  Playwright access is marshalled onto one dedicated worker thread via a queue.
- The in-page execution wrapper is copied verbatim (de-escaped) from the userscript
  bridge `assets/tmwd_cdp_bridge/background.js::buildExecScript`, so results match
  the userscript backend byte-for-byte: same return/await/_air auto-return handling
  and the same `smartProcessResult` (DOM nodes -> outerHTML, Window -> sentinel).
"""
import os
import sys
import re
import json
import time
import queue
import threading

# In-page wrapper, de-escaped from background.js buildExecScript (CDP errorHandler
# variant). RAW string: backslashes (\r \n \s \( \/) are literal regex, not Python
# escapes. The single placeholder __CODE_LITERAL__ is replaced with json.dumps(code),
# which yields a valid double-quoted JS string literal.
_WRAPPER = r"""(async () => {
    function smartProcessResult(result) {
      if (result === null || result === undefined || typeof result !== 'object') return result;
      try { if (result.window === result && result.document) return '[Window: ' + (result.location?.href || 'about:blank') + ']'; } catch(_){}
      if (typeof jQuery !== 'undefined' && result instanceof jQuery) {
        const elements = []; for (let i = 0; i < result.length; i++) { if (result[i] && result[i].nodeType === 1) elements.push(result[i].outerHTML); } return elements;
      }
      if (result instanceof NodeList || result instanceof HTMLCollection) {
        const elements = []; for (let i = 0; i < result.length; i++) { if (result[i] && result[i].nodeType === 1) elements.push(result[i].outerHTML); } return elements;
      }
      if (result.nodeType === 1) return result.outerHTML;
      if (!Array.isArray(result) && typeof result === 'object' && 'length' in result && typeof result.length === 'number') {
        const firstElement = result[0];
        if (firstElement && firstElement.nodeType === 1) {
          const elements = []; const length = Math.min(result.length, 100);
          for (let i = 0; i < length; i++) { const elem = result[i]; if (elem && elem.nodeType === 1) elements.push(elem.outerHTML); } return elements;
        }
      }
      try { return JSON.parse(JSON.stringify(result, function(key, value) { if (typeof value === 'object' && value !== null) { if (value.nodeType === 1) return value.outerHTML; if (value === window || value === document) return '[Object]'; try { if (value.window === value && value.document) return '[Window]'; } catch(_){} } return value; })); } catch (e) { return '[无法序列化: ' + e.message + ']'; }
    }
    try {
      const jsCode = __CODE_LITERAL__.trim();
      const lines = jsCode.split(/\r?\n/).filter(l => l.trim());
      const lastLine = lines.length > 0 ? lines[lines.length - 1].trim() : '';
      const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
      let r;
      function _air(c) { const ls = c.split(/\r?\n/); let i = ls.length - 1; while (i >= 0 && !ls[i].trim()) i--; if (i < 0) return c; const t = ls[i].trim(); if (/^(return |return;|return$|let |const |var |if |if\(|for |for\(|while |while\(|switch|try |throw |class |function |async |import |export |\/\/|})/.test(t)) return c; ls[i] = ls[i].match(/^(\s*)/)[1] + 'return ' + t; return ls.join('\n'); }
      if (lastLine.startsWith('return')) {
        r = await (new AsyncFunction(jsCode))();
      } else {
        try { r = eval(jsCode); if (r instanceof Promise) r = await r; } catch (e) {
          if (e instanceof SyntaxError && (/return/i.test(e.message) || /await/i.test(e.message))) { r = await (new AsyncFunction(_air(jsCode)))(); } else throw e;
        }
      }
      return { ok: true, data: smartProcessResult(r) };
    } catch (e) {
      return { ok: false, error: { name: e.name || 'Error', message: e.message || String(e), stack: e.stack || '' } };
    }
  })()"""


def _build_exec_script(code: str) -> str:
    return _WRAPPER.replace("__CODE_LITERAL__", json.dumps(code))


def _parse_meta_cmd(code):
    """code 若是带 .cmd 的 JSON 对象则返回该 dict，否则 None（当作页面 JS）。"""
    if not isinstance(code, str):
        return None
    s = code.strip()
    if not s.startswith("{"):
        return None
    try:
        p = json.loads(s)
    except Exception:
        return None
    if isinstance(p, dict) and p.get("cmd"):
        return p
    return None


def _safe_title(page):
    try:
        return page.title()
    except Exception:
        return ""


# 反检测 init script：在每个页面/frame 文档创建前注入，抹掉 headless/自动化指纹。
# 覆盖 navigator.webdriver / languages / plugins / window.chrome / permissions。
_STEALTH_JS = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => false }); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] }); } catch (e) {}
  try {
    const mk = (name, filename) => ({ name, filename, description: '', length: 1 });
    const arr = [mk('Chrome PDF Plugin', 'internal-pdf-viewer'),
                 mk('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai'),
                 mk('Native Client', 'internal-nacl-plugin')];
    Object.defineProperty(navigator, 'plugins', { get: () => arr });
    Object.defineProperty(navigator, 'mimeTypes', { get: () => [{ type: 'application/pdf' }] });
  } catch (e) {}
  try { if (!window.chrome) window.chrome = {}; if (!window.chrome.runtime) window.chrome.runtime = {}; } catch (e) {}
  try {
    const orig = navigator.permissions && navigator.permissions.query;
    if (orig) navigator.permissions.query = (p) =>
      (p && p.name === 'notifications')
        ? Promise.resolve({ state: Notification.permission })
        : orig.call(navigator.permissions, p);
  } catch (e) {}
  try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 }); } catch (e) {}
  // WebGL：软件渲染本会报 "Google SwiftShader"（headless 特征）→ 改成合理的 Linux+Mesa 值。
  // 不清空（清空反而是 masking 特征），只替换 UNMASKED_VENDOR/RENDERER。
  try {
    const VENDOR = 'Google Inc. (Intel)';
    const RENDERER = 'ANGLE (Intel, Mesa Intel(R) UHD Graphics (CML GT2), OpenGL 4.6 (Core Profile) Mesa 22.2.0)';
    const protos = [];
    if (self.WebGLRenderingContext) protos.push(WebGLRenderingContext.prototype);
    if (self.WebGL2RenderingContext) protos.push(WebGL2RenderingContext.prototype);
    for (const proto of protos) {
      const gp = proto.getParameter;
      proto.getParameter = function (p) {
        if (p === 37445) return VENDOR;    // UNMASKED_VENDOR_WEBGL
        if (p === 37446) return RENDERER;  // UNMASKED_RENDERER_WEBGL
        return gp.apply(this, arguments);
      };
    }
  } catch (e) {}
})();
"""


def _real_chrome_ua(browser):
    """把默认 HeadlessChrome UA 改成正常 Chrome UA（保持真实大版本号一致）。"""
    try:
        ver = (browser.version or "").split(".")[0] or "136"
    except Exception:
        ver = "136"
    return (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{ver}.0.0.0 Safari/537.36")


def _is_navigation_error(msg: str) -> bool:
    msg = (msg or "").lower()
    return ("execution context was destroyed" in msg
            or "context was destroyed" in msg
            or "target page, context or browser has been closed" in msg
            or "navigating and changing" in msg)


class PlaywrightDriver:
    """Sync-Playwright driver running on a dedicated worker thread."""

    def __init__(self, headless=True, storage_state=None, start_url="about:blank",
                 host=None, port=None):
        # host/port accepted for signature compatibility with TMWebDriver; unused.
        self.default_session_id = None
        self.latest_session_id = None
        self.is_remote = False

        self._headless = headless
        self._storage_state = storage_state
        self._start_url = start_url or "about:blank"
        # 反检测：可用 env 覆盖；UA 为空则启动后按真实 chromium 版本生成正常 Chrome UA
        self._ua = os.environ.get("GA_BROWSER_UA") or None
        self._locale = os.environ.get("GA_BROWSER_LOCALE", "zh-CN")
        self._timezone = os.environ.get("GA_BROWSER_TIMEZONE", "Asia/Shanghai")

        self._pages = {}            # sid(str) -> playwright Page
        self._cdp_sessions = {}     # sid(str) -> CDPSession（懒建，复用）
        self._sid_counter = 0
        self._last_save = 0.0       # storageState 上次回写时间（节流用）
        self._cmd_q = queue.Queue()
        self._ready = threading.Event()
        self._init_error = None
        self._call_lock = threading.Lock()   # serialize callers (GA is sequential anyway)

        self._worker = threading.Thread(target=self._run, name="pw-driver", daemon=True)
        self._worker.start()
        if not self._ready.wait(timeout=90):
            raise RuntimeError("PlaywrightDriver 启动超时（浏览器未就绪）")
        if self._init_error:
            raise self._init_error

    # ── worker thread: owns all Playwright objects ──────────────────
    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=self._headless,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage",
                    # 抹掉 navigator.webdriver=true（自动化标记）
                    "--disable-blink-features=AutomationControlled",
                    f"--lang={self._locale}",
                    # 让 WebGL 真正可用（软件渲染）：不加这些 headless 下 WebGL 全空，
                    # 反而被风控判为"masking/inconsistent"。不要再加 --disable-gpu。
                    "--use-gl=angle", "--use-angle=swiftshader",
                    "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
                ],
            )
            ua = self._ua or _real_chrome_ua(self._browser)
            ctx_kwargs = {
                "viewport": {"width": 1366, "height": 900},
                "user_agent": ua,                 # 去掉 HeadlessChrome 自曝
                "locale": self._locale,
                "timezone_id": self._timezone,
            }
            if self._storage_state and os.path.exists(self._storage_state):
                ctx_kwargs["storage_state"] = self._storage_state
            self._context = self._browser.new_context(**ctx_kwargs)
            # 每个文档创建前注入反检测脚本（webdriver/languages/plugins/chrome/permissions）
            self._context.add_init_script(_STEALTH_JS)
            page = self._context.new_page()
            if self._start_url and self._start_url != "about:blank":
                try:
                    page.goto(self._start_url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    print(f"[PlaywrightDriver] start_url 打开失败（忽略）: {e}")
            self._register_page(page)
            self._ready.set()
        except Exception as e:
            self._init_error = e
            self._ready.set()
            return

        while True:
            item = self._cmd_q.get()
            if item is None:
                break
            fn, holder, ev = item
            try:
                holder["result"] = fn()
            except Exception as e:
                holder["error"] = e
            finally:
                ev.set()

    def _dispatch(self, fn, timeout=30):
        """Run fn() on the worker thread; block until done or timeout."""
        holder, ev = {}, threading.Event()
        self._cmd_q.put((fn, holder, ev))
        if not ev.wait(timeout):
            raise TimeoutError(f"Playwright 操作超时（{timeout}s）")
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")

    # ── page bookkeeping (called on worker thread) ──────────────────
    def _register_page(self, page):
        sid = getattr(page, "_ga_sid", None)
        if sid is None:
            self._sid_counter += 1
            sid = str(self._sid_counter)
            try:
                page._ga_sid = sid
            except Exception:
                pass
            self._pages[sid] = page
            page.on("close", lambda *_: (self._pages.pop(sid, None),
                                         self._cdp_sessions.pop(sid, None)))
        self.latest_session_id = sid
        if self.default_session_id is None or self._pages.get(self.default_session_id) is None:
            self.default_session_id = sid
        return sid

    def _live_page(self, sid):
        p = self._pages.get(sid)
        if p is not None and not p.is_closed():
            return p
        for s, pg in list(self._pages.items()):
            if pg.is_closed():
                self._pages.pop(s, None)
                continue
            self.default_session_id = s
            return pg
        return None

    # ── public surface (mirrors TMWebDriver) ────────────────────────
    def execute_js(self, code, timeout=15, session_id=None):
        if session_id is None:
            session_id = self.default_session_id

        # 元命令探测：与浏览器扩展 bridge 的 ws.onmessage 一致——code 若能解析成
        # 带 .cmd 的 JSON 对象，则路由到 cdp/tabs/cookies/batch，而非当作页面 JS。
        meta = _parse_meta_cmd(code)
        if meta is not None:
            def cmd_job():
                r = self._dispatch_cmd(meta, session_id)
                self._maybe_save_state()    # cdp/tabs 可能触发导航/登录，回写登录态
                return r
            with self._call_lock:
                return self._dispatch(cmd_job, timeout=timeout + 15)

        def job():
            page = self._live_page(session_id)
            if page is None:
                raise ValueError(f"会话ID {session_id} 未连接")
            new_pages = []
            collect = lambda p: new_pages.append(p)
            self._context.on("page", collect)
            try:
                res = page.evaluate(_build_exec_script(code))
            finally:
                try:
                    self._context.remove_listener("page", collect)
                except Exception:
                    pass

            newtabs = []
            for p in new_pages:
                try:
                    p.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
                if p.is_closed():
                    continue
                sid = self._register_page(p)
                try:
                    newtabs.append({"id": sid, "url": p.url, "title": p.title()})
                except Exception:
                    newtabs.append({"id": sid, "url": "", "title": ""})

            if not isinstance(res, dict) or not res.get("ok"):
                err = (res or {}).get("error") if isinstance(res, dict) else None
                msg = err.get("message") if isinstance(err, dict) else str(err or res)
                raise Exception(msg)
            rr = {"data": res.get("data")}
            if newtabs:
                rr["newTabs"] = newtabs
            self._maybe_save_state()    # 页面 JS 可能改 cookie/登录态，回写
            return rr

        with self._call_lock:
            try:
                return self._dispatch(job, timeout=timeout + 15)
            except TimeoutError:
                return {"result": f"No response data in {timeout}s (script may still be running)"}
            except Exception as e:
                if _is_navigation_error(str(e)):
                    # page navigated/reloaded mid-execution — mimic TMWebDriver reload semantics
                    return {"result": f"Session {session_id} reloaded and new page is loading...", "closed": 1}
                raise

    # ── 元命令（与扩展 bridge 等价）：cdp / tabs / cookies / batch ───
    # 全部在 worker 线程上执行（由 execute_js 的 cmd_job 经 _dispatch 调度）。
    # 返回值对齐 bridge → ws → TMWebDriver.execute_js 的 {'data': res.data ?? res.results}。
    def _dispatch_cmd(self, msg, default_sid):
        cmd = msg.get("cmd")
        if cmd == "cdp":
            return {"data": self._cdp(msg.get("tabId") or default_sid,
                                      msg.get("method"), msg.get("params") or {})}
        if cmd == "tabs":
            return {"data": self._tabs(msg)}
        if cmd == "cookies":
            return {"data": self._cookies(msg, default_sid)}
        if cmd == "batch":
            return {"data": self._batch(msg, default_sid)}
        if cmd in ("management", "contentSettings"):
            raise Exception(f"{cmd} 在 playwright 后端不支持（浏览器扩展专用能力，容器内无扩展）")
        raise Exception(f"Unknown cmd: {cmd}")

    def _cdp(self, sid, method, params):
        if not method:
            raise Exception("cdp 命令缺少 method")
        page = self._live_page(str(sid) if sid is not None else self.default_session_id)
        if page is None:
            raise Exception(f"会话 {sid} 无可用页面")
        psid = getattr(page, "_ga_sid", None)
        sess = self._cdp_sessions.get(psid)
        if sess is None:
            sess = self._context.new_cdp_session(page)
            self._cdp_sessions[psid] = sess
        return sess.send(method, params or {})

    def _tabs(self, msg):
        method = msg.get("method")
        if method == "create":
            p = self._context.new_page()
            url = msg.get("url")
            if url:
                try:
                    p.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
            sid = self._register_page(p)
            return {"id": sid, "url": p.url, "title": _safe_title(p)}
        if method == "switch":
            page = self._live_page(str(msg.get("tabId")))
            if page is not None:
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                self.default_session_id = getattr(page, "_ga_sid", self.default_session_id)
            return {"ok": True}
        out = []
        for sid, p in list(self._pages.items()):
            if p.is_closed():
                self._pages.pop(sid, None)
                continue
            out.append({"id": sid, "url": p.url, "title": _safe_title(p),
                        "active": sid == self.default_session_id, "windowId": 0})
        return out

    def _cookies(self, msg, default_sid):
        url = msg.get("url")
        if not url:
            page = self._live_page(str(msg.get("tabId") or default_sid))
            if page is not None:
                url = page.url
        return self._context.cookies(url) if url else self._context.cookies()

    def _batch(self, msg, default_sid):
        R = []

        def resolve(params):
            # 复刻 bridge 的 $N 引用替换："$0.root.nodeId" -> R[0]['root']['nodeId']
            if not params:
                return params or {}
            s = json.dumps(params)

            def rep(m):
                v = R[int(m.group(1))]
                for k in m.group(2).split("."):
                    v = v[k]
                return json.dumps(v)
            return json.loads(re.sub(r'"\$(\d+)\.([^"]+)"', rep, s))

        for c in msg.get("commands", []):
            cmd = c.get("cmd")
            tab_id = c.get("tabId") or msg.get("tabId") or default_sid
            if cmd == "cdp":
                R.append(self._cdp(tab_id, c.get("method"), resolve(c.get("params"))))
            elif cmd == "tabs":
                R.append(self._tabs(c))
            elif cmd == "cookies":
                R.append(self._cookies(c, default_sid))
            else:
                R.append({"ok": False, "error": "unknown cmd: " + str(cmd)})
        return R

    # ── 登录态持久化：把 storageState 回写到共享文件 ──────────────────
    # 浏览器关闭/容器重启后下次 launch 会重新加载它 → 复用登录；
    # 同一文件也被 platform-command 的 playwright 命令读取 → 一次登录两边共享。
    def _maybe_save_state(self, force=False):
        if not self._storage_state:
            return
        now = time.time()
        if not force and (now - self._last_save) < 2.0:
            return
        try:
            os.makedirs(os.path.dirname(self._storage_state), exist_ok=True)
            tmp = self._storage_state + ".tmp"
            # 原子写：先写临时文件再 rename，避免 platform-command 命令读到半截
            self._context.storage_state(path=tmp)
            os.replace(tmp, self._storage_state)
            self._last_save = now
        except Exception as e:
            print(f"[PlaywrightDriver] storageState 回写失败: {e}")

    def get_all_sessions(self):
        def job():
            out = []
            for sid, p in list(self._pages.items()):
                if p.is_closed():
                    self._pages.pop(sid, None)
                    continue
                try:
                    out.append({"id": sid, "url": p.url, "title": p.title()})
                except Exception:
                    out.append({"id": sid, "url": "", "title": ""})
            return out
        with self._call_lock:
            try:
                return self._dispatch(job)
            except Exception:
                return []

    def get_session_dict(self):
        return {s["id"]: s["url"] for s in self.get_all_sessions()}

    def find_session(self, url_pattern: str):
        sessions = self.get_all_sessions()
        if url_pattern == "":
            latest = [s for s in sessions if s["id"] == self.latest_session_id]
            return [(s["id"], s) for s in (latest or sessions[-1:])]
        return [(s["id"], s) for s in sessions if url_pattern in s.get("url", "")]

    def close(self):
        # 关前强制回写一次登录态，避免丢失最新 cookie
        try:
            self._dispatch(lambda: self._maybe_save_state(force=True), timeout=15)
        except Exception:
            pass
        try:
            self._cmd_q.put(None)
        except Exception:
            pass
