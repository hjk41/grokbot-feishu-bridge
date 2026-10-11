# 运行与排错

## 首先判断哪一段失效

| 症状 | 检查 |
| --- | --- |
| 没有进度卡 | 云电脑是否醒着；relay 是否运行；CLI ready；权限/版本发布；白名单与群 @ID |
| 有卡但不执行 | Webhook URL/key、routine 是否暂停、投递状态、同聊天前方任务 |
| 一直蓝卡 | routine 是否调用了辅助命令；是否进入实际执行；阶段是否更新 |
| 完成后卡还变化 | 是否启动了第二套心跳或另一个 relay；锁目录是否共享 |
| 重复卡或重复执行 | 是否手动重放了旧 pending；多个副本是否使用不同状态目录 |
| 停止按钮无效果 | operator/聊天白名单、是否原发起人、card 事件权限；执行器是否检查 stop_requested |

前台日志和 `lark-cli event status --json` 可以核对两个消费者、received/dropped 和总线状态。`ready` 只证明连接启动；至少一条真实用户消息收到卡片和正确结果才证明入站链路通过。无消息时 received=0 本身正常，不足以判断连接故障。

## 暂停、重启与恢复

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

HTTP 超时可能发生在远端接受之后，自动网络重试可能产生多个 routine run。routine 应核对任务状态与终态，不重复执行已结束工作；同时启动的执行器仍可能竞争，当前没有独立的分布式 worker 租约。

派发声明落盘后、POST 前退出可能留下没有真正投递的任务。当前偏好避免盲目重复：先核对 routine 历史、pending 和任务状态，由操作者决定新建请求还是人工修复。不要直接删除队列文件让任务重跑。

启动超时与运行阶段超时结束的是卡片状态，不是底层进程。执行器必须配合停止，才可以对真实取消作结论。结果卡 API 失败也不会回滚已经完成的工作；查看任务里的发送结果后，人工恢复展示即可。

## 私密诊断

日志、seen、任务、pending、processed、卡片 JSON 都可能含用户名、聊天 ID、请求正文和内部地址。`.gitignore` 默认排除它们，但这不使备份或终端输出自动安全。分享日志前先人工删减，再检查秘密。不要上传 `.env`、CLI 配置、keychain、桌面登录缓存或完整 workspace 归档。
