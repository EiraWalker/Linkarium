# Docker 部署验收

2026-10-18，v0.1.1 的云部署配置在独立测试数据上通过 Docker / Nginx 功能验收。测试没有读取或迁移日常收藏库。推荐使用 v0.1.1 或更新的 deploy 包。

## 运行环境

| 项目 | 实测环境 |
| --- | --- |
| 宿主 | Windows，WSL2 Ubuntu 24.04，Linux amd64 |
| 容器运行时 | Docker Engine 29.9.1，Compose 5.6.0，Buildx 0.38.0，containerd 2.4.2 |
| 应用基础镜像 | `python:3.13-slim-trixie`，固定多架构 digest |
| 容器内 SQLite | 3.46.1 |
| 反向代理 | Nginx 1.30.6，启用 `http_auth_request_module` |
| HTTPS | 本地短期自签证书，`https://localhost:18443`；测试浏览器忽略该证书的信任错误 |
| 浏览器 | 独立 headless Chrome，虚拟 CTAP2 WebAuthn authenticator |

只安装 Docker Engine，复用已有 WSL；没有安装 Docker Desktop，也没有启动其他应用栈。基础 Compose 仅启动应用，公开 HTTPS 使用主机已有的 Nginx 和证书。本次 Nginx 测试容器只用于验收。

云包已预构建前端，运行与构建应用镜像均无需 Node.js；镜像没有 Chromium、Monolith 或 Docker socket。Playwright 的 Python 包由当前 capture 模块在导入时使用，应用依赖中仍保留该包。网页采集所需浏览器和宿主 launcher 单独配置，不随基础应用启动。

测试时应用镜像约 480 MB（Docker 显示的本地镜像总大小），应用容器空闲内存约 163 MiB，Nginx 约 28 MiB；这些是单次观测，不是容量保证。测试容器用后停止，运行时可按需启停。

## 验证结果

- 从 GitHub v0.1.0 deploy 包校验、解包、构建并启动，发现问题后在相同隔离环境复现并验证 v0.1.1 修复。
- 实际 HTTPS 首次登记 Passkey、签名登录、退出后登录，以及容器重启后的再次登录。
- Cookie 为 Secure / HttpOnly，真实代理 TCP peer 经过探测后写入配置；非默认 HTTPS 端口保持完整来源。
- 匿名 1 字节、2 MiB 和 chunked 大上传返回 401；登录后 2 MiB multipart 导入预览成功。普通、导入和恢复请求声明长度超过各自上限时返回 413，未发送巨大测试 body。
- 已登录请求缺少 CSRF token 时返回 403。
- 文件夹和收藏新增、修改 revision、星标筛选，以及桌面和手机尺寸的页面截图检查。
- 创建并下载 ZIP 备份，校验包内容；增添测试收藏后上传备份、验证并确认恢复，重新登录后确认恢复结果。恢复保留凭据并撤销旧会话。
- 容器重启后收藏和正常会话持久化；浏览器没有未处理的页面错误。
- 非 root UID/GID 10001、只读应用文件系统、数据目录可写、无有效 capabilities、未挂载 Docker socket、采集保持 isolated 模式。
- Linux 实际 TCP TIME_WAIT 重启回归和占用端口拒绝回归通过；Windows 启动与端口回归共 17 项，Linux 专属项在 Windows 跳过。

## 本次修复

1. Nginx 内部鉴权子请求继承上传的 Content-Length，原默认 1 MiB 限制使大上传收到 500。内部无 body 的鉴权 location 取消该限制；公开上传入口保留原上限。
2. Nginx 转发 Host 保留非默认端口，使 Passkey 来源检查与浏览器实际 HTTPS 地址一致。
3. Linux supervisor 在停止服务后的端口探测使用 `SO_REUSEADDR`，允许 TIME_WAIT 后重启，仍拒绝真实监听中的服务；Windows 保留独占端口探测。
4. 应用基础镜像改用 Debian trixie。原 bookworm 容器中的 SQLite 3.40.1 在 `trusted_schema=OFF` 下拒绝 JSON CHECK 约束写入，阻断恢复；新运行时通过相同能力测试，保留原安全设置。

## 复验入口与范围

真实 Nginx 上传门禁回归只应指向专用测试实例：

```sh
DEPLOY_TEST_ORIGIN=https://test.example.com python -m unittest discover -s tests/deploy -p test_upload_gate.py -v
python -m unittest discover -s tests/deploy -p test_sqlite_runtime.py -v
```

仅本地自签证书测试可额外设置 `DEPLOY_TEST_INSECURE_TLS=1`。SQLite 能力回归须在实际应用镜像内执行；宿主 SQLite 版本不能代替容器运行时验证。supervisor 回归可通过 `backend/manage.py test runtime.test_supervisor_ports runtime.test_bootstrap` 执行，并将 `BOOKMARK_DATA_ROOT` 指向独立空目录。

本次没有验证真实公网 DNS / 公共 CA、物理 Passkey 设备、arm64 运行、16 GiB 实际上传、AI 外部服务或完整 Linux 网页采集隔离。基础应用没有挂载 capture launcher，采集会明确返回隔离环境未就绪。不得以此报告替代 `deploy/capture/README.md` 的宿主隔离验收，也不得伪造 `VERIFIED.json`。
