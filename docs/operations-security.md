# 运行、认证、安全与备份恢复技术方案

规划日期：2026-10-09。应用、维护控制器及部署配置已实现；当前本机验证与 Linux 完整隔离未通过的项目见 [实现记录](implementation-status.md)。用户已确认单密码登录、个人多设备、整库备份和覆盖恢复；本文保留设计基线，具体运行入口见 [部署说明](../deploy/README.md)。

## 1. 运行边界与数据布局

首版运行在一台机器上，使用两个本地 SQLite 文件、HTML 文件目录、一个 Web 进程和一个低并发业务 worker。两份 SQLite 用来隔离可迁移收藏库与本机身份/凭据，不增加数据库服务。它替代早期“所有数据放一个 SQLite 文件”的细化方案。

| 存储 | 内容 | 是否进入业务备份 |
| --- | --- | --- |
| `state.sqlite3`，Django alias `default` | 固定 owner、密码哈希、Django session、axes 失败记录、AI API 密钥、维护操作记录、当前 generation 与运行 epoch | 否 |
| `<generation>/library.sqlite3`，alias `library` | Bookmark、Folder、Tag、SnapshotVersion、Artifact、Job、JobGroup、ClassificationProposal、LibrarySettings、导入来源路径 | 是 |
| `<generation>/artifacts/` | 所有被历史版本引用的不可变 HTML 归档文件 | 是 |
| `runtime/` | Django SECRET_KEY、主机/代理配置等本机设置 | 否 |
| `work/` | 上传待校验包、抓取临时产物、解压和迁移候选目录 | 否 |
| `backups/` | 已验证的业务备份及恢复前备份 | 不递归放进另一备份 |

建议数据目录布局：

```text
data/
  state.sqlite3
  runtime/
  generations/
    <generation-id>/
      library.sqlite3
      artifacts/<sha256-prefix>/<sha256>.html
  work/
  backups/
```

业务库不引用 auth.User；单 owner 权限由 API 边界验证，不建跨库外键。路由明确把 `auth/contenttypes/sessions/axes` 和本机运行状态放入 `default`，业务 app 放入 `library`。所有业务事务显式使用 `atomic(using="library")`，迁移分别执行并验证路由。两库之间没有原子事务保证；业务执行不依赖同时提交两库。Django 官方明确不支持跨数据库外键和多对多关系。[Django 多数据库](https://docs.djangoproject.com/en/5.2/topics/db/multi-db/)

`LibrarySettings` 保存刷新、界面及 active_ai_profile_id；AIProfile 保存模型和接口地址等非机密配置。接口地址不得内嵌密码或 token；API 密钥放运行库 ProviderSecret，绑定规范化 endpoint/protocol 指纹、验证状态及 verified_library_epoch，读取设置时只返回“已配置”及脱敏标识。密钥首版依靠服务账号专属文件权限保护，不引入自制加密算法，也不声称能抵御主机管理员读取。Linux 限制目录/文件权限；Windows 限制 ACL。Django SECRET_KEY 不进入 Git、日志或业务导出。

## 2. 认证与同源入口

2026-10-16 更新：正常模式仅 Passkey／私钥文件认证；`--debug` 模式额外允许六位及以上密码。当前实现与操作步骤见 [登录配置](authentication.md)。下文的“单密码”流程只适用于调试模式；生产不提供密码认证入口，运行库新增的公钥配置与一次性挑战也不进入收藏库备份。

部署终端的初始化命令创建唯一内部 owner，交互读取密码，显式运行 Django 密码校验器，再由 Django 保存哈希。没有公开注册或网页首次创建 owner 的入口。密码重置使用部署终端工具；不把密码放命令行参数。使用 Django 提供的默认 PBKDF2 密码哈希及升级机制，不自写哈希函数。`create_user` 本身不替代密码强度验证。[Django 密码管理](https://docs.djangoproject.com/en/5.2/topics/auth/passwords/)

| 路径/动作 | 实现规则 |
| --- | --- |
| 获取登录状态和 CSRF token | 返回状态及 `get_token(request)` 的 masked token，设置 CSRF cookie；响应不缓存 |
| 登录 | 前端只提交密码；后端固定 username，调用 `authenticate(request=request, username=owner, password=...)`，成功后调用 `login` |
| 所有业务读取/写入、任务、备份、快照和历史 | 验证有效会话及固定 owner，不只依赖前端路由 |
| 写入、退出、恢复确认 | 保留 Django CSRF 中间件，JSON 请求带 `X-CSRFToken`；GET 不做业务修改 |
| 退出 | `logout` 撤销当前会话 |
| 改密码、终端重置密码、成功切换恢复库 | 撤销全部会话；重新登录仍使用本机当前密码 |

身份接口调用 Django 完整会话机制，不手工生成认证 cookie。未登录 API 返回一致 JSON `401`；CSRF 拒绝为 `403`；axes 限流为 `429`，给出有限等待信息。登录失败不泄露内部 owner 名称。[Django 登录/退出](https://docs.djangoproject.com/en/5.2/topics/auth/default/)、[Django CSRF](https://docs.djangoproject.com/en/5.2/ref/csrf/)

生产采用同源入口：`/` 为前端、`/api/v1/` 为 JSON、`/api/v1/snapshots/{id}/preview` 为鉴权快照。前后端代码分离，不要求浏览器跨域。初始会话参数为 `HttpOnly=True`、`SameSite=Lax`、HTTPS 下 `Secure=True`，有效期 7 天，后续可由配置调整；CSRF cookie 同样 Secure，token 通过受同源策略保护的初始化响应取得，不依赖读取 HttpOnly cookie。安全参数不是“默认配置一定正确”的假设，须做实际响应头验收。

公网和手机跨设备访问使用 HTTPS；仅本机回环开发环境允许 HTTP。Django `ALLOWED_HOSTS` 精确列允许主机，不开放通配 CORS。仅在受控反向代理剥除并重写对应 header 时配置 `SECURE_PROXY_SSL_HEADER`。Waitress 只监听受保护的内网/回环接口，并配置受信代理，不能直接相信客户端提交的 `X-Forwarded-For`。[Django 设置](https://docs.djangoproject.com/en/5.2/ref/settings/)、[Waitress 反向代理](https://docs.pylonsproject.org/projects/waitress/en/stable/reverse-proxy.html)

axes 初始采用按可信客户端 IP 限流：5 次失败，15 分钟冷却，可配置；不按唯一 username 全局锁定。自定义登录必须传真实 request，接入 AxesBackend/ModelBackend 及官方中间件，配置 JSON lockout response。部署验收必须证明伪造转发头不能绕过限流，同一 owner 的一个失败来源不会锁死全部设备。共享出口 IP 会共享额度，这是该轻量选择的实际限制。[Axes 使用](https://django-axes.readthedocs.io/en/stable/3_usage.html)、[Axes 配置](https://django-axes.readthedocs.io/en/stable/4_configuration.html)

## 3. 私有快照和外站 HTML

HTML 归档不位于 WhiteNoise 静态根目录，也不挂到反向代理公共文件路径。访问以 SnapshotVersion ID 查库、检查 owner、解析 Artifact 的受控相对路径；不接受用户给出的磁盘路径。返回 `Cache-Control: private, no-store`，首版不做离线缓存。WhiteNoise 仅服务已构建前端资源。[WhiteNoise 对用户媒体的边界](https://whitenoise.readthedocs.io/en/stable/django.html#serving-media-files)

应用页面默认禁止被其他站点嵌入；快照视图单独返回 `X-Frame-Options: SAMEORIGIN`，避免 Django 全局 `DENY` 阻止自己的预览 iframe，并与下述 `frame-ancestors 'self'` 一致。应用自身的 CSP 仅允许加载自己的快照入口。

每次读取归档均由服务端添加限制性响应头，网格 iframe 同时使用空 `sandbox` 属性，不给 `allow-scripts`、`allow-same-origin`、表单、弹窗或顶层导航能力。服务端 CSP 至少包含以下基线；实际资源白名单须与归档原型共同验收：

```text
Content-Security-Policy:
  sandbox;
  default-src 'none';
  script-src 'none';
  connect-src 'none';
  img-src data:;
  font-src data:;
  style-src 'unsafe-inline' data:;
  frame-src data:;
  object-src 'none';
  base-uri 'none';
  form-action 'none';
  frame-ancestors 'self'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
```

归档前固化资源并移除可执行脚本、事件处理器、javascript URL、刷新跳转和外网导航入口。保留内嵌子页面需要递归应用相同固化策略；`frame-src data:` 只是给已经内嵌的内容一个受限加载通道，不允许 `https:` 子页面回连。CSS、字体、图片或内部 frame 未成功内嵌时标记受限/失败，不靠放宽外网白名单掩盖问题。具体 iframe/srcdoc 继承与各浏览器效果必须在原型中验证，不能简单删除所有内部 frames 来换取通过。

HTTP `CSP sandbox` 还能约束直接打开快照 URL 的情况；仅有 iframe 的 sandbox 属性无法覆盖该入口。sandbox 不等于完整禁止网络，必须结合资源白名单、归档净化和浏览器测试。[MDN CSP sandbox](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/sandbox)

首版默认只下载整个备份包，不额外增加“下载并执行原始网页”的操作。如果以后提供单份 HTML 下载，必须 owner 鉴权、`Content-Disposition: attachment`、`application/octet-stream` 和 `nosniff`；这些响应头只能控制本次 HTTP 展示，不能保证文件在用户本地打开后仍受服务器 CSP 保护。可迁移 HTML 内部也必须完成固化/净化，不能把“attachment”描述为让任意 HTML 永远不可执行。

AI API 密钥只由后端向明确配置的服务发送，不进入 browser context、网页资源请求或 Monolith 参数。网页内容是分类材料，不是可执行指令；AI 输出必须通过业务 schema、目标文件夹和人工确认校验。抓取网络出口的公开地址限制、重定向和子资源校验由[快照流水线](snapshot-pipeline.md)统一实现；Windows 原生抓取出口隔离仍需原型验证。

## 4. SQLite、worker 和发布一致性

SQLite 文件必须位于当前主机的本地磁盘/本地卷，不放网络共享或实时网盘同步目录。两库初始化 WAL，开启外键，初始 `busy_timeout` 5 秒、`synchronous=FULL`。WAL 能让读与写更好地并存，但仍只有一个写者，不能当作多 worker 扩展依据；运行中的 WAL 可能含尚未写回主文件的已提交数据。[SQLite WAL](https://sqlite.org/wal.html)

业务写入使用短事务；可选 Django SQLite `transaction_mode=IMMEDIATE` 避免在写事务中先读后升级锁的部分竞争，实际兼容性由锁定版本验证。不启用把整个网络请求包入事务的 `ATOMIC_REQUESTS`。Web 的 `CONN_MAX_AGE=0`；worker 在批次和退出边界显式关闭连接。SQLite 上 `select_for_update()` 不提供行级锁，不用于 claim。[Django SQLite](https://docs.djangoproject.com/en/5.2/ref/databases/#sqlite-notes)

正常业务只有一个 worker。任务领取使用条件 UPDATE：状态必须允许执行且未被其他 token 领取，成功影响一行才运行；每次执行生成新 lease token，记录期限和心跳。重试或启动恢复回收过期租约，但保留尝试记录。JobGroup 仅组织进度，不启动第二套队列系统。

每个进程启动时记录 `generation_id` 和 `library_epoch`，每个 Job attempt 携带两者及自己的 lease token。快照产物在事务外复制到该 generation 的临时文件并校验摘要，然后在业务库短写事务里：

1. 检查 Job 当前 token、状态和 lease，Bookmark 仍存在且快照开启，Bookmark.capture_epoch 与领取时相同。
2. 校验已有不可变文件或原子重命名临时文件；插入/复用 Artifact，再追加 SnapshotVersion，更新对应设备的当前成功版本，写成功状态。清理使用相同写锁，避免文件落盘与建立引用间发生回收。
3. 若任何检查失败，不更新当前快照；临时/无引用文件交给清理任务处理。

同一 attempt 只能关联一次已提交结果；网络操作可被重试，但不能重复插入相同 attempt 的版本。抓取和文件复制不在 SQLite 事务中。先文件后数据库使中断主要留下孤立文件，启动检查可清理；反向顺序会留下指向不存在文件的有效版本。它仍不是数据库与文件系统的原子提交。[Django 事务及 on_commit 边界](https://docs.djangoproject.com/en/5.2/topics/db/transactions/)

删除收藏、清理历史先更新数据库引用，再由单 worker 确认 Artifact 无引用后物理删除。文件清理与备份/恢复互斥；不因一个版本删除而误删共享 Artifact。文件缺失、损坏或已删除的当前指针是完整性故障，保留诊断并避免继续宣称该版本可用。

generation/epoch 不是跨数据库事务锁。恢复前必须真的停掉旧进程；进程把路径绑定到启动 generation，旧 attempt 即使迟到也不能动态解析路径写入新库。普通刷新则用 lease token 和 Bookmark.capture_epoch 拒绝过期结果。

## 5. 本机维护控制器

为使界面的覆盖恢复能在 Windows 上可靠关闭所有数据库与文件句柄，规划一个很薄的标准库启动父进程。它平时只守护 Waitress Web 子进程和 management command worker；不运行业务队列、不提供对外 API、不成为额外网络服务。Linux/Windows 都由这个相同入口管理子进程。

维护请求统一记录在 `state.sqlite3` 的 MaintenanceOperation（backup/restore/upgrade）和 RuntimeIdempotency 中，父进程以条件更新领取；它不放在将被替换的 library Job 表中。`active_generation` 和 `library_epoch` 的唯一事实来源同样是 state；不同时维护另一份需要双写同步的 active 指针文件。业务 worker 对外站渲染的隔离执行另由第 8 节的宿主 capture launcher 控制，该 launcher 不持有业务数据库连接。

备份、恢复、升级互斥。进入维护先在 state 设置状态，让新业务请求返回可识别的维护响应；随后停止到期调度，允许短时完成当前写事务，停止并等待 Web/worker 退出，关闭所有 Chromium/Monolith 子进程。隔离采集可能不属于应用父进程的直接子树，必须同时向 capture launcher 按 attempt UUID 发 cancel 并取得已退出确认。超时后有界终止对应任务的进程组/cgroup，必须确认退出后才继续。仅调用主线程的 `connections.close_all()` 不能证明 Waitress 其他线程或其他进程都已释放 Windows 文件句柄。

维护期间界面显示不可用/重连，操作页通过恢复后的状态接口继续查询。Web 已停止时会断连或由网关返回 502/503，不能保证应用持续返回维护 JSON，也不能将断连判为维护失败。前端保存 operation_id 与幂等键，重新登录后查询运行库中的原操作；若提交响应丢失，通过 /api/v1/maintenance-operations/lookup 找回，不重复覆盖。维护时间取决于历史体积和磁盘速度，不保证固定秒数。启动父进程不打开 library 长连接；维护执行用一次性子进程，退出前关闭自己所有数据库和文件句柄。父进程不根据磁盘上的任意字符串拼接 shell 指令，只执行固定程序及受控参数。

## 6. 业务备份格式与步骤

选用自有、带版本 manifest 的 ZIP64 包，扩展名暂定 `.bookmarklib.zip`。包内只允许以下受控路径：

```text
manifest.json
library.sqlite3
artifacts/<sha256-prefix>/<sha256>.html
```

manifest 记录格式版本、应用/schema 版本、创建时间、源库标识、数据库及每个 Artifact 的大小与 SHA-256、业务数量和设备历史统计。ZIP CRC 不替代 manifest 校验。历史默认全保留；同一 Artifact 只打包一次。已取消、历史成功等 Job 记录可作为历史保留，运行凭据和身份始终不在业务库中。

可实施备份步骤：

1. Web 创建备份操作，返回 operation ID。控制器进入维护并停止、确认全部业务子进程退出；单 worker 不再发布新版本或做文件清理。
2. 从当前 generation 用 Python `sqlite3.Connection.backup()` 生成独立数据库副本；该 API 会读取完整数据库状态，不假设只复制 `.sqlite3` 主文件就包含 WAL 内容。复制结束关闭源和目标连接。[Python SQLite backup](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.backup)
3. 在副本上运行 `integrity_check`、`foreign_key_check`，取得全部 SnapshotVersion 引用的 Artifact 清单，验证每项存在且摘要正确。读取失败则本次备份失败，不产生可用包。[SQLite Backup API](https://www.sqlite.org/backup.html)、[SQLite 检查](https://www.sqlite.org/pragma.html#pragma_integrity_check)
4. 将清单中的不可变文件复制到独立 staging，写 manifest，关闭文件。此时 staging 已冻结且自洽，正常 Web/worker 可重新启动，耗时压缩在 staging 上继续执行；不能引用稍后可能被用户清理的线上文件。
5. 压缩至 `.partial`，重新打开验证 ZIP 结构、manifest、数据库和所有摘要，通过后在同一目标目录发布正式备份文件，标记 succeeded；下载 API 再次验证 owner。
6. 失败时保留简要错误并清理本次临时产物，恢复正常服务；既有成功备份不被覆盖。空间预检查按数据库、所有唯一 Artifact、staging 和压缩输出估算，不能以“通常能压缩很多”作为空间保证。

首次实现选择可见的维护窗口换取清楚的一致性。数据库备份 API 的一致性不自动覆盖 HTML 目录；上面暂停发布/清理、冻结文件清单并复制到 staging 的步骤是应用自己的保证。备份结束后，worker 对已确认退出的旧进程所持租约执行中断恢复；对状态不明的 lease 仍按到期规则处理，不重用旧 token。

## 7. 覆盖恢复、失败回退与断电恢复

恢复目标是业务库回到所选备份；本机密码、SECRET_KEY、API 密钥和代理配置不从备份导入。AI 接口地址/模型名会随业务库 AIProfile 恢复，但凭据仍是当前机器上的凭据。恢复后所有配置默认不可调用，直到用户显式测试/重新配置并使 ProviderSecret 指纹与 verified_library_epoch 匹配当前库；不能仅凭相同 profile UUID 把当前密钥发送到备份带来的另一个地址。

准备阶段不影响当前库：

1. 流式上传到 `work/`；校验 ZIP 条目路径、大小上限、重复名称、文件数量和压缩展开预算。拒绝绝对路径、盘符、`..`、符号链接、硬链接、Windows 特殊路径/重解析点和包外目标；不执行包内任何脚本。
2. 按受控路径解压到新的候选 generation，校验 manifest 格式、版本、摘要以及数据库完整性/外键。先用只读 SQLite 连接检查，禁用扩展加载、不附加 state 数据库，核对表/索引/schema 与应用支持版本相符，拒绝未知 trigger/view 等可执行 schema。数据库作为数据载入，不执行来自包内的 SQL 脚本；只允许应用自带、版本锁定的迁移。
3. 拒绝未来不兼容 schema；支持的旧 schema 在候选副本上迁移。检查 Bookmark 唯一 URL、目录无环、版本/Artifact 引用、设备当前指针等业务不变量。校验失败保留当前库继续运行。
4. 展示现有库与备份数量、时间及覆盖说明。用户已选择覆盖模式；执行入口为明确的恢复确认，不把上传文件本身解释为立即覆盖授权。

执行阶段由控制器完成：

5. 领取独占恢复操作，记录 `old_generation`、`candidate_generation`、阶段和目标 epoch，进入维护；停止并确认 Web、worker、浏览器、Monolith 全部退出。
6. 对当前旧库按完整备份流程生成并验证“恢复前备份”，同时保留旧 generation 原目录；此调用保持维护状态，不执行普通备份步骤中提前重启业务的动作。备份失败或空间不足则中止恢复，原库重新启动。因旧库没有原位覆盖，回退不依赖即时重新解压备份。
7. 在候选业务库把 `queued/running/retry_wait` 等未结束 Job 置为 cancelled、error_code=cancelled_by_restore，清除 JobAttempt 租约/执行权，更新 JobGroup 统计；旧 LibraryIdempotency 执行权全部失效，新记录绑定新的 epoch。不自动重放旧导入、旧 AI 调用或旧手动刷新。保留历史结果、快照和待人工确认分类方案。重新计算已启用且未暂停站点的到期状态，恢复正常后每个过期站点最多补刷一次。
8. 赋予候选新 generation 标识；关闭迁移/恢复使用的连接，完成其 WAL checkpoint，并确认候选目录包含独立有效数据库与所有归档。checkpoint 失败则不切换，不能直接丢弃 `-wal` 文件。
9. 在 `state.sqlite3` 单个事务中更新 `active_generation`、递增 `library_epoch`、记录切换阶段并删除所有 session。state 的事务只提交运行指针；它不原子提交业务数据库和文件。候选已经事先完整准备好，旧库始终保留。
10. 启动 Web/worker 新进程，它们从 state 读取新 generation，先验证路径/schema/完整性摘要及就绪状态；通过后清除维护状态，前端重新登录。worker 在维护解除前不领取新业务任务。
11. 若新进程启动或就绪检查失败，停止新进程，在 state 事务把指针切回旧 generation 并再次递增 epoch，重启旧库。只有确认旧库恢复可用后才报告“恢复失败，已回退”。如果回退也失败，维持维护状态、保留两个目录和恢复前备份，给出终端修复入口。

这是“准备完整候选 → 停止访问 → 切换一处运行指针 → 重启验证”的协议；没有把多个目录重命名、数据库提交和进程启动描述成一个原子操作。实现不依赖替换仍被 Windows 打开的数据库。迁移前后都保留原 generation，旧库清理另行由用户确认或受控保留策略处理，不在恢复成功的瞬间递归删除。

| 故障点 | 启动恢复行为 |
| --- | --- |
| 候选校验/迁移中断，尚未切指针 | 按旧指针启动，标记本次操作失败，候选可清理 |
| 已停机、恢复前备份未完成 | 保持旧指针，验证后重启旧库 |
| state 已切换，新库尚未 ready | 控制器读取持久阶段，对候选执行启动检查；通过完成，否则回到旧 generation |
| 新库 ready 但尚未解除维护 | 幂等完成恢复记录，不再次导入数据或增加一批历史 |
| 恢复过程中用户刷新/重复提交 | 返回原 operation ID 或维护状态，不创建第二个覆盖操作 |
| 旧采集结果迟到 | 旧进程应已退出；额外依靠固定 generation 路径、epoch 和 Job token 拒绝提交新库 |

备份文件可能来自其他机器，不认可其中的运行 generation、session 或租约为当前权限。恢复后的手动队列重新发起，周期刷新按已确认规则补刷；不会把停机时错过的每一个周期重复执行。

## 8. 部署与升级计划

Linux 的唯一推荐部署分工为“非 root 应用容器 + 宿主受控采集 launcher + 临时采集环境”，均在同一机器上：

| 部分 | 权限与职责 |
| --- | --- |
| Docker Compose 中的应用容器 | 入口为本机控制器，内部运行 Waitress 和单业务 worker；挂本地 `data/`，拥有业务数据库权限；无 `NET_ADMIN`/`SYS_ADMIN`、无 Docker socket |
| 宿主 capture launcher | 安装阶段由部署脚本配置的受控本地服务，负责创建和销毁固定采集入口、namespace、网络规则和 cgroup；通过权限 Unix socket 接单，不监听公网 |
| 临时采集环境 | 非 root 运行匹配的 Playwright/Chromium 与 Monolith，隔离 net/mount/pid/user namespace；只读任务输入、有限可写输出，不挂数据库、密钥、应用 runtime 或 Docker socket |
| HTTPS 入口 | 复用现有 reverse proxy；没有现成入口时提供可选代理部署配置 |

launcher 的 start/cancel/status/退出确认统一使用 `(attempt_uuid,generation,library_epoch)`，不使用重试间复用的 Job ID。它不接受任意命令、可执行路径、宿主目录或 shell 文本。固定 spool 根目录由它自行解析，验证调用方本地权限、UUID、symlink/路径越界、JobAttempt 状态和资源预算；任务文件只含已校验 URL 与采集配置。创建隔离环境的控制面保留所需权限，但 Chromium、Monolith、Web 和普通 worker 均不继承这些权限。维护取消必须结束该 attempt 的完整进程组并返回同一身份的退出确认，防止旧取消请求误伤新重试。

计划固定挂载：宿主 `/run/bookmark-capture/launcher.sock` 映射应用容器同名 socket，以专用组限制连接；宿主 `/var/lib/bookmark-capture/spool` 映射应用的 `/capture-spool`。应用只提交 attempt UUID；launcher 根据 UUID 找固定子目录，封存输入后再启动，输入只读、输出有配额，返回时检查非符号链接普通文件与大小/哈希。采集子进程只挂该次输入输出，不挂 launcher socket 或整个 spool。具体服务账号/UID 映射由部署脚本固定并测试。

采集网络采用 namespace 内与宿主转发位置的默认拒绝规则：仅允许经公开地址判断的 TCP 80/443，以及指定 resolver 的 DNS，首版关闭采集 IPv6；因此重定向、子资源和 DNS rebinding 不能绕过最终内核目的地址检查。不得把默认 Docker bridge 当作公网地址过滤，也不得默认使用容器 `privileged` 或 Chromium `--no-sandbox`。具体规则、DNS、NAT 后过滤、公开特殊地址分类以及浏览器 sandbox 组合均列入快照原型硬验收。

这会增加一个受控的宿主安装组件，是动态网页执行隔离的实际成本。非 root 应用容器不会自动获得 `unshare/setns` 能力，不能把“启动 Compose 成功”当作隔离完成。首个技术原型必须交付可重复安装和卸载的 launcher/规则脚本，并验证该分工，否则部署能力保持未通过；不在失败时隐式回退为无限制联网抓取。

镜像多阶段构建：Node 仅构建前端；应用运行镜像包括 Python、Django、Waitress、WhiteNoise；采集工具链镜像/固定目录包含匹配的 Playwright、Chromium 和 Monolith。HTML 流水线只输出归档产物，后端验证完成后才发布到 generation 的 artifacts。两类工具链版本均锁定，无需常驻浏览器。

Windows 原生方案使用 Python 虚拟环境、匹配 Chromium/Monolith 的路径、前端 dist、同一控制器入口；任务计划程序负责开机启动和进程失败重启。开发和手动调试可以前台启动；常驻计划任务后台运行。Web 原生运行可行不代表整个抓取安全与进程资源限制已验证；出网隔离、子进程树终止、端口/代理设置需要 Windows 验收后才宣称支持完整部署。

正常启动先检查数据目录权限、schema、工具版本和当前 generation；只初始化缺失的显式新库，不在打不开旧库时静默创建空库。迁移由单一维护命令执行，Web/worker 不在同时启动时各自自动 migrate。升级先备份业务库并保存对应旧代码/镜像版本，在维护窗口迁移；代码降级不保证新 schema 可读，回退需成对使用旧版本代码与升级前库。

以 2 vCPU、4 GiB RAM 的 Linux 测试机作为首轮测量基线，Web 初始少量线程、采集并发 1；该数值是测试配置，不是已验证最低配置。实际峰值通常由目标网页和 Chromium 决定，必须测空闲、单站双设备、千条队列、备份峰值和磁盘增长后发布容量建议。两设备历史无限保留时，磁盘用量按唯一 Artifact 实测累计，不假设每站固定几 KB。

## 9. 观察与验收

日志只记 operation/job/attempt/group ID、错误分类、耗时、字节数与脱敏目标域名；不记密码、cookie、AI key、完整 HTML 或带 query token 的 URL。接口错误不向用户暴露内部路径/堆栈。登录日志和失败尝试保留有限天数并定期清理；清理的是运行日志，不影响用户要求永久保留的快照历史。

健康检查分为进程存活、应用就绪和 worker 心跳。匿名检查只返回必要状态，不暴露库路径、数量和配置；详细任务/磁盘/失败信息 owner 登录后查看。恢复进度放 state，业务库切换后仍可查到同一操作结果。

实现必须覆盖以下验收，而不是只检查配置文字：

- 未登录读取 API、任意历史、HTML、备份全部拒绝；登录 CSRF、退出、会话撤销和真实代理限流通过。
- 恶意归档尝试 script、事件属性、frame、表单、导航、网络回连和直接顶层打开，仍不能访问应用会话或业务接口；两端浏览器真实 iframe 渲染通过。
- 并发删除/停用与迟到采集、重复 claim、强杀 worker、文件已发布但事务未提交、事务成功后响应丢失，均不破坏当前版本和历史引用。
- 有活动 WAL 时备份恢复后数据齐全；备份只包含业务数据及全部被引用归档，解包没有密码哈希、session、密钥或 SECRET_KEY。
- 恢复前备份失败、候选损坏、迁移失败、磁盘满、切换各阶段断电、Windows 文件句柄未关闭，当前库可继续运行或明确停留在可回退维护状态。
- 恢复覆盖确认后新库数据符合备份，旧 Job 不执行，旧 worker 结果不进入新库，当前密码保留、全部 session 失效，AI endpoint 改变不会泄露旧凭据。

上述保留为完整验收目标；当前已经实施的测试、真实备份恢复和仍未通过的部署项见 [实现与验收记录](implementation-status.md)，不能把配置文件交付当成已完成生产部署。
