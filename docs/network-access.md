# 局域网访问与本地 HTTPS

Windows 标准启动入口支持以下两种模式。网络适配器参数可省略：默认选择已连接、有有效 IPv4 的物理 Wi-Fi，没有 Wi-Fi 时选择物理以太网。自动选择排除代理、VPN 和虚拟机网卡；多候选或多 IPv4 需要明确选择，显式指定的网卡不可用时直接报错。

| 模式 | 启动选项 | 设置页入口 | 新设备 Passkey |
| --- | --- | --- | --- |
| 普通 HTTP | 默认 | 所选适配器 IPv4＋实际端口的访问链接、复制按钮和二维码 | 不提供；本机继续通过 localhost 登录 |
| 局域网 HTTPS | `-LanHttps` | 固定 HTTPS 地址与设备授权二维码 | 经可信渠道安装并核验 CA 后可登记和登录 |

## 普通启动

```powershell
.\scripts\start.ps1 -DevelopmentCapture
.\scripts\start.ps1 -DevelopmentCapture -NetworkAdapter 'Wi-Fi' -Port 8001
```

可以用 `Get-NetAdapter` 查看网卡名称和索引；`-NetworkAdapter` 接受名称或索引。默认端口从 8000 寻找同时可在 loopback 和所选 IPv4 绑定的空闲端口，显式 `-Port` 保持固定。设置页使用实际端口，不根据浏览器当前 Host 拼接服务器地址。

普通模式的二维码是网站访问入口，不含授权令牌或授权码。私网 HTTP 不支持 Passkey 或浏览器私钥签名；本机认证继续使用终端显示的 localhost 地址。“在本机添加 Passkey”保持原能力，“添加其他设备”的授权操作仅在 HTTPS 运行模式提供。

## 启用局域网 HTTPS

首次准备可在安装时添加 `-LanHttps`，或只准备 HTTPS 运行时：

```powershell
.\scripts\install-https.ps1
.\scripts\start.ps1 -DevelopmentCapture -LanHttps
```

安装器下载官方 Caddy 2.11.7 Windows amd64 ZIP，核对固定 SHA-256 后解压到项目的 `tools/caddy`。也可通过 `BOOKMARK_CADDY` 指定已有兼容 Caddy 程序。安装器不创建系统服务、不安装全局受信任 CA。

首次没有保存配置时，默认 HTTPS 端口为 8443，服务器本机证书页为 `127.0.0.1:8081`，域名为 `linkarium.<IPv4 的连字符形式>.sslip.io`，解析到所选网卡 IP。依赖正常域名解析；DNS 重绑定防护或本机代理的域名路由规则可能影响私网解析，可改用自己已能正确解析的局域网域名。

```powershell
.\scripts\start.ps1 -LanHttps -NetworkAdapter 'Wi-Fi' -HttpsPort 8443 -CertificatePort 8081
.\scripts\start.ps1 -LanHttps -LanDomain 'linkarium.home.example'
```

`-LanDomain` 只配置应用身份和证书，不替用户创建 DNS 记录。HTTPS、证书安装页和 Web 上游端口必须不同。保存过配置后，省略参数即复用原域名和端口。已有的 `-PasskeyOrigin` 必须与最终 HTTPS 来源一致。

服务就绪后，终端显示 HTTPS 地址、公开 CA 文件的绝对路径、SHA-256 指纹和服务器本机证书页。证书页只绑定 `127.0.0.1`，局域网不能访问。HTTPS 代理绑定所选 IPv4，Web 上游绑定 loopback；同一 supervisor 管理它们和单个采集 worker。就绪检查通过实际域名验证 TLS 与应用响应，不会降级为 HTTP。

## 防火墙

服务已启动后，在管理员 PowerShell 执行：

```powershell
.\scripts\configure-network-firewall.ps1
# 独立数据目录时：
.\scripts\configure-network-firewall.ps1 -DataRoot 'E:\LinkariumData'
```

脚本从活动实例读取实际端口，检查服务器仍在运行及适配器 IP 未变化，只允许所选适配器、当前 IPv4 和 `LocalSubnet` 的 TCP 入站。普通模式放行 Web 端口，HTTPS 模式仅放行 HTTPS 端口；不开放本机证书页和上游。当前 Wi-Fi 即使标记为“公用网络”也无需改变整个网络类别。

脚本不自动提权、不关闭防火墙，也不声明路由器客户端隔离已通过。切换端口或网卡后，检查并移除不再需要的旧 `Linkarium-LAN-*` 规则。

## 手机安装与添加设备

电脑首次使用同一 HTTPS 域名时，也需要信任这份公开 CA。在服务器本机核对终端显示的 SHA-256 指纹后，可将 CA 加入当前 Windows 用户的受信任根证书库：

```powershell
certutil -user -addstore Root .\data\runtime\lan-https\public\linkarium-ca.crt
```

使用独立数据目录时替换证书路径。该命令会修改当前用户的证书信任，启动脚本不会自动执行；Android 仍需在手机上单独安装。

1. 从服务器本机取得公开证书文件，通过实物传输或受管理的设备配置交给手机。使用服务器屏幕或另一条已认证渠道提供 SHA-256 指纹，不能把 HTTP 下载页及同页指纹当作独立验证。
2. 手机核对证书指纹后，在 Android 设置中搜索“安装证书”，选择“CA 证书”并安装。根证书影响设备的 HTTPS 信任；具体系统入口见 [Android 证书管理](https://support.google.com/pixelphone/answer/2844832?hl=zh-Hans)。
3. 电脑和手机使用同一 HTTPS 域名。首次登记时打开服务器终端提供的完整注册链接并输入独立授权码；已有所有者及原凭据保留。
4. 已登录电脑在设置页生成新设备授权。手机扫描添加链接二维码，另行输入独立授权码并创建 Passkey。新设备授权继续五分钟有效、一次成功使用后失效。

服务器本机证书页只提供公开证书和说明；不提供授权码、令牌、私钥或数据库。设置页不再提供局域网 HTTP 安装入口。

## 复用、接管与地址变化

域名、IPv4、端口及 CA 指纹保存在数据目录的 `runtime/lan-https/connection.json`。CA 与私钥保留在同一持久运行目录，公开副本位于 `runtime/lan-https/public/linkarium-ca.crt`。它们不是收藏库内容，不进入普通收藏库导出或源码版本库；服务端叶证书在相同 CA 下续签。

接管之前独立脚本准备的配置：

```powershell
.\scripts\start.ps1 -LanHttps -ImportLanHttps 'E:\ExistingLinkariumHttps'
```

源目录需要 `config.json` 中的 `Address`、`Domain`、`HttpsPort`、`CertificatePort`，以及 `state/caddy/pki/authorities/local` 中完整的 root 和 intermediate 证书／私钥。接管只复制 CA 资料，保留源目录；目标已有不同 CA 时拒绝覆盖，不操作收藏库和凭据。

建议在路由器为服务器保留 IPv4。IP 与保存配置不符、CA 缺失或指纹不符时启动明确失败；先修正网络或恢复匹配配置和 CA，应用不会悄悄更换身份。主动变更域名后需要重新登记该域名的 Passkey；localhost 的凭据不能自动转换成另一个域名的凭据。

## 验收范围

自动化入口为 `tests/network_access_ui.py`，通过隔离代码／数据、真实 supervisor 和 HTTPS 服务、二维码解码及独立浏览器的 WebAuthn 虚拟认证器验证。`NETWORK_UI_DIST` 可指向隔离的前端构建，`NETWORK_UI_CADDY` 可指定验证使用的 Caddy。

数据保留场景在同一隔离目录依次运行 HTTP、HTTPS、HTTPS 重启和 HTTP，比较非空收藏、文件夹、标签、备注、快照文件哈希、所有者、原 Passkey 和文件公钥，并再次用原 localhost Passkey 登录。

浏览器测试只对已校验证书的公钥作临时信任，不修改系统证书库。虚拟认证器和手机视口不能替代 Android 的系统 CA 安装、真实认证器和 Wi-Fi 实机验证；相关证据需单独记录。自动网卡选择和上述脚本当前支持 Windows，既有 Linux／容器的显式绑定与外部 HTTPS 代理配置继续按原部署方式运行。
