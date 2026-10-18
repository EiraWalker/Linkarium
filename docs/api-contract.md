# HTTP API 契约

2026-10-09。已实现的首版契约，机器可读入口为 [OpenAPI](openapi.json)，实际验证见 [实现记录](implementation-status.md)。所有业务端点以 `/api/v1` 开头；登录准备、登录及最小 health 响应不要求已有会话。

## 1. 统一约定

- 同源 cookie 会话；所有写操作验证 CSRF 和 owner。`GET /session` 调用框架 get_token 返回 masked `csrf_token` 并确保 CSRF cookie；请求写入附 `X-CSRFToken`，不依赖前端读取 HttpOnly cookie。登录令 token 轮换，成功后再次请求 `/session` 取得新 token；该响应 no-store。
- ID 为 UUID，时间为 UTC ISO 8601，字符串 UTF-8。密钥字段只能写入，不以 GET 返回。
- 单对象响应 `{data:{...}}`；列表 `{data:[],page:{number,size,total}}`；业务异步响应 `{data:{job_id,group_id,state}}`。维护异步响应使用 `{data:{operation_id,kind,state,stage}}`，不伪装成业务 Job。
- 错误 `{error:{code,message,field_errors?,details?,request_id}}`，不返回堆栈、完整远端正文或密钥。
- `400` 输入错误、`401` 未登录、`403` CSRF/权限、`404` 不存在、`409` revision/业务冲突、`413` 上传过大、`429` 限流、`503` 维护/不可就绪。
- PATCH/批量修改附当前 revision；删除及破坏性操作先取影响预览，提交同一影响 token。预览后数据变更则 `409 PREVIEW_STALE`，不删除后来出现的数据。
- 网站批量写入、删除预览和 AI 建议批量确认/驳回最多 1000 项；历史快照清理最多 100 项。JSON 请求默认上限 1 MiB。导入文件初始上限 20 MiB；备份为流式上传/下载，按磁盘预算预检，不受普通 JSON 限制。
- 需要防重复提交的操作带 `Idempotency-Key`，由固定 owner、方法、路径、键及规范化请求/上传包摘要识别；同键异参返回 `409 IDEMPOTENCY_CONFLICT`。业务记录在 library，备份/恢复/升级记录在 state，与目标对象同库事务登记 request_hash、脱敏 response_status/response_body 及 job/group 或 operation ID。初始保留 24 小时，未结束维护操作仍可关联查询；不能替代 Job active_key 去重。恢复使导入的旧业务幂等执行权失效，state 中的维护记录继续有效。

## 2. 会话与设置

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | /session | `{authenticated, csrf_token, initialization_required, maintenance_mode, auth_mode, password_login_enabled, passkey_configured, file_key_configured, passkey_origin, passkey_available, credential_id, auth_expires_at}`；登录后附 active_generation 和 library_epoch；token 是 masked 值 |
| POST | /session/login | 仅显式调试模式接受 password；正常模式禁止密码登录 |
| POST | /session/passkey/register/options、/verify | 本机有效凭据会话，或链接标识＋独立码授权注册（包括首次登记）；用途由服务器授权记录确定 |
| POST | /session/passkey/login/options、/verify | 浏览器 WebAuthn 登录；校验来源、RP ID、challenge、签名和用户验证 |
| POST | /session/key/options、/verify | 可选 Ed25519 文件密钥登录；浏览器本地签名，只上传签名 |
| POST | /session/passkey/grants | 有效凭据会话签发五分钟单次添加授权；201 返回 enrollment_url、authorization_code、expires_at |
| GET | /session/passkeys | items、can_add；记录含名称、RP ID、创建和最后使用时间、is_current、can_delete |
| PATCH/DELETE | /session/passkeys/{id} | body `{name}` 重命名；删除返回 deleted、logged_out，服务端原子检查最后可用凭据 |
| POST | /session/logout | 撤销当前会话 |
| GET/PATCH | /settings | 默认刷新间隔、前端偏好和 revision |
| GET/PATCH | /capture-network | 后端本机采集代理：direct/clash/manual；GET 返回检测及使用地址，PATCH 需 development 采集模式及 CSRF，详见 capture-proxy.md |
| GET/POST/PATCH | /ai-profiles、/ai-profiles/{id} | 非密钥配置；写入可带 api_key，读回 key_configured、key_verified、binding_matches，不回明文 |
| POST | /ai-profiles/{id}/test | 显式连接验证任务，返回可用性/输出模式；绑定不匹配时要求重新配置/明确绑定，不自动发送旧密钥 |
| GET | /runtime/status | 鉴权后的工具可用性、worker 心跳、磁盘与维护状态 |
| POST | /data/clear | 确认清空所有网站数据、快照、任务、应用内备份；保留密码、刷新设置和 AI 配置/密钥 |
| POST | /data/reset | 确认删除全部用户数据，包括密码、会话、AI 配置和密钥；返回密码初始化状态 |

首次初始化由终端 `passkey_initialize` 输出成功使用前不过期的完整链接和初始化码；每次服务启动重新签发并使旧授权失效，网页完成首个 Passkey 后建立唯一所有者。已有所有者、尚无 Passkey 时同一命令签发相同期限的首个 Passkey 链接和码，保留原账号和文件密钥。手动重新生成会替换旧授权；只有持有高熵链接标识的请求才可消耗该授权的五次错误额度；注册 challenge 仍为三分钟。`passkey_reset` 只签发十分钟的重置入口，新的 Passkey 成功提交时才撤销旧 Passkey、文件密钥、授权及会话，收藏数据保留。完整流程见 [认证说明](authentication.md)。整库恢复后要求重新登录，不从备份恢复认证记录。

注册 options 输入严格互斥：本机会话 `{name}`，或所有链接授权 `{name,enrollment_token,authorization_code}`。旧 `bootstrap_code`、`first_passkey_code` 和 `setup_token` 被拒绝。首次链接使用 `#initialize=token`，新设备使用 `#enroll=token`，重置使用 `#reset=token`；片段不会发送给 HTTP 服务器，前端在 POST 中提交 token。verify 输入 `{challenge_id,credential}`，返回 `registered/authenticated/credential_id/already_completed/registration_mode`；`registration_mode` 为服务器验证的 `local/bootstrap/first_passkey/add_device/reset`，前端据此显示重置结果。同一提交重试不重复登记或再次发放登录 cookie；响应丢失后读取 `/session`，必要时使用刚创建的 Passkey 登录。授权码不出现在链接、二维码、日志或查询接口中。

匿名 options 仅设置十五分钟签名 HttpOnly、SameSite=Strict 浏览器 nonce，不分配数据库 session；verify 必须携带同一 cookie。options、verify 共享跨进程的来源和全局限流；挑战总量与每浏览器未完成挑战有硬上限，来源计数表有硬容量。超过限制返回 `429 AUTH_RATE_LIMITED`。预算和定期回收规则见 [认证说明](authentication.md)。

正常会话绑定具体登录凭据和认证版本，自登录成功起固定十四天到期，不因访问续期。删除凭据使它的所有会话在下一次受保护请求失效；本机添加保持原会话关联和期限。授权错误包括 `AUTH_GRANT_INVALID/EXPIRED/LOCKED/USED`、`AUTH_ALREADY_INITIALIZED`、`AUTH_LAST_CREDENTIAL`、`AUTH_REVOKED`、`AUTH_ATTEMPT_IN_PROGRESS`。

数据管理端点要求 owner、CSRF 和 `Idempotency-Key`，body 为 `{confirmation:"清空仓库"或"重置数据",library_epoch:当前版本}`。确认授权清理执行时的整个仓库；与逐项删除的影响预览不同，不保留确认后新增的网站。接受后立即进入维护状态，返回 202 的 MaintenanceOperation，通过 `/maintenance-operations/{id}` 查询。不会自动生成保留旧数据的备份，已有应用内备份、恢复候选和上传/采集临时文件也会删除；用户自行下载到应用数据目录之外的备份不受影响。

数据重置删除 owner、登录凭据、授权和所有会话，轮换应用生成的签名密钥；部署者通过环境变量配置的签名密钥仍由部署配置管理。匿名 `/session` 的 `initialization_required=true` 显示首次配置界面，管理员通过 `passkey_initialize` 获取新的初始化码。

AIProfile 的 endpoint/协议修改会撤销对应运行凭据的验证状态。恢复后 AI 调用先禁用；相同 profile UUID 不能绕过地址/协议指纹与当前 library_epoch 校验。用户显式验证匹配的绑定后方能继续调用；不匹配时不能把当前密钥用于测试备份中的新地址。密钥不进入任何业务 Job payload 或业务备份。

## 3. 收藏、目录和标签

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET/POST | /bookmarks | 查询/添加；重复添加返回已有记录并标记 created=false |
| GET/PATCH | /bookmarks/{id} | 详情、修改网址/标题/备注/文件夹/星标/刷新配置 |
| POST | /bookmarks/delete-preview | 输入 ID 列表，返回网站/版本/可回收文件数量及 token |
| POST | /bookmarks/bulk | move、add_tags、remove_tags、delete；显式列出 `{id,revision}` |
| GET/POST | /folders | 树与创建；显示计数包含子树 |
| PATCH | /folders/{id} | 名称、父目录、排序；禁止环与同级重名 |
| POST | /folders/{id}/delete-preview | 返回整个子树、网站与历史的影响量 |
| DELETE | /folders/{id} | body 带 revision/preview_token；删除整个子树及其收藏历史 |
| GET/POST | /tags | 标签列表/创建 |
| PATCH/DELETE | /tags/{id} | 改名/删除标签；删除只移除关联，不删除网站 |

用户取消星标只是 `PATCH {revision,is_starred:false}`。选择卡片是前端状态，不请求修改星标。

查询参数示例：

```text
/bookmarks?q=设计%20工具&fields=title,domain,tags,folder,notes,url
  &scope=all&folder_id=...&include_descendants=true
  &tag_ids=id1,id2&starred=true&variant=mobile
  &sort=added_at&order=desc&page=1&page_size=48
```

scope=all 时 folder_id 不限制查询；scope=folder 时指定子树。`fields` 省略为全部已确认字段，空列表拒绝；空 q 正常浏览。关键词任一命中选中字段；tag_ids 任一命中；不同过滤维度之间取交集。排序允许 added_at、snapshot_updated_at、title，空快照时间排最后，以 ID 保持稳定。

默认 page_size=48，上限 100；added_at 对应 Bookmark.created_at，不随刷新改动。

增加 `selection=true` 时，使用相同搜索、文件夹、标签和星标筛选，返回所有匹配网站的 `{id,revision}` 列表，不分页、不加载预览。网站库的“全选”使用此列表，翻页、排序或切换预览设备保留选择；改变筛选范围则清空选择。批量操作提交所有选中网站及其选择时的 revision，版本冲突则整批拒绝。

BookmarkSummary 包含 `id,revision,title,url,domain,folder_path,tags,is_starred,snapshot_enabled,preview,capture_status`。preview 为当前所选 variant 的 `version_id,viewport_width,viewport_height,captured_at,preview_url,state,warnings` 或 null。capture_status 为同一设备的 `{state,last_error_code}`，state 为 queued/running/retry_wait/failed/ready/missing/disabled，用于区分首次失败与尚在队列。刷新失败时 preview 仍保留最近成功版本。列表不返回完整备注、历史或 HTML 正文。

## 4. 快照与任务

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | /bookmarks/{id}/refresh | 单站立即刷新，返回两设备任务；关闭快照时返回 skipped 原因 |
| POST | /folders/{id}/refresh-preview | 显示子树 eligible/disabled/active 数量 |
| POST | /folders/{id}/refresh | 递归排队并去重；不依赖前端当前分页 |
| POST | /folders/all/refresh-preview | “全部网站”虚拟文件夹的全库 eligible/disabled/active 数量，包含未分类 |
| POST | /folders/all/refresh | 对全库排队并去重；快照关闭的网站跳过；分别生成桌面和手机任务 |
| GET | /job-groups | 当前收藏库持久化任务目录，分页返回 groups、active_count、pending_proposals；需要 owner 会话 |
| GET | /job-groups/{id} | 网站和设备任务进度、partial/failed/skipped；分类包含 pending_count |
| GET | /jobs/{id} | 脱敏状态与错误；不泄露进程参数或原始页面内容 |
| POST | /jobs/{id}/cancel | 取消排队或当前在途 attempt；worker 按该 attempt UUID 请求 launcher 终止并确认退出 |
| POST | /jobs/{id}/retry | 对当前输入重新验证后排队，创建新 attempt UUID/token，不沿用失效 lease 或 spool |
| GET | /bookmarks/{id}/snapshots?variant=desktop | 分页历史，标明最新成功版本 |
| GET/HEAD | /snapshots/{id}/preview | 鉴权 HTML 响应；HEAD 检查状态与内容类型且无正文；两种方法保持相同 sandbox/CSP 与 no-store |
| POST | /snapshots/delete-preview | 校验指定版本是否可清理并预览影响 |
| POST | /snapshots/bulk-delete | 批量删除旧版本，任何 current 指针被选中则整批拒绝 |

预览 URL 含版本 ID，内容不可变但鉴权逐次执行。响应使用 `Cache-Control: private, no-store`，避免退出后继续从缓存取得私有快照；备份下载同样鉴权。当前指针可变的接口不永久缓存。

JobAttempt 是内部执行身份，启动、取消和 launcher 退出通知均绑定 attempt UUID，而不是可重试的 Job UUID。发布快照时同库事务检查当前 attempt/token、generation/library_epoch、capture_epoch，写 SnapshotVersion 的唯一 capture_attempt_id、当前指针和成功终态；重复回报返回已提交结果，不生成第二个版本。

同源外层 iframe 导航带登录 cookie；其 sandbox 去掉脚本和同源权限，响应本身也施加 sandbox CSP，避免直接打开归档 URL 后获得应用源权限。完整策略见 operations-security.md。

## 5. AI 分类

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | /classification/batches | 输入明确网站 ID 或 folder_id；folder_id="all" 对全库（包括未分类）生成候选任务，仍需已验证的 AI 服务和人工校对 |

分类任务组 `total/completed/counts` 的单位是 AI 批次，每批最多 10 个网站。`website_total/website_completed` 为网站数量，`succeeded_sites/failed_sites/cancelled_sites/queued_sites/running_sites/retrying_sites` 细分网站状态；`jobs[].website_count` 为单批网站数量。没有运行中请求的待执行任务组返回 `queued`，全部活动请求均在等待重试时返回 `retry_wait`，不再统一显示为 `running`。

`GET /job-groups?page=1&page_size=20` 从业务库恢复进行中和已结束的任务组。`data.groups` 只包含 id、kind、source、created_at、requested_count、state、total、completed、counts、pending_count 摘要；不展开每组 jobs 或 AI 请求正文。排序为运行中、其他活动任务、已结束任务，各层按创建时间倒序。`data.active_count` 与 `data.pending_proposals` 是整库统计，不受当前页限制；`page.total` 为任务组数量。AI 结果经校验后立即保存为建议，网页轮询统计和校对列表；确认之前不会自动创建文件夹或移动网站。

`POST /jobs/{id}/retry` 创建或复用活动替代任务，并在单一业务事务内替换原任务组成员，使原进度继续追踪重试。共享任务更新其所有原任务组，不增加网站/批次分母。原任务仍保留历史状态；相同原任务重复重试在替代任务完成前去重。

对于 `retry_wait`，手动重试原地将同一任务改为 queued，立即到期并提升到手动操作优先级，清除旧错误和旧执行关联；下一次领取时创建新的 attempt。原任务编号、尝试历史和全部任务组关联保持不变。对已经 queued 的同一任务重复请求仅返回原编号；运行中任务返回 409，不中断正在执行的请求。重试前校验收藏库身份与采集设置，快照已关闭或采集版本失效时拒绝。界面错误行只显示红色错误原因，`retry_wait` 和 failed 行提供“重试”，正常排队/执行行提供“取消”。
| GET | /classification/proposals | pending/stale 等筛选；含网页、原目录、目标路径、创建目录与理由 |
| PATCH | /classification/proposals/{id} | 人工编辑目标目录，带 proposal revision |
| POST | /classification/accept | `{items:[{id,revision}],tree_revision}`；全批校验并事务执行 |
| POST | /classification/reject | 驳回指定方案，保留原网站和当前归属 |

`GET /classification/proposals?selection=true` 按同一 status/import_id/bookmark_id/group_id 范围返回所有页的 `{id,revision}`，不返回网页详情，不受 page/page_size 限制，最多 1000 项。前端在“选择当前页”旁提供“全选所有页”，保留跨页选择并用单次确认请求批准整个选中集合；选中集合取自点击时的建议快照，不自动加入之后新生成的建议。批量确认/驳回最多 1000 项；任何建议、网站分类版本或目录版本失效时整批拒绝，不分块提交。重复相同 Idempotency-Key 和请求参数返回原结果。

内部模型输出示例：

```json
{
  "items": [
    {
      "bookmark_id": "uuid",
      "target": {"existing_folder_id": "uuid"},
      "reason": "该站主要提供字体资源"
    },
    {
      "bookmark_id": "uuid-2",
      "target": {"parent_folder_id": "uuid", "new_segments": ["配色工具"]},
      "reason": "现有目录中缺少对应分类"
    }
  ]
}
```

两类 target 严格择一，拒绝未知字段、未知 ID、重复网站、非法层级或超过预算的目录创建。模型不返回 SQL、shell 命令或磁盘路径。客户端不能把任意模型 JSON 直接提交到执行端点；接受端点只处理已保存且仍有效的 proposal。

## 6. 导入、备份与恢复

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | /imports/preview | multipart HTML，解析为 ImportBatch/Entry，返回统计 |
| POST | /imports | multipart HTML；仅解析预览，返回 201/ImportBatch，与 /imports/preview 相同 |
| GET | /imports | 列出待确认预览、进行中流程及尚未自动提醒的已完成批次 |
| POST | /imports/{id}/execute | body 带预览返回的 confirmation_token；验证内容、工作量与 epoch 后执行 |
| GET | /imports/{id} | 进度与跳过/失败原因 |
| POST | /imports/{id}/review-notified | 可校对后记录已自动提醒；关闭弹窗后不再自动打开 |
| POST | /backups | 带 Idempotency-Key，在 state 创建 kind=backup 的 MaintenanceOperation，返回 202/operation_id，由控制器执行 |
| GET | /backups/{operation_id} | 查询相同 state 操作的阶段、实际大小、创建时间；只接受 kind=backup |
| GET | /backups/{operation_id}/download | 完成后鉴权流式下载对应产物；文件不是公开 static |
| POST | /restores/validate | 流式上传并登记 kind=restore 的 MaintenanceOperation，返回 202/operation_id；后台验证后通过 GET 查询清单、所需空间、兼容性和确认 token |
| POST | /restores/{operation_id}/execute | body 带验证 token 与覆盖确认，附 Idempotency-Key；同一 state 操作进入维护执行，返回 202/operation_id |
| GET | /restores/{operation_id} | 查询同一运行操作的阶段/结果；只接受 kind=restore，重启期间可能暂不可达 |
| POST | /restores/{operation_id}/cancel | 取消未执行的预览或未领取恢复，回收其源文件与候选；正在激活时拒绝 |
| GET | /restores/{operation_id}/pre-backup/download | 鉴权下载该次覆盖恢复前自动生成的备份；尚未生成时返回 404 |
| GET | /maintenance-operations/{operation_id} | 统一查询 state 中的 backup/restore/upgrade 阶段与脱敏结果；始终需要 owner 会话 |
| POST | /maintenance-operations/lookup | `{method,path,idempotency_key}`，按当前 owner 查询 state 幂等记录，返回原 operation_id/状态；查询只读但仍校验 CSRF |

两种恢复状态查询（专属 `/restores/{id}` 与统一 `/maintenance-operations/{id}`）均在 awaiting_confirmation 阶段返回短时 validation_token。令牌不写进幂等响应日志，重复轮询不轮换令牌或使前一个令牌失效。

所有入口在 Waitress 请求头阶段限制普通请求体为 1 MiB。更大 multipart 上传先以 owner 会话和 CSRF 调用 `POST /runtime/uploads`，提交完整 API `path` 和含 multipart 余量的 `size`，得到一次性 `upload_token`，随后置于 `X-Linkarium-Upload`。授权120秒内开始，只允许指定路径、大小和一个并发上传；入站大请求绝对时限最多1小时，断线和超时释放额度。HTML 文件仍最多20 MiB，multipart门禁21 MiB，恢复门禁16 GiB，并先预留同时缓冲、解析和持久副本的磁盘空间。

导入预览返回 `execution_plan`（entries/captures/ai_requests/can_execute/entry_limit/confirmation_token）。确认15分钟有效；按实际条目而非备份内汇总字段计算。每次执行最多1000个网站、2000个抓取任务、101次AI请求，一次只运行一个导入。每次AI实际出网前持久扣减，重试不能重置预算。旧未确认工作在升级后重新显示预览。

恢复授权针对具体上传包 hash 和当前库 epoch；包替换或库已被另一次恢复切换则 token 失效。校验失败不改变当前库。覆盖操作由控制器执行，视图不在请求线程里覆盖 SQLite。

备份和恢复共享 state 中的 MaintenanceOperation，互斥权由控制器领取，不依赖将替换的 library Job。上传校验进入 awaiting_confirmation 不自动覆盖；确认 token 有时限并绑定 package_sha256 和 expected_library_epoch。操作原始路径、内部 claim token、密钥及库 SQL 不通过查询接口返回；确认 token 在幂等日志中只保存摘要。升级同样使用维护实体，由部署终端触发，首版不新增公开升级接口。

### 维护断连与响应丢失

进入维护但 Web 尚运行时返回可识别 `503 MAINTENANCE`；Web/worker 停止期间可能连接失败或由入口返回 502/503，不能保证操作查询持续可用，也不能把断连显示为恢复失败。前端保留 operation_id 和 Idempotency-Key，按有界退避重连；恢复切换撤销会话后先重新登录，再查询运行库操作状态。

登记操作后若 202 响应丢失，重新登录后调用 `/maintenance-operations/lookup` 找回同一操作；同键同参重试登记只返回原响应/operation_id，不能再次覆盖。只查到明确终态才显示相应结论。幂等查询接口不执行请求，不能通过回放过期确认 token 重新触发维护；活动操作的关联记录保持到结束。操作状态可能为 uploading、validating、awaiting_confirmation、queued、running、succeeded、failed、cancelled、expired，stage 解释正在执行的具体步骤。

## 7. 原子性与失败表现

导入返回 `stage/progress/capture_total/capture_completed/capture_failed_sites/classification_total/classification_completed/ai_status/ai_error_code/review_ready/review_notified`。`succeeded` 只表示自动流程终结；未配置 AI 或分类服务失败仍进入人工校对。记录写入不再标记整个流程成功。`GET /classification/proposals` 支持 `import_id`、`bookmark_id`、`group_id` 范围过滤；建议返回 `manual` 和 `capture_failed`。

网站批量编辑及 AI 批量确认/驳回最多 1000 项，历史清理最多 100 项，单事务全部成功或明确失败。导入及多站抓取允许分项完成，返回逐项结果；不能宣称整个网络批次事务原子。目录删除影响预览和备份恢复确认属于产品中的具体操作流程，不是在开发阶段反复要求用户审批。

`REVISION_CONFLICT` 提供当前 revision；`PROPOSAL_STALE` 提供需重看的项目；`SNAPSHOT_DISABLED` 返回跳过原因；`CAPTURE_BLOCKED`、`CAPTURE_TIMEOUT`、`AI_UNCONFIGURED`、`AI_INVALID_RESULT` 等供前端映射可理解的文字，不展示内部实现细节。

实施时将这些契约固化为 OpenAPI 文件和前后端共享样例，契约测试覆盖状态码、权限和业务不变量，不只检查返回 JSON 的字段拼写。
