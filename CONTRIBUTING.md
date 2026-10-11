# 贡献说明

欢迎改进可靠性、CLI 版本兼容、跨主机进度协议和文档。

1. 使用 Linux、Python 3.10+。测试无需账号、网络或生产配置。
2. 运行 `python3 -m unittest discover -s relay/tests -v`。
3. 运行 `python3 scripts/audit_public_tree.py --git-history`，人工检查 staged diff。
4. 写清触发条件、修改后的行为、测试，以及哪些外部集成尚未实测。

禁止提交真实用户/聊天/应用 ID、密钥、私有 IP/域名、绝对用户目录、登录提取代码、原始运行日志或聊天内容。请使用明显虚构的 `ou_example` / `oc_example` / `om_example`。

不要用模拟传输通过来声称真实端到端验收。不要把连接 ready、HTTP 200、进度卡终态或停止标记混同于真实工作完成。跨主机部署需要明确认证、共享状态替代、重放防护和故障语义，不能只改一个路径。
