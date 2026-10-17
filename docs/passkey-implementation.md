# Passkey 实现与验收记录

日期：2026-10-17。对应 [Issue #4](https://github.com/EiraWalker/Linkarium/issues/4)、[规格](passkey-onboarding-spec.md)和[技术方案](passkey-technical-plan.md)。

## 实现范围

- 无私钥启动；终端初始化码与网页首个 Passkey 注册在成功提交后建立唯一所有者。
- 本机直接添加；其他设备使用五分钟链接／本地二维码和独立八位码，成功一次后失效。
- Passkey 名称、重命名、创建／最后使用时间、删除；最后可用凭据由数据库事务保护。
- 会话绑定登录凭据及认证版本，固定十四天；撤销某把凭据使所有相关会话失效。
- 终端重置签发十分钟授权，成功登记新 Passkey 后撤销旧凭据、旧授权及会话，保留收藏内容。
- 可选 Ed25519 文件密钥在浏览器内签名；正常模式没有密码入口；调试密码不能授权添加。
- 规范 HTTPS 来源、本机 localhost 提示、首次准备恢复标记、离线迁移实例锁及旧数据保护。
- 注册成功回复丢失时重试相同提交，只返回已完成结果，不重新发放登录会话。

## 主要入口

| 职责 | 路径 |
| --- | --- |
| WebAuthn 验证与凭据管理 | `backend/runtime/authentication.py` |
| 授权码、有效期和授权消费 | `backend/runtime/auth_grants.py` |
| 凭据会话与绝对期限 | `backend/runtime/auth_sessions.py`；`backend/config/middleware.py` |
| schema 与旧凭据迁移 | `backend/runtime/migrations/0003_passkey_enrollment.py` |
| 首次准备和迁移互斥 | `backend/runtime/bootstrap.py`；`backend/runtime/instance_lock.py` |
| 初始化、添加、重置命令 | `backend/runtime/management/commands/passkey_{initialize,setup,reset}.py` |
| 首次／链接／重置表单 | `frontend/src/features/auth/PasskeyEnrollment.tsx` |
| 凭据管理和设备授权 | `frontend/src/features/settings/Passkeys.tsx`；`DeviceEnrollment.tsx` |
| API 文档 | `docs/api-contract.md`；`docs/openapi.json` |

## 自动化验收

认证测试使用真实 COSE／Ed25519 签名，浏览器使用 Chromium WebAuthn 虚拟认证器，不把服务端协议验证模拟为成功。并发测试使用临时磁盘 SQLite 与独立连接。所有准备、迁移、授权和重置都在隔离数据中进行。

| 规格项目 | 证据入口及范围 |
| --- | --- |
| A01–A04 首次配置和失败恢复 | `runtime.test_bootstrap`、`runtime.test_authentication_enrollment`、`tests/authentication_ui.py`；无私钥、首次提交、错误／过期码、无用户验证失败不建 owner、回复重试 |
| A05 旧文件密钥安装 | `runtime.test_authentication_concurrency` 的迁移场景：旧 Ed25519 密钥经真实 API 签名登录；浏览器可选文件入口 |
| A06 本机添加 | 浏览器新增时不保留旧认证器可调用能力，注册仍完成；凭据关联与期限前后相同 |
| A07–A08 其他设备 | jsQR 解码本地 canvas，内容严格等于链接且不含独立码；独立手机尺寸浏览器创建自己的凭据 |
| A09–A12 授权边界 | API 检查错配码、五次错误、一次消费、五分钟最后提交边界、独立有效授权；磁盘多连接并发消费 |
| A13–A17 管理和撤销 | 浏览器重命名、删除后两个相关会话 401、另一会话 200、删除当前退出、最后凭据保护；磁盘并发删除与签发凭据撤销 |
| A18 十四天会话 | Cookie 持久性、关闭并重建浏览器 context、固定到期时间；API 时钟验证期限及访问不续期 |
| A19 旧版本升级 | 0002→0003 保留凭据 ID、公钥、计数和旧 userHandle；真实旧 Passkey 登录计数 42→43；旧会话重新登录 |
| A20–A22 重置与用途 | 签发／失败不提前撤销；新凭据提交后旧 Passkey、文件密钥、会话、授权与完成记录失效；陈旧 Session 写回仍被拒绝 |
| A23–A24 协议与秘密 | CSRF、origin、RP ID、用户验证、签名和重放检查；原文不在请求、授权持久字段或二维码中；正常模式拒绝密码绕过 |
| A25 业务数据保留 | Folder、Tag、Bookmark／notes、关联、Job、Attempt、Artifact、SnapshotVersion、CaptureState、RuntimeState 逐行相等及快照文件 SHA256 相等；旧会话快照 401，新会话获得原字节 |
| A26 界面 | 桌面与手机尺寸真实浏览器截图、表单与成功路径；实体系统认证窗口另行验收 |

认证专项 40 项通过，其中两个子进程场景覆盖四组磁盘并发和旧版本迁移。启动专项 12 项通过，包括真实隔离 supervisor、Web、worker、健康状态、直接 migrate 锁和损坏旧目录保护。

综合验证结果：

- `manage.py check` 通过；`makemigrations --check --dry-run` 没有模型漂移。
- `manage.py test library runtime tests.portability`：207 项，204 通过、3 项原有 Linux 专用预览进程测试在 Windows 跳过，123.593 秒。日志：`output/passkey-implementation/backend-final.log`。
- 原有采集完整集成回归（`RUN_CAPTURE_INTEGRATION=1`）：211 项全部通过，839.116 秒。日志：`output/passkey-implementation/capture-regression.log`。
- 前端 TypeScript 检查和独立 Vite 构建通过；最终构建入口 `index-xkujQ7DH.js`。`git diff --check` 通过。
- 桌面／手机尺寸浏览器最终完整 11 组流程全部通过，包含服务端已完成本机添加后断开首个 verify 回复：自动重放相同请求、凭据仅增加一条、原会话关联和期限不变。报告：`output/playwright/passkey-20261017-170728-3aa49f/report.json`；首次配置、手机注册和最终设置三张截图位于同目录。测试服务器已停止，临时私钥已删除。

验证过程中保留了原始失败记录：首次启动测试因 Windows SQLite 连接未关闭导致临时目录清理失败，修复连接关闭后全部通过；早期浏览器脚本误选 Chromium 禁用的 5060 端口，单独复现 `ERR_UNSAFE_PORT` 后改为从高端口范围选择。并行测试期间还出现一次首次页面连接等待失败；没有提高超时或删除用例，最终浏览器验收在其他回归退出后执行。首次失败遗留的七个临时测试目录清理被工具策略拒绝，未绕过删除；这些目录不是日常收藏库。

### 可复现命令

PowerShell 中先指定独立 `BOOKMARK_DATA_ROOT` 和 `PYTHONPATH`，不要用日常数据运行验收：

```powershell
$env:BOOKMARK_DATA_ROOT = Join-Path $PWD 'output/passkey-check/data'
$env:PYTHONPATH = "$PWD;$(Join-Path $PWD 'backend')"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD 'tools/browsers'
.\.venv\Scripts\python.exe -X utf8 backend/manage.py check
.\.venv\Scripts\python.exe -X utf8 backend/manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe -X utf8 backend/manage.py test library runtime tests.portability --verbosity 1

Push-Location frontend
npm run build -- --outDir ../output/passkey-implementation/frontend-dist
Pop-Location
$env:AUTH_UI_DIST = Join-Path $PWD 'output/passkey-implementation/frontend-dist'
.\.venv\Scripts\python.exe -X utf8 tests/authentication_ui.py
```

`scripts/verify.ps1` 已将后端选择扩大到 `library runtime tests.portability`，原有采集集成测试仍保留。浏览器脚本将后端与构建产物复制到自己的隔离目录运行，默认使用 `frontend/dist`，可通过 `AUTH_UI_DIST` 指向独立构建；不覆盖日常静态产物。报告位于 `output/playwright/passkey-*`，不保存授权原文、私钥或登录 cookie。

## 实机与部署边界

以下项目尚需实际环境，自动化通过不替代它们：

1. Windows Hello 或真实安全密钥注册及登录。
2. Android／iOS 手机扫码、创建凭据和再次登录。
3. 另一台物理电脑复制链接并加入。
4. 手机切换另一网络后，通过同一正式 HTTPS 来源访问。
5. Docker 部署的实际启动及 TLS 代理配置验收。

本轮保留工作区已有侧栏改动；未迁移、重置日常收藏库，未替换日常 `frontend/dist`，未重启已有服务，未执行 Git 提交或推送。已有安装上线前按 [认证版本升级步骤](../deploy/README.md#认证版本升级)在停服维护窗口备份并迁移；旧会话需要重新登录一次。
