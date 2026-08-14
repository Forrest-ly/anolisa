#!/usr/bin/env bash
# uninstall.sh — Remove the tokenless hook-bridge registration from
# deepseek-harness (dsh) profiles.
#
# Removes the managed block this adapter wrote into each profile's
# cordis.patch.yml (identified by its marker comments). The bridge package
# itself is removed best-effort via the dsh launcher; a leftover package is
# inert once the patch entry is gone.
#
# Environment:
#   DSH_HOME                 dsh home (default: ~/.dsh)
#   TOKENLESS_DSH_HOME       test override for DSH_HOME
#   TOKENLESS_DSH_PROFILE    restrict removal to one profile name
#   DSH_BIN                  path to the dsh launcher (default: search PATH)
#   ANOLISA_DRY_RUN=1        print actions without changing anything
set -euo pipefail

AGENT="${ANOLISA_TARGET:-deepseek-harness}"
COMPONENT="${ANOLISA_COMPONENT:-tokenless}"
BRIDGE_PACKAGE="@deepseek-ai/dsh-hooks-claude-code"
MARK_BEGIN="# >>> ${COMPONENT} deepseek-harness hooks >>>"
MARK_END="# <<< ${COMPONENT} deepseek-harness hooks <<<"
DSH_HOME="${DSH_HOME:-${TOKENLESS_DSH_HOME:-$HOME/.dsh}}"

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

if [ ! -d "$DSH_HOME/profiles" ]; then
    echo "[${COMPONENT}] ${AGENT}: no profiles under ${DSH_HOME}/profiles — nothing to remove."
    exit 0
fi

PROFILE_NAMES=()
if [ -n "${TOKENLESS_DSH_PROFILE:-}" ]; then
    PROFILE_NAMES=("$TOKENLESS_DSH_PROFILE")
else
    for profile_dir in "$DSH_HOME/profiles"/*/; do
        [ -d "$profile_dir" ] || continue
        PROFILE_NAMES+=("$(basename "$profile_dir")")
    done
fi

DSH_BIN="${DSH_BIN:-}"
if [ -z "$DSH_BIN" ]; then
    DSH_BIN="$(command -v dsh 2>/dev/null || true)"
fi

REMOVED=0
for profile in ${PROFILE_NAMES[@]+"${PROFILE_NAMES[@]}"}; do
    patch_file="$DSH_HOME/profiles/$profile/cordis.patch.yml"
    [ -f "$patch_file" ] || continue
    if ! grep -qF "$MARK_BEGIN" "$patch_file"; then
        continue
    fi
    if [ "${ANOLISA_DRY_RUN:-0}" = "1" ]; then
        echo "DRY-RUN: remove tokenless hook block from $patch_file"
        continue
    fi
    tmp="$(mktemp "${patch_file}.XXXXXX")"
    awk -v begin="$MARK_BEGIN" -v end="$MARK_END" '
        $0 == begin { skipping = 1; next }
        $0 == end   { skipping = 0; next }
        !skipping   { print }
    ' "$patch_file" > "$tmp"
    mv "$tmp" "$patch_file"
    echo "[${COMPONENT}] removed tokenless hook block from profile '${profile}'."
    REMOVED=$((REMOVED + 1))

    # Best-effort package removal; a failure here leaves an inert package.
    if [ -n "$DSH_BIN" ] && { [ -x "$DSH_BIN" ] || command -v "$DSH_BIN" >/dev/null 2>&1; }; then
        "$DSH_BIN" plugin --profile "$profile" remove "$BRIDGE_PACKAGE" >&2 2>/dev/null || true
    fi
done

if [ "$REMOVED" -eq 0 ]; then
    echo "[${COMPONENT}] ${AGENT}: no managed tokenless hook block found — nothing to remove."
else
    echo "[${COMPONENT}] ${AGENT} hook bridge removed from ${REMOVED} profile(s). Restart dsh to deactivate."
fi
