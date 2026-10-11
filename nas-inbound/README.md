# Independent inbound host

This package merges Grok Bot's standalone `inbound` / `cloud-worker` roles
and adds the missing cross-host task callbacks. The inbound host owns Feishu
credentials, cards, the FIFO and heartbeats; cloud routines execute tasks.

Configure the root `.env` with `RELAY_ROLE='inbound'`, allowlists and a public
HTTPS `BRIDGE_PUBLIC_URL`. Keep it mode 600. Python 3.10+ and an authenticated
Lark CLI are required on the inbound host. A dedicated `FEISHU_PROFILE` leaves
other app profiles intact.

On macOS use `scripts/install-launchd.sh --prepare-only` to prepare the plist
without starting listeners, then run it without the flag after cutover. On Linux `install.sh` generates a
user service in `runtime/`; install it into your user systemd directory, then
enable it only after completing [cutover](CUTOVER.md). For manual operation
run `bash nas-inbound/run.sh`.

Keep task state on durable local storage. Expose only the loopback callback
server through HTTPS. See the [VPS reverse proxy and outbound SSH tunnel](../docs/reverse-proxy.md)
deployment; set its public HTTPS base URL in `.env`. Preserve existing proxy routes.
The public endpoint supports task-scoped bearer authentication; `/health`
contains no account or task data. `healthcheck.sh` does not prove user ingress.

Docker is not supplied as a working deployment: the upstream compose used a
Python image without Node, Lark CLI or authentication. Install the CLI and its
credential store on the actual inbound host instead.
