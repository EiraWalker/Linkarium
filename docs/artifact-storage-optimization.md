# Artifacts 无损存储优化

## 目标与边界

- 保留所有收藏、桌面/手机快照、历史版本、原始 HTML 字节和 SHA-256，不降低图片质量。
- 不修改认证、会话、域名、Nginx 公网路径；不包含工作区已有前端改动。
- 分阶段测试、仅暂存相关文件、提交并推送，最终用已提交的干净源码构建并部署。
- 旧 `.bookmarklib.zip` 仍可导入；新备份保留原有包格式及逻辑 HTML 内容，不将存储编码泄露给格式消费者。

## 已有证据

- 活动库 495 收藏、1,313 快照版本，1,288 个 artifacts；原始 HTML 约 8.32 GiB。
- 其中当前唯一文件 882 个/3.80 GiB，历史独有文件 406 个/4.52 GiB；无已知孤儿文件。
- 这批 artifacts 全部与旧升级回退树硬链接。只压缩活动库或删除一侧引用不等于节省实际磁盘块。
- 基线后端回归：305 tests，6 skipped，通过。

## 设计决策

选择逐对象、确定性 gzip 无损压缩，而非立即重写 HTML 为共享资源仓库：后者会改变字节/哈希/离线导出 contract，还需引用事务及新备份格式。独立压缩不改变逻辑内容，现有测试及备份边界可复用；图片资源级去重留待独立规格，不暗中改变本次保真目标。

数据库中的 SHA、byte_size、relative_path 继续描述原始 HTML；物理层允许 legacy `.html` 与压缩 `.html.gz` 混合读取。新的压缩表示应由单一存储模块封装，HTTP/预览/备份/恢复/GC 不各自猜测编码。

要求：分块压缩/解压、展开字节限额、完整性校验、无目录穿越/符号链接、原子发布；错误/中断不能先删唯一原件。HTTP 保持原始 HTML、sandbox/CSP、鉴权及正确 Content-Length，不能把 gzip 文件长度当 HTML 长度。

## 阶段与验收

1. **存储及调用链兼容**：先写失败测试；实现透明读取、压缩写入、旧文件支持，覆盖 HTTP、预览、备份恢复和 GC；回归通过后 commit/push。
2. **存量优化工具**：dry-run、逐文件校验、可重复/断点续做、低空间和硬链接组处理，提供还原为旧格式的方法；测试通过后 commit/push。
3. **发布验收**：独立 code review、全后端回归、相关 capture 测试、干净源码前端构建与发布包校验，完成对应 commit/push。
4. **生产部署与迁移**：先读实时状态；仅停启 Linkarium，保留冷数据库/配置与版本恢复资料；在维护窗口逐组迁移活动及明确关联的回退文件，不删除历史内容；原始 HTML SHA、引用/记录数、真实 HTTP、实际占用前后对账。提交推送部署报告。

## 操作约束

- 旧回退树不具备新代码解压能力，必须随迁移保留清楚的恢复工具与说明，不声称旧镜像可以直接读取压缩树。
- 最近只读测量（2026-11-10 23:55 UTC+08）空余 2,433,966,080 bytes，约 2.27 GiB；写入前须重新测量。禁止同时展开/复制全库；按硬链接组逐一生成、解压校验、发布并移除已被无损替代的原始表示。
- 恢复/备份验证的容量预算须区分逻辑字节和物理字节；不得通过跳过验证解决空间问题。
- 线上部署前再次确认 current、image revision、认证配置、容器挂载和剩余空间，不根据旧运维文档假定路径。

## 验证记录

### 2026-11-10：只读全库测量

- 对活动库全部 1,288 个对象检查原始 SHA/大小，并逐块执行 gzip level 6，仅累计编码长度、不写线上 artifacts。
- 原始长度 8,932,330,312 bytes，gzip 长度 2,628,779,564 bytes，约减少 70.57% / 5.87 GiB。真实文件系统块与硬链接释放量必须在迁移后另测，不能将此预测写成已回收空间。
- 远程 CPU 232.507 秒、墙钟 241.244 秒。压缩候选的完整恢复验证仍受原有 300 秒 CPU / 900 秒墙钟边界保护，生产规模验收不能仅用小 fixture 代替。
- 本地运维证据：`E:/AI-Agent/Hermes/ops/aliyun-storage-audit/compression-full-level6-v1-completion.json` 与同前缀 `-output.txt`，云执行成功、退出码 0、输出无丢失。

### 存储接口与兼容路径

- `library.artifact_storage` 统一提供 `open_artifact` / `read_artifact` / `write_artifact` / `storage_path` / `delete_artifact`，并封装 GC 的表示枚举与时间判定。
- 原始捕获交接仍是 HTML；新写入选择更小的 gzip 表示。读取流完整消费后校验原始长度、SHA 和编码完整性；提前关闭会释放句柄，但不声称尚未读取的内容已验证。
- HTTP 正文仍是原始 HTML，Content-Length 使用数据库的逻辑大小；桌面/手机预览子进程可直接读取 gzip，不必在 artifacts 下展开临时 HTML。
- 备份 ZIP v1 中仍是 `.html` 原始字节。应用恢复流程逐对象暂存与压缩，候选重新验证也使用逻辑内容；底层 `validate_package` 的默认 raw 行为保留，显式 `compact_artifacts=True` 才生成混合表示。
- 备份写入按实际 ZIP 输出检查剩余容量；压缩恢复按单对象原始暂存、最坏压缩暂存和保留空间检查，不把全库逻辑长度误当必须同时占用的物理空间。
- `capture` 包对浏览器入口延迟导入，使存储与离线工具可以在仅标准库的 Python 中运行，不通过为服务器安装浏览器依赖掩盖边界错误。

### 已执行的局部验证

- 磁盘统计 5 tests 通过，包含 `.html.gz` 与硬链接唯一 inode 计量。
- 真实 Chromium 预览 2 tests 通过，覆盖 raw/gzip、桌面/手机版本以及不生成常驻 raw 副本。
- WSL Linux 标准库环境存储 16 tests 通过，1 项 Windows junction 专属测试跳过。
- 以上为早期局部结果；后续阶段记录如下，均不表示已上线。

### 已提交阶段与线上只读门禁

- 存储兼容阶段已独立复审、提交并推送：`906ec989d5825350cb39d29422459b354ede6048`。精确源码后端 352 tests / 8 skipped、HTTP/统计 6 tests、Chromium 预览 2 tests、Linux 非 root 只读环境 22 tests / 1 skipped 通过。
- 迁移阶段已独立复审、提交并推送：`fd80621441d832354a79115af721ff7b87bb51ca`。Windows 48 tests / 6 skipped、Linux 原生双文件系统 48 tests / 4 skipped 通过；树分配减少与文件系统实际 free 差分别计量，不把未关闭 reader 持有的块算作已释放。
- 代码升级 operator 已独立复审、提交并推送：`583a67eb23c294d3213904a19cd6b5dfa75d3636`。Windows 50 tests / 31 POSIX skipped、Linux 原生文件系统 50 tests / 0 skipped 通过；绑定实际容器环境与端口，在停机前复查，锁内保存并核对业务 counts。自动代码回退不覆盖现有业务或认证库。
- 2026-11-10 23:55 UTC+08，线上 10 项只读门禁通过，仍运行旧版 `32b1b857b1ba3dc7b5835a5e750cef2ef4ed8576`，495 收藏 / 1,313 快照 / 1,288 artifacts，ready=true。冷检查点复制、开销及 512 MiB 安全预留合计 665,177,437 bytes；这不是发布包/镜像准备或完整恢复的峰值预算，也不是实际用户登录验收。
- capture 完整 222 tests 中的 3 failures / 4 errors 在精确旧基线四格对照中同样复现，未修复、不作为全绿结果；其证据独立保留，不通过延长 timeout 隐藏。

### 备份自校验临时占用优化

- `create_backup` 继续拥有已关闭的最终 `.partial` ZIP、唯一 verification 临时目录及最终 rename。仅其私有 `backup_verify` worker action 调用同一个 `_validate_package` 循环：逐条完整执行 ZIP CRC、SHA、长度与路径校验后释放 raw 暂存，不再次压缩或保留整份 artifacts 验证副本。
- SQLite 保留至原有 schema/可信旧版本升级、外键、引用、metadata 与 counts 校验全部完成；返回摘要须与本次备份一致才能发布。链接/空目录门禁、失败清理、300 秒 CPU / 768 MiB 内存 / 900 秒墙钟 / 4096 bytes 返回限额不变。
- 公共 `validate_package` 签名与默认 raw 行为不变；显式 compact restore 仍生成完整候选，保留单对象 raw 加最坏压缩发布空间门禁。不能通过普通 package 请求设置私有 verify-only 行为，不能返回成功但缺失 artifacts 的公共候选。
- 旧测试强制 `create_backup` 调用公共 `validate_package(compact_artifacts=True)`，与上述内部生命周期优化冲突。保留两平台原始失败，测试改为观察真实受限 worker 对最终 `.partial` ZIP 的验证；原实际 ZIP 容量、逐项原字节/SHA 与清理断言保留，并由最终 ZIP 损坏、DB、资源限额及公共恢复完整性负向测试补证。不是跳过验证、伪造 wrapper 调用或修改生产资源上限。
- 完整确认流程通常先清除上传 ZIP，因此旧方案一般为三份压缩体共存；上传清理失败才是四份。按 ZIP 大小近似已有 gzip 测量，逐条自校验后的代理峰值约为 5.10 GiB / 7.45 GiB（另加 DB 和其他开销）。这些不是实测验收；压缩组件 CPU 或顺序 core 调用不能替代完整上传、预览、确认、自动前置备份、自校验、候选复验、generation 切换和清理。
- 本阶段交付门禁为独立复审、精确最终源码回归及对应提交，原始失败证据须保留；完整生产规模恢复预算、干净最终发布包、生产部署/迁移、全 SHA 和真实释放空间均尚未验收。
