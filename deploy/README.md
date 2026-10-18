# 部署入口

GitHub Release 提供包含预构建前端的云服务器 deploy 包，服务器无需 Node.js。下载后从包根目录 `README-DEPLOY.md` 开始，或参阅[云服务器部署说明](cloud/README.md)。基础配置可独立启动收藏管理，网页采集通过额外的 Compose 配置接入已验收的宿主隔离服务。重新打包入口为 `python scripts/build-deploy.py --ref HEAD --version v0.1.2`。

应用由 `backend/supervisor.py` 管理 Web、单 worker 和备份恢复。数据存放当前主机的本地磁盘；`state.sqlite3`、`runtime/secret-key` 不进入收藏库备份。数据库 schema 变化必须先停服务，再备份、迁移和验证，不能运行 Web 同时迁移。

## Windows 本机

需要 uv、Node 24，uv 会准备 Python 3.13。按[根目录快速开始](../README.md#快速开始)，在项目根目录安装和启动：

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1 -DevelopmentCapture
```

安装脚本会安装锁定依赖、构建前端、安装采集工具并离线准备数据库。服务就绪后，终端给出含 `#initialize=` 片段的完整注册链接与独立授权码；尚未登记首个 Passkey 时，每次重启都会签发新授权并使旧链接和码失效。用户打开该完整链接、输入码并创建首个 Passkey。普通登录地址加单独授权码不能完成首次登记。已有私钥可选传给 `setup.ps1 -PrivateKey`，保留原文件密钥初始化能力；详见[登录方式](../docs/authentication.md)。

再次启动只需 start 脚本，访问终端打印的 `http://localhost:实际端口`；默认从 8000 寻找空闲端口，需要固定端口时附 `-Port 18765`。电脑和手机跨网络使用需部署同一个可访问的 HTTPS 域名，并设置 `BOOKMARK_PASSKEY_ORIGIN`；原始局域网 IP 不作为 Passkey 站点身份。需要密码调试时，安装和启动脚本均加 `-DebugAuth`。

`-DevelopmentCapture` 明确启用本机开发采集：Chromium 直接在当前机器执行网站脚本，没有生产 launcher 的 namespace/cgroup/内核出网隔离，不作为公网部署安全验收。省略该参数则默认 isolated，缺少宿主 Linux capture launcher 时抓取明确失败，收藏整理、导入、AI、备份仍可运行。完整隔离部署使用 Linux 或 WSL2 Linux 环境，安装和验证步骤见 `deploy/capture/` 文档。

Windows 的标准启动入口另支持 `-LanHttps` 和可选 `-NetworkAdapter`，详情见[局域网访问与本地 HTTPS](../docs/network-access.md)。同一 supervisor 管理 loopback Web、单 worker、HTTPS 代理及仅服务器本机可访问的证书页。新设备通过独立可信渠道取得 CA 并核验终端指纹。部署入口 `start-windows.ps1` 支持相同选项；`-SkipBuild` 时先单独准备 HTTPS 运行依赖。

## Linux / Docker Compose

1. 按 `deploy/capture/` 安装并验证宿主 launcher；确认 `/run/bookmark-capture/launcher.sock` 和 `/var/lib/bookmark-capture/spool` 已存在。应用 UID/GID 默认为 `10001`，通过 `BOOKMARK_CAPTURE_GID` 加入 launcher 的实际 socket / `app_gid` 组。spool 根目录为 root 拥有的 `0750`，只有 `incoming/` 为该组可写的 `0770`；`private/` 为 root 独占的 `0700`，`results/` 为 root 拥有、该组可读的 `0750`。保持 root 对这些目录的所有权。
2. 在主机创建本地 data 目录、设置应用 UID `10001` 的读写权限。把 `deploy/.env.example` 复制为 `deploy/.env`，填写路径、域名和 HTTPS 配置，不把私钥、密码或 AI 密钥写入镜像。
3. 首次安装无需私钥挂载。以下命令在项目根执行；supervisor 在实例锁内准备全新数据库，并在服务就绪后输出完整注册链接与独立授权码：

   ```sh
   docker compose --env-file deploy/.env -f deploy/compose.yaml build
   docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
   docker compose --env-file deploy/.env -f deploy/compose.yaml exec app python backend/manage.py passkey_initialize
   ```

4. 最后一条命令在管理员终端重发首次登记授权，并使启动时的链接和码失效。打开新输出的完整 `#initialize=` 链接，再输入独立授权码创建 Passkey；链接来源由 `BOOKMARK_PASSKEY_ORIGIN` 指定，本机默认为 `http://localhost:8000`。调整 `BOOKMARK_PORT` 时同步调整该来源的端口。公网使用下面的 HTTPS reverse proxy 配置。Compose 的应用端口只绑定宿主 loopback，不能直接对公网开放 Waitress；大文件的鉴权门禁必须在代理层执行。

已启用采集的旧实例，升级前停止应用和 launcher，更新同版本 launcher、隔离 rootfs 中的 capture 代码及上述目录权限。launcher 更新会使绑定旧 SHA-256 的 `VERIFIED.json` 失效；须在目标 Linux 主机重新完成隔离验收，按真实结果签发新证明后再启动，不能复用旧证明。

初始化码和新设备授权只向管理员终端显示，不在健康检查、匿名会话状态或业务日志中返回；容器 stdout 可能由宿主日志驱动保留，因此按服务器控制台权限管理容器日志。已有安装的数据库升级须先停止服务，按下面的维护步骤执行，不能用上述启动命令自动迁移。

## HTTPS 与上传鉴权

`deploy/nginx.conf.example` 提供独立 HTTPS Nginx server。先替换域名、证书和 upstream 地址，再用 `nginx -t` 验证，检查 `nginx -V` 中启用了 `--with-http_auth_request_module`（发行版也可能将模块单独提供，需要按该版本加载）。缺少该模块不能删掉上传鉴权指令后直接发布。

默认请求体上限 1 MiB。精确路径 `/api/v1/imports`、`/api/v1/imports/preview` 的 multipart 门禁为21 MiB（HTML文件仍最多20 MiB），`/api/v1/restores/validate` 为16 GiB。Nginx `auth_request` 在 access phase 以原 session Cookie、无body的内部GET验证owner，其他结果拒绝转发。应用仍执行owner和CSRF检查。[Nginx auth_request](https://nginx.org/en/docs/http/ngx_http_auth_request_module.html)

所有内置HTTP、LAN HTTPS和代理入口还共享Waitress请求头门禁：普通body最多1 MiB；大上传先以owner和CSRF调用 `/api/v1/runtime/uploads`，将一次性返回值放入 `X-Linkarium-Upload`。额度绑定路径、字节数、120秒开始期限及单个并发，先预留磁盘；断线和最多1小时总时限释放。Nginx门禁是附加防护，不代替该授权。multipart边界计入上限；不承诺零内存或零磁盘缓冲。

公开 HTTPS 时配置 `BOOKMARK_PASSKEY_ORIGIN=https://你的域名`、`BOOKMARK_HTTPS=1`、准确的 `BOOKMARK_ALLOWED_HOSTS`、完整的 `BOOKMARK_CSRF_ORIGINS=https://你的域名`，同时设置 `BOOKMARK_TRUST_PROXY=1` 与 `BOOKMARK_PROXY_PEER=实际代理TCP来源IP`。允许主机需保留用于容器内部健康检查的 `127.0.0.1`。Passkey 来源必须与用户浏览器地址一致；链接生成不会信任客户端 Host。Waitress 只信任这一个精确 IP 的 X-Forwarded-For / X-Forwarded-Proto；Nginx 用 `$remote_addr` 覆盖传入 X-Forwarded-For，并清空其他 forwarding headers，不追加客户端声明的链。原生 Nginx 到原生 loopback 服务通常是 `127.0.0.1`；宿主 Nginx 到 Docker 发布端口可能在容器中表现为 bridge gateway IP，必须在受控访问测试中确认应用看到的 TCP peer，再填写，不能直接照抄 127。多层代理/CDN 不在此单代理示例的信任范围内。

历史Docker/Nginx验收范围见 [Docker 部署验收](../docs/docker-deployment-acceptance.md)。新增源码门禁使用真实Waitress socket验证声明长度、Expect、chunked、票据重放、断线和慢速上传；16 GiB以请求头拒绝边界验证，未实际发送16 GiB文件。公网仍需部署可信TLS代理并验证实际配置。

应用容器非 root，丢弃 Linux capabilities，不挂 Docker socket。宿主 launcher 是独立的受控组件；启动 Compose 并不证明采集隔离已通过。Docker 与 Linux launcher 的运行验收结果须以本机实际测试报告为准。

`requirements.lock` 由项目 `uv.lock` 导出，含精确版本和包哈希；依赖变化后使用 `uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file deploy/requirements.lock` 更新。前端按 npm lockfile 构建，Python 基础镜像固定 image digest。应用镜像使用 Debian trixie 的 Python 3.13，使 SQLite 在 `trusted_schema=OFF` 时仍支持恢复库中的 JSON 约束；对应能力由 `tests/deploy/test_sqlite_runtime.py` 验证。

## 备份与恢复

界面中创建备份和上传恢复包，由父进程执行。上传首先验证，只有用户提交覆盖确认才切换。恢复前生成可下载的 `*.pre-restore.bookmarklib.zip`，保留旧 generation；当前认证配置保留但会话撤销，AI 配置恢复后需要重新验证凭据。维护时 Web 短暂断连，重新登录后继续查询原 operation ID。

发生 `rollback_failed` 时父进程停止，不反复操作数据。保留 state、旧/新 generations、上传包与恢复前备份，在副本上诊断；确认可用的 generation 后由维护终端更新 state 指针并递增 epoch、撤销 session，再重启。不可删除 WAL 文件或把旧包直接覆盖运行中的数据库。

## 认证版本升级

1. 先用隔离数据副本验证新版本迁移；停止正在使用该目录的 supervisor、Web 和 worker，在维护窗口保存整个数据目录与匹配版本的代码／配置。收藏库导出不包含认证运行库，不能单独代替这份恢复点。
2. 设置与服务相同的 `BOOKMARK_DATA_ROOT`，离线运行 `python backend/manage.py initialize`。Windows 可使用 `.\.venv\Scripts\python.exe`；Compose 在停止 app 后使用 `docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm app python backend/manage.py initialize`。命令取得同一实例锁，再迁移两个数据库；不创建新所有者、不要求私钥、不撤销现有凭据。
3. 构建并同时部署前后端，重启服务，确认健康状态、真实登录页及原凭据登录。升级前的旧格式会话要求重新登录一次，原 Passkey 与文件公钥保持有效。

自动首次准备在 `runtime/preparing-databases.json` 写入不含秘密的标记，记录目录与格式版本；成功后移除。中断后同目录可以继续。没有该标记的半成品或已有数据库存在未执行迁移时，服务会停止并要求离线准备；不要删除原数据库来“修复”启动。

尚未发生新的认证登记、撤销或重置时，可以停服务并恢复维护前匹配的代码与数据。发生这些操作后，直接恢复旧状态库会复活已撤销凭据；须采用前向修复或保留新认证语义的兼容回退版本。Passkey 终端重置只替换认证配置、保留收藏数据，命令见[登录配置](../docs/authentication.md#服务器终端添加与重置)。

官方配置依据：[Docker multi-stage](https://docs.docker.com/build/building/multi-stage/)、[Compose services](https://docs.docker.com/reference/compose-file/services/)、[Compose run](https://docs.docker.com/reference/cli/docker/compose/run/)。本文件和脚本已写入仓库，未据此宣称 Docker/launcher 已在当前 Windows 主机实际部署。
