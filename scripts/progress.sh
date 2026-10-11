#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
umask 077
set -a
source "$project_dir/.env"
set +a
cd "$project_dir/relay"
exec python3 progress.py "$@"
