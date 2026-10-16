# 隔离浏览器耗时采集

入口：`scripts/performance_browser.py`。仅搭建观测，既有业务操作和调度逻辑不变。

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\performance_browser.py --base-url http://127.0.0.1:8001 --output output\performance\example\browser.json
```

默认桌面 1440×1000、手机 390×844，各最多 60 秒。手机是 Chromium 模拟视口。每端测独立新上下文的冷加载、同上下文启用 HTTP 缓存的再次加载，以及点击设备切换、打开详情后的图片显示。隔离 Chromium 进程的缓存清理不会影响日常浏览器；CDP 方法拦截不采用会禁用 HTTP 缓存的 Playwright `route()`。

加 `--extra-scenarios` 观测同一应用会话的滚动、翻页、文件夹和设置往返。`--devices desktop` 可只采一端，`--timeout 30` 调整每端总预算。超时写 `incomplete`，不会把缺少样本当作通过。

`browser.json` 的 schema_version 为 1，包含逐次导航的墙钟耗时、导航/paint 指标、long task 样本、JS/CSS/API/图片的网络和 Resource Timing 样本、传输字节和可用缓存命中信号。p50/p95 保留样本数及原始数值；并行资源的耗时相加不代表页面耗时。`Server-Timing` 中的 app/db 等白名单字段和合法 `X-Request-ID` 可与服务日志关联。

显示阶段单列：资源响应头、资源 body 完成、`img.decode()` 和有效自然尺寸、预览 ready、外层连续两次 requestAnimationFrame 后的绘制机会。图片样本可以将响应头/body 完成与该图的 decode/绘制机会串接；同一应用会话已经下载的图标注 cached_before_action，其下载阶段留空。区分首张、所有可用预览、屏幕内图片。这里测量浏览器可观测到的绘制机会，无法证明显示器实际呈现时间。缺少 HTML 的卡片记 missing；已有 HTML 但图片仍在生成记 pending/202，不等待整个旧库补图。

默认还对已有 HTML 抽样每端一个最小的、最多 2 MiB 的版本。根据列表 API 的 byte_size 选择，并 HEAD 再次核对 Content-Length；在 sandbox iframe 内关闭脚本并保留服务 CSP，测离线 HTML 下载、iframe load 和外层绘制机会。`--html-sample 0` 跳过，`--html-max-bytes` 显式扩大单个样本上限，最高 96 MiB。不会为所有列表卡片重新加载 HTML。

请求只允许指定回环源的 GET/HEAD，阻断 POST/PATCH/PUT/DELETE 和外部 URL。临时创建 15 分钟 owner session，finally 删除，不打印密钥，也不改变 owner 的 last_login。现有 thumbnail GET 的业务语义可能为缺失派生图写入优先补图标记，报告明确记录这一点；不会触发原网站抓取或 AI 分类。报告只保留白名单 route 模板和数字，不保存网址、标题、搜索词、响应正文、截图或 cookie。

已知边界：临时会话要求此本机服务使用当前仓库的数据目录；同端的冷暖加载可能遇到后台缓存补图和正在执行的任务，报告应与同期后端采集一起解读。浏览器探针自身的 CDP 拦截和 PerformanceObserver 也有少量开销。

HTML 抽样失败也会保留 `stage`、白名单 `error_code`、HEAD 请求耗时和已观察到的网络阶段；收到响应时另存状态、长度及安全的 Server-Timing/Request-ID。不会保存原始异常消息或请求 header，也不会自动重试换取成功。首次总采集中的手机 HTML 抽样未完成；定点观察出现过在 HEAD 阶段失败、尚未进入宿主导航的情况。尚未确定 HTTP 服务或传输层原因，也没有证据认定 JSON 宿主导致该失败，因此不修改软件业务或采样宿主。
