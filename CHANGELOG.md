# Changelog

## 0.2.0 (unreleased)

- Merge the Grok Bot standalone inbound/cloud-worker roles and independent deployment structure.
- Add task-scoped authenticated HTTPS callbacks, persistent worker claims and idempotent terminal retries.
- Keep state, card updates, heartbeats and FIFO on the inbound host; cloud helper needs no Feishu credentials.
- Add macOS launchd installation, Linux user-service generation and dedicated CLI profile support.
- Reject invalid roles; cloud-worker launches no local queue or event listeners.
- Add transport/authentication/FIFO and role regression coverage.
- Pass card content as literal JSON for CLI 1.0.96 compatibility; remove unsupported @file content references.

Still experimental: host availability, callback reachability and routine behavior require real acceptance tests;
hard cancellation and exactly-once execution are not provided.

## 0.1.0

Initial experimental open-source extraction:

- Feishu/Lark bot events → Grok Bot webhook routine → reply-linked progress and result cards.
- Persistent per-chat FIFO, dispatch claims, process locks, relay-owned heartbeats and terminal freeze.
- Configurable CLI and webhook; fail-closed user/chat allowlists and explicit group mentions.
- Cooperative stop request bound to task, card, chat and original sender.
- Generic setup/routine templates, architecture and recovery documentation, privacy audit and Linux tests.

Limitations: shared-filesystem Linux deployment; cloud idle pauses ingress; no remote callback protocol,
no exactly-once execution, no hard cancellation, no media inputs or deterministic approval workflow.
