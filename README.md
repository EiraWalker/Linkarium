<div align="center">

# Linkarium

**把网站收藏、页面快照与分类整理放在一起。**

面向个人电脑与手机的自托管可视化网站收藏库。

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
![Preact](https://img.shields.io/badge/Preact-673AB8?logo=preact&logoColor=white)
![Django](https://img.shields.io/badge/Django-092E20?logo=django&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)

[快速开始](#快速开始) · [文档](#文档) · [问题反馈](https://github.com/EiraWalker/Linkarium/issues)

</div>

## 功能

- **可视化收藏**：网站卡片、嵌套文件夹、标签、备注、星标、多字段搜索和批量整理；适配桌面与手机布局。
- **HTML 页面快照**：分别保存桌面与手机版本，保留历史，支持定时刷新和文件夹批量刷新。
- **AI 分类校对**：连接兼容 Chat Completions 的服务生成建议，经人工确认后创建目录并移动网站。
- **导入与备份**：导入 Chrome / Firefox 书签 HTML；整库备份包含页面快照与历史，支持校验和覆盖恢复。
- **密钥登录**：支持 Passkey 与 Ed25519 私钥文件；单用户访问自己的收藏库。

技术栈为 Preact + TypeScript + Vite、Django + SQLite。前端构建后无需常驻 Node 服务，也无需 Redis 或独立数据库；Chromium 与 Monolith 按采集任务启动。

## 快速开始

Windows PowerShell，需要 [uv](https://docs.astral.sh/uv/) 与 Node.js 24。在克隆后的项目根目录执行，uv 会准备 Python 3.13：

```powershell
uv sync --frozen --python 3.13
.\.venv\Scripts\python.exe backend\manage.py file_key_setup --generate '..\website-login-key.pem'
.\scripts\setup.ps1 -PrivateKey '..\website-login-key.pem'
.\scripts\start.ps1 -DevelopmentCapture
```

私钥保存在项目外，生成命令不会覆盖同名文件。首次登录选择这个 PEM 文件，之后可在设置中添加 Passkey；已有 Ed25519 PKCS8 PEM 私钥也可直接传给 `setup.ps1 -PrivateKey`。密钥应由使用者妥善保管。

安装脚本会安装锁定依赖和采集工具、构建前端并初始化用户。后续只需运行启动命令；访问终端显示的实际地址，默认从 8000 寻找空闲端口，`-Port 8001` 可固定端口。

正常模式仅接受密钥登录。需要密码调试时，安装和启动脚本均加 `-DebugAuth`，密码由终端交互设置，至少六位。Passkey 来源配置、跨设备使用与凭据管理见[登录配置](docs/authentication.md)。

## 运行与部署

数据默认保存在项目的 `data/` 目录，可通过 `BOOKMARK_DATA_ROOT` 指定独立数据目录。多设备共享同一后端；局域网访问和 HTTPS 配置见[部署说明](deploy/README.md)。备份不含登录凭据、会话和 AI 密钥；恢复保留本机凭据并撤销会话。

Windows 的 `-DevelopmentCapture` 会在本机执行网页采集。使用 Clash Verge 时，可在“设置 → 网页缓存代理”配置本机代理；fake-IP DNS 环境也可启动时附加 `-PublicDns`。详见[缓存兼容性与代理](docs/capture-proxy.md)。

当前版本可供本机使用。**完整 Linux 采集隔离尚未通过验收，Docker / Nginx / 实体手机测试仍待完成**，公网生产部署需先完成对应验证。网页归档的兼容性以已测样本为准，验证码、受限资源和复杂动态内容可能导致抓取失败或显示差异。实际证据与限制见[实现与验收记录](docs/implementation-status.md)。

## 文档

| 入口 | 内容 |
| --- | --- |
| [登录配置](docs/authentication.md) · [部署说明](deploy/README.md) | 安装后的认证与运行配置 |
| [浏览器导入](docs/browser-import.md) · [后台任务诊断](docs/background-task-diagnostics.md) | 导入、任务与采集排查 |
| [技术方案](docs/technical-plan.md) · [数据模型](docs/data-model.md) · [API 契约](docs/api-contract.md) | 架构与接口 |
| [首版需求](docs/requirements.md) · [实现与验收记录](docs/implementation-status.md) | 产品规则、验证结果与限制 |

[本机管理 API、CLI 与 Agent Skill](docs/management-api-spec.md) 尚为待实施规格；[网站更新订阅](docs/website-subscriptions.md) 仍在需求澄清中。

## 开发

应用代码位于 `backend/`、`frontend/`，网页采集位于 `backend/capture/`，启动与部署入口位于 `scripts/`、`deploy/`。依赖由 `uv.lock` 和 `frontend/package-lock.json` 锁定。

安装完成后，在项目根目录运行检查：

```powershell
.\scripts\verify.ps1
```

该脚本执行 Django 检查、迁移差异检查、后端测试、Chromium / Monolith 采集测试及前端类型检查与构建。复验命令和验证边界见[实现与验收记录](docs/implementation-status.md)。

欢迎通过 [Issues](https://github.com/EiraWalker/Linkarium/issues) 反馈问题或讨论改进。提交内容请使用示例数据，保留自己的收藏库、私钥与运行配置。

## 许可证

采用 [GNU Affero General Public License v3.0](LICENSE)（AGPL-3.0-only）。
