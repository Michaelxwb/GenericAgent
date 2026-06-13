# 定时任务 SOP

目录：`../sche_tasks/` 放任务定义JSON，`../sche_tasks/done/` 放执行报告

## ⭐结果会自动推送给创建者（不要再问用户投递方式）
本框架**到点会把任务的最终结果（含文件附件）自动主动推送回创建者的 IM 渠道**（企微/微信/Telegram 等），由前端完成，**你无需关心“怎么发给用户”**：
- 用户说“每天8点发我天气”这类需求 → 直接建任务即可，**别问“文件/邮件/webhook 哪种”**——框架自带推送。
- 你在 prompt 里把要做的事写清楚（如“获取深圳天气并简洁汇报”），执行产出的最终回复就是推送内容。
- `target`（接收者 uid）**由前端在你写 sche_tasks/*.json 时自动盖上**（谁创建谁接收），你不用手填。

## 任务JSON格式（*.json）
```json
{"schedule":"08:00", "repeat":"daily", "enabled":true, "prompt":"...", "max_delay_hours":6}
```
repeat可选：daily | weekday | weekly | monthly | once | every_Nh（每N小时）| every_Nd（每N天）
max_delay_hours（可选，默认6）：超过schedule多少小时后不再触发，防止开机太晚执行过时任务
（target 字段由前端自动注入，无需你写）

## 触发流程
1. scheduler.py（reflect/）每60秒轮询 sche_tasks/*.json
2. 条件全满足才触发：enabled=true + 当前时间≥schedule + 冷却时间已过（基于done/最新报告时间戳）
3. 触发时拼prompt，含报告路径 `../sche_tasks/done/YYYY-MM-DD_任务名.md`
4. **收到任务后第一件事**：用 update_working_checkpoint 记录报告目标文件路径，防止长任务执行中遗忘
5. 执行完毕后将报告写入上述路径（scheduler靠此文件判断今天已执行）

## 日志与监控
- scheduler自动写日志到 `sche_tasks/scheduler.log`（触发/跳过/错误）
- `scheduler.health_check()` 返回所有任务状态列表（HEALTHY/OVERDUE/DISABLED/NEVER_RUN/ERROR）
- JSON解析错误、schedule格式错误、未知repeat类型均会记录日志

## 注意
- once类型：执行一次后冷却100年（实际效果为永久跳过）
- 任务文件只管"干什么"，报告路径由scheduler自动生成注入prompt
- sche_tasks目录在../，即code root下