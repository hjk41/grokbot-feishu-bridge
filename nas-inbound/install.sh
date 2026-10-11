#!/usr/bin/env bash
# Generate a systemd unit for this checkout; never install/start as root implicitly.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$project_dir/runtime"
chmod 700 "$project_dir/runtime"
chmod +x "$project_dir/scripts/"*.sh
cat > "$project_dir/runtime/grokbot-feishu-bridge.service" <<EOF
[Unit]
Description=Grok Bot Feishu ingress
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=$project_dir
ExecStart=/bin/bash "$project_dir/scripts/run.sh"
Restart=on-failure
RestartSec=10
UMask=0077
[Install]
WantedBy=default.target
EOF
echo 'Generated runtime/grokbot-feishu-bridge.service. Install it as a user unit after configuring .env and stopping the former listener.'
