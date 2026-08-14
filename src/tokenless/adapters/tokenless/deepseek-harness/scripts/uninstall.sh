#!/usr/bin/env bash
# uninstall.sh — Remove the tokenless hook bridge registration for
# deepseek-harness (dsh) from $DSH_HOME/cordis.patch.yml.
#
# The managed block is bounded by anolisa-tokenless markers; anything outside
# the markers is user content and is preserved verbatim. When nothing but
# comments/blank lines would remain, the file is deleted instead: dsh treats
# an absent patch file as "no user layer", while a comments-only file would
# fail dsh boot ("parses to nothing, not to a list").
#
# The @deepseek-ai/dsh-hooks-claude-code package installed under
# $DSH_HOME/node_modules is intentionally left in place — other profiles or
# plugins may use it. Remove it with:
#   npm --prefix "$HOME/.dsh" uninstall @deepseek-ai/dsh-hooks-claude-code
set -euo pipefail

AGENT="${ANOLISA_TARGET:-deepseek-harness}"
COMPONENT="${ANOLISA_COMPONENT:-tokenless}"

DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
PATCH_FILE="$DSH_HOME/cordis.patch.yml"
MANAGED_MARKER="anolisa-tokenless:deepseek-harness-bridge"
START_MARKER="# >>> ${MANAGED_MARKER} >>>"
END_MARKER="# <<< ${MANAGED_MARKER} <<<"

echo "[${COMPONENT}] Uninstalling ${AGENT} hook bridge..."

if [ ! -f "$PATCH_FILE" ] || ! grep -qF "$MANAGED_MARKER" "$PATCH_FILE"; then
    echo "[${COMPONENT}] ${AGENT}: no tokenless bridge registered in ${PATCH_FILE} — nothing to do."
    exit 0
fi

tmp="$(mktemp "${PATCH_FILE}.XXXXXX")"
awk -v s="$START_MARKER" -v e="$END_MARKER" '
    index($0, s) { skip = 1; next }
    index($0, e) { skip = 0; next }
    !skip { print }
' "$PATCH_FILE" > "$tmp"

# Keep the file only if real YAML entries remain outside the managed block.
# A comments-only patch file throws at dsh boot, so fall back to deletion.
if grep -qE '^[[:space:]]*-' "$tmp"; then
    mv "$tmp" "$PATCH_FILE"
    echo "[${COMPONENT}] removed tokenless bridge block from ${PATCH_FILE} (user entries preserved)."
else
    rm -f "$tmp"
    rm -f "$PATCH_FILE"
    echo "[${COMPONENT}] removed ${PATCH_FILE} (only the tokenless bridge block remained)."
fi

echo "[${COMPONENT}] Restart dsh to deactivate. The bridge package under"
echo "[${COMPONENT}] ${DSH_HOME}/node_modules was left in place (may be shared)."
