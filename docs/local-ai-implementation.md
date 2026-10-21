# 本机 AI 分类实施与验收

2026-10-21；对应 [Issue #9](https://github.com/EiraWalker/Linkarium/issues/9) 和 [规格](local-ai-classification-spec.md)。代码基线为 c4eb9531e1166603e7e723d04edee24c4435a4c7，Firefox 插件版本 0.2.0、本机协议 1。

## 实施范围

云端生成完整分类／共享规划 prompt，Firefox 调用该设备配置的 OpenAI 兼容 API。所有分类入口采用发起设备绑定；服务器模型入口失败关闭，历史凭据保留。设备 proof 只在 runtime 留摘要，供应商密钥只在插件 storage.local。网页新增、选中／文件夹／全库分类和 HTML 导入保留人工校对；仅工具栏新建收藏自动归档，重复网址保留原记录。

Job 的阶段请求保存固定输入、prompt 摘要和收据。完整本机结果先持久化，回传幂等且事务提交。租约过期需原设备重新授权，输入未变时允许缓存重传；取消、替代、编辑和恢复切库阻止旧结果。共享规划复用持久检查点，采集与本机推理共享材料版本保护。API／会话中断暂停；缓存不可写时明确停止自动重试，原设备恢复前不重新推理。

library 0006 和 runtime 0006 保留历史数据并隔离设备身份，旧无主模型任务标为需重新发起。旧备份恢复注册新迁移，兼容性测试独立构造最新实际 schema。协议与配置说明见 [API 契约](api-contract.md#7-发起设备与本机分类协议协议-1)、[ADR-0012](adr/0012-device-owned-ai-execution.md) 和 [插件使用说明](../extensions/firefox/README.md)。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| `scripts/verify.ps1 -SkipCapture` | 301 项：295 通过、6 跳过；Django 检查、迁移漂移检查、前端类型及构建通过 |
| `npm --prefix extensions/firefox test` | 4 项外部 HTTP／存储适配器回归通过：来源与密钥关联、旧协议升级提示、验证与分类串行、缓存配额失败后恢复且不重新推理 |
| `tests/firefox_extension_ui.py` | 真实 Nightly 160.0a1，17 项通过；使用两个隔离 Firefox 配置文件，正式安装并读回 0.2.0、启用、非临时状态 |
| 仅暂存内容导出的前端构建 | 通过；不依赖用户尚未提交的侧栏修改 |
| 完整采集回归 | 221 项，3 失败、4 错误、6 跳过；失败集合与既有基线一致，详见下文 |

Firefox 验收使用真实私钥登录、Cookie／CSRF、队列和事务。模型服务运行在测试设备另一进程，云端 fixture 明确禁止连接其端口；只替换外部采集和模型服务，不替换分类引擎。覆盖工具栏保存／去重／归档、首次共享规划、完整结果在云端接受前后遇到后台重启且不再次推理、双设备隔离、网页校对与确认、业务页面关闭、API 中断恢复、HTML 导入和完整 Firefox 退出／重新打开后的原设备恢复。

本地证据：`output/local-ai/verification-final.log`、`output/local-ai/browser-persistent.log`、`output/firefox-extension/acceptance-report.json`、`output/local-ai/capture-regression.log`。回归测试额外覆盖过期租约、旧缓存、取消与切库、非法 JSON／目录、全库规划材料依赖，以及在自动归档和任务收据之间注入数据库故障后完整回滚。

采集既有失败为 application_readiness 的两项时限检查、source_navigation_budget 的慢文档检查，以及 media_resources 的三个方法（其中一个有两个错误子例）。与 `output/firefox-extension/baseline-capture.log` 对比，失败名称与集合相同；本次没有将这些检查跳过或改成通过。

## Standards review

独立规范审查发现来源密钥继承使用设置页旧状态，以及缓存恢复后唤醒期限未重置。均已修复：密钥关联在后台根据实际已存来源判断，恢复重置期限；外部适配器测试覆盖两者。针对性复核未发现剩余阻断项。

## Spec review

独立规格审查发现材料采集竞争、配置验证绕过串行锁、结果缓存失败后自动重新推理，以及首次规划材料等待范围不足。均已修复，补充未选中收藏已有运行采集任务的 HTTP 回归，并验证恢复后的后续普通唤醒。针对性复核未发现剩余阻断项。

## 交付边界

产物为 `extensions/firefox/artifacts/linkarium-firefox-unsigned.xpi`。真实浏览器验收只操作隔离测试配置文件；现有用户 Nightly 配置文件与生产收藏未在本轮改动。此轮提交不推送、不部署；上线需协调服务器协议与插件升级，再在实际设备配置并验证自己的 API。

受控模型证明协议、恢复和权限边界，不证明真实模型分类质量。真实供应商模型质量、生产部署和现有用户配置正式升级仍待后续验收。Firefox 退出后任务暂停；未获得完整响应时恢复会重新请求，不承诺中间 token 续传或上游恰好一次。
