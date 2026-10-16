# 登录方式

2026-10-16：正常模式仅公私钥认证；调试模式额外允许六位及以上密码。它替换早期仅密码登录的方案。认证模式由后端启动参数决定，前端隐藏入口之外，后端也拒绝正常模式的密码请求。

| 登录方式 | 正常模式 | `--debug` 模式 |
|---|---|---|
| Passkey（Windows Hello／手机／安全密钥） | 支持 | 支持 |
| 私钥文件（Ed25519、PKCS8 PEM） | 支持 | 支持 |
| 密码（允许六位数字） | 禁止 | 支持 |

## 首次安装

正常模式初始化必须提供已有的 Ed25519 PKCS8 PEM 私钥。Windows 首次使用按[根目录快速开始](../README.md#快速开始)操作，在项目根目录生成项目外的密钥并安装：

```powershell
uv sync --frozen --python 3.13
.\.venv\Scripts\python.exe backend\manage.py file_key_setup --generate '..\website-login-key.pem'
.\scripts\setup.ps1 -PrivateKey '..\website-login-key.pem'
.\scripts\start.ps1 -DevelopmentCapture
```

生成命令不会覆盖同名文件。已有符合格式的私钥可直接传给安装脚本，无需再次生成；安装只登记对应公钥。需要密码调试时，安装和启动脚本均加 `-DebugAuth`，安装时由终端交互设置密码。

## 启动

正常启动不加认证调试参数：

```powershell
.\.venv\Scripts\python.exe backend\manage.py run_web --port 8001
```

需要调试密码时：

```powershell
.\.venv\Scripts\python.exe backend\manage.py run_web --port 8001 --debug
```

带单业务 worker 的原启动脚本：

```powershell
.\scripts\start.ps1 -Port 8001 -DevelopmentCapture -PublicDns -DebugAuth
```

删去 `-DebugAuth` 即为正常认证模式。`-DevelopmentCapture` 控制本机网页抓取，`-PerformanceMonitoring` 控制耗时监测，与认证模式独立。`--debug` 不启用 Django 的错误堆栈公开页面。

修改调试密码（密码不放命令行参数）：

```powershell
.\.venv\Scripts\python.exe backend\manage.py initialize --reset-password --debug
```

正常模式下该改密操作被拒绝，密码登录接口返回 `PASSWORD_LOGIN_DISABLED`。切回正常模式后，原密码登录会话无法继续访问；真实密钥登录会话在两种模式间兼容。

## 初始化与首次登录

已按上述安装流程初始化的库无需重复执行 `initialize`。正常模式的 `initialize` 必须指定已有私钥文件；缺少文件、格式无效或只指定 `--no-password` 都会在初始化写入前失败。手动首次初始化时，可以先生成新私钥，再初始化：

```powershell
.\.venv\Scripts\python.exe backend\manage.py file_key_setup --generate '..\website-login-key.pem'
.\.venv\Scripts\python.exe backend\manage.py initialize --private-key '..\website-login-key.pem'
```

私钥文件必须是未加密的 Ed25519 PKCS8 PEM，生成命令不会覆盖已有文件。已有私钥时跳过生成步骤。初始化只在认证数据库登记对应公钥；重复初始化保留已有凭据，不修改私钥文件。调试模式的初始化允许省略私钥，并由终端交互设置至少六位的密码。

启动后，在登录页选择私钥文件登录，再进入“设置 → 登录与 Passkey”，填写设备名并点击“添加 Passkey”，由用户完成 Windows Hello／手机确认。已有 Passkey 登录后也可以添加；仅用调试密码登录的会话需先改用私钥或 Passkey 登录。设置页显示已登记的 Passkey，不需要控制台绑定链接。

如需本机管理员恢复入口，仍可生成有效期十分钟的一次性绑定链接：

```powershell
.\.venv\Scripts\python.exe backend\manage.py passkey_setup --origin http://localhost:8001 --name "我的电脑"
```

在保存 Passkey 的设备打开输出链接并点击“绑定 Passkey”。绑定成功后链接失效，私钥由认证器持有。另一设备也可使用系统提供的同步／跨设备 Passkey。

绑定地址的主机名必须与之后的登录地址一致：`localhost` 和 `127.0.0.1` 的凭据不同。公开服务器需 HTTPS，并在启动前设置准确来源，例如：

```powershell
$env:BOOKMARK_PASSKEY_ORIGIN = 'https://bookmarks.example.com'
```

同步设置既有 `BOOKMARK_ALLOWED_HOSTS`、HTTPS／可信代理配置；配置后绑定命令的 `--origin` 也必须使用该来源。手机访问应使用可解析的 HTTPS 地址。

## 私钥文件

在自己的电脑生成新的 Ed25519 PKCS8 PEM 私钥文件；如果已经初始化用户，同时登记对应公钥，否则提示下一步运行 `initialize --private-key`：

```powershell
.\.venv\Scripts\python.exe backend\manage.py file_key_setup --generate '..\website-additional-login-key.pem' --name "我的私钥"
```

输出文件必须不存在且父目录已存在，命令不会覆盖旧私钥。私钥保存在项目外，登录页点击“私钥文件登录”并选择这个 PEM 文件；浏览器本地导入并签名，仅发送一次性挑战的签名。API 不接受上传私钥或客户端自报公钥来建立身份。

完成首次初始化后，添加另一把文件密钥时，可以在可信电脑生成密钥并向服务器登记公钥（PEM 或 OpenSSH Ed25519），私钥留在使用登录的设备：

```powershell
.\.venv\Scripts\python.exe backend\manage.py file_key_setup --public-key '..\login-public.pem' --name "我的私钥"
```

已存在的 OpenSSH 私钥文件不能直接用于浏览器导入；当前文件入口要求未加密的 PKCS8 PEM 格式。服务器使用 `cryptography` 校验 Ed25519 签名；Passkey 使用 Duo Labs `webauthn`，浏览器调用使用 `@simplewebauthn/browser`。

## 存储与恢复

公钥、设备名、签名计数、一次性绑定授权及挑战值位于运行库，不进入收藏库备份。收藏库覆盖恢复保留本机认证配置并撤销会话；重置数据随用户级联删除认证记录。`clear` 保留认证。自行生成的私钥文件属于用户选择保存的文件，重置数据不会冒然删除它。

挑战有三分钟有效期、绑定会话／来源／用途，并在校验签名前消耗；注册要求已有私钥／Passkey 登录会话，或一次性控制台授权。正常登录校验签名，Passkey 强制用户验证并校验 RP ID、origin 和签名计数。浏览器端操作保留 CSRF 校验。

实现依据：[Duo Labs py_webauthn](https://github.com/duo-labs/py_webauthn)、[SimpleWebAuthn 浏览器库](https://simplewebauthn.dev/docs/packages/browser)。
