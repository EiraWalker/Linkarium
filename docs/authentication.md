# 登录与 Passkey

Linkarium 继续使用单一所有者。当前 Passkey 仅支持并验证了 Windows 上的 Passkey，以及 Android Chrome 中的 Google 密码管理工具；其他系统、浏览器、密码管理器和独立安全密钥尚未列入支持范围。兼容性与手机操作步骤见 [Passkey Wiki](https://github.com/EiraWalker/Linkarium/wiki/Passkey-Compatibility)。私钥文件登录可选；密码仅在显式 `--debug` 模式下可用。

## 首次安装与初始化

Windows 在项目根目录执行：

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1 -DevelopmentCapture
```

安装只准备运行环境和数据库。当前网站尚无 Passkey 时，每次启动就绪后，服务器终端显示网站地址和新的一次性登记码；打开该地址，在网页输入码、命名并创建首个 Passkey。码在本次运行期间不会因时间到期，重启重新签发并使旧码失效。注册成功后才创建所有者并直接登录；取消、认证器失败或断网不会留下没有凭据的新账号。

本机开发使用 `http://localhost:实际端口`。未设置规范来源时，默认启动命令会寻找空闲端口，指定 `-Port 8001` 可以固定；设置了 `BOOKMARK_PASSKEY_ORIGIN` 后保持指定的后端端口，以免改变代理的 upstream。监听地址仍可为 `127.0.0.1`，但该 IP 不能作为 Passkey 的 RP ID。已登记的 Passkey 与域名绑定，后续登录应使用同一个地址。

首次登记码的明文不保存在数据库。启动或维护恢复开放 Web 入口前，旧的首次登记码先失效；服务就绪后生成并输出新码，并排除历史登记码的数值。已登记当前网站的 Passkey 后，启动不再签发首次登记码，直接使用已有凭据登录。运行期间遗失或输错五次锁定后，可在另一个服务器终端重新生成，旧码随即失效，服务不需要停止：

```powershell
.\.venv\Scripts\python.exe backend\manage.py passkey_initialize --origin http://localhost:8001
```

若配置了 `BOOKMARK_PASSKEY_ORIGIN`，这些授权命令可省略 `--origin`；显式参数必须与配置一致。授权命令只签发授权，不运行迁移。

已有所有者但当前网站尚无 Passkey 时，登录页直接显示“创建第一个 Passkey”。同一个 `passkey_initialize` 命令会签发成功使用前不过期的首个 Passkey 授权码，用户直接在登录页输入，无须特殊链接。登记保留原所有者、文件密钥和收藏数据；不会重新初始化账号。配置首个 Passkey 后，该码不能继续添加凭据。已有 Passkey 的恢复使用终端重置。

授权码长期有效不延长单次浏览器注册请求：认证窗口的 challenge 仍有三分钟期限，超时后可使用同一个未消费的授权码重新开始。其他设备的添加授权仍为五分钟，终端重置仍为十分钟。

## 日常登录与凭据管理

登录后保持十四天，关闭浏览器后仍可使用；期限自登录时固定计算，普通浏览不会延长。主动退出立即结束当前会话。设置 → 登录与 Passkey 提供：

- 在本机添加：已用 Passkey 或文件密钥登录，直接创建新 Passkey，无须授权码或额外复验；保持原会话及原截止时间。
- 添加其他设备：生成链接、二维码、独立八位授权码和五分钟截止时间。手机扫码；新电脑复制链接到浏览器。新设备输入另外提供的授权码，创建 Passkey 后直接登录。
- 重命名与删除：显示名称、创建时间和最后使用时间；删除某把 Passkey 后，由它登录的所有会话在下一次受保护请求时失效。删除当前凭据会退出当前页面。网页不能删除最后一种可用登录凭据。

二维码在浏览器本地生成，内容只有链接，不含授权码。每条授权自签发起五分钟内只能成功注册一次；过期后重新生成。刷新或验证授权码不会延长期限；错误输入五次后不可再使用。不提供待用授权列表、手动撤回或状态查询，重新生成不撤销其他尚未到期的链接。

标准 Windows 启动的普通 HTTP 模式在此显示“网站访问”：链接和二维码使用所选网卡 IP 与实际端口，不生成新设备授权。启用 `-LanHttps` 后，电脑手机统一使用规范 HTTPS 地址，添加链接和二维码才用于新设备 Passkey 登记；“安装 CA 证书”按钮位于生成授权按钮右侧，并打开独立的公开证书安装页。证书准备、重启复用及地址变化处理见[局域网访问](network-access.md)。

Passkey 按凭据管理，一条记录可能由系统同步到多台设备。删除该凭据会影响所有持有它的设备；网站不能远程删除密码管理器中的条目或本地私钥文件。

链接的片段信息进入页面后会从地址栏移除，并暂存在当前标签页的 `sessionStorage`，刷新后继续显示配置表单；不写入 `localStorage`，不保存输入的授权码。完成、明确失效或恢复时发现到期后清除，刷新不延长服务器授权期限。浏览器禁用会话存储时保留链接片段以支持刷新。若注册已完成但成功响应丢失，页面检查会话后提示继续，或使用刚创建的 Passkey 正常登录；不会重复创建凭据。

## 服务器终端添加与重置

只添加 Passkey、保留现有凭据时，运行：

```powershell
.\.venv\Scripts\python.exe backend\manage.py passkey_setup --origin http://localhost:8001 --name "我的电脑"
```

命令输出五分钟有效的一次性链接和独立授权码。打开链接的设备输入码并完成注册；可使用电脑或手机，无须已有登录会话。

所有登录方式丢失时，在服务器终端运行：

```powershell
.\.venv\Scripts\python.exe backend\manage.py passkey_reset --origin http://localhost:8001
```

输出十分钟有效的重置链接与独立授权码。新 Passkey 成功登记后，全部旧 Passkey、文件密钥登录凭据、登录会话及旧授权失效，重置设备直接以新凭据登录。收藏、文件夹、标签、备注和页面快照保留。仅签发重置入口、取消或注册失败均不提前撤销旧凭据。旧私钥文件和密码管理器条目可能仍存在，但已不能登录此网站。

## 电脑与手机的统一 HTTPS 地址

跨网络使用时，在服务器启动环境设置唯一来源：

```powershell
$env:BOOKMARK_PASSKEY_ORIGIN = 'https://bookmarks.example.com'
$env:BOOKMARK_ALLOWED_HOSTS = 'bookmarks.example.com,localhost,127.0.0.1'
$env:BOOKMARK_HTTPS = '1'
$env:BOOKMARK_CSRF_ORIGINS = 'https://bookmarks.example.com'
```

HTTPS 代理还需配置 `BOOKMARK_TRUST_PROXY` 和实际 TCP 来源 `BOOKMARK_PROXY_PEER`，详见[部署说明](../deploy/README.md)。手机和电脑统一访问此 HTTPS 域名；手机扫描 `localhost` 链接会指向手机本身，不能用于另一台机器。本机开发也可用 `scripts/start.ps1 -PasskeyOrigin` 传入已经准备好的规范来源。

授权链接只由配置来源生成，不使用未校验的 Host 或转发头。`--origin` 不接受 IP 作为 RP ID，也不接受与规范配置不一致的地址。生产来源使用 HTTPS；本机开发只允许 HTTP localhost。

## Android 密码管理器兼容性

当前 Android 支持范围为 Chrome ＋ Google 密码管理工具。2026-10-18 已在 Android 15、Chrome 155.0.8059.79 真机完成新 Passkey 注册、退出及再次登录，并确认新会话使用刚创建的凭据。

网站通过标准 WebAuthn 接口调用 Android 选择的通行密钥服务，未硬编码 Google 提供程序；这不代表其他提供程序已经通过兼容性验收。注册和登录选项会携带已保存凭据的实际 `transports`；没有记录连接方式的旧凭据仍省略该提示。

2026-10-18 的真机排查发现，小米 FIDO `220250317` 不认识某些新增的连接方式，会在序列化凭据时触发空指针异常。保留已知的 `internal` 等提示可避免本次请求中因缺省连接方式扩展而出现的异常。

该设备仍存在另一项提供程序返回格式问题：小米完成指纹与本地凭据创建后，Chrome 155 报缺少 `publicKeyAlgorithm`，Firefox 158 报缺少 `transports`。这时凭据尚未交回网页，网站也未完成登记；网页脚本无法补充尚未收到的响应。需要继续验证厂商组件或浏览器的兼容修复，不能将系统指纹成功视为网站登记成功。

## 可选私钥文件

全新安装若希望先使用文件密钥，可以生成项目外的未加密 Ed25519 PKCS8 PEM，再显式初始化：

```powershell
uv sync --frozen --python 3.13
.\.venv\Scripts\python.exe backend\manage.py file_key_setup --generate '..\website-login-key.pem'
.\scripts\setup.ps1 -PrivateKey '..\website-login-key.pem'
```

生成命令不会覆盖同名文件。初始化只登记公钥，私钥留在用户保存的位置。登录页选择该 PEM，浏览器本地签名，仅发送挑战签名。无需同时准备 Passkey 和私钥才能开始使用。

已有所有者可在服务器登记另一把文件密钥公钥：

```powershell
.\.venv\Scripts\python.exe backend\manage.py file_key_setup --public-key '..\login-public.pem' --name "我的私钥"
```

公钥接受 PEM 或 OpenSSH Ed25519；浏览器私钥入口要求未加密 PKCS8 PEM，不直接接受 OpenSSH 私钥。已配置的文件密钥升级后仍可登录并添加 Passkey。

## 数据准备、升级和调试

`initialize` 不再要求私钥；它在持有实例锁时离线准备或迁移两个数据库，不自动创建所有者。全新目录可由 supervisor 自动准备；若首次准备中断，同目录的准备标记允许继续。已有目录存在未执行迁移或缺少可信准备标记的半成品，启动会拒绝自动修改，要求按[维护升级步骤](../deploy/README.md#认证版本升级)处理。

升级保留所有者、Passkey、公钥、签名计数及收藏数据；旧格式登录会话需要重新登录一次。运行中的 supervisor 持有实例锁，`initialize` 和直接执行的 `manage.py migrate` 都取得同一把锁，不会在该实例运行时迁移。日常升级使用 `initialize`，同时处理两个库和运行单例。

调试密码配置和启动均须显式启用：

```powershell
.\scripts\setup.ps1 -DebugAuth
.\scripts\start.ps1 -DevelopmentCapture -DebugAuth
```

离线修改调试密码使用 `manage.py initialize --reset-password --debug`；`--no-password` 跳过交互设置。调试密码会话不能授权本机添加 Passkey 或生成新设备链接，须先用 Passkey／文件密钥登录。切回正常模式后，调试密码会话不能继续访问。

认证记录和会话位于运行状态库，不进入收藏库备份。收藏库覆盖恢复保留本机认证配置并撤销会话；清空收藏保留认证。完整数据重置与此处的 Passkey 终端重置是不同操作。

协议验证使用 [Duo Labs py_webauthn](https://github.com/duo-labs/py_webauthn) 与 [SimpleWebAuthn 浏览器库](https://simplewebauthn.dev/docs/packages/browser)。真实 Windows Hello、实体手机及另一网络的 HTTPS 访问须分别实机验收，虚拟认证器和手机视口不能代替这些结果。
