# 安全审计修复与验收

对应2026-10-18、提交9ab545e的11项静态审计发现。验证使用隔离数据；真实用户收藏库不承受资源耗尽测试。

| 发现 | 修复边界 | 回归依据 |
| --- | --- | --- |
| LAN匿名大请求体 | Waitress解析请求头时限制普通body；owner一次性路径/大小/磁盘额度才允许大上传；并发和绝对时间受限 | tests/deploy/test_ingress.py、test_ingress_disconnect.py的真实socket |
| 开发抓取DNS重绑定 | system DNS和DoH均通过固定已验证IP的连接层，浏览器不能另行解析实际目标 | tests/capture/test_doh_proxy.py真实浏览器重绑定和私网拒绝 |
| HTTP建立CA信任 | 证书页只监听127.0.0.1；新设备经独立可信渠道传输并核对服务器终端指纹 | tests/network_access_ui.py的真实Caddy/TLS和局域网证书端口拒绝 |
| isolated spool残留 | release协议、失败/取消回收、1小时TTL、32次/1GiB聚合容量门禁 | tests/capture/test_launcher.py、library.test_worker_release |
| 恢复SQLite行展开 | 流式资源校验、容量对齐行上限与孤儿预算；不可信校验在受限子进程 | tests/portability/test_portability.py真实Windows Job Object及大库往返 |
| root可替换路径 | incoming原子接管到root-only private；results只读；私有父路径与文件描述符固定对象 | WSL root运行tests/capture/test_launcher.py的降权UID、rename、symlink及TTL测试 |
| 导入任务放大 | 先预览后签名确认；按实际行计算；1000网站/2000抓取任务/101AI请求；一次一个导入；实际AI出网前持久扣减 | library.test_security_limits、test_security_review及import workflow UI |
| 匿名session膨胀 | 短期签名nonce绑定挑战，不创建django_session；共享限流/512挑战/1024来源容量；定期清理 | runtime.test_authentication_budgets及真实认证UI |
| 远程AI明文 | 非canonical loopback强制HTTPS；旧配置可读取修改，但不能继续发送Key | library.test_security_limits、test_security_review的零网络拒绝 |
| 首次Passkey共享锁死 | 高熵initialize链接定位grant，加独立码；未知token或旧单码不消耗合法授权次数 | runtime.test_authentication_enrollment、test_authentication_budgets及真实首次登记 |
| 恢复残留 | 上传源、失败/过期/取消候选和旧无主文件按生命周期回收；保护活动及回退generation | tests.portability维护生命周期与数据保留测试 |

## 升级

停止该实例后备份整个数据目录和匹配版本，再离线运行 `backend/manage.py initialize`，迁移运行库和活动收藏库。原Passkey、文件公钥、收藏和快照保留；旧未确认导入转回预览，取消原自动任务，已导入记录不删除。重新确认后只补缺失抓取。

隔离launcher代码改变会使旧attestation失效。更新launcher和目录权限后仍须完成真实主机隔离验收；安装、单元测试和文件系统测试不生成或替代该证明。

普通业务自动重试仍受其既有边界约束。导入AI配额包括目录规划、失败请求及手动重试；用尽后需用户另行明确发起分类操作。抓取上限指任务数，每个任务的尝试次数另由队列重试限制约束。

复核同时补齐两条失败边界：HTML发布的复制或fsync失败也回收本次创建的临时文件；AI响应要求identity编码，在原始字节进入缓冲前检查2 MiB和总时限，防止自动解压先分配超预算内存。

本次没有修改系统CA信任。虚拟认证器与手机视口不代表Android实机安装或Windows Hello验证；这些既有边界保持独立记录。

## 2026-10-18 验收记录

完整Django/运行库/便携性回归278项：272通过、6项条件跳过；真实Waitress socket回归10项通过。另行完成WSL root launcher权限测试11项、实际Chromium/Monolith抓取测试14项，以及真实Caddy HTTPS、认证和导入浏览器验收。前端TypeScript检查及生产构建通过，模型迁移检查无遗漏。

完整回归中的跳过项涉及Linux进程寿命、Windows符号链接权限及专用受限文件系统等环境条件；不把条件跳过记为通过。测试输出、UI截图和部署备份回执保存在本地忽略目录，不包含在源码提交中。
