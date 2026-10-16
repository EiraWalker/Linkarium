# 后台任务错误收集与调试

实现日期：2026-10-10。用途是定位当前任务错误；诊断导出不会重试、取消或修改生产任务，也不新增前端按钮。

## 一键收集

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/diagnose_tasks.py
```

默认读取正在使用的 generation，从**最新网页刷新批次**导出快照，而非混合所有历史失败；通过 `--group <完整批次UUID>` 可选择其他批次。输出位于 `output\task-diagnostics\<时间>\report.json` 和 `report.md`。

报告包含批次、设备任务数、状态/错误码汇总、失败任务 ID、域名、桌面/手机、尝试次数、尝试开始/结束时间和可用的阶段诊断。只读 SQLite URI + `query_only`，不初始化库、不运行迁移、不改队列。

网站可以包含桌面和手机设备任务，实际数量以读取时的状态为准。任务的 `updated_at` 并未在每次状态更新时刷新，排查发生时间应看 **JobAttempt.created_at/finished_at**。

## 自动收集路径

当前 Windows development 采集子进程自动写入：

```text
data\diagnostics\capture\<attempt-UUID>.json
```

诊断文件在临时快照工作目录以外，任务结束清理临时 HTML 时不会丢失。每个新子进程从磁盘读取采集代码，因此当前队列无需重启即可开始收集。修改前已结束的任务没有详细记录，报告会明确显示 `diagnostic_available=false`，不能还原当时未保存的 HTTP 状态或异常详情。

记录导航 HTTP 状态、阶段耗时、原页/回放图片数量、HTML 大小、异常类型/安全特征、代码位置；不存页面正文、标题、完整网址、query、Cookie、请求头、凭据或原始异常文本。事件最多 80 条，默认保留最近 14 天、最多约 1000 个尝试文件（新子进程启动时清理）。写入失败不改变任务成败。

明文 HTTP 代理错误另存于 `proxy_http_responses`：`origin=local_proxy` 表示本地连接、协议或预算错误生成的响应，`origin=upstream` 表示上游实际返回的错误状态。记录域名、出错阶段、HTTP 状态和脱敏错误链，最多保留最近 40 条错误；`total`/`dropped` 标记总量与截断。正常响应不占额度，内部标记不会写入网页或响应头。此字段在代理关闭时统一落盘，不覆盖采集阶段；进程被强杀时可能来不及写出。HTTPS 隧道没有解密网页响应，不能用该字段判断 HTTPS 源站状态。

阶段：网络准备 → URL 校验 → 浏览器启动 → 导航 → 首屏就绪 → 状态固化 → HTML 改写 → Monolith 归档 → 离线回放 → 质量检查。每阶段落盘，因此进程被 120 秒 watchdog 强杀也留下最后到达位置；文件的 `running` 只是最后写入状态，应以数据库尝试状态为准。导出用 `diagnostic_incomplete=true` 标注这种中断。

自动路径仅适用于既有 `work/capture/<attempt-UUID>` development 工作目录。Linux 隔离采集器可通过 `python -m capture --request <请求文件> --diagnostics <允许写入的路径>` 收集；本次未部署或验证 Linux launcher 日志汇出。

## 定点复现

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/diagnose_tasks.py --group <批次UUID> --replay-job <任务UUID> --dns-mode public-doh
```

`--replay-job` 可重复指定，串行运行。它读取原任务的网址和设备，使用现有动态快照流水线、120 秒预算、公网地址校验及匿名浏览器。诊断默认采用 development + public-doh 模式，其他启动模式需显式选 `--dns-mode system`。仅导出时不产生网络抓取。

复现产物 `replay-<job-UUID>-<随机后缀>.json` 关联原任务；临时请求和 HTML 随后清理。**不认领原任务、不更新快照、不改变队列状态**。网页和网络会变化，复现成功不能证明历史失败不存在。生产队列同时工作时，耗时还会受并发资源竞争影响。

## 验证

### 2026-10-11：坏图验收规则

图片验收现在按每张图片的源状态和首屏可见性判断，不再因整页任意坏图拒绝归档：

- 冻结时覆盖写入每个 `img` 的原始加载状态及首屏可见性，随 HTML 一起经过改写和 Monolith；不使用坏图总数相减判断新增损坏。
- 原页面已经确定加载失败（加载完成但没有可解码图片）的图片允许保留，记录 `source-broken-images:N`；首屏仍有原有坏图时记录 `source-broken-first-screen-images:N`。尚未加载完成的图片单独记为 `source-pending-images:N`，不能享有原站坏图豁免。
- 屏外、透明父容器或被滚动／裁剪容器完全裁掉的坏图记录 `offscreen-broken-images:N`，不导致整个任务失败。
- 原本正常的首屏图片，或回放首屏出现且没有原有坏图基线的图片，若回放损坏，仍返回 `CAPTURE_REPLAY_FAILED`。诊断原因是 `archive_new_first_screen_broken_images`；保留源可见性可防止损坏图片尺寸塌缩后逃过检查。
- 日志 `replay_check.new_visible_broken` 表示影响源首屏或回放首屏的新增坏图数。可见性使用视口、祖先透明度及 overflow 裁剪，不提供像素级遮挡判定。10 月 15 日的后续修复已将正文/图片检查扩展到实际归档的嵌套 iframe；CSS 背景仍没有逐图片解码门禁。不能将零新增坏图当作动态应用已完成初始化或整页视觉完整的证明。

文本丢失、外部网络请求、地址安全检查、媒体固化及容量限制保持原规则。既有成功快照和历史任务状态不修改；Windows development 的后续采集子进程自动读取新代码。历史失败需要重试后才重新验收。

针对性验证：`tests.capture.test_image_quality` 使用真实 Chromium、改写器和 Monolith，覆盖桌面／手机原有坏图、屏外及隐藏／裁剪图片、逐图损坏（坏图总数相同）、尺寸塌缩和正常图片。

```powershell
$env:PYTHONPATH = "$PWD\backend;$PWD"
$env:RUN_CAPTURE_INTEGRATION='1'
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.capture.test_diagnostics tests.capture.test_task_report tests.capture.test_development -v
```

验证异常脱敏、阶段即时落盘、磁盘写入失败、自动路径边界、真实 HTTP 403 子进程错误跨临时目录清理保留，以及正常动态归档和进程取消/超时行为。
