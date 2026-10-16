# 前端

Preact 11.0.1、TypeScript 7.0.2、Vite 8.3.4 和 CSS Modules。生产端只需要 `dist` 的静态文件，不需要 Node 常驻服务。运行时依赖只有 Preact。

在此目录执行：

```powershell
npm ci --registry=https://registry.npmjs.org --cache=.npm-cache
npm run dev
npm run typecheck
npm run build
```

开发服务器将 `/api` 同源代理到 `http://127.0.0.1:8000`。生产由 Django 提供已知前端路由的 `dist/index.html`，WhiteNoise 提供 `/assets/`。API 密钥和密码不进入 Vite 环境变量或构建文件。

界面包含密码登录、嵌套目录和标签维护、六字段部分搜索、独立星标、同页详情、批量移动与标签整理、桌面/手机 HTML 快照与历史清理、后台刷新进度与任务重试/取消、AI 配置和分类校对、浏览器导入、整库备份及覆盖恢复。所有页面连接真实 `/api/v1`，没有模拟网站列表。

卡片、详情及历史默认加载缓存的首屏 JPEG 图片，桌面与手机使用各自快照；点击“查看完整快照”才打开受限 HTML 归档。当前分页全部预览排队加载（桌面并发 2、手机 1），同版本的卡片与详情共享下载，在当前登录会话中跨滚动、翻页、目录和应用内导航保留。退出、会话失效或更换收藏库会清理图片及对象 URL；删除收藏或历史时释放对应缓存。尚未补图的旧归档按 `Retry-After` 等待图片就绪，不自动下载大型 HTML。应用只持久保存不含库内容的维护操作关联信息，以便恢复断连后重新查询同一操作。

2026-10-09 在独立 `output/acceptance-data` 和真实 Chromium 中验证了：密码登录；文件夹/标签创建；添加关闭快照的网站；星标不导航；备注/标签/间隔编辑及部分搜索；真实 Monolith HTML 预览；桌面和手机版本切换；最多 8/4 个 iframe 且离屏回收；320px 双列无横向溢出；浏览器 HTML 导入跳过重复与无效项；鉴权 ZIP 备份下载；覆盖恢复、恢复后撤销会话并重新登录、原操作续查、恢复前自动备份下载、恢复备份时的原星标；使用本机受控 Chat Completions 服务进行配置保存/连接验证、重新分类、人工修改多级路径、确认后创建目录并移动网站。

上述 2026-10-09 的 iframe 数量及离屏回收为历史验收规则，后由会话内保留和图片预览替代。当前验证运行 `D:/website/.venv/Scripts/python.exe -X utf8 frontend/scripts/thumbnail-preview-acceptance.py`，使用构建产物、隔离浏览器和 API 夹具，不操作真实收藏库。报告写入 `D:/website/output/playwright/thumbnail-preview/report.json`。

验证范围与限制见[实现与验收记录](../docs/implementation-status.md)。浏览器设备模拟已经验证，实际手机硬件上的浏览器、内存表现及真实 AI 服务商仍需相应环境验证。个人库的运行截图与诊断报告保留在本机，不随源码发布。
