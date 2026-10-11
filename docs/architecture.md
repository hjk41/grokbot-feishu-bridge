# 架构与协议

## 当前实现：同一工作区

`relay.py` 消费 lark-cli 输出的**扁平 NDJSON**，不是直接处理飞书原始 webhook envelope。修改 CLI 版本时先检查 `event schema`；不要把原始飞书 HTTP payload 直接传给 `on_message`。

消息在身份、聊天、消息类型和群内明确 @检查通过后，按原 message_id 去重。先保存任务并回复进度卡，再入队，避免扫描线程删掉尚未创建的任务。只有队首能在聊天锁→任务锁顺序下持久抢占，然后释放锁进行网络 POST。阶段命令仅写终态和推进通知，下一条派发由 relay 执行。

状态机：

```mermaid
stateDiagram-v2
  [*] --> ack: 收件并创建卡片
  ack --> waiting: 前方有任务
  ack --> dispatching: 抢占队首
  waiting --> dispatching: 前任务终态后抢占
  dispatching --> queued: webhook 接受
  dispatching --> running: 执行器先于 ACK 启动
  queued --> running: start
  running --> done: done
  running --> failed: fail
  dispatching --> failed: 投递失败
  queued --> timeout: 启动超时
  running --> timeout: 阶段长期无更新
```

`done`、`failed`、`timeout` 冻结卡片；晚到的心跳或 webhook ACK 不可回退状态。停止请求是一项独立标记，不是终态。超时没有底层强制取消能力，因此“单个队首”不等同于“所有真实执行永远不重叠”。

## Webhook payload 示例

以下都是虚构值。标识符来自部署者自己的飞书应用。

```json
{
  "kind": "message",
  "event_id": "event_example",
  "message_id": "om_example",
  "chat_id": "oc_example",
  "chat_type": "p2p",
  "sender_open_id": "ou_example",
  "sender_type": "user",
  "msg_type": "text",
  "text": "查看工作目录，不修改文件",
  "task_id": "example_task",
  "progress_message_id": "om_progress_example",
  "queue_position": 0,
  "executable": true
}
```

实际还有原消息关联字段、执行说明等。`task_id` 用原 message_id 的 SHA-256 前 16 位生成，重收同一消息不会另建卡。传输身份由 Webhook bearer key 验证；payload 中的用户 ID 本身不是独立的密码。

## 文件职责

| 文件 / 目录 | 职责 |
| --- | --- |
| `relay.py` | 事件接收、授权、投递、唯一心跳、连接恢复与队列扫描 |
| `settings.py` | 入口白名单、消息类型、群 @身份、配置校验 |
| `progress.py` | Card 2.0 构建、CLI API 传输、任务锁和阶段命令 |
| `chat_queue.py` | 聊天 FIFO、持久抢占、重启恢复和终态推进 |
| `model_resolve.py` | 保留兼容的内部元数据验证；公开模型页脚关闭 |
| `tasks/` | 任务与进度状态、任务锁 / 聊天锁 |
| `queues/` | 每个聊天的队列及推进通知 |
| `pending/` | 已经可执行的 webhook 输入；等待任务不会写入这里 |
| `processed/` | 执行后归档的输入 |
| `seen_ids.txt` | 重启后消息去重记录 |

运行目录必须持久保存且不公开。源代码可以重新拉取，任务目录不可当作临时 build 输出清理。去重缓存保留最近 5000 项；它不是永远完整的消息数据库。

## 全天候入口：尚未实现的扩展

云电脑上的 relay 不能在云电脑暂停时唤醒自身。需要独立的常开 ingress：

1. 常开主机持久收件、授权、排队，立即通过飞书 API 回复进度卡。
2. ingress 通过 routine 的 webhook 唤起 Grok。
3. Grok 通过 HTTPS、任务绑定的认证回调上报阶段和终态；ingress 持有任务状态和卡片操作权限。
4. ingress 验证回调身份、task_id、状态迁移和防重放，结束后推进 FIFO。

这里列的是协议演进方向，不是当前可启动的第二种模式。现有 `progress.py` 操作本地文件，无法在另一台机器读取 ingress 的任务状态。也不应通过公网共享整个状态目录或把飞书凭据放到 webhook payload。
