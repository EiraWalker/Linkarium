# 网页缓存兼容性与本机代理

实现日期：2026-10-15。

## 使用

启动本机开发采集后，在“设置 → 网页缓存代理”选择“不使用代理”“Clash Verge（本机）”或“手动 HTTP 代理地址”，保存后对后续任务生效。Clash 模式每次读取后端电脑的生成配置顶级 mixed-port，缺失时使用 port，不读取订阅、节点凭据或控制器 secret。不更改系统代理、TUN 或节点。

手动模式只接受无凭据的 loopback HTTP 地址。Clash 或代理不可用时返回错误，不自动直连。代理配置只供当前本机开发采集；未扩大 Linux 隔离采集器的网络权限。

`GET/PATCH /api/v1/capture-network` 使用现有密码登录会话、CSRF 和维护模式边界。PATCH 格式为 `{"mode":"clash"}`，或 `{"mode":"manual","proxy_url":"http://127.0.0.1:7897"}`。配置位于 `data/runtime/capture-network.json`，属于后端运行环境，不随收藏库备份迁移到其他电脑。GET 不改变选择。

## 请求路径

Chromium → 应用的公网校验代理 → Clash HTTP CONNECT。公共 DoH 查询也显式走同一个 Clash 代理。应用发送 CONNECT 时使用已校验的公网 IP，TLS SNI 和 HTTP Host 仍使用原网站域名；应用不使用系统 fake-IP 直连。

本机 Clash 是可信代理边界：其嗅探／路由规则可能按 SNI 或 Host 再解析并改变实际目的地，故应用校验 CONNECT 参数不能证明 Clash 最终连接的公网 peer。该功能仅供 development 采集，公共部署和 Linux 隔离采集仍拒绝此代理路径。

保留私网、混合 DNS、部署域名和受限端口检查。代理握手有超时、响应头容量限制；关闭代理的错误经过转发层及浏览器异常层仍保留为 `CAPTURE_PROXY_UNAVAILABLE`。

代理 CONNECT 返回 502、503、504 时，视为该目标地址暂时无法连接，继续通过同一代理尝试 DNS 返回的其余已校验公网 IP。所有地址失败后只拒绝该请求；可选子资源不会因此将整页标记为代理不可用，主文档仍会失败。连接本机代理失败、407 认证拒绝或畸形握手属于代理故障，不尝试直连或其他未校验地址。

## 兼容性修复

- 冻结样式采用共享声明与差异规则，避免每个元素重复保存数百个计算属性。仍保存 HTML，保留动态 CSSOM、伪元素、Shadow DOM、布局与当前动画状态。
- 动画暂停会同步锚定已采样的 `currentTime`，避免异步暂停多前进一帧；回归使用固定 10000 ms 采样及两个动画帧后的稳定性断言，未放宽布局容差。
- 视频优先保存当前帧，再尝试海报；无法读取的视频／Canvas 生成局部静态 SVG 占位，记录 `media-placeholder:*`。这表示媒体降级，不是完整视频缓存。
- 原有逐图基线和首屏质量检查保持。原页坏图或屏外坏图允许带警告保存；首屏新增图片损坏仍拒绝。
- 修复 Monolith 把 SVG 的页面内渐变／遮罩／裁剪引用绝对化后，最终改写误当缺失资源的问题。仅严格匹配当前文档 URL（含 query）的引用还原为 `#id`，外部资源不误合并。真实 SVG 像素回归修复前差异 64.53%，修复后低于 0.5%。
- 非关键子资源 DNS 失败允许记录警告；主文档 DNS 失败和私网资源请求仍拒绝。
- 对明确 HTTP 401/403/429、404/410 以及已知人工验证页标题给出单独错误。主文档／主框架样式表的 AWS WAF 或 Cloudflare 强验证响应头也会阻止发布伪正常快照；就绪等待期间恢复成功的响应会清除旧拦截状态。图片和广告子框架仅记录警告。该识别不保证覆盖所有验证码或登录页。
- 父进程终止／清理错误不再覆盖原始 timeout、cancelled；诊断追加脱敏 parent_failure 和 cleanup_errors。
- 大型页面保留全部 DOM 和原始 CSS；超过 30,000 个节点时，仅对首屏及其祖先、SVG 采样计算样式，计算样式采样不超过 30,000 个节点，DOM 硬上限为 100,000 个节点。这样减少冻结开销，同时明确保留资源上限。

### 页面跳转、就绪与 WebSocket

已定位一种采集器自身的死锁：当前 Playwright Python 版本直接在 dispatcher 中执行 WebSocket 路由回调，在该回调中调用同步 `WebSocketRoute.close()` 会停在同步适配器的 dispatcher 切换循环。它可使已取得 HTTP 200 的页面在导航或就绪阶段一直等待，不能把这类超时归因于网站没有响应。独立固定夹具中，原回调 8 秒未退出；仅移除同步 close 后正常完成，测试服务器收到的 WebSocket Upgrade 请求为 0。

修复后的路由匹配全部 WebSocket，记录 `blocked_websocket` 域名及局部警告，保持本地拦截而不调用 `connect_to_server()`，也不在回调内执行同步 close。因此没有为了避免死锁而开放真实 WebSocket 外连；静态快照不承诺保存实时推送的数据。[Playwright WebSocketRoute 文档](https://playwright.dev/python/docs/api/class-websocketroute#web-socket-route-connect-to-server)

首屏就绪改用 `wait_for_function` 的宿主超时，检查字体与首屏图片的加载状态，单轮最多等待 4 秒，并受整个采集任务剩余时间约束。超时后再用最多 2 秒的宿主超时检查文档仍可响应；图片未就绪仍沿用原图基线和离线回放校验，不依赖网站内的 `Promise` 或 `setTimeout` 来提供退出保证。这是额外的等待边界改进，已定位的 WebSocket 死锁不能改称 Promise 问题。[Playwright 等待函数文档](https://playwright.dev/python/docs/api/class-page#page-wait-for-function)

客户端跳转或 reload 销毁执行上下文时，就绪与冻结最多尝试 3 次。采集跟踪真实主文档请求、响应与提交；同一文档内的 history/hash 更新不当作新文档。每次使用最新响应重新检查公开地址、HTTP 错误、WAF 强验证标记和验证页标题。若冻结后主文档又发生跳转，最多沿新目标重新采集一次；两次采集共享原始 deadline、累计请求数、资源字节预算及同一个公网代理实例，不重新从零计算限额。新的目标仍经过地址及文档检查，旧页面快照不会在目标尚未验证时发布。

## 验证

上一阶段验证：82 项采集回归全部通过（含真实 Chromium／Monolith、动画冻结、SVG 像素、原／新增坏图、代理失败与目标 IP 回退、强验证响应）；6 项代理 API 测试通过；前端 TypeScript 检查与 Vite 构建通过；Django system check 无问题。该阶段完整采集套件耗时 84.671 秒。最初动画回归暴露的一帧暂停竞态已修复，未放宽容差，并额外连续验证 30 次。

本轮继续修复后的完整采集回归为 93 项，全部通过，耗时 111.713 秒，包含新增的 2 项大型 DOM 回归。这些结果证明该轮测试通过，不能替代任意网站的逐项结果。

在上述 93 项之后，追加验证了 13 项导航用例与 1 项 WebSocket 子进程用例：覆盖同 URL reload、同文档 history 更新、冻结后的跳转、最终 HTTP/WAF/私网拒绝、有限重试与共享预算、节点/样式预算错误映射，以及 WebSocket 上游 Upgrade 为 0。新增用例合入后的完整套件结果以 `output/capture-recovery-20261015/capture-tests-final.log` 为准，不能将此前 93 项结果充作最终全部测试结果。

真实 Chromium fixture 中，600 张重复卡片冻结 HTML 从 28,220,355 字节降到 189,335 字节，冻结耗时约 5.369 秒降到 1.1–1.3 秒。该数字仅代表固定夹具；布局、字体、颜色、动态样式、伪元素及 Shadow DOM 有独立断言。

主文档 HTTP 200 不足以证明页面正常：样式、脚本或图片仍可能返回空 HTML 或强验证响应。采集器结合主文档／主框架样式表的限定响应头识别挑战页，避免把无内容页面误判为正常缓存。

后台任务界面已验证 `CAPTURE_CHALLENGE` 显示为“网站要求人机验证，暂时无法缓存”。

未使用的字体声明报错不代表首屏实际字体丢失；字体兼容性需要结合实际使用字体与相同视口、DPR 下的页面结果判断。

测试命令：

```powershell
$env:PYTHONPATH = "$PWD\backend;$PWD"
$env:RUN_CAPTURE_INTEGRATION='1'
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests/capture -p 'test_*.py' -v
```

API 测试 `runtime.test_capture_network` 使用独立数据目录及测试库，覆盖授权、CSRF、模式切换、配置拒绝保持原值与 isolated 限制。

本次包含长驻 worker 的父进程错误处理以及前端构建，既有服务需重新载入才能完整生效。

不存在保证所有网站成功的通用匿名归档方案：登录、人工验证、已失效链接及服务端拒绝仍可能失败。不得把验证页、占位媒体或仅 HTTP 200 当成完整页面一致性的证明。

## 当前失败快照的恢复操作

管理命令 `retry_failed_captures` 默认只读，统计当前 `CaptureState` 仍带错误、且网站启用了快照的桌面／手机设备。历史失败 `Job` 不作为重新抓取依据；某个设备已成功时，不会因同一网站另一端失败而再抓一次。

在项目目录先查看计划：

```powershell
# 在项目根目录执行
$plan = .\.venv\Scripts\python.exe -X utf8 backend\manage.py retry_failed_captures | ConvertFrom-Json
$plan | Format-List
```

核对后执行；generation 和 epoch 使用本次只读结果，避免收藏库已切换时按旧计划入队：

```powershell
.\.venv\Scripts\python.exe -X utf8 backend\manage.py retry_failed_captures --execute --expected-generation $plan.generation --expected-library-epoch $plan.library_epoch
```

`--execute` 会在事务内重新检查维护状态、收藏库身份、当前错误、快照开关和活动任务，创建一个来源为 `failed-capture-recovery` 的新批次。已经在排队、执行或等待重试的同设备任务会去重，不会移动旧批次的成员；没有需要新建的任务时不创建空批次。若请求在入队过程中失败，整个新批次回滚。

输出字段：

| 字段 | 含义 |
| --- | --- |
| `failed_devices` / `failed_websites` | 当前仍保留上次错误的启用设备数／网站数，包含已有活动任务的设备 |
| `already_active_devices` | 已有有效任务，无需重复入队的设备数 |
| `to_queue_devices` / `to_queue_websites` | 本次需要新建任务的设备数／涉及网站数 |
| `queued_devices` | 本次实际新建任务数；只读模式恒为 0 |
| `group_id` | 新恢复批次 ID；只读或无新任务时为 null |

本操作不清除历史 `Job`、`JobAttempt` 或历史批次的失败记录，也不会预先清空 `CaptureState`。只有对应设备的新快照成功发布后，才清除其当前错误并重置连续失败次数。排队／执行期间，网站列表和详情 API 只呈现当前活动状态，暂不把上一轮错误当成当前失败；进入等待重试或最终失败时继续显示真实错误。历史批次中的失败仍可用于诊断。

在现有“后台任务”入口查看新批次的进度；这不是立即把所有失败记录改成成功。人工验证、失效网址和服务器拒绝等仍可能返回明确错误。此恢复机制已通过 `runtime.test_capture_recovery` 的 13 项隔离数据库回归，覆盖只读、定向设备、历史保留、活动去重、维护和 epoch 复核、事务回滚及成功后清错。

已打开页面会在任务批次、活动状态数量或完成计数变化时刷新网站列表与详情，原有滚动、选择和弹窗状态保留；相同轮询响应不会重复刷新列表。

## 最终合并回归

2026-10-15 上一轮合并代码的完整采集回归 107 项全部通过，耗时 183.994 秒；恢复命令/API 的 13 项隔离数据库测试和前端构建通过。日志为 `output/capture-recovery-20261015/capture-tests-final.log`。

## 第二轮容量与资源处理修复

代理仍最多处理 32 个活动连接；正常突发的后续连接在有界 accept 队列等待，不再把第 33 个连接直接当成整页容量失败。等待受原始采集 deadline 限制，停止时释放待处理连接；不增加活动线程、网络字节和总连接数上限。诊断包含活动峰值及等待次数、秒数，用于区分容量耗尽与排队延迟。

对 `.gif` 图片，采集可经同一个认证公网代理读取实际完整首帧并形成单帧 GIF。先请求 128KiB Range 并复用受控客户端，必要时有界续传；每段检查偏移、长度、identity 编码、总长度及 ETag／Last-Modified，避免拼接不同版本。单图上限仍为 16MiB，GIF 画布与图像 descriptor 均有 16MP 限制。每次重定向重新校验公开地址并计入请求预算；不使用直连或另一个绕开校验的 Clash 客户端。没有完整首帧时不生成假图片。

每个 frame 的画面／海报冻结完成后，停止该源 frame 的 video/audio 和新增媒体请求，避免源页面在 Monolith 处理、离线回放期间继续循环下载。主文档导航、地址、HTTP 状态、挑战检查和图片质量门禁保持生效。frame 检查、冻结和媒体停止均记录阶段、host、深度、可见性及大小，不记录完整网址。

被浏览器拦截的应用协议（例如 `bilibili://video`）不会再清空有效 HTTP 主文档。对未带强挑战标记的 502／503／504 文档，仅在原 deadline 内观察网站自身跳转最多 2.5 秒；最终仍须获得成功文档，稳定错误页及跳转后的访问拒绝／挑战页不归档。

第二轮定向回归 20 项通过，含真实 GIF 像素、Range 和公开地址边界、冻结后媒体停止、应用协议和临时 HTTP 自恢复。该轮测试使用的容量门禁为 64 MiB，后续预算变更见下文。

## 第二轮样式、图片与浏览器兼容性

脚本运行后的 DOM 使用 `html5lib` 的 `scripting=True` 解析，并去掉未激活的 `noscript`，避免归档重新显示未加载的备用图片。对 CSS 顶层解析错误只跳过无效规则，保留有效相邻规则及声明内部的无效标记；不把无效声明修成另一种样式。

计算样式中的相同声明按精确选择器集合共享，包括局部 CSS 变量；不扩大变量作用域。固定夹具另行验证布局、变量值与作用域一致，不以不同源站输入的体积差值替代一致性验证。

对于至少 8MP、已解码且按显示尺寸明显缩小的普通位图，生成保持宽高比的单图 PNG 候选，尺寸覆盖 CSS 显示尺寸乘设备 DPR；每张不超过 2MP，整次冻结不超过 8MP。只有候选小于实际原资源才采用。跨源画布不可读、原像素裁切、像素画和无显示尺寸的图片保留原图。该优化牺牲不用于首屏显示的原始缩放细节，记录 `image-display-resolution`，仍然保留 HTML 页面。

桌面采集使用当前 Chromium 版本对应的普通 Chrome User-Agent；手机保持 Pixel 7。最终状态、公开地址、强挑战和离线回放检查均保留；HTTP 403 仍被拒绝。

证据：`output/capture-recovery-20261015/round2-findings.md` 及其 `round2-images/`、`round2-browser/`、`round2-size/` 子目录。

## 内嵌 PDF 与跨框架回放

PDF.js 的 viewer DOM 出现不等于文档已渲染。对主页面和首屏可见 iframe 中的 PDF viewer，使用宿主侧最多 10 秒、受原始任务 deadline 约束的轮询，等待首屏页的 renderingState 完成且画布已生成。未完成的 PDF 继续报超时，不保存空阅读器。该实现保存当前已渲染的页画面，不承诺下载和渲染整份 PDF。

离线回放同时统计嵌套 srcdoc 的文字和图片，拒绝可见子框架新增坏图或正文丢失；源和回放都隐藏的框架不作为首屏坏图。原来的主文档检查可能漏掉可见 iframe 内的损坏，也会误拒绝正文全在 iframe 中的页面。

4 项真实浏览器回归通过，旧实现的延迟 PDF 和 iframe 坏图用例先失败。当前保存的是已渲染页面的 HTML 归档，不是完整 PDF 文档的离线阅读器。

## 第二轮合并验收和正式恢复

2026-10-15 21:51，本轮完整采集套件 **149 项全部通过**，耗时 250.315 秒，包含真实 Chromium/Monolith、跨 iframe 内容、PDF 就绪、GIF、图像显示尺寸、CSS raw-text 语义与像素回归。Django system check 无问题。最终日志为 `output/capture-recovery-20261015/round2-capture-tests-final.log`。

CSS 内含 `</style>` 等字符串时，冻结和最终改写均保持正确的 raw-text 边界；覆盖计算样式、CSSOM、adopted stylesheet 以及连续两次改写，防止 CSS 内容变成额外 HTML 元素，保留实际 CSS token、文字、尺寸和像素。

冻结后源 DOM 插入节点会使重新计算的遍历下标失效，导致 iframe 身份错配。现在以冻结时的 WeakMap 元素身份匹配归档 key，冻结后新建的 iframe 不错误加入旧快照。两项确定性回归先失败，修复后嵌套内容 6 项和样式／冻结 17 项全部通过；此前 149 项完整套件不冒称在此改动后重新执行。

## SVG 数据图片与慢导航修复

SVG 原图的 XML 声明中裸 `?` 可能被 Monolith 当作 URL 查询边界。改写器现在仅把非 base64 SVG data URL 的 payload 中裸 `?` 转义为 `%3F`，保持 MIME、实际 `#fragment` 和数据中的 `%23` 不变。4 项新增回归通过，实际 Monolith 的图片字节以及浏览器源图／归档像素一致。

源 `goto` 期间串行读取 GIF 首帧可能提前耗尽导航时限。仅将源导航上限设为 60 秒，仍受原始 120 秒总预算约束；回放上限保持 30 秒。新增真实 32 秒慢导航与 3 秒总预算回归均通过，后者仍拒绝产生归档。

对冻结时明确为 CSS 隐藏、透明或零尺寸的 iframe，保留父页面的框架布局但不再执行其子页面冻结脚本；记录 `hidden-frame-not-archived` 及具体原因。隐藏状态与父页面 clone 同时记录，屏幕外但有实际渲染尺寸的 iframe 仍然保存，冻结后才隐藏的原可见 iframe 也不丢失。9 项嵌套内容回归通过。

## 静态音频与回放完成事件

离线页面中的 `<audio preload="auto">` 配合空数据源可能一直等待加载；最小浏览器夹具已复现此问题，不能将它误判为外网请求或图片尚未解码。

冻结时现在保留 audio 元素、控件、回退文本和布局，去掉 src、source、track、autoplay，并设置 preload=none；记录 `audio-not-archived`，不修改源页面 DOM。静态快照不包含音频播放。2 项新增浏览器／Monolith 回归通过，布局值一致，回放仍须 `wait_until='load'`，未降低为只等 DOMContentLoaded。

## 后续就绪与视觉检查修复

PDF.js 空 viewer 初始高度为 0 时，可能被当作不可见而跳过等待。现在区分 CSS 隐藏与尚未撑开布局的阅读器：后者仍等待文档页和画布渲染。新用例先红后绿，PDF 与嵌套内容 **13 项通过，41.769 秒**。

已内嵌的图片仍可能保留 `opacity-0` 和加载占位，图片 load 事件早于应用处理器绑定时尤其如此。当前新增最多 10 秒的应用就绪观察：跟踪主文档同源 script/XHR/fetch 完成、首屏 DOM 变化、当前图片和字体；关键网络完成后观察 2 秒安静窗口。若达到上限时仍有未结束的同源 script，最多增加一次 10 秒观察，给主应用初始化机会；持续 XHR/fetch 或动画不能触发延长。页面变化后重新检查图片，不强制图片显现、不伪造业务事件；原 120 秒总预算、导航安全和 PDF 门禁不变。

三个真实浏览器夹具复现异步脚本、异步数据、晚插入慢图片被过早冻结，旧版全部失败；修复后连同导航回归 **16 项通过，57.819 秒**，证据 `application-readiness-red.log` / `application-readiness-tests.log`。真实站点效果仍需单独验收，测试通过不代表每个外部应用都已就绪。

后续完整回归发现挑战 gate 被新轮询提前执行，导致页面或样式表自然恢复仍被拒绝。已恢复在有界观察结束后的强检查，观察期间仍检查导航、HTTP 和地址安全。新增脚本延长、持续请求、隐藏/离屏变化等边界与挑战回归 **16 项通过，75.846 秒**，见 `application-challenge-tests-final.log`。最终完整采集回归 **180 项全部通过，354.576 秒**，日志 `round2-quality-capture-tests-final.log`；Django system check 也通过。

图片状态区分 `loaded/pending/broken`，不再将未加载完成的图片标为原站已坏图并豁免回放检查；3 项真实浏览器回归先红后绿。

## 2026-10-16：大归档回放交付

较大 HTML 通过 `route.fulfill(body=...)` 交付时，传输开销可能耗尽回放导航窗口。受控浏览器回归夹具复现了原交付路径耗尽总预算的问题；development 模式改用带原 CSP 的临时本机 HTTP 交付。

只有显式 `development` 本机采集使用 `backend/capture/replay_document.py`，通过临时 loopback 端口交付本次已改写的 HTML。回放上下文设置独立的本机代理，复用现有 Chromium；抓源上下文继续使用原代理。服务仅响应随机 token 对应的固定主文档请求，其余目标和 CONNECT 均拒绝，不读取请求指定的文件，也不转发请求。浏览器仍执行原 CSP、请求拦截、HTTP 200、正文与新增首屏坏图门禁，并新增显式 WebSocket 拒绝。

Linux `isolated` launcher 的 nftables 明确禁止 127/8，本轮保持该内核规则不变；隔离模式继续通过原来的内存 `route.fulfill` 交付，不创建本机服务或监听端口。两种交付共用精确主文档请求检查、原 CSP、HTTP 200、WebSocket 拒绝与质量门禁。较大制品在 Linux 内存交付下仍可能消耗较多导航时间，性能尚未验收；Windows 验证不能当作 Linux 隔离验收结果，也不为此开放 loopback 网络。

正文分块发送遵守原始总 deadline，允许浏览器解析时的短暂停读；关闭服务主动中断活动连接并清理监听端口。8 项专属回归通过，覆盖大归档、源代理隔离、CSP与新增坏图拒绝、单文档边界、慢读和退出清理；完整采集回归结果见 `output/capture-recovery-20261015/replay-delivery-capture-tests-final.log`。

隔离模式分支修正先通过真实浏览器夹具复现误建 loopback 服务，再验证不再构建该服务，同时保留 HTTP 错误与新增坏图拒绝。修正后整个回放交付模块 **12 项通过，22.467 秒**，日志为 `output/capture-recovery-20261015/round2-network/replay-isolated-branch-red.log` 与 `replay-isolated-branch-green.log`。这些测试只在安全的本地源采集与最终清理结束后切换回放分支，没有修改 URL/请求安全校验，也不冒充 Linux namespace 实测；修正前完整 200 项结果另见 `output/capture-recovery-20261015/capture96-tests-final.log`。

该交付改动验证时保留 64 MiB 容量、60 秒源导航、30 秒回放导航与 120 秒总预算；未因个别慢页面继续增加生产源导航时限。

## 2026-10-16：容量预算一致性

采集的默认与最大预算提高为 96 MiB。`CaptureRequest`、归档发布、备份数据库验证和恢复包条目检查采用同一上限，避免快照生成后无法发布或无法备份。开发代理累计网络字节、已收集资源及各阶段 HTML 的预算继续从请求传递；显式较小预算仍有效，不因触顶自动重抓。

Linux root-owned launcher 固定使用 96 MiB 网络配额和产物门禁，调用方协议仍不接受容量或主机路径字段。输出 tmpfs 提高到 384 MiB，以容纳临时和最终 HTML 同时存在；私有 `/tmp` 仍为 256 MiB，进程组内存仍为 1536 MiB。launcher 修改会使旧 SHA-256 验收证明失效，Linux 部署仍须按原流程重新验证，不能据 Windows 业务测试宣称隔离验收通过。

本次保持 120 秒总时限、源/回放导航时限、32 个代理连接处理器、单资源 16 MiB、SSRF、CSP及新增首屏坏图门禁。普通小页面按实际字节使用资源；提高上限不预分配 96 MiB，也不删图或缩小缓存范围。较大 HTML 的解析/解码与内存峰值仍须通过真实样本检查，超过 96 MiB 或总时限仍报告失败。

无浏览器回归已验证：65 MiB 真实 HTML 可发布为当前快照，文件大小和哈希一致；96 MiB 真实 HTML 可经备份 ZIP 和恢复验证往返，96 MiB+1 在发布、备份与恢复入口被拒绝。发布相关 4 项、预算/launcher/client 12 项及备份 PackageTests 9 项通过，日志分别为 `budget96-publication-green.log`、`budget96-contract-green.log`、`portability96-green.log`，位于 `output/capture-recovery-20261015/`。这些测试不代表 Linux cgroup 隔离或真实大页面视觉验收已完成。旧版软件仍有 64 MiB 门禁，含较大快照的备份须使用更新后的版本恢复。

## 参考

上述后续修复合并后，完整采集测试重新执行：**162 项全部通过，382.875 秒**。日志为 `output/capture-recovery-20261015/round2-merged-capture-tests.log`，包含真实浏览器/Monolith、源导航总预算、SVG 像素、嵌套 PDF/隐藏框架及静态音频 load 回归。该结果证明回归套件通过，不代表外部拒绝访问或失效网址已经恢复。

随后独立审查又复现两个 iframe 完整性问题并修复：iframe 自身的动画/过渡现在也按采样帧冻结，防止透明框架在离线时重新显现；父页面冻结时记录可见框架清单，框架随后被删除或替换导致内容未取得时明确拒绝，避免发布可见空框。相关新增回归先失败，修复后的 iframe、嵌套内容、冻结布局和样式 **29 项通过，64.186 秒**，日志 `iframe-state-regression-tests.log`。162 项全套结果早于这最后两项修复，未重复全跑。

- [HTTPX 0.28.1 显式代理](https://github.com/encode/httpx/blob/0.28.1/docs/advanced/proxies.md)
- [Playwright Python 代理定义](https://github.com/microsoft/playwright-python/blob/main/playwright/_impl/_api_structures.py)
- [Playwright 上下文独立代理测试](https://github.com/microsoft/playwright-python/blob/main/tests/async/test_browsercontext_proxy.py)
- [AWS WAF Challenge 响应](https://docs.aws.amazon.com/waf/latest/APIReference/API_ChallengeAction.html)
- [Cloudflare Challenge 响应头](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/)
- [Mihomo 嗅探与目标覆盖](https://wiki.metacubex.one/config/sniff/)
