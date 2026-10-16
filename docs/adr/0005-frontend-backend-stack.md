---
status: accepted
---

# 首版采用轻量 Preact 与 Django 技术栈

后续变更：2026-10-15，[ADR-0009](0009-preview-document-retention.md) 替换本文的按可见区域挂载与数量上限卸载策略，采用当前分页完整加载和跨导航保留。其余归档与安全约定继续适用。

后续细化：用户授权完整技术规划后，单 SQLite 文件部分由 [ADR-0007](0007-runtime-and-library-state.md) 调整为两份本地 SQLite，以隔离运行身份与可覆盖恢复的业务库。下文保留初始选型背景；完整当前方案见 [technical-plan.md](../technical-plan.md)。

2026-10-09，用户委托技术选型，并要求尽可能轻量。最终选择 Preact、TypeScript、Vite SPA 和标准 CSS/CSS Modules 作为前端；选择 Python 3.13、Django 5.2 LTS 原生 JSON views、Django auth/session 与 django-axes 作为后端和认证；一个 SQLite 保存收藏、配置、会话与刷新任务，Django management command 执行低并发任务，HTML 文件保存 Monolith 快照。生产部署选择 Waitress 与 WhiteNoise，不需要常驻前端 Node 服务或独立数据库、消息服务。

单密码界面由后端补固定 owner 身份，使用 Django 完整认证与会话流程。首屏网格采用分页与可见区域按需挂载快照，不引入独立状态库、UI 框架或虚拟列表库。用户明确首版需要动态网页支持，并分别保存桌面/手机快照；保留 Playwright Python 与 Chromium，默认先浏览器渲染再由 Monolith 归档，低并发分别处理两种布局。按批次启动和复用一个浏览器，空闲释放。详细职责与验收边界见 ../technical-stack.md。

轻量化指减少依赖、独立服务和常驻资源，未做包体积/内存比较。SQLite 写竞争、应用任务恢复、Windows worker、单密码登录和 HTML 首屏保真均需实际验证。此决定确定技术方案，不表示已经实现或测试通过。
