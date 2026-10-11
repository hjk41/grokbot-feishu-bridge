#!/usr/bin/env bash
# Mac-originated reverse TCP tunnel; the VPS HTTPS proxy targets its loopback port.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
umask 077
set -a
source "$project_dir/.env"
set +a
for name in TUNNEL_HOST TUNNEL_USER TUNNEL_IDENTITY_FILE TUNNEL_KNOWN_HOSTS_FILE; do
  if [[ -z "${!name:-}" ]]; then echo "Configure $name first" >&2; exit 2; fi
done
if [[ ! -f "$TUNNEL_IDENTITY_FILE" || ! -f "$TUNNEL_KNOWN_HOSTS_FILE" ]]; then
  echo 'Configure the dedicated identity and verified host-key file first.' >&2
  exit 2
fi
for port in "${TUNNEL_REMOTE_PORT:-18765}" "${BRIDGE_PORT:-8765}" "${TUNNEL_SSH_PORT:-22}"; do
  if [[ ! "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
    echo 'Invalid tunnel port' >&2; exit 2
  fi
done
exec /usr/bin/ssh -4 -N -T -a -F /dev/null \
  -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$TUNNEL_KNOWN_HOSTS_FILE" \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes -o ConnectTimeout=15 \
  -i "$TUNNEL_IDENTITY_FILE" -p "${TUNNEL_SSH_PORT:-22}" \
  -R "127.0.0.1:${TUNNEL_REMOTE_PORT:-18765}:127.0.0.1:${BRIDGE_PORT:-8765}" \
  "$TUNNEL_USER@$TUNNEL_HOST"
