# Linkarium 云服务器部署包

此包包含后端、预构建前端、对应源码、锁定依赖、Docker Compose、Nginx 示例和 SHA-256 清单。服务器需要 Linux、Docker Engine 与 Compose v2；构建应用镜像需要联网下载 Python 基础镜像和依赖，不需要 Node.js。基础镜像固定多架构 digest，支持 Linux amd64 / arm64；实际服务器验收仍需在目标架构执行。

建议使用 v0.1.2 或更新的部署包；该版本包含本次 11 项安全审计修复，详见[安全修复与验收](../../docs/security-audit-remediation.md)。此前 v0.1.1 的 Docker / Nginx 验收范围见[Docker 部署验收](../../docs/docker-deployment-acceptance.md)，新版仍需在目标服务器复验。

版本与源码 commit 见包根目录 `release.json`。这是全新实例安装包，不包含本机收藏、账户、Passkey、私钥、证书、AI 密钥或运行数据。要迁移已有收藏，可在本机界面导出整库备份，在云端完成首次登录后恢复；备份不迁移登录凭据，需要在云端登记新的 Passkey。

## 首次启动

下载 Release 中的 `Linkarium-*-deploy.tar.gz` 和 `SHA256SUMS.txt`，放在同一目录。先检查外层归档，解压后进入包根目录，再检查内部文件：

```sh
sha256sum --ignore-missing -c SHA256SUMS.txt
tar -xzf Linkarium-v0.1.2-deploy.tar.gz
cd Linkarium-v0.1.2-deploy
sha256sum -c SHA256SUMS
sudo install -d -m 0700 -o 10001 -g 10001 /srv/linkarium/data
cp deploy/cloud/.env.example deploy/cloud/.env
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml config --quiet
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml up -d --build --wait --wait-timeout 180
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml exec app python backend/manage.py passkey_initialize
```

`install -d` 只用于新建的专用数据目录；已有实例先按升级步骤处理，保留原目录所有者和权限。需要 Docker 权限的主机，在 Docker 命令前加 `sudo`。命令最后输出含 `#initialize=` 片段的完整注册链接与独立授权码；打开该完整链接，再输入码创建首个 Passkey。普通登录地址加单独授权码不能完成首次登记。重发会使此前的链接和码失效。数据写入宿主 `/srv/linkarium/data`，应用映射到容器 `/data`。前端已在包内构建，无需服务器构建 JavaScript。

默认只监听服务器 `127.0.0.1:8000`。本机可先建立 SSH 隧道验证，在自己的电脑运行 `ssh -L 8000:127.0.0.1:8000 用户@服务器`，保持连接，然后在这台电脑打开终端输出的完整注册链接。默认链接使用 `http://localhost:8000` 来源。更改端口时，同时更新 `.env` 中的 `BOOKMARK_PORT` 与 `BOOKMARK_PASSKEY_ORIGIN`，并调整 SSH 隧道。

## 公网 HTTPS

准备可解析到服务器的域名和有效 TLS 证书；在宿主 Nginx 配置 [`deploy/nginx.conf.example`](../nginx.conf.example)，替换域名、证书路径及应用端口。该配置包含大文件上传前的登录鉴权，需要 Nginx `auth_request` 模块。公网只开放反向代理的 HTTPS 端口，应用端口继续保持 loopback。

编辑 `deploy/cloud/.env`：

```dotenv
BOOKMARK_ALLOWED_HOSTS=bookmarks.example.com,localhost,127.0.0.1
BOOKMARK_PASSKEY_ORIGIN=https://bookmarks.example.com
BOOKMARK_HTTPS=1
BOOKMARK_CSRF_ORIGINS=https://bookmarks.example.com
BOOKMARK_TRUST_PROXY=1
BOOKMARK_PROXY_PEER=实测的代理TCP来源IP
```

`BOOKMARK_PROXY_PEER` 必须是应用实际看到的单个 IP，不能填写域名、网段或通配符。宿主 Nginx 访问 Docker 映射端口时，这个值可能是 Docker bridge gateway；可用 `docker network inspect linkarium_default` 查看 gateway 候选，再结合受控请求的 TCP 观测确认。不能直接假定为 `127.0.0.1`。

执行 `sudo nginx -t`，检查通过后按服务器已有方式加载配置，再用上述 Compose `up` 命令应用 `.env`。使用浏览器检查 HTTPS 登录、原凭据登录、大上传的匿名拒绝与登录允许、CSRF 和 Secure Cookie。Passkey 绑定站点身份；建议在正式域名配置完成后登记长期使用的凭据。从 localhost 切到域名后，需要按[认证文档](../../docs/authentication.md)在服务器终端重设认证入口，收藏数据保留。

超过 1 MiB 的上传还需应用签发的一次性路径和大小额度，网页会自动申请。Nginx 鉴权与请求体上限继续保留；接口客户端的额度申请方式见[部署说明中的上传鉴权](../README.md)。

## 网页采集

基础配置不挂载采集 socket，因此无须先创建宿主 launcher 目录即可运行收藏管理、导入、AI 和备份。未安装采集服务时，网页快照任务会明确失败。

完整 Linux 采集隔离尚未通过验收。需要网页快照时，先完成 [`deploy/capture/README.md`](../capture/README.md) 的宿主安装与隔离验收，再填写 `BOOKMARK_CAPTURE_GID`，并在每条 Compose 命令中增加第二个文件：

```sh
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml -f deploy/cloud/compose.capture.yaml up -d --build --wait --wait-timeout 180
```

宿主 spool 根目录由 root 拥有，组为 launcher 配置的 `app_gid`，权限 `0750`；只有 `incoming/` 使用该组的 `0770` 写权限。`private/` 由 root 独占，权限 `0700`；`results/` 为 root 拥有、该组可读的 `0750`，供应用读取最终产物。应用通过 `BOOKMARK_CAPTURE_GID` 加入对应组，默认 UID 为 `10001`。保持现有整棵 spool 挂载；不得将整棵目录改为应用拥有或可写。

已启用采集的旧实例，升级时先停止应用和 launcher，安装同版本 launcher，核对上述目录权限，并使隔离 rootfs 中的 capture 代码与发布版本一致。旧 `VERIFIED.json` 绑定旧 launcher 的 SHA-256，更新后会失效；须在目标主机重新完成隔离验收，再按真实结果签发新证明并启动。不得伪造 `VERIFIED.json` 或在公网用 development 模式绕过隔离验收。这里提供部署材料，不将组件测试等同于 Linux 采集验收。

## 维护与升级

```sh
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml ps
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml logs --tail 100 app
curl --fail http://127.0.0.1:8000/health/ready
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml stop
```

容器日志可能包含初始化码，按管理员控制台权限管理。升级时先停旧版本，在服务停止后备份整个 `/srv/linkarium/data` 与旧 `.env`、代码和镜像；界面的收藏备份不包含认证运行库，不能替代整目录备份。将新包解压到新的版本目录，复制旧 `.env` 并更新 `LINKARIUM_VERSION` / `LINKARIUM_REVISION` 为新包示例中的值，数据路径继续指向原目录。确认离线升级恢复点后执行：

```sh
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml build
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml run --rm --no-deps app python backend/manage.py initialize
docker compose --env-file deploy/cloud/.env -f deploy/cloud/compose.yaml up -d --wait --wait-timeout 180
```

本次升级需要离线迁移运行库和活动收藏库，已有 schema 不会在服务启动时自动迁移。原 Passkey、文件公钥及收藏保留，旧格式会话需要重新登录；未完成的旧导入回到待确认预览，已导入记录和快照保留。回退必须匹配旧代码、旧配置和停机备份的数据；新增或撤销凭据后不能直接恢复旧认证库，以免复活已撤销凭据。详见[完整部署说明](../README.md)。

## 重新制作包

在 Git 仓库根目录，准备 uv、Python 3.13 和 Node.js 24 后执行：

```sh
python scripts/build-deploy.py --ref HEAD --version v0.1.2
```

脚本仅导出指定 Git commit 的文件，从其 `uv.lock` 离线导出镜像依赖清单，并在临时目录执行锁定依赖安装和前端构建，输出 `output/releases/v0.1.2/` 中的 `.tar.gz`、`.zip` 与外层 `SHA256SUMS.txt`。不会将工作区未提交文件、`data/`、`.env` 或本机虚拟环境带入包中。源码及其许可证随包保留，适用 AGPL-3.0-only。
