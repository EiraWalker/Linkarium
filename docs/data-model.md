# 数据模型与状态规则

2026-10-09。两库实体及迁移已实现；本文描述数据约束，实际验证见 [实现记录](implementation-status.md)。

## 1. 两个数据库边界

`default`/`state.sqlite3`：Django auth/contenttypes/session、axes、RuntimeState、ProviderSecret、MaintenanceOperation、RuntimeIdempotency。`library`/当前 generation 的 `library.sqlite3`：下述业务实体及 LibraryIdempotency。数据库路由按 app label 明确分配，迁移分别运行；不使用跨库外键。备份、恢复、升级操作均由运行库登记和维护控制器执行，不放入业务 Job，也不由业务 worker 领取。

业务库属于单一个人收藏库，不在每行增加用户外键。每个请求仍在后端验证固定 owner；此模型不隐含多租户支持。运行表与业务任务以 UUID 字符串和 generation/epoch 校验建立联系。

所有业务 ID 使用 UUID；时间保存 UTC，界面按设备时区显示。所有数据写入服务层，禁止在前端拼出文件路径或让 AI 直接执行写入。

## 2. 运行状态

| 实体 | 关键字段 | 规则 |
| --- | --- | --- |
| RuntimeState | active_generation、library_epoch、maintenance_mode、active_maintenance_id、schema_version | 单例，记录当前业务目录；generation 切换和回退均递增 library_epoch，由维护控制器执行 |
| ProviderSecret | profile_id、api_key、bound_endpoint、bound_protocol、endpoint_fingerprint、verified、verified_library_epoch、updated_at | profile_id 为业务 UUID 字符串，不是跨库 FK；API 仅返回配置/验证状态；不进入业务备份 |
| MaintenanceOperation | id、kind、state、stage、source_package、package_sha256、old_generation、target_generation、expected_library_epoch、claim_token、active_key、validation_token_hash、validation_expires_at、result、error_code、timestamps | kind 为 backup/restore/upgrade/clear/reset；运行库持久登记，业务库切换后仍可查同一操作；原始文件路径、claim token 和验证 token hash 不返回普通接口 |
| RuntimeIdempotency | id、owner_key、method、path、key、request_hash、operation_id、state、response_status、response_body、created_at、expires_at | 维护请求的幂等记录；同库关联 MaintenanceOperation，不随收藏库恢复替换 |
| Django 内置表 | 固定 owner、密码哈希、会话、axes 状态 | 不随收藏库备份回滚；恢复完成撤销会话，当前密码保留 |

运行 SQLite 和密钥文件只允许应用账号访问。密钥保护不宣称能抵抗拥有主机管理员权限的攻击者；采集 runner 不挂载这些文件。

ProviderSecret 的指纹由规范化的完整 API base URL（scheme、hostname、有效端口、路径）和协议组成，不仅比较域名或 profile UUID。接口地址禁止内嵌凭据；改变 endpoint/协议立即令 verified=false。每次发请求前重新比较当前 AIProfile 的指纹、verified 和当前 library_epoch，不在 Job payload 中保存密钥。恢复后所有 AI 调用先禁用；已有本机密钥只有绑定匹配且用户显式验证后才可继续使用。绑定不匹配时要求重新提供或明确重新绑定凭据，不能用旧密钥自动测试备份中的新地址。跨机器恢复缺少密钥时保持未配置状态。

同时修改非机密 AIProfile 和运行凭据不是跨库原子操作：先使调用门控不可用，分别保存两库，最后显式验证并启用。任一步失败维持未验证/不可调用状态，允许重试配置；每次发送前的指纹/epoch 校验不能被配置界面的成功提示替代。

维护状态包括 validating、awaiting_confirmation、queued、running、succeeded、failed；恢复上传校验不自动授权覆盖。进入备份/恢复执行或升级时，以 state 短事务领取 claim_token 并占有统一 active_key，确保同一时间至多一个执行中的维护操作；维护父进程自身也须单实例。备份 staging 冻结后可解除 maintenance_mode 并重启业务，但压缩/校验结束前仍保留维护独占权。进程重启根据持久 stage 和 generation 指针恢复或回退，不重新发起第二次覆盖。

### 幂等记录的两库作用域

RuntimeIdempotency 只用于备份、恢复和升级等维护入口；LibraryIdempotency 用于导入执行、批量编辑、分类确认等业务入口。两者使用相同字段约定但独立模型/表，不跨库外键；业务记录可含 job/group UUID，运行记录只关联 state 中的 operation。`(owner_key,method,path,key)` 唯一，request_hash 为规范化请求内容/上传包摘要的哈希；同键异参拒绝，同键同参返回已保存的脱敏 HTTP 状态/响应或操作 ID。登记幂等记录与创建对象在各自同库事务内完成，不要求两库同时提交。初始保留 24 小时，未结束的维护操作不得因到时而丢失查询关联；不保存密码、API 密钥、CSRF token 或认证 cookie。

业务幂等记录还保存创建时的 generation/library_epoch；恢复候选时使导入的旧业务幂等执行权失效，不把旧 job/group 的响应当作当前库新请求结果。维护记录保持在 state，因此即使确认请求响应丢失或业务库已经切换，重新登录后仍能用原幂等键查询同一 operation。

## 3. 收藏与目录

| 实体 | 关键字段 | 约束及索引 |
| --- | --- | --- |
| LibrarySettings | default_refresh_seconds=604800、tree_revision、revision、search/UI defaults、active_ai_profile_id | 单例；AI profile 引用位于业务库，密钥另存运行库 |
| Folder | id、parent_id、name、name_key、position、revision、created_at/updated_at | 父指针可空；禁止自引用/环；同级规范名唯一；parent_id 索引 |
| Bookmark | id、original_url、canonical_url、final_url、domain、source_title、title_override、notes、folder_id、is_starred、snapshot_enabled、auto_refresh_paused、refresh_interval_seconds、revision、classification_revision、capture_epoch、created_at/updated_at | canonical_url 全局唯一；folder 可空；URL/域名/标题、folder/created_at、star/created_at 索引 |
| Tag | id、name、name_key、revision | name_key 全库唯一 |
| BookmarkTag | bookmark_id、tag_id | 联合唯一，删除网站或标签时清理关系 |
| BookmarkSource | id、bookmark_id、import_batch_id、source_folder_path、source_title | 保留导入来源参照；重复导入跳过已有网站，不新增或覆盖它的来源信息 |

### 目录和名称

名称比较使用 Unicode NFKC + casefold 后的 name_key，但显示保留原名。根目录同名用 `WHERE parent_id IS NULL` 的部分唯一索引，同一非根父目录用 `(parent_id,name_key)` 唯一约束；不依赖 SQLite 对 NULL 的默认唯一语义。

未分类是 `folder_id=NULL` 的视图，不创建一个实际同名文件夹。文件夹移动先加载父链验证无环，同一事务更新 revision 与 tree_revision。技术初始上限：名称 100 字符、目录深度 32，超限返回明确校验错误；不据此限制收藏总量。

网页删除/目录子树删除是业务服务：先计算影响范围，再短事务取消相关任务、删除候选/历史/归属记录，最后异步回收无引用 HTML。删除目录不能仅依赖某个外键的 SET_NULL，否则会违背级联删除收藏的要求。

### URL 身份

仅接受 HTTP/HTTPS、拒绝 URL 内嵌用户名密码及不支持的协议。canonical_url 规范 scheme、IDNA hostname 和默认端口；空路径转 `/`。保留 query 内容及顺序、fragment、非根路径尾斜杠和 HTTP/HTTPS 差别；不默认删除跟踪参数，不按重定向自动合并。

主机是否为公开目标由采集策略另查；规范化不是 SSRF 防护。修改 URL 与其他对象冲突时 `409 URL_EXISTS`；修改成功增加 capture_epoch、classification_revision，旧抓取结果失效，旧历史仍保留其原 URL。

### 编辑版本

- revision：用户可编辑字段每次写入递增；API 以 `WHERE id=? AND revision=?` 更新，影响行为零则返回冲突。
- classification_revision：URL、标题/分类输入或文件夹人工修改时递增；星标变化不使分类方案过期。
- capture_epoch：URL、采集配置、快照启停变化时递增；关闭时在途任务不能再发布版本，保留旧成功指针。
- 后台更新 source_title 不覆盖 title_override；普通快照完成不伪装成用户编辑，不改变排序中的 added_at。

## 4. 快照与文件

| 实体 | 关键字段 | 约束 |
| --- | --- | --- |
| CaptureState | bookmark_id、variant、current_version_id、last_attempt_at、last_success_at、next_due_at、failure_count、last_error_code | 每网站+desktop/mobile 唯一；current 指向同网站同设备的 SnapshotVersion，由服务校验 |
| SnapshotVersion | id、bookmark_id、variant、artifact_id、capture_attempt_id、captured_at、source_url、final_url、viewport_width、viewport_height、recipe_version、capture_epoch、content_warnings | capture_attempt_id UNIQUE，关联同业务库 JobAttempt；不可变历史；成功才建立；按 bookmark/variant/captured_at/id 索引 |
| Artifact | sha256、relative_path、byte_size、created_at | SHA256 唯一；相同字节允许复用；路径由系统生成 |

归档路径采用 `artifacts/<sha256-prefix>/<sha256>.html`，prefix 为哈希前两位。文件不放进公开静态目录，API 通过 SnapshotVersion ID 验证有效引用后读取。不能通过猜测 hash 读取无引用文件。

SnapshotVersion 对 Artifact 和 JobAttempt 的引用使用受保护删除规则（PROTECT），禁止清理任务记录时级联抹掉快照历史。仅在不再被任何版本引用、已结束且没有活动执行权时，才能清理相应 attempt/Job；删除网站先按业务规则删除其版本，再回收无引用对象。

发布顺序：事务外复制临时文件、刷新并校验长度/哈希→进入业务库 IMMEDIATE 写事务，重新检查 generation、library_epoch、当前 attempt/lease、capture_epoch 和对象存在→在该写锁内同卷 rename 成不可变文件，或校验并复用已有同内容文件→插入/复用 Artifact、插入版本、更新当前指针、将 attempt/Job 标记成功→提交。文件清理使用同一业务库写锁，避免相同内容的旧孤儿文件在 rename 与建立引用之间被删除。SnapshotVersion 的 capture_attempt_id 唯一约束使同一 attempt 的重复提交只能返回原结果，不能追加第二个版本；attempt 与版本关联和终态提交在同一事务。若事务失败，文件成为待回收孤儿；数据库与文件系统仍不是联合原子提交。

同一 active generation 的发布和回收经统一维护门闩协调；回收任务在删除前重查所有 SnapshotVersion 引用。历史清理请求包含版本 ID 列表；若包含任一设备的 current_version，整批 `409 CURRENT_SNAPSHOT_PROTECTED`，不部分误删。业务删除整个网站时可删除全部版本。

## 5. 后台任务

| 实体 | 关键字段 | 用途 |
| --- | --- | --- |
| Job | id、kind、target_id、variant、state、priority、available_at、attempt_count、active_attempt_id、max_attempts、active_key、generation、library_epoch、input_revision、payload、error_code、timestamps | 元信息/快照/AI/导入/清理业务任务；target 可为已删除对象的 ID；不含维护任务和 API 密钥 |
| JobAttempt | id、job_id、attempt_no、state、lease_token、lease_expires_at、heartbeat_at、generation、library_epoch、capture_epoch/input_revision、error_code、timestamps | id 为独立 UUID；UNIQUE(job_id,attempt_no)；认领/重试产生新行、新 token，不重用旧执行权 |
| JobGroup | id、kind、source、requested_count、created_at | 文件夹刷新、导入或分类批次 |
| JobGroupMember | group_id、job_id、bookmark_id | 多个请求可以观察同一去重任务；联合唯一 |
| LibraryIdempotency | id、owner_key、method、path、key、request_hash、generation、library_epoch、job_id/group_id、state、response_status、response_body、created_at、expires_at | 业务请求的幂等记录；不与 state 表建立 FK；恢复后旧执行权失效 |

状态：`queued → running → succeeded/failed/cancelled`；可重试失败先 `retry_wait → running`。活跃任务以 active_key 唯一，终态清空 active_key；设备快照的 key 包含网站 ID、variant、capture_epoch。重复请求链接已有任务，不再排第二份。

认领用短事务中的条件 UPDATE 并检查影响行数，同时创建 JobAttempt 并设置 active_attempt_id；不能以 SQLite 的 select_for_update 作为锁。网络操作在事务外；小型心跳线程维护当前 attempt 租约。初始租约 180 秒、心跳 10 秒，超过总任务期限由控制器结束对应 attempt 的子进程。重试只针对超时、临时网络和限流；不重试权限禁止或已失效输入。

结果提交要求 active_attempt_id、token、generation、library_epoch 和对象输入版本仍匹配；快照使用 capture_epoch，不因星标 revision 改动丢弃结果。恢复、删除、URL 变化、停用快照或任务取消会使旧结果失效。进程重启扫描过期租约：终止旧 attempt，再重入 retry_wait 或结束 Job；保证可恢复和幂等结果，不宣称严格 exactly-once。任何仍被 SnapshotVersion 引用的成功 attempt 随历史保留，不能用普通任务记录清理删掉该唯一提交依据。

调用宿主 capture launcher 的启动、取消、状态查询和退出确认全部使用 JobAttempt.id；spool 目录同样由受控根目录映射 attempt UUID，并记录 generation/library_epoch。重试不能复用旧 attempt 的目录或 launcher 身份；迟到取消/结果只作用于相同 attempt，不能误伤下一次执行。URL/启停变化使旧 attempt 取消或失效，新 capture_epoch 可登记自己的去重任务。

计划默认优先级：手动 60、新增 40、定时 10、清理 0；每处理五个高优先任务检查一个已等待的低优先任务。AI 使用同一 worker，不另建常驻消费者，HTTP 调用也设总时限。

默认每次成功后 `next_due_at = last_success_at + interval`；单设备失败不推进成功时间。未成功过的设备首次立即到期；每轮最多三次尝试，间隔按 1 分钟、5 分钟退避。耗尽后保留失败提示并把 next_due 设为最后尝试时间加当前刷新间隔，避免每次扫描立即重排；手动可提前重试。关闭自动更新时 next_due 不触发，手动仍可排队；关闭快照时两种触发都跳过。

组进度同时返回网站数和设备任务数：两个设备都完成才计网站完成；一成一败标记 partial。原网站被删除或禁用计 skipped/cancelled，不长时间卡在运行中。

## 6. AI 与导入

| 实体 | 关键字段 | 约束 |
| --- | --- | --- |
| AIProfile | id、name、base_url、model、protocol、output_mode、enabled、revision | 业务配置可备份；profile UUID 仅用于查运行凭据，还须验证 endpoint/protocol 指纹与当前 epoch；无跨库 FK |
| PageMetadata | bookmark_id、title、description、text_excerpt、fetched_at、source_url、content_hash | 正文片段有界，不建全文索引；供 AI 输入及详情元信息 |
| ClassificationBatch | id、job_group_id、profile_id、tree_revision、created_at | 一次新增或主动重分类 |
| ClassificationProposal | id、batch_id、bookmark_id、expected_classification_revision、expected_tree_revision、target_folder_id/target_path、reason、status、revision | target 两种表示择一；pending/accepted/rejected/stale；每网站只保留一个可接受的当前方案 |
| ImportBatch | id、state、source_type、total/valid/duplicate/invalid、created_at | 预检与执行分开，分块写入有进度 |
| ImportEntry | id、batch_id、url、title、original_folder_path、status、bookmark_id/error_code | 预检缓存，执行再次检查唯一性；来源文件可清理，不等于删除已导入收藏 |

AI 输出先严格验证再落候选表；正文不是指令。确认选择项时先验证所有 revision、共享目标路径及目录约束；一个批次最多 100 项，一个事务全部成功或不执行。树变化导致 stale 时返回具体项供重看，不悄悄改用另一目录。人工改动或新一轮方案替代旧方案后，旧方案不能再被接受。

## 7. 初始查询与容量策略

个人千条规模下采用数据库过滤和普通索引，不引入全文搜索。搜索使用规范化比较文本的子串匹配；日期/目录/标签关系索引用于缩小候选范围。计数和分页允许单次短事务快照，返回 total 与稳定排序。

文件大小与版本数决定磁盘用量，历史不会按时间自动淘汰。可以显示实际库用量和剩余磁盘；空间不足时停止创建新文件并报错，保留历史。业务库与所有 artifact 通过 manifest/hash 验证备份完整性，恢复前后的不变量验收见 operations-security.md。
