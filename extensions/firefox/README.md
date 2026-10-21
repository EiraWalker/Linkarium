# Firefox 本机 AI 执行端（0.2.0）

工具栏按钮保存当前网页，Alt+Shift+L 打开最近收藏和本设备 AI 设置。只适配 Firefox 桌面版的普通标签页，不采集浏览器书签库。

设置 OpenAI 兼容 API 基础地址（或完整 chat/completions 地址）、模型、密钥和输出模式，点击“授权来源并验证”。验证会发出真实非流式 JSON 请求。密钥、设备 proof 和待回传结果仅保存在此 Firefox 配置文件；不使用 storage.sync，也不传至 Linkarium。更换来源须重新提供密钥。来源权限使用 Firefox 匹配模式（不含端口），实际请求仍限于配置的精确 URL。

云端下发完整 prompt，本机执行，完整结果先保存后回传。Firefox 退出、后台卸载或连接中断后，由原设备恢复；完成结果优先重传。未完成模型响应只能重新请求，不能从中间 token 继续。其他设备不能接管。网页及导入结果需要人工确认；工具栏新建收藏沿用单项自动归档，重复网址不会重排原收藏。

保持 Linkarium 的 owner 登录有效；插件可恢复一个后台站点标签作为 Cookie/CSRF 通道。站点页关闭不取消分类。状态页可暂停／恢复，网页任务窗口也显示发起设备及执行阶段。本机配置不会改变历史服务器配置。

构建：`npm --prefix extensions/firefox ci`，然后 `npm --prefix extensions/firefox run build`。产物为 `artifacts/linkarium-firefox-unsigned.xpi`，未签名；安装遵循 Nightly 既有策略。正式升级前先部署支持本机协议 1 的服务器，再安装 0.2.0 插件并验证设备来源权限。构建不等于已部署或已升级真实浏览器。

隔离验收：先构建前端，再运行 `scripts/build_firefox_extension.py --test-origin http://127.0.0.1:18831 --output output/firefox-extension/linkarium-firefox-test.xpi`；用 `tests/firefox_extension_ui.py` 运行独立 Nightly 配置文件。默认二进制为 `C:\Program Files\Firefox Nightly\firefox.exe`，可用 `FIREFOX_EXTENSION_TEST_BINARY` 指定支持未签名测试包的浏览器。覆盖正式安装及完整退出重开；不操作常用配置文件。云端 fixture 禁止连接模型端口，模型服务由测试设备另一进程中的 HTTP server 提供；外部采集使用受控适配器。测试不访问生产收藏或真实供应商密钥。`npm --prefix extensions/firefox test` 额外验证 HTTP／存储故障与配置边界。
