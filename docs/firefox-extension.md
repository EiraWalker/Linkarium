# Firefox 一键收藏与 AI 自动归档

工具栏按钮「收藏到 Linkarium」保存点击时页面的网址和标题到固定实例 `https://link.eirawav.me`。新收藏先保存到未分类，再由服务器获取有限页面信息、AI 分类并自动归档；已有网址只返回原收藏，保留人工字段，不重新分类。

## 构建

需要项目 Python 虚拟环境和 Node/npm。项目根目录执行：

```powershell
npm ci --prefix extensions/firefox
npm --prefix extensions/firefox run build
```

产物为 `extensions/firefox/artifacts/linkarium-firefox-unsigned.xpi`，命令同时输出 SHA256。构建进行严格 TypeScript 检查；XPI 只含编译脚本、清单、图标及状态页面，不打包依赖、源码映射、测试配置或凭据。

## 安装与使用

首版为 Firefox 桌面 Manifest V3 插件，最低 Firefox 140，支持普通窗口；不支持隐私窗口或容器标签页。

正常 Firefox 长期安装需要 Mozilla 签名的 XPI。当前构建输出**未签名**，不代表已经签名、发布、安装或接入线上后端。发布前须完成签名分发和匹配版本的服务端部署；本功能实现不自动发布扩展商店、不修改 Firefox 的签名配置，也不部署生产。

开发验证可在 `about:debugging#/runtime/this-firefox` 选择「临时载入附加组件」，使用构建目录或测试包；临时载入在浏览器重启后失效，不能代替正式安装验收。

安装匹配的包并授予 Linkarium 站点权限后：

1. 将插件固定到工具栏。在要保存的 HTTP/HTTPS 页面点击书签图标。
2. 未登录时插件打开 Linkarium 登录页；完成现有 Passkey 或私钥文件登录后，继续提交原页面。插件不保存登录密码、私钥、Cookie 或 AI 密钥。
3. 服务器接受后显示「已保存，正在分类」，可以继续浏览或关闭来源页。后台分类不依赖 Firefox 继续运行。
4. 用 `Alt+Shift+L` 或扩展管理页的选项打开最近收藏状态，查看归档目录、失败原因或重试。状态窗口打开时定期查询结果；关闭后服务器仍会执行。

「已自动归档」表示目录归属已经落库；「已存在」表示返回已有记录；「已保存，分类需要处理」表示链接已经保留，AI 配置、请求或版本问题仍需处理。「提交结果待确认」不宣称保存成功，用户可使用原提交标识重试。人工改变目录后，旧自动应用不会覆盖新归属。

插件只在主动点击时读取当前页 URL 和标题，不读取浏览器书签库、正文、历史或第三方站点登录态。扩展存储保留最多最近 30 次主动提交的恢复信息；没有离线批量上传和跨设备同步配置功能。

`scripting` 权限只用于固定 Linkarium 站点的同源桥失联时重新安装随包脚本，不用于来源页面。网络中断可用同一请求键有界重试；后台重启仅对两分钟内未确认的主动提交执行一次恢复，较旧请求保留明确状态供用户主动重试。

## 开发验收

后端专项：

```powershell
$env:BOOKMARK_DATA_ROOT = "$PWD\output\firefox-extension\test-data"
$env:PYTHONPATH = "$PWD;$PWD\backend"
.\.venv\Scripts\python.exe -X utf8 backend/manage.py test library.test_extension_submissions --noinput
```

真实 Firefox 验收使用新建 WebDriver 配置和独立收藏库；正常私钥文件登录、真实会话／CSRF、HTTP API、worker、目录事务均执行，只有外部 AI 和采集服务使用受控协议样本。不会访问生产实例或修改用户的浏览器配置。

```powershell
npm --prefix extensions/firefox run build
.\.venv\Scripts\python.exe -X utf8 scripts/build_firefox_extension.py --test-origin http://127.0.0.1:18831 --output output/firefox-extension/linkarium-firefox-test.xpi
# Selenium 装在独立工具目录；浏览器测试使用同一 Python 版本运行。
C:\Python312\python.exe -X utf8 -m pip install --target output/firefox-extension/python-tools selenium==4.51.0
C:\Python312\python.exe -X utf8 tests/firefox_extension_ui.py
```

默认使用安装的 Firefox；可通过 `FIREFOX_EXTENSION_TEST_BINARY` 指定其他适用 Firefox。工具必须能取得 geckodriver。测试服务只监听 127.0.0.1 的 18831、18832 端口；执行前确认可绑定且未被 Windows 排除。测试构建只允许 loopback 源，具有独立插件 ID 和「隔离测试」名称，不覆盖正式插件。测试私钥、数据库、浏览器日志和结果均保存在被忽略的 output 目录。

报告位于 `output/firefox-extension/acceptance-report.json`。成功的受控测试证明提交及分类执行链路，不能证明真实模型的分类质量、正式签名安装或生产部署已经完成。线上验收应在另行部署并安装正式包后，以实际公开页面单独记录。

2026-10-20 本地验收：11 项插件 HTTP／worker 专项通过；291 项后端／运行时／可迁移性测试通过（6 项跳过）；Firefox 158 实际按钮、私钥登录、CSRF、重复收藏、请求接受前后强制停止后台并恢复、最终状态页面六项检查通过。网页与插件 TypeScript 检查及构建通过。AI 与采集服务使用受控样本，正式签名安装、真实模型质量和生产部署尚未验收。

全量采集回归另外运行 221 项：3 处断言失败、4 处错误、6 项跳过，涉及未修改的页面就绪、媒体预算和来源导航测试。日志在 `output/firefox-extension/full-verification.log`；不能将该次全量脚本描述为全部通过。

已将改动前基线 `79ff57eac921aecac6924e5aa40f35020e5b254c` 导出到隔离目录，独立运行上述六个失败测试方法（其中一项包含两个失败子用例），复现相同的 3 处断言失败和 4 处错误，日志为 `output/firefox-extension/baseline-capture.log`。这些是已证实在基线存在的采集回归，本次未修改采集引擎或对应测试。双轴代码审查发现的自动归档权限撤销和客户端中断恢复问题均已修复，复审无剩余发现。
