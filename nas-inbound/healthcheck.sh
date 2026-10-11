#!/usr/bin/env bash
# Transport health only; real user-message acceptance is a separate check.
set -euo pipefail
curl --fail --silent --show-error "http://127.0.0.1:${BRIDGE_PORT:-8765}/health"
