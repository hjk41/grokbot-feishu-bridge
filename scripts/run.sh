#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
umask 077
if [[ ! -f "$project_dir/.env" ]]; then
  echo 'Copy .env.example to .env and configure it first.' >&2
  exit 2
fi
# Trusted user-maintained shell config; never source untrusted input.
set -a
source "$project_dir/.env"
set +a
cd "$project_dir/relay"
exec python3 -u relay.py "$@"
