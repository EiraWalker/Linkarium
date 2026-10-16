# 软件耗时监测工作流

## CPU 与图片显示的专项诊断

2026-10-16 新增 `scripts/performance_cpu.py`。它分别记录正式 Web 进程的 CPU 差值、临时 Web 实例的请求区间，以及独立单线程 Django GET 批次的 CPU。没有修改正式服务的并发、缓存、鉴权和业务代码，也不重启正式服务。

先从当前监听端口确认 Web PID，再执行；输出目录必须不存在：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8001 | Select-Object OwningProcess
.\.venv\Scripts\python.exe -X utf8 scripts\performance_cpu.py collect --base-url http://127.0.0.1:8001 --web-pid <上一步的PID> --output output\performance\cpu-new-run
.\.venv\Scripts\python.exe -X utf8 scripts\performance_cpu_report.py output\performance\cpu-new-run
```

当前 CPU 采集器使用 Windows 原生进程计数。桌面/手机各测 3 轮冷暖，共 12 次正式服务导航和 12 次临时服务导航；临时服务使用同一库，只允许 GET/HEAD，阻止 SQL 数据修改和缺图补图请求，测试结束后关闭。每次导航同时记录浏览器图片计时；临时请求按 Request ID 关联。单线程批次测网站列表、目录统计、任务历史列表与 JPEG 200/304，请求使用现有快照，不启动抓取或 AI。临时会话在退出时删除。

区分三个数值：

- CPU 时间：操作系统累计的实际运行量，包含 Python、SQLite、哈希和原生系统调用；不是纯解释器时间。
- 请求区间：还包含数据库/文件等待、线程调度与流式消费者的拉取间隔；WSGI 外的排队不在其中。
- 图片加载跨度：浏览器第一张缩略图请求发出，到当前页全部可用预览解码就绪；桌面的并发区间不能相加。

Windows CPU 记账有粒度，短请求可能显示 0，也可能某次 CPU 差值超过其区间。使用较长批次的累计 CPU 求平均；不要据单次短请求算精确 CPU 占比。`cProfile` 使用单独样本定位调用热点，其函数区间不是独占 CPU，不将分析器开销混入批次 CPU。真实进程 CPU 还可能包含用户其他页面的请求，不作为某张图片的独占成本。

报告包括整页图片时间、Web CPU、单核/整机占用、每类请求平均 CPU、手机串行加载的服务端/链路/前端推进分段，以及可展开的函数明细。它不把可消除的理论 CPU 工作量当作换语言的实际提速承诺。

实现日期：2026-10-16。本次只增加监测、采集与报告，不调整缓存策略、分类算法、并发、重试、超时、数据库索引或资源预算。没有新增产品按钮、遥测服务器或第三方运行依赖。

## 使用

在项目根目录执行。以下命令以 `http://127.0.0.1:8001` 为本机服务地址示例，请按实际启动地址调整。

重新启动软件时，使用既有启动参数并加 `-PerformanceMonitoring`：

```powershell
.\scripts\start.ps1 -Port 8001 -DevelopmentCapture -PublicDns -PerformanceMonitoring
```

此命令用于启动服务，不要在已有实例运行时再开一个实例。Linux 部署或直接启动可设置 `BOOKMARK_PERF=1`；进程启动时读取开关。未设置开关默认关闭。更换开关需正常重启服务；监测脚本自身不重启或控制服务。

一次收集已有记录、30秒资源样本、桌面/手机浏览器探测：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\performance.py collect --browser --duration 30 --base-url http://127.0.0.1:8001
```

在采集期间可照常操作软件。API 和正常后台任务会产生关联记录；采集命令不新建抓取、AI 分类、导入、备份、恢复或重试。默认浏览器探测含双端冷暖列表、设备切换、详情与每端一个不超过2MiB的已有HTML抽样。浏览器可能比资源采样窗口运行更久，两者分别记录实际时间，不把30秒宣称为整个命令的截止时间。

只读清单与后台证据，不启动浏览器：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\performance.py collect --duration 60
```

可增加 `--extra-scenarios` 测量滚动、分页、文件夹和设置页往返。场景有有界时间预算，未完成会如实标记 `incomplete`，不会通过延长超时或修改软件来获得完成结果。默认每种设备总预算60秒，可用 `--browser-timeout` 显式配置。指定 `--devices desktop` 可只测桌面。

输出默认在 `output\performance\<台北时间>`。`--output <新目录>` 指定目录，须不存在。`--data-root` 明确指定本机库目录，同时传给浏览器临时会话；`--log-dir` 可读另存的监测日志。浏览器探测仅支持本机HTTP服务，拒绝将本机登录会话发往远程服务。

对已有采集重新生成报告，不访问软件：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\performance.py report output\performance\first-monitored-run
```

与已保存报告比较相同操作、路由、设备、阶段和时间口径的P95：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts\performance.py collect --browser --baseline output\performance\first-monitored-run\report.json
```

对比结果保存在 `report.json` 的 `comparison`，包含两次样本数。执行环境、库大小、代理、冷暖状态和后台负载变化时，不将差值归因于代码变化。

## 覆盖范围

| 环节 | 实际记录 | 主要边界 |
| --- | --- | --- |
| 服务启动与退出 | 恢复检查、启动子服务、等待ready、正常退出 | 强制终止可能无结束记录；不主动反复重启来测量 |
| HTTP/API | 处理耗时、HTTP状态、响应字节、Request ID、Server-Timing | 从Django中间件开始，非Waitress之前的排队/完整网络耗时 |
| 数据库 | 每请求SQL驱动调用次数、累计耗时 | 不保存SQL/参数，不包含取回后Python逐行处理；同步线程作用域 |
| 页面加载 | 文档TTFB、DOM/load、FP/FCP、脚本/样式/API/图片请求、传输量、长任务 | 隔离Chromium；冷暖分别记录；主线程长任务不是全部CPU时间 |
| 快照显示 | 图片响应头/body结束、解码完成、首张ready、整页ready、外层两次rAF、可视区图像 | 绘制机会不等于显示器硬件实际呈现；缺图pending不能填零耗时 |
| 操作后的预览 | 设备切换、详情打开、可选页面/目录往返 | 以内存已缓存图片复用为独立场景，不重算不存在的新下载阶段 |
| 完整HTML显示 | 已保存小样本HEAD、受限iframe加载、外层绘制机会、字节与状态 | 每端最多1份默认≤2MiB，不关闭sandbox或执行归档脚本；失败按阶段保留 |
| 任务队列 | 创建至认领、available_at至认领、各attempt执行、重试间隔、批次墙钟 | job_age与排队不同；旧缺失结束时间不借updated_at猜测 |
| 网页抓取 | 现有导航/就绪/冻结/改写/Monolith/重放等事件区间、预算、状态 | 相邻累计标记差分；不是每个函数独占耗时；默认最多现有1000份记录 |
| 快照发布 | HTML hash、复制、发布、元信息保存、工作目录清理 | 与抓取分开；子区间包含在父区间内，不相加 |
| 图片补图 | HTML校验、离线子进程、图片写入、单次补图总时长 | 只观测既有补图流程；本次不调整吞吐或失败策略 |
| AI分类 | job整体、AI请求和读响应、JSON解析、响应状态 | 不存prompt/response/密钥，不主动调用AI；未使用时保留空缺 |
| 导入与整理 | API处理、import job、分类job、常规CRUD总耗时与SQL | 不为了测量自动导入或批准分类；部分历史导入无可靠完成时间 |
| 备份恢复 | 维护stage、数据库复制/校验、文件复制、压缩、压缩包验证、恢复解包校验 | 只在用户正常执行维护时产生；采集不会启动维护 |
| 资源与存储 | 系统CPU/可用物理内存/提交额度，软件进程CPU/RSS/private commit，DB/HTML/图片/日志大小 | Windows原生只读API；Linux无private_commit时为null；RSS合计含共享页 |

## 如何读报告

- `report.html`：任务列表，显示每项的排队、执行及总耗时；点击“阶段耗时”展开。默认显示本次页面操作，可切换 API 请求或历史后台任务；每页50项。
- `tasks.json` / `task-stages.csv`：逐任务与逐阶段数据。浏览器与服务端按 Request ID 关联；网络总耗时和服务端执行耗时分别保留。

任务明细以瀑布图展示：条形的位置表示开始偏移，宽度表示持续时间，菱形表示观测时间点。起点缺失时不猜测位置；浏览器和服务端使用独立时间轴。后台重试的各次执行分别绘制，避免长时间排队压缩阶段细节。

“并发时间线”按可靠的绝对开始时间对齐任务执行区间。默认页面操作没有绝对开始时间时，显示同期前50项API；其他筛选使用当前页。它展示任务并发，不声称掌握线程归属。现有记录不含函数调用栈，不能据此生成函数火焰图。
- `report.md`：同一采集的可读文本。
- `report.json`：完整覆盖、区间分布、原始清单、资源和浏览器数据，以及可选比较。
- `timings.csv`：请求、后台span、排队和维护阶段分布，保留每行时间字段。
- `inventory-timings.csv`：从历史attempt和抓取事件推导的分布。
- `browser.json`：冷暖显示链路、逐图阶段、网络聚合、完整HTML抽样与未完成原因。
- `events.jsonl`：本次窗口内再次脱敏的关联span/event。
- `inventory.json` / `inventory-after.json`：采集前后只读清单和历史证据覆盖。
- `resources.json`：按间隔采样的资源时间序列，含采样自身耗时。
- `run.json`：时间窗口、参数、截断/坏行/不可读取日志等采集完整性。

分布报告给出样本数、P50、P95和最大值。span采用线性插值分位数，历史清单采用最近秩P95；两类都保留来源，跨来源边界不直接混合。少于20个样本的span P95标记低样本，不设未经确认的性能达标阈值。

父子span通常重叠。API耗时含SQL，抓取job含子进程和发布，维护阶段也可能含嵌套文件阶段；这些不能相加为一个“总耗时”。进程CPU差值不是该请求独占CPU。`http.stream`表示应用流迭代生命周期，包含消费者拉取间隔；服务器直接文件发送可能不经此迭代器，实际下载由浏览器网络记录补充。

`complete` 表示这次探测完成，不代表快照质量或速度已合格；`incomplete`、缺图、失败、历史字段缺失均保留。已有历史任务来自不同代码、网络和负载，仅用于定位调查方向，不是当前版本的纯净基准。

## 自动记录与开销

运行记录位于：

```text
data\diagnostics\performance\perf-<PID>.jsonl
```

默认每进程主文件5MiB，另留3份轮转文件；`BOOKMARK_PERF_DIR`可改目录，`BOOKMARK_PERF_MAX_BYTES`可改单文件限额（65KiB–50MiB）。不同旧PID文件保留供比对，当前没有跨进程自动删除策略，长期启用应定期归档这些监测文件。收集器单次至多读取64MiB或100000行，截断会标明。日志轮转时文件消失会记入不可读计数并继续报告。

关闭时不装SQLwrapper，span为no-op。启用后有时钟、上下文和本地JSONL写入成本；不会称作零开销。默认采样间隔为2秒，实际开销取决于进程数量、系统负载和采样环境。报告保留每次实际采样成本。浏览器探测本身也消耗CPU/内存，测量应尽量串行，避免同时跑大型抓取验收或压力测试。

不记录SQL、请求正文、prompt、AI回复、Cookie、Authorization、完整目标网址、query或搜索词。路由仅保留静态模板。数据库探针使用SQLite `mode=ro`与`query_only`，不初始化Django、不迁移或认领任务。浏览器使用独立匿名context和临时15分钟本机owner会话，finally删除；仅GET/HEAD，写请求和外站请求被阻断。访问尚未生成图片的thumbnail GET可能优先排入**既有离线补图**，这项读请求副作用在报告明确标记。

实现选择是进程内有界日志加独立采集器。它比新增Prometheus/OpenTelemetry服务更轻量，满足当前个人网站库的阶段关联与重复采样；代价是没有远程监控面板、跨机链路追踪、硬件呈现测量或自动告警。本次没有创建定时任务或后台常驻浏览器。

参考API语义：[Django数据库观测](https://docs.djangoproject.com/en/5.2/topics/db/instrumentation/)、[Django流式中间件](https://docs.djangoproject.com/en/5.2/topics/http/middleware/)、[Windows进程内存字段](https://learn.microsoft.com/windows/win32/api/psapi/ns-psapi-process_memory_counters_ex)。

## 验证

```powershell
$env:PYTHONPATH = "$PWD\backend;$PWD"
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests/performance -v
.\.venv\Scripts\python.exe -X utf8 backend\manage.py test runtime.test_performance --noinput
```

验证只读数据库、导出隐私、监测失败不改变响应、禁用路径、SQL和流式响应、嵌套区间、事件分布、缺数据null、进程归属/PID复用，以及实际隔离浏览器阻断POST。后台边界另以临时库和受控provider验证，未对真实网站库执行AI/导入/备份操作。

## 观察结果的边界

本机观察曾在完整 HTML 抽样的 HEAD 阶段遇到 `http_parser_expected_status_line`，记录为 `incomplete`；当时尚未确定服务或连接层根因。报告应保留失败阶段，不能将未完成的样本当成零耗时或成功。

历史清单也可能出现 attempt 与 job 状态不一致的证据缺口。监测只呈现问题，不修复或更改队列。AI、导入、备份和恢复的细分 span 已使用临时库和受控服务验证，正常操作后才会产生对应的运行记录。
