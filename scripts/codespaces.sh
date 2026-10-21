#!/usr/bin/env bash
# Compatibility for existing Codespaces terminals and saved startup commands.
set -Eeuo pipefail
exec bash "$(dirname "${BASH_SOURCE[0]}")/codespace.sh" "$@"
