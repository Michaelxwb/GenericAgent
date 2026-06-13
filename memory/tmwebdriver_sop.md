# TMWebDriver SOP

- 直接用web_scan/web_execute_js工具。本文件只记录特性和坑。
- **两种后端**（由 `ga.first_init_driver` 按环境自动选，对工具透明）：
  - **PlaywrightDriver**（容器/headless 默认，无 DISPLAY 时）：容器内自带 headless Chromium，登录态走 `storageState`。**无浏览器扩展、无桌面**。
  - **TMWebDriver**（桌面，有 DISPLAY 或 `GA_BROWSER_BACKEND=tmwd`）：通过 Chrome 扩展接管用户真实浏览器，保留登录态/Cookie。
- 下方 CDP/元命令配方（`{"cmd":"cdp|tabs|cookies|batch"}`、`Page.captureScreenshot`、`DOM.*`、CDP 点击等）**两种后端通用**——playwright 经 `new_cdp_session` 透传同样的 CDP 命令。
- 标注 **[仅 TMWebDriver]** 的段落只适用扩展/桌面后端，playwright 后端不适用。

## 通用特性
- ⚠web_execute_js里使用`await`时需**显式`return`**才能拿到返回值（底层async包裹，不写return则返回null）
- ✅web_scan自动穿透同源iframe；跨域iframe需CDP或postMessage（见下方章节）

## 限制(isTrusted)
- JS事件`isTrusted=false`，敏感操作（如文件上传/部分按钮）可能被拦截；这类场景首选**CDP桥**
- ⚠JS点击按钮打不开新tab→可能是浏览器弹窗拦截，换CDP点击试试
- Vue3自定义组件(Select/Dropdown)：⭐优先vnode实例调用(无视口限制)→见**vue3_component_sop**；CDP坐标点击仅适合选项少且可见的场景
- 文件上传：⭐首选**DataTransfer API**（纯JS，无CDP依赖）：`new File([content],name,{type}) → new DataTransfer().items.add(file) → input.files=dt.files → dispatch input+change`；CDP `DOM.setFileInputFiles` 在tmwd桥环境nodeId跨调用失效，不推荐；备选ljqCtrl物理点击
- **[仅 TMWebDriver]** 需转物理坐标时（ljqCtrl 物理点击，依赖桌面屏幕）：`physX = (screenX + rect中心x) * dpr`，`physY = (screenY + chromeH + rect中心y) * dpr`；其中 `chromeH = outerHeight - innerHeight`。playwright headless 无物理屏，用 CDP `Input.dispatchMouseEvent`（视口坐标=getBoundingClientRect）

## 导航
- `web_scan` 仅读当前页不导航，切换网站用 `web_execute_js` + `location.href='url'`

## Google图搜
- class名混淆禁硬编码，点击结果用 `[role=button]` div
- web_scan过滤边栏，弹出后用JS：文本`document.body.innerText`，大图遍历img按`naturalWidth`最大取src
- "访问"链接：遍历a找`textContent.includes('访问')`的href
- 缩略图：`img[src^="data:image"]`直接提取；大图src可能截断用`return img.src`

## Chrome下载PDF
场景：PDF链接在浏览器内预览而非下载
```js
fetch('PDF_URL').then(r=>r.blob()).then(b=>{
  const a=document.createElement('a');
  a.href=URL.createObjectURL(b);
  a.download='filename.pdf';
  a.click();
});
```
注意：需同源或CORS允许，跨域先导航到目标域再执行

## Chrome后台标签节流
- 后台标签中`setTimeout`被Chrome intensive throttling延迟到≥1min/次，扩展脚本中避免依赖setTimeout轮询
- 某些SPA页面需CDP `Page.bringToFront`切到前台才会加载数据

## CDP 元命令 ⭐首选（两后端通用）
- **PlaywrightDriver**：driver 内 `context.new_cdp_session(page)` 透传 CDP，无需扩展，开箱即用
- **[仅 TMWebDriver]** 走 Chrome 扩展 `assets/tmwd_cdp_bridge/`(需安装，含debugger权限)；⚠TID约定标识首次运行生成到 `config.js`(已gitignore)，扩展通过manifest引用
调用（两后端一致）：`web_execute_js` script直传JSON字符串（工具层自动识别对象格式并路由到 cdp/tabs/cookies/batch）
```js
// 直接传JSON字符串作为script参数，无需DOM操作
web_execute_js script='{"cmd": "cookies"}'
web_execute_js script='{"cmd": "tabs"}'
web_execute_js script='{"cmd": "cdp", "tabId": N, "method": "...", "params": {...}}'
web_execute_js script='{"cmd": "batch", "commands": [...]}'
// 返回值直接是JSON结果
```
通信方式：⭐JSON字符串直传(首选，两后端通用) | **[仅 TMWebDriver]** TID DOM方式(TID元素+MutationObserver，扩展底层依赖)
单命令：`{cmd:'tabs'}` | `{cmd:'cookies'}` | `{cmd:'cdp', tabId:N, method:'...', params:{...}}`（三者两后端通用）
- **[仅 TMWebDriver]** `{cmd:'management', method:'list|reload|disable|enable', extId:'...'}`：扩展管理；playwright 后端无扩展，调用会报不支持
  - management：list返回所有扩展信息；reload/disable/enable需传extId
- **[仅 TMWebDriver]** `{cmd:'contentSettings', type:'automaticDownloads', pattern:'https://*/*', setting:'allow'}`：扩展专用；playwright 后端不支持
  - 绕过Chrome"下载多个文件"对话框（该对话框会阻塞整个浏览器JS执行）
  - type可选：automaticDownloads/popups/notifications等；setting：allow/block/ask
  - ⚠CDP的Browser.setDownloadBehavior在扩展中不可用（chrome.debugger仅tab级），此为替代方案；playwright 后端反而可直接用 CDP `Browser.setDownloadBehavior`
- ⭐batch混合：`{cmd:'batch', commands:[{cmd:'cookies'},{cmd:'tabs'},{cmd:'cdp',...},...]}`
  - 返回`{ok:true, results:[...]}`，一次请求多命令，CDP懒attach复用session
  - 子命令会自动继承外层batch的tabId（如cookies命令可正确获取当前页面URL）
  - `$N.path`引用第N个结果字段(0-indexed)，如`"nodeId":"$2.root.nodeId"`
  - ⚠batch前序命令失败时，后续`$N`引用会静默变成undefined；要检查results数组中每项的ok状态
  - 典型文件上传：getDocument(**depth:1**) → querySelector(`input[type=file]`) → setFileInputFiles
  - 思想：
    - 同一链路内保持nodeId来源一致，不混用querySelector路径与performSearch路径
    - 上传后前端框架可能不感知，必要时JS补发`input`/`change`事件
    - 上传前检查`input.accept`；多input时用accept/父容器语义区分
    - 等待元素优先用`DOM.performSearch('input[type=file]')`做轻量轮询
    - 瞬态input的核心是**缩短发现→setFileInputFiles时间窗**：优先同batch完成；再不行用DOM事件监听；猴子补丁仅作兜底思路
  - ⚠tabId：CDP默认sender.tab.id(当前注入页)，跨tab需显式tabId或先batch内tabs查
- ⭐跨tab无需前台：指定tabId即可操作后台标签页

## CDP点击完整生命周期（✅已验证）
- 通用点击需**三事件序列**：mouseMoved → mousePressed → mouseReleased（间隔50-100ms）
  - 省略mouseMoved会导致MUI Tooltip/Ant Design Dropdown等hover依赖组件失效
  - ⚠autofill释放是特例，只需mousePressed即可（见下方autofill章节）
- ⭐**坐标系结论**：稳定状态下 CDP坐标 = `getBoundingClientRect()` 坐标，**无需修正**
  - **[仅 TMWebDriver]** ⚠**首次attach陷阱**：扩展 chrome.debugger 首次attach时Chrome弹出infobar("正在受自动化控制"，~20px高)，页面内容被推下；attach前测坐标→attach后点击会偏移。解决：测坐标在attach稳定后，或先发无害`mouseMoved(0,0)`预热。**playwright headless 无 infobar，无此问题**
- ⭐**下拉框(Vue3 oxd-select等)CDP操作流程**：
  1. 获取select元素rect → CDP点击打开下拉
  2. 获取option元素rect → CDP点击选中（option是动态DOM，打开后才能测量）
  - 已验证：CDP点击对自定义下拉框有效，无isTrusted问题
  - ⚠**限制**：选项多时底部option超出视口，CDP坐标够不着→此时应优先vnode方案(见vue3_component_sop)
- 坐标修正（页面有transform:scale/zoom时）：
  ```js
  var scale = window.visualViewport ? window.visualViewport.scale : 1;
  var zoom = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
  var realX = x * zoom; var realY = y * zoom;
  ```
- iframe内元素CDP点击：坐标需合成 `finalX = iframeRect.x + elRect.x`
  - 跨域iframe拿不到contentDocument：
  - ⚠`Target.getTargets`/`Target.attachToTarget`在CDP桥中返回"Not allowed"(chrome.debugger权限限制)
  - ⭐**已验证方案**：`Page.getFrameTree`找iframe frameId → `Page.createIsolatedWorld({frameId})`获取contextId → `Runtime.evaluate({expression, contextId})`在iframe中执行JS
  - batch链式引用：`$0.frameTree.childFrames`遍历找url匹配的frame，`$1.executionContextId`传给evaluate
  - postMessage中继方案仅在content script已注入iframe时有效，第三方支付iframe通常无注入

## CDP文本输入（未验证，BBS#23）
- `insertText`快但无key事件；受控组件需补dispatch `input`事件
- 需完整键盘模拟时用`dispatchKeyEvent`逐键派发

## CDP DOM域穿透 closed Shadow DOM（未验证，BBS#24/#25）
- `DOM.getDocument({depth:-1, pierce:true})` 穿透所有Shadow边界（含closed）
- `DOM.querySelector({nodeId, selector})` 定位 → `DOM.getBoxModel({nodeId})` 取坐标
- getBoxModel返回content八值[x1,y1,...x4,y4]，中心用**四点平均**：centerX=sum(x)/4, centerY=sum(y)/4
  - ⚠不能简化为对角线平均——元素有transform:rotate/skew时四点非矩形
- querySelector**不能跨Shadow边界写组合选择器**，需分步：先找host再在其shadow内找子元素
- ⚠nodeId在DOM变更后失效 → 用`backendNodeId`更稳定，或重新getDocument刷新


## autofill获取与登录
检测：web_scan输出input带`data-autofilled="true"`，value显示为受保护提示(非真实值，Chrome安全保护需点击释放)
- ⚠**前置条件：必须先CDP `Page.bringToFront` 切tab到前台**，Chrome仅在前台tab释放autofill保护值，后台tab物理点击无效
- ⭐**一键释放与登录**：bringToFront → mousePressed点任一字段(无需Released，一个释放全页) → 等500ms → 补input/change事件 → 点登录

## 验证码/页面视觉截图
- ⭐⭐**截图存文件首选**（PlaywrightDriver，一步出合法 PNG，勿手动处理 base64）：
  `web_execute_js script='{"cmd":"screenshot","path":"/data/platform/output/x.png","fullPage":true}'`
  （可选 `"selector":"css"` 只截某元素）→ 直接存合法文件 → 回复 `[FILE:/data/platform/output/x.png]` 发给用户。
  **不要**自己 `code_run` 解 base64 写文件——极易把 base64 文本当字节存成坏图，微信/企微打不开。
- **[仅 TMWebDriver]** CDP截图：`Page.captureScreenshot`(format:'png')→返回base64（自己解码存盘易出错，能用上面的 screenshot 就别用这个）
- 验证码canvas/img：JS `canvas.toDataURL()` 直接拿base64最干净

## simphtml与driver调试
- simphtml调试必须通过`code_run`注入JS到浏览器（Python端无法模拟DOM）
- 复用当前 driver：`import ga; ga.first_init_driver(); ga.driver.execute_js(code)` → 返回`{'data': value}`（两后端通用，勿在 playwright 后端手动 `TMWebDriver()`）
- simphtml：`str(simphtml.optimize_html_for_tokens(html))` — 返回BS4 Tag需str()

## 连不上排查（先确认后端：`import ga; ga.first_init_driver(); print(type(ga.driver).__name__)`）

### PlaywrightDriver（容器/headless 默认）
浏览器是容器内自带的，**不存在"浏览器没开/扩展没装/WS master"问题**，按序查：
①driver 起不来？→ 看容器日志是否有 `PlaywrightDriver 启动超时` / chromium 启动报错；确认镜像装了 `playwright==1.52.0` 且 `/ms-playwright` 有浏览器二进制
②没有标签页（web_scan 返回空）→ 先 `web_execute_js location.href='url'` 导航出一个页面再 scan
③未登录/拿不到数据 → `storageState` 过期，需重新登录刷新 `GA_BROWSER_STORAGE_STATE` 指向的文件（与桌面扩展无关）

### [仅 TMWebDriver]（桌面/扩展后端）
web_scan失败时按序排查（自动检测优先，用户参与放最后）：
①浏览器没开？→检查浏览器进程是否在跑(tasklist/ps)，没有则启动并打开正常URL（⚠about:blank等内部页不加载扩展）
②WS后台挂了？→本机18766端口没监听即dead→手动**后台持续运行**`from TMWebDriver import TMWebDriver; TMWebDriver()`起master
③扩展没装？→读Chrome用户目录下`Secure Preferences`→`extensions.settings`中找`path`含`tmwd_cdp_bridge`的条目
  找到→扩展已装，排查其他原因；没找到→走web_setup_sop
④以上都正常仍连不上→请求用户协助
