#!/usr/bin/env bash
# uninstall.sh — Remove tokenless AgentScope middleware.
#
# Removes the ``anolisa_tokenless_agentscope`` symlink from the active Python
# environment's user site-packages directory. The tokenless binary and stats
# database are intentionally left untouched because they are shared with other
# adapters.
#
# Usage:
#   ./uninstall.sh

set -euo pipefail

AGENT="${ANOLISA_TARGET:-agentscope}"
COMPONENT="${ANOLISA_COMPONENT:-tokenless}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 2>/dev/null || true)}"
PACKAGE_NAME="anolisa_tokenless_agentscope"

line() { printf '[%s] %s\n' "$COMPONENT" "$*"; }

line "Uninstalling ${AGENT} middleware..."

if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    line "WARNING: python3 not found — cannot locate site-packages directory." >&2
    exit 0
fi

SITE_DIR="$("$PYTHON_BIN" -m site --user-site)"
LINK_PATH="$SITE_DIR/$PACKAGE_NAME"

if [ -L "$LINK_PATH" ]; then
    rm -f "$LINK_PATH"
    line "Removed symlink: $LINK_PATH"
else
    line "No installed symlink found at: $LINK_PATH"
fi

line "Uninstall complete."
