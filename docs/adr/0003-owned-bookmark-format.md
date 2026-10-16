---
status: accepted
---

# 维护自有收藏格式并提供浏览器导入

2026-10-09，用户确定由本软件维护自有收藏格式，同时提供从 Firefox 和 Chrome 导入书签的方法。自有格式用于保存浏览器书签不能完整表达的软件配置，例如每个网站独立的刷新间隔；不只是为了导出备份。收藏还需要表达多级文件夹、单一文件夹归属、标签和备注。

后续已选择 SQLite 保存元数据，HTML 文件保存快照；重复导入跳过且不覆盖已有记录，已确认。具体 schema 属于实施设计。用户已进一步确认首版整库备份与覆盖恢复；软件调用 AI API 生成文件夹和分类方案，经人工校对确认后执行。AI 配置、目录规则和浏览器原目录关系继续明确，见 browser-import.md、ai-classification.md、backup-restore.md 与 pending-decisions.md。
