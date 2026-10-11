# 运行与排错

## 首先判断哪一段失效

| 症状 | 检查 |
| --- | --- |
| 没有进度卡 | 入口主机是否在线；relay 是否运行；CLI ready；权限/版本发布；白名单与群 @ID |
| 有卡但不执行 | Webhook URL/key、routine 是否暂停、投递状态、同聊天前方任务 |
| 一直蓝卡 | routine 是否调用正确 helper；独立入口 HTTPS 回调是否可达；start 抢占是否成功；阶段是否更新 |
| 完成后卡还变化 | 是否启动了第二套心跳或另一个 relay；锁目录是否共享 |
| 重复卡或重复执行 | 是否手动重放了旧 pending；多个副本是否使用不同状态目录 |
| 停止按钮无效果 | operator/聊天白名单、是否原发起人、card 事件权限；执行器是否检查 stop_requested |

前台日志和 `lark-cli event status --json` 可以核对两个消费者、received/dropped 和总线状态。`ready` 只证明连接启动；至少一条真实用户消息收到卡片和正确结果才证明入站链路通过。无消息时 received=0 本身正常，不足以判断连接故障。

## 引用消息

引用回复会读取同一聊天里被明确引用的那一条消息，并把引用正文和用户本条指令分开传给 Grok。
支持文字、富文本和卡片的 CLI 文本表示，最多 6000 字符；不递归读取引用，也不下载附件。
只有 @Grok 且没有有效正文或引用时，入口直接提示补充内容，不派发空任务。
引用读取失败时说明缺失；引用正文仅作上下文，不授予新的操作权限。

明确引用的单条消息读取使用 bot 身份的官方 API，必须传
`card_msg_content_type=user_card_content`，否则飞书可能把 Card 2.0 正文替换为
“请升级至最新版本客户端，以查看内容”。返回该占位内容时明确标记引用读取失败。

## 群聊历史按需读取

按用户最新要求，Mac 入口不再读取两次 @ 之间的群聊历史，也不附加或暂存
`conversation_context`，不推进群聊边界，不消费旧卡片恢复内容。
新任务只传本条正文及明确引用；chat_id、message_id、create_time 和话题关联字段
仍传给 Grok，供它需要时自行通过飞书 CLI 查询历史。
执行授权仍只来自白名单用户当前明确 @ 的指令，普通聊天不触发任务。
已有任务和原先私密的群聊快照保留；已创建任务的完整输入及认领不会因升级被改写。

## 暂停、重启与恢复

Mac inbound 的 webhook 投递使用小控制包，先放 task_id、executable、callback，
完整用户正文和引用保存在该任务的 immutable pending 输入。
云执行器按控制包 progress_instruction 用任务令牌 GET callback.url + `/payload`，
核验 task_id、callback 和 canonical JSON 的 SHA-256，保存返回的完整 payload 后才执行。
该接口与阶段回调使用同一任务令牌及期限，不提供无认证下载，也不改写历史任务。
输入文件在投递前完成持久化，避免云执行器先读取而 Mac 尚未记录路径的竞态。
Webhook 200 和云 routine 的 status=ok 不等于任务 start/done 已成功回调；
启动超时需同时核对 Grok 的自动审核记录和 wake 内容，不能只重启 Mac。

## 飞书聊天对应独立 Grok 对话

Mac inbound 可设置 `GROK_CHAT_ISOLATION_ENABLED=1`，使用私密的
`GROK_CHAT_ROUTES_FILE` 固定映射 `chat_id → {agent_id, session_id}`。
当前个人 Grok 的原生接口拒绝任意新建 sessionId，且没有可验证的同 Bot 创建会话
接口；本部署为不同聊天配置不同的本人私有 Grok Bot，使用各自 main session。
飞书仍只有一个 @Grok，Grok App 中会出现用于其他聊天的独立 Bot。
原来的搜索群绑定原 Bot，保留原工作资料；其既有混合历史不会被删除或搬移。

同一群内的普通消息和话题使用同一持久对话；不同群和私聊使用不同对话。
隔离从切换后的新任务生效。显式引用保留；不恢复自动群聊历史注入。
新 Bot 不继承原 Bot 的聊天记忆，其电脑也可能为空。不会自动复制项目文件或密钥；
项目工作前需通过本人已连接的工具取得代码、读取交接资料，并核验所需授权。
同一账户的已连接服务和主动共享的文档仍可能共用，对话隔离不构成账户权限隔离。

首次配置，在加载可信 `.env` 后执行：

```bash
python3 scripts/provision_chat_routes.py --primary-chat oc_replace_with_original_search_chat
```

该命令保留原 Bot，为其他白名单聊天创建本人私有 temporal Bot，使用稳定 ID
恢复中断的配置，保存 mode 600 路由文件。不会复制密钥或旧聊天记录，也不会发送
群消息。完整映射、权限和运行模式校验通过后再启用开关并重启入口。
每个白名单聊天必须有唯一的原生对话；未配置、重复绑定、非本人 Bot、路由改变
都会拒绝派发，没有共用 webhook 的自动降级。任务投递前绑定并持久化原生身份，
完整输入仍通过认证 `/payload` 读取；原生消息附带执行和回调协议。
完整输入还携带当前小型 `worker_progress.py` 的源码及 SHA-256，执行者校验后
保存到自己的 mode 700 运行目录，使用该副本回调；无需先有桥接仓库或原 Bot 电脑文件。

派发调用 `SendGrokBotUserMessage`，消息 ID 由任务和原生对话确定，重试保持相同 ID。
网络丢 ACK 时先查询 `GetGrokBotSendStatus`；接受或重复投递只证明原生入口收件，
仍需实际 start/done。停止按钮更新原任务停止标记，执行器用原回调检查，不额外
注入会打断当前工作的原生 turn。重启保留路由、队列、任务身份和审批状态。
原 webhook 保留供历史排错；开关关闭是显式回滚，不提供聊天隔离。

云电脑 idle pause 会冻结所有本机线程。wall-clock/monotonic 差值检测只是恢复后尝试刷新同一 CLI 总线；无法保证识别所有暂停方式或补回遗漏事件。避免无依据定时杀消费者；两个消费者共用同一总线，主消费者先启动。

当前默认关闭“零收件很久”和固定最大年龄的额外刷新。若诊断需要，可设置 `FEISHU_BUS_ZOMBIE_UPTIME_SEC` / `FEISHU_BUS_MAX_UPTIME_SEC` 为正秒数；这只是启发式措施，不是可靠性保证。刷新会影响当前 CLI 应用的其他事件监听，所以应使用专用配置和运行用户。

电脑更新/恢复后检查 Python、CLI、配置及脚本，再重新 `bash scripts/run.sh`。持久目录的存在不证明进程仍运行。不要给实验部署写“24/7 保证”。

## 有范围的历史补收

已知一条真实用户消息漏收时，可以先查看其任务是否存在，再人工补收该 message_id：

```bash
bash scripts/run.sh ingest-history om_replace_with_real_message_id
# 群内：
bash scripts/run.sh ingest-history om_replace_with_real_message_id --chat-type group
```

此命令用官方 bot `+messages-mget` 读取原消息，再经过相同的授权/去重/排队。已有任务或已知 message_id 会跳过；被拒收会报错。不要批量盲扫旧聊天，不要凭模糊“可能没执行”自动重复副作用操作。运行着的 relay 与手工补收共享文件锁，但 seen 文件本身不是跨进程原子数据库；补收前先停掉 relay，并确认 CLI 消费者已退出，然后补收并恢复前台服务。

## 投递与超时

HTTP 超时可能发生在远端接受之后，自动网络重试可能产生多个 routine run。独立入口的执行者须用独立 payload 文件调用 start，已被其他 worker 抢占时返回 409，禁止继续执行。full 模式仍依赖本地状态检查。两种模式都不保证崩溃后的副作用 exactly-once。

派发声明落盘后、POST 前退出可能留下没有真正投递的任务。当前偏好避免盲目重复：先核对 routine 历史、pending 和任务状态，由操作者决定新建请求还是人工修复。不要直接删除队列文件让任务重跑。

启动超时与运行阶段超时结束的是卡片状态，不是底层进程。执行器必须配合停止，才可以对真实取消作结论。结果卡 API 失败也不会回滚已经完成的工作；查看任务里的发送结果后，人工恢复展示即可。

## Grok 原生权限审批（Mac 入口）

设置 `GROK_APPROVALS_ENABLED=1`，并配置 `.env.example` 中的原生 agent、
Mac 登录文件、审批操作人和其白名单私聊。观察器从现有 webhook 的 HTTPS origin
访问 GrokBotService；先验证当前登录者拥有该 agent，而且它包含当前 webhook 的
automation ID。运行和转发不依赖 NAS，不需要改变 routine 提示词或让执行器主动
上报审批。登录文件每次请求重新读取，不能把 webhook key 当作账户认证。

原生 transcript 分页扫描间隔默认 8 秒。只处理明确 `pending` 的原生审批；历史
approved/denied/always/expired 不补发。审批提出的命令含精确的本入口任务回调 URL，
且任务发起人/聊天通过授权时，卡片回复原消息并保留已有话题；无法明确关联的请求
只发送到操作人与 Grok 的白名单私聊，不能根据时间或“最近活跃群”猜测来源。
开启聊天隔离后，观察器同时扫描所有路由的原生对话及原 Bot 的默认对话；
新 Bot 只需核验本人所有权，不要求拥有旧 webhook。关联任务时还必须匹配任务
已绑定的 agent/session，卡片决定由所属 Bot 的观察器提交，不能跨聊天暂停或审批。

卡片提供“允许本次”和“拒绝”，不提供长期授权。点击须匹配操作人、聊天、卡片 ID、
请求 ID 和 nonce；提交前再次读取原生条目并核对仍待审批及内容摘要。决定直接调用
原生 ResolveGrokBotAutoReviewApproval / ResolveGrokBotLocalToolPermission，不派发
新 routine 或将批准文本交给模型。超过卡片展示长度的命令不提供允许按钮，请在
Grok App 完整查看后审批。命令、原因的凭据值会做脱敏，账户 token 不进入卡片。

私密状态保存在 `relay/approvals/`，卡片发送用稳定幂等键，重启不重复通知。在 App
处理后，观察器更新同一卡片并移除按钮。决定发送前持久记录意图；RPC 超时不能
证明未接受，因此不自动重试批准，等待原生状态同步或在 App 核对。完整历史扫描
确认条目已清除时关闭旧卡；历史读取失败或超出扫描期限时不推断审批结果。

明确关联的任务会显示等待审批并暂停启动/阶段超时；原生请求处理后恢复计时，不
反复刷新已完成审批的任务时间。没有确切关联的审批不擅自改写其他任务的状态。
观察器认证/网络失败仅记录固定错误标签，不影响消息监听、进度回调和停止按钮。
若 Mac 登录文件失效，重新登录相应账户，让现有客户端刷新文件，再查看日志中的
`native approvals connected` 或 `native approvals recovered`；不能以 HTTP `/health`
正常替代对原生审批连接的检查。上线时应核对真实 pending 数和下一次真实按钮操作。

## 私密诊断（日志）

日志、seen、任务、pending、processed、卡片 JSON 都可能含用户名、聊天 ID、请求正文和内部地址。`.gitignore` 默认排除它们，但这不使备份或终端输出自动安全。分享日志前先人工删减，再检查秘密。不要上传 `.env`、CLI 配置、keychain、桌面登录缓存或完整 workspace 归档。
