# Changelog

## 0.1.0

Initial experimental open-source extraction:

- Feishu/Lark bot events → Grok Bot webhook routine → reply-linked progress and result cards.
- Persistent per-chat FIFO, dispatch claims, process locks, relay-owned heartbeats and terminal freeze.
- Configurable CLI and webhook; fail-closed user/chat allowlists and explicit group mentions.
- Cooperative stop request bound to task, card, chat and original sender.
- Generic setup/routine templates, architecture and recovery documentation, privacy audit and Linux tests.

Limitations: shared-filesystem Linux deployment; cloud idle pauses ingress; no remote callback protocol,
no exactly-once execution, no hard cancellation, no media inputs or deterministic approval workflow.
