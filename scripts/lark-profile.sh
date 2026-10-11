#!/usr/bin/env bash
# The relay owns a dedicated profile; never change the CLI's active profile.
set -euo pipefail
exec "${LARK_CLI:-lark-cli}" --profile "${FEISHU_PROFILE:?Configure FEISHU_PROFILE}" "$@"
