# HTTPS reverse proxy and outbound SSH tunnel

An HTTPS tunnel provides a route into a private host through a connection
initiated by that host. A reverse proxy accepts HTTP requests and forwards them
to an upstream it can reach. They solve different parts of the path and can be
used together. This deployment uses your own VPS Nginx HTTPS endpoint plus a
Mac-originated OpenSSH reverse tunnel; it does not require Tailscale Funnel.

```text
Cloud worker → public HTTPS reverse proxy → VPS loopback port
                                             ↓ encrypted SSH tunnel
                                      Mac loopback callback server
```

Use a dedicated unprivileged VPS account and key. Restrict its SSH authorization
to remote forwarding of one loopback port (for example 18765), with no shell,
PTY, agent/X11 forwarding or other forwarding targets. Preserve the server's
`GatewayPorts no`. Read the [OpenSSH forwarding](https://man.openbsd.org/ssh#R)
and [authorized-key options](https://man.openbsd.org/sshd#AUTHORIZED_KEYS_FILE_FORMAT)
documentation for your installed version. Transfer only the public key to the
VPS; keep the private key mode 600 on the Mac, outside the repository.

Obtain the VPS host key through an already authenticated administration channel
and pin it in a dedicated known-hosts file. Do not disable host-key verification
or trust an unauthenticated ssh-keyscan result alone.

Add these fields to the private root `.env`:

```bash
BRIDGE_PUBLIC_URL='https://proxy.example/grokbridge'
TUNNEL_HOST='proxy.example'
TUNNEL_USER='bridge-tunnel'
TUNNEL_SSH_PORT='22'
TUNNEL_REMOTE_PORT='18765'
TUNNEL_IDENTITY_FILE="$HOME/.ssh/grokbridge_tunnel"
TUNNEL_KNOWN_HOSTS_FILE="$HOME/.ssh/grokbridge_known_hosts"
```

Add a location to the existing HTTPS server, preserving its other routes:

```nginx
location ^~ /grokbridge/ {
    client_max_body_size 64k;
    access_log off;
    proxy_pass http://127.0.0.1:18765/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_connect_timeout 5s;
    proxy_read_timeout 90s;
    proxy_send_timeout 90s;
    proxy_cache off;
    add_header Cache-Control "no-store" always;
}
```

The trailing slash strips `/grokbridge/` before forwarding to the callback
server. Validate with `nginx -t` before reloading. HTTPS certificates must be
trusted by the cloud worker. Where an existing CDN terminates HTTPS, use its
existing verified setup and ensure the callback route is not cached/challenged;
do not turn off TLS checks in the worker.

On Mac install the tunnel separately using `bash scripts/install-launchd-tunnel.sh`.
SSH detects a dead connection with keepalives; launchd restarts the process.
Check both the public `/grokbridge/health` and unauthenticated task response
(401) from the cloud worker. Stop the old listener before starting the Mac
relay, following [cutover](../nas-inbound/CUTOVER.md). A working tunnel does
not itself prove message ingress or task execution.

Also enable server-side liveness checks for the dedicated tunnel account.
Client-side ServerAlive alone cannot release a server listener held by an
unreachable old client. Append a `Match User <tunnel-user>` block at the end
of the server's sshd configuration with `ClientAliveInterval 15` and
`ClientAliveCountMax 3`; preserve other accounts' configuration, validate
with `sshd -t` and effective `sshd -T -C user=...`, then reload SSH.
For `remote port forwarding failed` plus a timed-out upstream, inspect
the exact loopback listener and owning SSH account/process first. Terminate
only the verified stale tunnel connection; do not kill processes by port
without checking ownership. launchd will establish a fresh connection.
