# 平台方案比较

调查日期：2026-10-09。状态：已选轻量 Preact/Django 技术栈，Monolith HTML 快照，Playwright/Chromium 默认动态渲染。下方最初截图比较为历史调查；当前方案见 technical-stack.md。

## 比较

| 方案 | 截图执行位置 | 优点 | 代价与限制 | 适用条件 |
| --- | --- | --- | --- | --- |
| Web 界面与服务器截图 | 服务器的浏览器环境 | 无需安装，多设备访问；个人电脑关闭时仍可刷新 | 需运行截图服务、维护存储；登录态需在服务器单独建立；截图效果受服务器网络影响 | 跨设备访问、后台持续更新优先 |
| 桌面软件与本地截图 | 本机的浏览器环境 | 数据可保存在本地，便于人工登录；断网仍能浏览已保存截图 | 使用本机资源；程序或电脑关闭时不能刷新；跨设备另需同步 | 个人 Windows 使用、本地数据和登录页面优先 |
| Web 界面与本地助手，可选同步 | 本机助手 | 可保留 Web 界面，同时让截图与登录态留在本机 | 需维护助手通信与同步；本地助手停止后无法继续本地抓取 | 本地登录截图与跨设备访问均为必要需求 |

表中的适用性和维护成本是工程判断，未做真实收藏库的批量性能测试。数据保存位置和截图执行位置可以分别设计，桌面形态本身不等于必须本地保存，Web 形态本身也不等于必须公开数据。

## 已核实的技术事实

- 普通网页前端受同源策略限制，不能任意读取跨站页面内容来截图；一些网站也限制被其他页面嵌入。Web 管理界面需要另设截图执行环境。[MDN 同源策略](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Same-origin_policy)
- Playwright 支持页面截图和全页截图。[Playwright Screenshots](https://playwright.dev/docs/screenshots)
- Playwright 支持保存和复用网站登录状态，但不等于直接继承日常浏览器登录。[Playwright Authentication](https://playwright.dev/docs/auth)
- Playwright 不支持直接自动化默认 Chrome 用户配置目录；应使用独立截图浏览器配置。[Playwright launchPersistentContext](https://playwright.dev/docs/api/class-browsertype#browser-type-launch-persistent-context)
- Electron 提供网页可见区域截图能力；这不能直接视作全页截图实现。[Electron capturePage](https://www.electronjs.org/docs/latest/api/web-contents#contentscapturepagerect-opts)

上述早期调查通过 Context7 的 resolve/query 流程和官方网页核查；当时尚未选择技术栈。当前技术选型已确定，见下文。

## 早期评估问题（历史记录，已回答）

使用设备、登录页面范围、书签数量、更新要求和截图范围会改变平台取舍。若主要在个人 Windows 电脑使用，优先评估本地桌面；若多设备与电脑关闭后仍刷新是硬要求，优先评估服务器截图；两组要求均为硬需求时再考虑混合方案。

## 早期方向（已由下节的需求进一步明确）

用户随后明确跨设备支持、前后端分离，并要求同时支持定时刷新与批量立即刷新。基于这一方向，建议优先评估浏览器前端、统一后端与后台截图任务，但该建议尚未成为完整平台选型决定。前后端分离本身不限定浏览器或桌面前端，不确定部署在云端、家用设备还是其他常驻环境；设备范围、登录页面需求和规模确定后再比较具体部署与技术栈。

## 当前已确认需求对方案的约束

用户进一步明确：使用者为本人，支持电脑和手机，首版仅联网使用；Web 前端仅输入密码登录，后端支持本地或公开服务器部署，只抓取公开页面，首版规模为一千个网站以内。网页保存为 Monolith HTML 快照，首屏由前端展示；默认每周更新，各网站独立间隔，按文件夹及子文件夹批量刷新，失败保留旧成功快照。网站可关闭快照，或仅暂停自动刷新。

确定 Preact/Vite 静态前端、Django 原生 JSON API、SQLite、自有低并发刷新 worker；Waitress/WhiteNoise 提供 Web 及前端静态资源。用户明确需要动态网页，保留 Playwright/Chromium 按批次启动渲染，再调用 Monolith，不让浏览器空闲常驻。本地部署时其他设备需能连接该后端；前后端代码分离、生产可共用入口。详细职责和保真边界见 technical-stack.md。
