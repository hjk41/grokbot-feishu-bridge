#!/usr/bin/env bash
# Independent ingress; one owner for the Feishu event bus and task directory.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec /bin/bash "$project_dir/scripts/run.sh" "$@"
