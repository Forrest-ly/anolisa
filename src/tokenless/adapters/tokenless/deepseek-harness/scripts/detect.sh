#!/usr/bin/env bash
# detect.sh — Inspect deepseek-harness (dsh) presence and the tokenless
# hook-bridge state. Read-only.
#
# Always exits 0 (fail-open, like codex/scripts/detect.sh): dsh is in
# developer preview and detection must never block `make adapter-install`
# or RPM transactions. The report below is informational; capability
# gating happens through manifest.json.
set -euo pipefail

COMPONENT="${ANOLISA_COMPONENT:-tokenless}"
AGENT="${ANOLISA_TARGET:-deepseek-harness}"
ADAPTER_DIR="${ANOLISA_ADAPTER_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"

ADAPTER_SRC="$ADAPTER_DIR/deepseek-harness"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
PATCH_FILE="$DSH_HOME/cordis.patch.yml"
BRIDGE_PKG="@deepseek-ai/dsh-hooks-claude-code"
MANAGED_MARKER="anolisa-tokenless:deepseek-harness-bridge"

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

line()  { printf '[%s] %s\n' "$COMPONENT" "$*"; }
field() { printf '[%s]   %-26s %s\n' "$COMPONENT" "$1" "$2"; }

DSH_BIN="${DSH_BIN:-}"
if [ -z "$DSH_BIN" ]; then
    DSH_BIN="$(command -v dsh 2>/dev/null || true)"
fi

line "${AGENT} detect"
DSH_PKG="@deepseek-ai/dsh"
if [ -n "$DSH_BIN" ] && [ -x "$DSH_BIN" ]; then
    DSH_VER="$("$DSH_BIN" --version 2>/dev/null | head -1 || echo unknown)"
    field "dsh CLI"           "present (${DSH_BIN}, ${DSH_VER})"
elif command -v npm &>/dev/null && npm ls -g --depth=0 "$DSH_PKG" >/dev/null 2>&1; then
    field "dsh CLI"           "npm global install (binary not on PATH)"
else
    field "dsh CLI"           "missing (npm i -g ${DSH_PKG})"
fi

if [ -d "$DSH_HOME" ]; then
    field "dsh home"          "present ($DSH_HOME)"
else
    field "dsh home"          "missing (created on first dsh run)"
fi

# The Claude Code hook bridge plugin must resolve from the dsh home (Node
# parent-walk from any profile dir passes through $DSH_HOME/node_modules).
if [ -f "$DSH_HOME/node_modules/$BRIDGE_PKG/package.json" ]; then
    field "cc hook bridge"    "present ($BRIDGE_PKG)"
elif command -v npm &>/dev/null && npm ls -g --depth=0 "$BRIDGE_PKG" >/dev/null 2>&1; then
    field "cc hook bridge"    "present (npm global)"
else
    field "cc hook bridge"    "missing ($BRIDGE_PKG)"
fi

if [ -f "$PATCH_FILE" ] && grep -qF "$MANAGED_MARKER" "$PATCH_FILE"; then
    field "tokenless bridge"  "registered ($PATCH_FILE)"
elif [ -f "$PATCH_FILE" ]; then
    field "tokenless bridge"  "not registered ($PATCH_FILE exists)"
else
    field "tokenless bridge"  "not registered"
fi

if [ -f "$ADAPTER_SRC/hooks/hooks.json" ]; then
    field "hooks.json"        "present"
else
    field "hooks.json"        "missing"
fi

if [ -e "$ADAPTER_SRC/hooks/run-hook.sh" ]; then
    field "hook dispatcher"   "present"
else
    field "hook dispatcher"   "missing (hooks/run-hook.sh)"
fi

if command -v python3 &>/dev/null; then
    field "python3"           "present ($(command -v python3))"
else
    field "python3"           "missing"
fi

runtime_bin="$(command -v tokenless 2>/dev/null || true)"
if [ -n "$runtime_bin" ]; then
    field "tokenless binary"  "present (${runtime_bin})"
else
    field "tokenless binary"  "missing"
fi

# Fail-open: report only, never block.
line "${AGENT}: detect complete (informational)"
exit 0
