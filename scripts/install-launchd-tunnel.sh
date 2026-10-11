#!/usr/bin/env bash
# Install a separately supervised tunnel for the current logged-in macOS user.
set -euo pipefail
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --prepare-only ) ]]; then
  echo 'Usage: install-launchd-tunnel.sh [--prepare-only]' >&2; exit 2
fi
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
label=com.grokbot.feishu-bridge.tunnel
plist="$HOME/Library/LaunchAgents/$label.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$project_dir/runtime"
chmod 700 "$project_dir/runtime"
"${PYTHON_BIN:-python3}" - "$project_dir" "$plist" "$label" <<'PY'
import os, plistlib, sys
root, path, label = sys.argv[1:]
data = {'Label': label,
        'ProgramArguments': ['/usr/bin/caffeinate', '-s', '/bin/bash', root + '/scripts/ssh-tunnel.sh'],
        'WorkingDirectory': root, 'RunAtLoad': True, 'KeepAlive': True,
        'ThrottleInterval': 10,
        'StandardOutPath': root + '/runtime/tunnel.log',
        'StandardErrorPath': root + '/runtime/tunnel-error.log'}
with open(path, 'wb') as out: plistlib.dump(data, out)
os.chmod(path, 0o600)
PY
if [[ "${1:-}" == --prepare-only ]]; then
  echo "Prepared $label; tunnel has not been started."; exit 0
fi
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl kickstart "gui/$(id -u)/$label"
echo "Installed $label; logs are in runtime/."
