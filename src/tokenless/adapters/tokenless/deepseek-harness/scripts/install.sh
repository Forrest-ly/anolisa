#!/usr/bin/env bash
# install.sh — Register the tokenless hook bridge for deepseek-harness (dsh).
#
# dsh composes every profile as patch layers over an empty root config; the
# user-facing extension point is $DSH_HOME/cordis.patch.yml (the profile root
# cordis.yml is rewritten on every boot — "edit cordis.patch.yml, not this
# file"). This script appends a managed `insert` block that loads the official
# @deepseek-ai/dsh-hooks-claude-code bridge pointed at this adapter's
# Claude-Code-shaped hooks.json.
#
# Fail-open conventions (kept consistent with the other adapters):
#   - no dsh CLI            -> informational no-op, exit 0
#   - no npm / install fail -> warn and exit 0 WITHOUT registering the patch
#     entry, because an unresolvable plugin entry would fail dsh boot loudly
#   - hook scripts only run while dsh executes tools, so a partial install
#     can never block the agent
set -euo pipefail

AGENT="${ANOLISA_TARGET:-deepseek-harness}"
COMPONENT="${ANOLISA_COMPONENT:-tokenless}"
ADAPTER_DIR="${ANOLISA_ADAPTER_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"

ADAPTER_SRC="$ADAPTER_DIR/deepseek-harness"
HOOKS_JSON="$ADAPTER_SRC/hooks/hooks.json"
DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
PATCH_FILE="$DSH_HOME/cordis.patch.yml"
BRIDGE_PKG="@deepseek-ai/dsh-hooks-claude-code"
BRIDGE_PKG_DIR="$DSH_HOME/node_modules/@deepseek-ai/dsh-hooks-claude-code"
MANAGED_MARKER="anolisa-tokenless:deepseek-harness-bridge"
START_MARKER="# >>> ${MANAGED_MARKER} >>>"
END_MARKER="# <<< ${MANAGED_MARKER} <<<"

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

echo "[${COMPONENT}] Installing ${AGENT} hook bridge..."

# 1. Require the dsh CLI. Graceful no-op keeps `make adapter-install` usable
#    on machines without deepseek-harness (same contract as claude-code).
DSH_PKG="@deepseek-ai/dsh"
DSH_BIN="${DSH_BIN:-}"
if [ -z "$DSH_BIN" ]; then
    DSH_BIN="$(command -v dsh 2>/dev/null || true)"
fi
# npm-global installs whose bin dir is not on PATH still count: the bridge
# registration is config-file based and does not need the binary itself.
if [ -z "$DSH_BIN" ] && command -v npm &>/dev/null \
        && npm ls -g --depth=0 "$DSH_PKG" >/dev/null 2>&1; then
    DSH_BIN="(npm global)"
fi
if [ -z "$DSH_BIN" ]; then
    echo "[${COMPONENT}] dsh CLI not found — skipping ${AGENT} install."
    echo "[${COMPONENT}] Install deepseek-harness first (npm i -g ${DSH_PKG}), then re-run."
    exit 0
fi

# 2. Require our own hook resources (shipped with the adapter payload).
if [ ! -f "$HOOKS_JSON" ] || [ ! -e "$ADAPTER_SRC/hooks/run-hook.sh" ]; then
    echo "[${COMPONENT}] ERROR: adapter hook resources missing under $ADAPTER_SRC" >&2
    echo "[${COMPONENT}]        Reinstall the tokenless adapter resources first." >&2
    exit 1
fi

# 3. Ensure the Claude Code hook bridge plugin resolves from the dsh home.
#    Bare plugin specifiers in config resolve through Node's parent-walk from
#    the profile directory, which passes through $DSH_HOME/node_modules.
if [ ! -f "$BRIDGE_PKG_DIR/package.json" ]; then
    if ! command -v npm &>/dev/null; then
        echo "[${COMPONENT}] WARN: npm not found — cannot install ${BRIDGE_PKG}." >&2
        echo "[${COMPONENT}] WARN: skipping registration; install the bridge package into" >&2
        echo "[${COMPONENT}] WARN: ${DSH_HOME} manually and re-run this installer." >&2
        exit 0
    fi
    echo "[${COMPONENT}] installing ${BRIDGE_PKG} into ${DSH_HOME}..."
    install -d -m 0755 "$DSH_HOME"
    if ! npm --prefix "$DSH_HOME" install --no-audit --no-fund "$BRIDGE_PKG"; then
        echo "[${COMPONENT}] WARN: failed to install ${BRIDGE_PKG}." >&2
        echo "[${COMPONENT}] WARN: skipping registration so dsh boot cannot break;" >&2
        echo "[${COMPONENT}] WARN: re-run this installer once the network/registry works." >&2
        exit 0
    fi
fi
if [ ! -f "$BRIDGE_PKG_DIR/package.json" ]; then
    echo "[${COMPONENT}] WARN: ${BRIDGE_PKG} still unresolved after npm install." >&2
    echo "[${COMPONENT}] WARN: skipping registration (an unresolvable plugin entry" >&2
    echo "[${COMPONENT}] WARN: would fail dsh boot)." >&2
    exit 0
fi

# 4. Register the managed patch block (idempotent: drop a stale block first so
#    a re-install refreshes relocated adapter paths).
install -d -m 0755 "$DSH_HOME"
if [ -f "$PATCH_FILE" ] && grep -qF "$MANAGED_MARKER" "$PATCH_FILE"; then
    tmp="$(mktemp "${PATCH_FILE}.XXXXXX")"
    awk -v s="$START_MARKER" -v e="$END_MARKER" '
        index($0, s) { skip = 1; next }
        index($0, e) { skip = 0; next }
        !skip { print }
    ' "$PATCH_FILE" > "$tmp"
    mv "$tmp" "$PATCH_FILE"
fi

# Ensure the file ends with a newline before appending (a missing trailing
# newline would merge our first comment line into the previous YAML line).
if [ -s "$PATCH_FILE" ] && [ -n "$(tail -c 1 "$PATCH_FILE")" ]; then
    printf '\n' >> "$PATCH_FILE"
fi

{
    printf '%s\n' "$START_MARKER"
    printf '# tokenless hook bridge for deepseek-harness (managed by anolisa tokenless).\n'
    printf '# Bridge-mode capabilities: tool-ready + stats attribution. rewrite and\n'
    printf '# compress-* cannot save tokens until dsh honors updatedInput/updatedToolOutput.\n'
    printf -- '- insert:\n'
    printf '    - id: tokenless\n'
    printf "      name: '%s'\n" "$BRIDGE_PKG"
    printf '      config:\n'
    printf '        configPath: %s\n' "$HOOKS_JSON"
    printf '        pluginRoot: %s\n' "$ADAPTER_SRC"
    printf '%s\n' "$END_MARKER"
} >> "$PATCH_FILE"

echo "[${COMPONENT}] registered tokenless bridge in ${PATCH_FILE}"
echo "[${COMPONENT}] Restart dsh to activate (profile patch layers reload on boot)."
