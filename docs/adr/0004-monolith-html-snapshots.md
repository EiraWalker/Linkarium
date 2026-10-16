---
status: accepted
---

# 使用 Monolith 保存 HTML 网页快照

后续变更：2026-10-15，[ADR-0009](0009-preview-document-retention.md) 替换本文的按可见区域挂载与数量上限卸载策略，采用当前分页完整加载和跨导航保留。其余归档与安全约定继续适用。

2026-10-09，用户指定使用 y2z/monolith 保存网页快照，替代此前 PNG 截图方案，并允许网站不生成快照。首屏是前端预览范围，保存内容为网页快照；默认每周更新、单站独立间隔、首次生成、停机补刷和文件夹批量刷新规则继续有效。刷新文件夹包含所有子文件夹；后台展示进度，失败保留上次成功快照。用户进一步明确关闭快照功能时保留已有快照，并可查看快照历史；成功更新应追加历史记录和文件引用，不覆盖旧版本。停用时不生成新版本，已有快照仍可预览。历史默认全部保留并可手动清理，删除收藏或文件夹时相关历史一起删除。

Monolith 不执行 JavaScript，官方支持浏览器预先渲染 DOM 后通过 stdin 归档。用户进一步明确首版必须支持动态页面，目标是与直接在浏览器看到的页面一致。因此所有启用快照的网站默认经 Playwright/Chromium 渲染与状态预处理，再由 Monolith CLI 生成 HTML。该组合尚未集成验收，不能将 HTML 序列化当作任意网站视觉完全一致的证明。[Monolith 官方说明](https://github.com/Y2Z/monolith)、[Playwright DOM 获取](https://playwright.dev/docs/api/class-page#page-content)

浏览器按批次启动和复用、空闲退出，低并发抓取以满足轻量目标。用户明确每个启用快照的网站分别保存桌面版与手机版快照，前端按设备选择对应版本；两份分别记录成功时间和失败状态。页面内部 iframe 不能无条件删除，否则会破坏首屏内容；canvas、视频、shadow DOM 和脚本生成样式等状态的保存需专门验证。

预览在限制脚本与来源权限的 iframe 中展示快照首屏，限制同时挂载数量；快照不作为应用同源可信脚本运行。具体样式保真、手机内存与流畅度需真实网页验证。[iframe 文档](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe)
