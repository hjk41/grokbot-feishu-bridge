#!/usr/bin/env bash
# Install for the current logged-in macOS user. Configuration stays in .env.
set -euo pipefail
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --prepare-only ) ]]; then
  echo 'Usage: install-launchd.sh [--prepare-only]' >&2
  exit 2
fi
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
label=com.grokbot.feishu-bridge
plist="$HOME/Library/LaunchAgents/$label.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$project_dir/runtime"
chmod 700 "$project_dir/runtime"
chmod +x "$project_dir/scripts/"*.sh
"${PYTHON_BIN:-python3}" - "$project_dir" "$plist" "$label" <<'PY'
import os, plistlib, sys
root, path, label = sys.argv[1:]
data = {'Label': label,
        'ProgramArguments': ['/usr/bin/caffeinate', '-s', '/bin/bash', root + '/scripts/run.sh'],
        'WorkingDirectory': root, 'RunAtLoad': True, 'KeepAlive': True,
        'ThrottleInterval': 10,
        'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'},
        'StandardOutPath': root + '/runtime/relay.log',
        'StandardErrorPath': root + '/runtime/relay-error.log'}
with open(path, 'wb') as out:
    plistlib.dump(data, out)
os.chmod(path, 0o600)
PY
if [[ "${1:-}" == --prepare-only ]]; then
  echo "Prepared $label; service has not been started."
  exit 0
fi
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
# launchd may still be removing the old registration after bootout returns.
for attempt in 1 2 3 4 5; do
  if bootstrap_error="$(launchctl bootstrap "gui/$(id -u)" "$plist" 2>&1)"; then
    break
  fi
  if [[ "$attempt" == 5 ]]; then
    printf '%s\n' "$bootstrap_error" >&2
    exit 1
  fi
  sleep 2
done
launchctl kickstart "gui/$(id -u)/$label"
echo "Installed $label; logs are in runtime/."
