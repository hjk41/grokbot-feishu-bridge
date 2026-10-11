# Cutover and rollback

1. Prepare the inbound host, dedicated CLI profile, allowlists, private `.env`
   and reachable HTTPS callback server. Run the test suite before deployment.
2. Install `relay/worker_progress.py` in the cloud workspace and configure
   `prompts/standalone-routine.md` for the existing webhook routine. Preserve
   its URL/key and other routines. Do not print capabilities or credentials.
3. Wait until the old relay has no active or waiting work. Stop its consumers
   and bus; disable its full-mode autostart/keepalive. Set cloud `RELAY_ROLE`
   to `cloud-worker`. This role starts no listeners, heartbeats or local queue.
4. Start the inbound host. Verify exactly one event bus for this app, both
   message/card consumers and independent cloud-to-host HTTPS access.
5. Send real user requests and verify original-message association, one
   progress card and one summary, stages, terminal freeze and FIFO order.
   Repeat after cloud idle/suspension. Connection readiness and webhook HTTP
   success alone do not satisfy acceptance.

Rollback: stop inbound consumers and its dedicated app bus first, then restore
the former cloud configuration/prompt and start the former listener. Never run
two owners for the same app. Preserve both state directories for diagnosis;
do not copy waiting tasks to another host and replay them without reconciliation.

macOS launchd is a per-user login agent. `caffeinate -s` prevents idle sleep on
AC while it runs; lid closure, logout and shutdown still interrupt service.
For unattended Linux operation configure user lingering where appropriate.
