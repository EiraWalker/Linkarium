# 软件密码登录方案调查

调查日期：2026-10-09。状态：已选 Django auth/session 与 django-axes，未集成。下面 Better Auth 比较保留为选型依据。

## 需求边界

本软件供本人多设备使用，Web 前端需要密码登录，后端支持本地或公开服务器部署。被收藏网站仅覆盖公开页面，不实现被收藏网站的登录逻辑。鉴权应覆盖收藏、文件夹、截图、刷新任务和导入导出资源。

最新确认：登录页面仅输入一个密码，不显示或要求账号、用户名、邮箱。首版仅联网，支持电脑和手机。下面候选的账户字段是内部实现能力，不是要求用户在登录页面填写的内容。

## 候选比较

| 候选 | 适合的后端 | 已核实的能力 | 需要补齐或注意 |
| --- | --- | --- | --- |
| Better Auth | TypeScript / Node | 密码登录、数据库会话、会话撤销、内置限流、禁注册、用户名插件 | 用户数据模型仍需要 email 字段；服务端 auth.api 调用不自动经过 HTTP 限流；需正确配置代理、来源与安全 cookie |
| Django 内置 auth | Python / Django | 原生用户名密码登录、密码哈希、会话、CSRF 和密码验证工具；用户名登录不需邮件服务 | 默认认证后端没有暴力破解限流；设备会话列表需产品封装；需正确配置安全 cookie 与代理 |

已根据用户委托选择 Python/Django 后端，因此采用 Django 内置 auth/session，加 django-axes 处理实际登录失败限流。轻量方案使用原生 Django JSON views，不使用 DRF。Better Auth 留作历史候选，不安装集成。完整技术选型见 technical-stack.md。[Axes 自定义认证](https://django-axes.readthedocs.io/en/stable/3_usage.html)

## Better Auth 的核查依据

默认密码哈希使用 scrypt；提供服务端会话、撤销能力和 CSRF/来源保护，HTTPS 下提供安全 cookie 默认设置。[官方安全文档](https://better-auth.com/docs/reference/security)

可关闭密码注册，内置限流应显式覆盖实际登录接口；开发环境默认不开启，直接服务端 auth.api 调用不自动受其保护。[配置选项](https://better-auth.com/docs/reference/options)、[限流边界](https://better-auth.com/docs/concepts/rate-limit)

用户名插件支持用户名密码登录，用户记录仍要求 email；不启用验证邮件或邮件找回流程时不需发送邮件。以内部 email 元数据实现只展示用户名的方案尚未实测。[用户名插件](https://better-auth.com/docs/plugins/username)

会话支持列出与撤销。首版建议不启用 cookieCache，避免撤销后其他设备在缓存有效期内继续使用缓存会话。[会话文档](https://better-auth.com/docs/concepts/session-management)

## Django 的核查依据

内置 auth 支持用户名密码；email 可为空，默认采用 PBKDF2-SHA256，并提供其他密码哈希器。创建用户工具应显式调用密码校验器，create_user 本身不会自动验证密码强度。[用户 API](https://docs.djangoproject.com/en/5.2/ref/contrib/auth/)、[密码哈希与验证](https://docs.djangoproject.com/en/5.2/topics/auth/passwords/)

ModelBackend 明确不自带暴力破解限流，需在实际登录入口配置；前后端会话交互应保留 CSRF 中间件与校验。[认证后端边界](https://docs.djangoproject.com/en/5.2/topics/auth/customizing/)、[CSRF 文档](https://docs.djangoproject.com/en/5.2/ref/csrf/)

## 已纳入技术方案的实现形态（待实施验证）

1. 库内预置单一所有者身份，登录页面只发送密码，服务器补固定身份完成认证，不接受客户端选择其他身份。禁止公开注册。在部署机器通过受控的一次性初始化工具创建内部身份，交互输入密码，不将密码写入命令行参数、代码或日志。
2. 采用库提供的密码哈希与服务端会话，浏览器使用 HttpOnly 会话 cookie。通过同一入口代理前端、/api 和受保护的截图资源，代码和职责仍保持前后端分离。公网及跨设备网络访问使用 HTTPS。
3. 后端每个收藏、任务、导入导出接口与截图访问入口都检查有效会话及所有者身份。截图工作进程使用后端内部权限，不保存用户明文密码。
4. 配置实际登录入口的限流与可信代理，保留 CSRF/来源检查。代理覆写客户端可以伪造的转发头。
5. 普通退出撤销当前会话，修改或重置密码后撤销其他或全部会话。无邮件服务时，候选恢复方式是部署机器上的受控密码重置工具。

上述已纳入当前技术方案；其中 owner 是库内部身份，不改变用户仅输入密码的界面。前后端分离不要求浏览器跨域访问；选择同源入口减少 cookie 与来源配置复杂度。尚未实际集成或部署验证。

## 单密码页面的具体适配边界

没有将两库描述为原生 password-only 成品。建议前端仅 POST password，后端取固定 owner 身份并使用库的完整登录流程，保留密码哈希、会话与撤销机制；不手工拼会话 cookie。

Better Auth 可通过 signInEmail 的 asResponse 或 returnHeaders 返回完整响应/cookie，服务器填入内部 email。自定义入口必须正确传递每个 Set-Cookie，不能把多个 cookie 合并；同时在入口补充 CSRF/来源检查和限流，不能因库的 HTTP 路由自带保护就假设 auth.api 调用也覆盖这些保护。[官方 API](https://better-auth.com/docs/concepts/api)、[服务端登录示例](https://better-auth.com/docs/basic-usage)、[限流边界](https://better-auth.com/docs/concepts/rate-limit)

核查 v1.7.7 源码后，确认没有 ctx.request 的服务端路径会跳过相关 Origin/CSRF 中间件；仅传 headers 不足以认定已覆盖。因此这一适配路径的 CSRF、限流与 cookie 传递都需要实施时验收。[Origin/CSRF 源码](https://github.com/better-auth/better-auth/blob/v1.7.7/packages/better-auth/src/api/middlewares/origin-check.ts)、[dispatch 源码](https://github.com/better-auth/better-auth/blob/v1.7.7/packages/better-auth/src/api/dispatch.ts)

Django 可在原 POST 视图中以固定 username 调用 authenticate(request, ...) 和 login(request, user)，保持 SessionMiddleware 与 CSRF 中间件，无需手工转发另一套登录服务的 cookie；仍需补限流。[认证 API](https://docs.djangoproject.com/en/5.2/topics/auth/default/)、[CSRF](https://docs.djangoproject.com/en/5.2/ref/csrf/)

选型结果：采用 Python/Django 的固定 owner 适配，通过部署终端初始化与重置密码；保留 CSRF、会话机制并配 django-axes。Better Auth 路径只是未采用候选的边界记录；未验证真实登录。

## 维护与安全修复核查

Better Auth 1.7.7 发布记录包含 Magic Link/OAuth 相关账户接管修复，以及 Drizzle adapter 的 PostgreSQL 数据库限流并发修复。即便只用密码登录，实施时也需核对主库和所用 adapter 的修复版本。[1.7.7 release](https://github.com/better-auth/better-auth/releases/tag/v1.7.7)、[限流公告](https://github.com/better-auth/better-auth/security/advisories/GHSA-44jh-23m7-hpcf)

Django 2026-10-06 发布 6.1.2、6.0.9、5.2.18 安全更新。若选择 5.2 系列，应采用包含该次修复的版本，并在实施时重新核查最新补丁。[官方安全发布](https://www.djangoproject.com/weblog/2026/oct/06/security-releases/)

没有依据 star 数量认定库安全。核查覆盖 Context7、官方文档、release 和安全公告，尚未完成集成、源码审计、真实登录测试或部署验证。

开源项目：[better-auth/better-auth](https://github.com/better-auth/better-auth)、[django/django](https://github.com/django/django)。
