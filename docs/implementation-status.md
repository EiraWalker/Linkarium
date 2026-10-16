# 首版实现与验收记录

## 2026-10-15：修复滚动时页面预览跟随滞后

原先的 body 固定定位宿主通过 scroll → requestAnimationFrame 更新屏幕坐标，浏览器滚动当帧会先移动卡片，预览稍后才跟随。最小浏览器复现仅需 6 张预览：滚动 80px 时错位 80px，等待位置更新后回到 0px。修复将文档放在各自稳定的列表／详情滚动容器内，采用内容坐标绝对定位，位置移动和裁剪交给原生滚动；仍保留会话内的快照文档，列表及表单离开页面时照常卸载。

验证：类型检查、生产构建、原有完整加载／跨导航保留／删除／故障验收均通过。新增 `tests/preview_scroll_ui.py` 在隔离的 Chromium 浏览器中覆盖桌面和手机模拟视口、6 张与整页 48 张预览、滚轮输入及详情面板；滚动当帧最大错位均为 0px（容差 1px）。见 [修复前测量](../output/playwright/preview-scroll-before.json)、[修复后报告](../output/playwright/preview-scroll-report.json) 和 [原有功能复验](../output/playwright/preview-retention-report.json)。复验命令为 `.\.venv\Scripts\python.exe -X utf8 tests\preview_scroll_ui.py`，无需操作正式收藏库或已有浏览器会话。模拟视口结果不代表实体手机验收。

本机 8001 Web 已重载，新 JS/CSS 返回 200，worker 保持运行。独立浏览器使用该实际服务的前端资源与隔离 API 夹具复验 48 张列表及详情滚动，错位仍为 0px；见 [实际服务验收](../output/playwright/preview-scroll-live-report.json) 和 [资源更新报告](../output/playwright/preview-scroll-web-reload.json)。

## 2026-10-15：当前分页完整加载与跨导航保留

已实现 [Issue #2](https://github.com/EiraWalker/Linkarium/issues/2) 与 [ADR-0009](adr/0009-preview-document-retention.md)。当前分页所有可用预览自动入队，加载并发为桌面 2、手机 1；已加载 iframe 文档在滚动、翻页、收藏文件夹切换和应用内返回时保留。刷新整个网页可重新加载。会话失效、退出、收藏库身份变化和相应数据删除会释放旧文档。

验证结果：类型检查与生产构建通过；现有快照发布／权限测试已扩展 HEAD 检查并通过，覆盖成功响应、空正文、CSP、no-store、未登录 401 和不存在版本 404。真实构建界面的 Chromium 验收通过：桌面及 390×844 手机模拟视口均在不滚动时加载全部 48 条，访问两页后加载 96 个唯一快照，往返导航没有重复文档加载；卡片选择、详情显示、版本更新、会话失效、重新登录和网页刷新行为正确。桌面流程还验证了删除收藏、清理旧历史与删除文件夹的缓存清理，并确认不相关预览保留。失败、15 秒超时、迟到恢复及无快照夹具也通过。

验收只使用构建后应用及浏览器内隔离 API 夹具，未登录正式 owner、未修改正式收藏库或运行采集任务。复验：`.\.venv\Scripts\python.exe -X utf8 tests\preview_retention_ui.py`。结果与 CDP 资源观测见 [浏览器报告](../output/playwright/preview-retention-report.json)，截图见 [桌面](../output/playwright/preview-retention-desktop-top.png)、[手机模拟](../output/playwright/preview-retention-mobile-top.png) 与 [手机详情](../output/playwright/preview-retention-mobile-detail.png)。

本机 Web 已单独重载，新 JS/CSS 返回 200，独立浏览器访问实际 8001 路由正常显示登录页且无页面错误，原有 worker PID 保持运行。见 [服务资源报告](../output/playwright/preview-web-reload-report.json) 与 [实际路由报告](../output/playwright/preview-live-route-report.json)。

范围限制：资源观测来自小型受控 HTML 夹具；CDP 堆／文档指标不等于整个浏览器的峰值内存。本次保留会随访问过的不同快照增长，尚未实测大量大型真实归档、实体手机或 Firefox/Safari。下方旧验收记录中的 8／4 个 iframe、离屏卸载结论保留为历史证据，不再是当前验收规则。


2026-10-09。仓库已经包含可运行应用、数据库迁移、锁定依赖、启动脚本和测试。原有技术文档保留设计基线，本文件记录实际行为与验证范围。

## 2026-10-15 缓存兼容与本机代理

设置页新增本机 Clash Verge／手动 loopback HTTP／直连选择。压缩重复计算样式、修复 Monolith 的 SVG 片段引用、保留原始异常，并为不可读媒体提供带警告的局部占位。目标 IP 暂时不可达时通过同一代理尝试其余已校验地址，不自动直连。该功能仅适用于本机开发采集，未改变生产隔离边界或 Clash 全局配置。

SVG 引用还原和强验证响应识别已修复；挑战页返回明确错误并使用对应后台任务文案。相关测试与限制见 [缓存兼容性与代理](capture-proxy.md)，测试通过不能宣称任意网页均可无损缓存。

## 导入流程更新

选择 Firefox/Chrome HTML 后立即运行记录导入 → 桌面/手机抓取 → AI 分类，显示三阶段进度。全批抓取终结后才调用 AI，失败仍参与；AI 不可用留下人工校对条目，保留部分成功建议。100% 后移除进度条，导入页弹出共享校对窗口，其他页面不打断；关闭不自动重开。独立校对页面与导航移除，网站和分类任务提供“查看建议”。

新增 library/0002_import_workflow 数据库迁移。旧版备份仅对精确已知 schema 在隔离临时库中执行本地迁移，恢复仍校验候选 hash；暂停中的导入随覆盖恢复取消。完整规则见 [浏览器导入](browser-import.md)。

更新验证：67 项后端/备份兼容测试通过，Django 检查与迁移一致性检查通过，前端类型检查和构建通过。真实浏览器受控流程通过（含立即运行、延迟弹窗、关闭不重弹、人工确认、AI HTTP、单站弹窗、手机无横向溢出）。整库备份/覆盖恢复 API smoke 通过，密码和密钥保留、会话撤销、AI 重验证通过。未在这次更新中重新执行 Chromium/Monolith 全套保真验收。

## 设置页数据管理（2026-10-10）

设置页最底部新增“清空仓库”和“重置数据”，确认弹窗需输入对应操作名称。清空删除文件夹、网页、标签、快照/分类/任务/导入记录，以及应用内备份、旧 generation 和临时文件，保留密码、刷新与界面设置、AI 配置/密钥；重置额外删除所有用户、会话、AI 密钥、登录失败记录和用户设置，并轮换应用生成的签名密钥。重置后显示本机密码初始化命令，不开放远程匿名注册。

通过统一维护独占和 generation 切换执行。确认接受后暂停业务读写，停止后台进程、确认空库可启动，再开始不可逆清理；清理前失败保留旧库，清理中断则保留维护状态并在 supervisor 重启后继续，不回退到已删除的用户数据。清理拒绝数据根目录之外的路径和指向其他目录的链接。重置保留应用 schema、运行协调状态和当前操作记录，以便服务继续启动。

验证使用独立临时库：101 项后端测试、类型检查及构建通过；真实 supervisor 和 Chromium 界面已验证取消不删除、输入确认、清空保留登录与设置、重置撤销会话/删除凭据、显示密码初始化，以及桌面/手机确认弹窗。证据为 `output/playwright/data-actions-live-report.json` 和 `settings-data-actions-desktop.png`、`settings-reset-confirm-mobile.png`、`settings-reset-initialization.png`。正式数据未用于破坏性验收。

## 可拖动侧栏（2026-10-11）

非手机窗口可通过侧栏右侧拖动条调整宽度，范围为 208–300px；大于等于 1200px 时默认 250px，其余非手机窗口默认 208px。拖动条支持左右箭头每次调整 10px，大窗口和较窄窗口分别保存浏览器偏好，使用 `website-*` 键并随重置数据清除。文件夹名称保留单行省略号和完整路径提示，手机保留原有 280px 抽屉。

类型检查与构建通过；Chromium 使用构建后的界面和浏览器内 API 夹具，验证默认值、拖动边界、键盘步长、刷新恢复、跨断点切换、拖动取消、文件夹操作、独立滚动、手机布局及存储不可用时的回退，没有修改正式仓库。证据见 `output/playwright/sidebar-resize-report.json`。正式网页服务已加载新资源并通过健康与资源请求检查，见 `output/validation/sidebar-resize-service-report.json`。

## 运行入口

在项目根目录使用 Windows PowerShell：

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1 -DevelopmentCapture
```

首次安装需要 uv 与 Node 24；uv 按锁文件准备 Python 3.13。安装脚本校验官方 Monolith 二进制 SHA-256，并安装匹配的 Chromium。初始化交互输入至少 12 字符的 owner 密码，不把密码作为命令行参数。访问启动成功后打印的实际地址；默认从 8000 自动寻找空闲端口，显式 `-Port` 保持固定端口。

后续仅需启动脚本。修改密码：

```powershell
.\.venv\Scripts\python.exe backend\manage.py initialize --reset-password
```

如果本机使用 Mihomo fake-IP DNS 并返回保留地址 `198.18/15`，默认公开目标检查会拒绝。可显式运行 `.\scripts\start.ps1 -DevelopmentCapture -PublicDns`：仅开发采集进程使用固定公开 DoH 和短生命周期 loopback CONNECT 代理，连接固定到已校验的公开 IPv4，TLS 仍由 Chromium 按原 hostname 验证；不改系统 DNS/代理，不允许生产 launcher 使用该选项。

数据库 schema 升级前停服务并备份，再重新安装和迁移。多设备联网使用同一后端；局域网绑定、HTTPS、公网代理与 Linux launcher 见 [部署说明](../deploy/README.md)。正式库默认在 `data/`，验收库在 `output/acceptance-data/`，两者分开。

## 已实现的行为

| 范围 | 实现结果 |
| --- | --- |
| 登录 | 单密码 UI；Django auth/session、CSRF 与 django-axes；仅 owner 可读写业务 API、HTML、备份 |
| 网站库 | URL 去重；嵌套目录与单一归属；目录删除级联子树及历史；标签、备注、星标、批量移动/编辑 |
| 搜索 | 网站结果；URL、域名、标题、标签、目录路径、备注部分匹配；字段复选、全库/子树与排序 |
| 前端 | 灰白工具界面；电脑三栏、手机双列、同页详情；卡片固定尺寸；点星不打开详情、不删除数据 |
| 网页归档 | Chromium 执行动态页，固化 DOM/CSSOM、canvas、shadow DOM、已加载资源，再由 Monolith 生成单 HTML |
| 双设备历史 | desktop 1366×768、mobile 390×844 独立产物、当前指针和状态；首屏缩放；关闭快照仍保留历史 |
| 任务 | 首次抓取、默认每周、逐站间隔、目录子树立即刷新；去重、手动优先、租约/心跳、重试、取消、停机一次补刷 |
| 导入 | Chrome/Firefox HTML 解析与预检；重复跳过保留原配置/历史；保留来源目录参照 |
| AI | Chat Completions 协议；地址/模型/密钥配置与绑定验证；新站建议、已有站重分类；人工改/确认/驳回后才创建目录和移动 |
| 备份恢复 | ZIP64 全库和全部 HTML 历史；manifest/hash/schema 检查；恢复前备份、覆盖切 generation、失败回退、会话撤销 |
| 运维 | supervisor 管理 Web/单 worker/维护；两 SQLite 分开保存业务与身份/密钥；私有数据权限、错误和健康响应 |

备份包含 AI 非密钥配置，但不包含登录密码、会话、AI 密钥和运行状态。恢复保留当前密码及 API 密钥，AI 需重新验证后才能调用。公共页面采集不使用日常浏览器 profile 或目标站登录。

## 锁定版本与体积

当前工具链：Python 3.13.5；Django 5.2.18、django-axes 8.3.1、Waitress 3.0.2、WhiteNoise 6.12.0、HTTPX 0.28.1、Playwright 1.63.0、html5lib 1.1、tinycss2 1.5.1。Chromium revision 1243（153.0.8010.12）、Monolith 2.10.1 官方 Windows 二进制。

前端：Preact 11.0.1、TypeScript 7.0.2、Vite 8.3.4。以 `uv.lock`、`requirements.lock`、`deploy/requirements.lock`、`frontend/package-lock.json` 为复现依据。Monolith 2.11.1 源码引用不表示已安装该版本。

前端 production bundle 约 30.5 KB JavaScript / 6.3 KB CSS（gzip，不含 HTML 快照、图片、字体和浏览器工具链）。生产前端无需 Node 服务。当前每次采集启动并关闭 Chromium，尚未做跨任务浏览器复用。

开发机上的一次空闲验收 supervisor + Web + worker 进程树的工作集约 180 MiB，private bytes 约 135 MiB，包含 Windows venv 启动进程，不含采集浏览器和系统文件缓存。它只是当前机器的一次测量，不是最低配置或峰值承诺。[测量记录](../output/validation/idle-processes.json)

## 实际验证

首版交付检查通过：53 项后端测试、30 项 capture 测试（合计 83 项，包含真实 Chromium/Monolith），Django system check、迁移差异检查、Python compileall 和前端 typecheck/build。28 份 Markdown 的 109 个本地链接检查无缺失。

后续本机启动修复：默认端口被其他应用占用；默认启动现在自动选空闲端口，成功后显示实际 URL，显式 `-Port` 仍固定。11 项相关端口/启动/协议测试通过，实际运行到 8001 的健康响应、静态资源与浏览器登录页已验证；没有停止原服务或修改 owner 密码。[登录页截图](../output/playwright/startup-login-8001.png)

这些测试覆盖真实 SQLite、认证/CSRF/限流、目录/版本约束、重复 URL、并发 revision、迟到任务拒绝、孤儿文件安全、分类凭据绑定、恶意 AI 输出、导入、ZIP 路径/schema/hash、恢复回退及启动失败子进程清理。

真实 Waitress + supervisor + HTTP 的维护测试完成：2 个收藏备份后覆盖恢复为 1 个；当前密码与 API 密钥保留；旧会话失效；AI 重新验证；library_epoch 递增。界面也完成上传验证、覆盖确认、短暂断连、重新登录、原 operation 查询与恢复前备份下载。

浏览器实际点击流程：登录 → 建目录/标签/网站 → 独立点星 → 修改备注和间隔 → 部分匹配搜索 → 双设备预览及历史 → 浏览器导入（1 新增 / 1 重复 / 1 无效）→ 完整备份 → 覆盖恢复。

AI 使用仅 loopback 的受控 Chat Completions HTTP 服务验证配置、连接测试、重分类、人工改路径与确认。确认前目录仍不变；确认后创建两层目录并移动网站，保留星标和备注。该证据验证本软件与协议集成，不代表已验证某个付费模型的判断质量或兼容性。

预览池实际断言：桌面最多 8 个、手机最多 4 个 iframe，包含详情预览；离开可见范围移除 iframe；两端真实 viewport 尺寸正确；sandbox 不授予脚本/同源权限；320px 模拟屏幕无横向溢出。手机尺寸为浏览器模拟，尚未实测实体 Android/iOS。

受控动态页桌面/手机均生成 Monolith HTML。原页面与受限归档重放的首屏变化像素分别约 0.188% / 0.163%，通过该 fixture 的 1% 阈值。诊断 PNG 只用于测试对照，产品归档仍为 HTML。[动态页报告](../tests/capture/results/report.json)

真实公开 Example 页面完成 API 入队、worker 执行、双设备版本发布和鉴权 HTML 读取，两端约 137 KB；离线重放文字完整、0 破图、0 外网。桌面对照首屏变化像素约 0.213%；手机额外在线对照因 DoH TLS 超时未完成，已记录而未当成通过。该页面有一项缺失资源提示，界面保留提示且不放宽 sandbox。[API/worker 报告](../output/validation/public-snapshot-api.json)、[采集与重放报告](../tests/capture/results/public-doh/example-report.json)

开发模式应用 worker 使用 `python -X utf8 -m capture` 受控子进程，父进程检查 lease/cancel 并施加 120 秒总时限，超时终止自有进程树。测试验证实际取消、总时限、子进程及其孙进程退出和真实 JS → CLI → Monolith；成功发布后清理自己的工作目录。底层 `capture()` 的阶段期限仍为协作检查，不能将其直接调用等同于进程硬预算或完整生产隔离。

1000 站点、100 目录、10 标签和 2000 设备状态的元数据查询测量，列表/备注搜索/目录搜索/目录树 p95 约 12/21/51/17 ms。这是 Django 请求处理时间，未包含 HTTP 网络，不代表 1000 真实网页抓取吞吐或手机内存验证。[规模报告](../output/validation/catalog-capacity.json)

界面截图：[电脑](../output/playwright/frontend-desktop.png)、[手机](../output/playwright/frontend-mobile.png)、[手机详情](../output/playwright/frontend-mobile-detail.png)、[AI 校对](../output/playwright/frontend-ai-review.png)、[导入与备份](../output/playwright/frontend-transfers.png)。

## 必须保留的验证边界

1. **完整 Linux 采集隔离未通过。** 当前 WSL2 的 namespace、非 root user namespace、loopback 内核拒绝检查通过；nftables byte quota 不支持、MemoryMax 未有效限制测试分配，IPv4 forwarding 未开启且未擅自修改。rootfs/Chromium sandbox、DNS rebinding、post-DNAT、完整 cgroup 取消等仍需目标 Linux 主机验收。没有签发 `VERIFIED.json`，生产 launcher 拒绝未经验证启动。[报告](../tests/capture/results/linux-preflight.json)
2. **网页视觉兼容按样本验证。** 任意网页、反爬/验证码、closed shadow、跨源 tainted canvas、WebGL/视频以及特定嵌入页面可能失真或拒绝抓取。成功退出不等于逐像素一致；首屏要求不包含页面交互、未加载的无限滚动内容或登录个性化状态。失败保留旧成功版本。
3. **Docker/Nginx/实体手机未运行验收。** 提供了 Compose、非 root 镜像和 HTTPS proxy 配置；上传鉴权使用 Nginx `auth_request`，仍须目标服务器 `nginx -t`、实际大上传与可信代理 IP 测试。公开生产部署需要完成这些条件。
4. **未调用真实 AI 供应商。** 必须在界面提供自己的兼容地址、模型和密钥并测试；失效或缺少 AI 时，整理、导入、快照和备份不依赖 AI 成功。

## 复验命令

```powershell
.\scripts\verify.ps1
.\.venv\Scripts\python.exe -X utf8 tests\portability\maintenance_smoke.py
.\.venv\Scripts\python.exe -X utf8 tests\catalog_capacity.py
```

`verify.ps1` 执行迁移差异检查、后端测试、Chromium/Monolith capture 测试和前端类型检查/build。仅捕获测试使用显式的本机 fixture 例外，不把该例外开放给应用用户。P0 视觉报告可通过设置 `$env:PYTHONPATH = "$PWD\backend"` 后运行 `tests\capture\verify_p0.py` 重建。验收网页和 AI fixture 服务脚本分别在 `tests/run_acceptance_server.py` 与 `tests/run_ai_acceptance_provider.py`，只作为测试工具使用。
