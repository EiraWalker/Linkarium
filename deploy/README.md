# 部署入口

应用由 `backend/supervisor.py` 管理 Web、单 worker 和备份恢复。数据存放当前主机的本地磁盘；`state.sqlite3`、`runtime/secret-key` 不进入收藏库备份。数据库 schema 变化必须先停服务，再备份、迁移和验证，不能运行 Web 同时迁移。

## Windows 本机

需要 uv、Node 24，uv 会准备 Python 3.13。按[根目录快速开始](../README.md#快速开始)，在项目根目录生成项目外的 Ed25519 PKCS8 PEM 私钥，再安装和启动：

```powershell
uv sync --frozen --python 3.13
.\.venv\Scripts\python.exe backend\manage.py file_key_setup --generate '..\website-login-key.pem'
.\scripts\setup.ps1 -PrivateKey '..\website-login-key.pem'
.\scripts\start.ps1 -DevelopmentCapture
```

已有符合格式的私钥可直接传给 `setup.ps1 -PrivateKey`，无需再次生成。正常模式使用私钥文件或 Passkey 登录，首次登录选择生成的 PEM 文件，之后可在设置中添加 Passkey；详见[登录方式](../docs/authentication.md)。安装脚本会安装锁定依赖、构建前端、安装采集工具并登记公钥。

再次启动只需 start 脚本，访问终端打印的实际地址；默认从 8000 寻找空闲端口，需要固定端口时附 `-Port 18765`。联网跨设备使用时部署到局域网地址或 HTTPS 服务器，防火墙和登录来源由部署账号配置。需要密码调试时，安装和启动脚本均加 `-DebugAuth`。

`-DevelopmentCapture` 明确启用本机开发采集：Chromium 直接在当前机器执行网站脚本，没有生产 launcher 的 namespace/cgroup/内核出网隔离，不作为公网部署安全验收。省略该参数则默认 isolated，缺少宿主 Linux capture launcher 时抓取明确失败，收藏整理、导入、AI、备份仍可运行。完整隔离部署使用 Linux 或 WSL2 Linux 环境，安装和验证步骤见 `deploy/capture/` 文档。

## Linux / Docker Compose

1. 按 `deploy/capture/` 安装并验证宿主 launcher；确认 `/run/bookmark-capture/launcher.sock` 和 `/var/lib/bookmark-capture/spool` 已存在。应用 UID/GID 为 `10001`，launcher 专用 socket 组通过 `BOOKMARK_CAPTURE_GID` 配置，spool 必须授权该应用 UID 写入每次任务目录。
2. 在主机创建本地 data 目录、设置应用 UID `10001` 的读写权限。把 `deploy/.env.example` 复制为 `deploy/.env`，填写路径、域名和 HTTPS 配置，不把私钥、密码或 AI 密钥写入镜像。
3. 正常模式首次初始化需要已有的 Ed25519 PKCS8 PEM 私钥，可在可信电脑按[根目录快速开始](../README.md#快速开始)生成。以下命令在项目根执行，将 `/absolute/path/website-login-key.pem` 替换为 Docker 宿主机上项目外的真实文件绝对路径；文件必须已存在，权限须允许容器 UID `10001` 读取，避免向其他用户开放私钥。初始化通过一次性只读挂载读取私钥并登记公钥，后续 `up` 不挂载此文件。已初始化的库无需重复执行初始化命令。

   ```sh
   docker compose --env-file deploy/.env -f deploy/compose.yaml build
   docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm --volume /absolute/path/website-login-key.pem:/run/website-login-key.pem:ro app python backend/manage.py initialize --private-key /run/website-login-key.pem
   docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
   ```

4. 本机访问 `http://127.0.0.1:8000`（自定义 `BOOKMARK_PORT` 时使用对应端口），选择私钥文件登录，之后可在设置中添加 Passkey。公网使用下面的 HTTPS reverse proxy 配置。Compose 的应用端口只绑定宿主 loopback，不能直接对公网开放 Waitress；大文件的鉴权门禁必须在代理层执行。

## HTTPS 与上传鉴权

`deploy/nginx.conf.example` 提供独立 HTTPS Nginx server。先替换域名、证书和 upstream 地址，再用 `nginx -t` 验证，检查 `nginx -V` 中启用了 `--with-http_auth_request_module`（发行版也可能将模块单独提供，需要按该版本加载）。缺少该模块不能删掉上传鉴权指令后直接发布。

默认请求体上限 1 MiB；只有精确路径 `/api/v1/imports/preview` 和 `/api/v1/restores/validate` 分别提高到 20 MiB、16 GiB。上传前，Nginx 的 `auth_request` 在 access phase 发内部 GET 到现有 `/api/v1/runtime/status`，保留原 session Cookie、不发送上传 body；未登录返回 401，固定 owner 返回 200，其他错误拒绝转发。鉴权成功后才以 HTTP/1.1 和 `proxy_request_buffering off` 向应用转发上传。应用仍执行 owner 与 CSRF 校验；代理门禁不代替这些检查。[Nginx auth_request](https://nginx.org/en/docs/http/ngx_http_auth_request_module.html)、[proxy_request_buffering](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_request_buffering)

这是对 Waitress 在进入 Django 前缓冲请求体的补充：匿名访客不能通过这两个大上传入口先向应用临时目录写入整个 body。16 GiB 是单请求初始上限；认证用户上传仍受实际可用磁盘和包校验限制，multipart 边界也计入 Nginx 请求体大小。上传 URL 按上述精确路径使用，带额外路径/尾斜杠会落到默认 1 MiB 配置。Nginx 示例不代表请求全程零内存或零磁盘缓冲。

公开 HTTPS 时配置 `BOOKMARK_HTTPS=1`、准确的 `BOOKMARK_ALLOWED_HOSTS`、完整的 `BOOKMARK_CSRF_ORIGINS=https://你的域名`，同时设置 `BOOKMARK_TRUST_PROXY=1` 与 `BOOKMARK_PROXY_PEER=实际代理TCP来源IP`。Waitress 只信任这一个精确 IP 的 X-Forwarded-For / X-Forwarded-Proto；Nginx 用 `$remote_addr` 覆盖传入 X-Forwarded-For，并清空其他 forwarding headers，不追加客户端声明的链。原生 Nginx 到原生 loopback 服务通常是 `127.0.0.1`；宿主 Nginx 到 Docker 发布端口可能在容器中表现为 bridge gateway IP，必须在受控访问测试中确认应用看到的 TCP peer，再填写，不能直接照抄 127。多层代理/CDN 不在此单代理示例的信任范围内。

当前已检查实际后端端点及 owner 鉴权返回值；**此 Nginx 配置尚未实际启动验证**。正式发布前还需运行 `nginx -t` 和真实匿名/登录大上传、chunked 上传、CSRF、代理 IP/HTTPS cookie 测试。直连 Waitress 的本机开发入口没有这层上传前鉴权门禁，公网入口须通过已验收的代理。

应用容器非 root，丢弃 Linux capabilities，不挂 Docker socket。宿主 launcher 是独立的受控组件；启动 Compose 并不证明采集隔离已通过。Docker 与 Linux launcher 的运行验收结果须以本机实际测试报告为准。

`requirements.lock` 由项目 `uv.lock` 导出，含精确版本和包哈希；依赖变化后使用 `uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file deploy/requirements.lock` 更新。前端按 npm lockfile 构建。Node/Python 基础镜像按主版本标签，正式发布另固定经测试的 image digest。

## 备份与恢复

界面中创建备份和上传恢复包，由父进程执行。上传首先验证，只有用户提交覆盖确认才切换。恢复前生成可下载的 `*.pre-restore.bookmarklib.zip`，保留旧 generation；当前认证配置保留但会话撤销，AI 配置恢复后需要重新验证凭据。维护时 Web 短暂断连，重新登录后继续查询原 operation ID。

发生 `rollback_failed` 时父进程停止，不反复操作数据。保留 state、旧/新 generations、上传包与恢复前备份，在副本上诊断；确认可用的 generation 后由维护终端更新 state 指针并递增 epoch、撤销 session，再重启。不可删除 WAL 文件或把旧包直接覆盖运行中的数据库。

官方配置依据：[Docker multi-stage](https://docs.docker.com/build/building/multi-stage/)、[Compose services](https://docs.docker.com/reference/compose-file/services/)、[Compose run](https://docs.docker.com/reference/cli/docker/compose/run/)。本文件和脚本已写入仓库，未据此宣称 Docker/launcher 已在当前 Windows 主机实际部署。
