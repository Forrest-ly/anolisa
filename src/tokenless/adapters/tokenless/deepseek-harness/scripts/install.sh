#!/usr/bin/env bash
# install.sh — Register the tokenless hook bridge with deepseek-harness (dsh).
#
# dsh composes its plugin tree per profile: each profile directory under
# $DSH_HOME/profiles/<name> carries a package.json (out-of-tree plugin deps)
# and a cordis.patch.yml user patch layer (see the dsh CLI README). This
# installer therefore, for every targeted profile:
#
#   1. ensures the upstream hook-bridge plugin package
#      (@deepseek-ai/dsh-hooks-claude-code) is installed in the profile, and
#   2. appends a clearly-marked managed block to the profile's
#      cordis.patch.yml that loads the bridge with configPath/pluginRoot
#      pointing at this adapter's hooks.json / adapter directory.
#
# The patch entry is only written when the bridge package is actually
# resolvable in the profile: an unresolvable plugin row would fail dsh boot.
#
# Environment:
#   DSH_HOME                 dsh home (default: ~/.dsh)
#   TOKENLESS_DSH_HOME       test override for DSH_HOME
#   TOKENLESS_DSH_PROFILE    restrict registration to one profile name
#   DSH_BIN                  path to the dsh launcher (default: search PATH)
#   ANOLISA_DRY_RUN=1        print actions without changing anything
set -euo pipefail

AGENT="${ANOLISA_TARGET:-deepseek-harness}"
COMPONENT="${ANOLISA_COMPONENT:-tokenless}"
ADAPTER_DIR="${ANOLISA_ADAPTER_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
PLUGIN_SRC="$ADAPTER_DIR/deepseek-harness"
BRIDGE_PACKAGE="@deepseek-ai/dsh-hooks-claude-code"
MARK_BEGIN="# >>> ${COMPONENT} deepseek-harness hooks >>>"
MARK_END="# <<< ${COMPONENT} deepseek-harness hooks <<<"
DSH_HOME="${DSH_HOME:-${TOKENLESS_DSH_HOME:-$HOME/.dsh}}"

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

if [ ! -f "$PLUGIN_SRC/hooks/hooks.json" ]; then
    echo "[${COMPONENT}] ERROR: hooks.json not found: $PLUGIN_SRC/hooks/hooks.json" >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "[${COMPONENT}] ERROR: python3 is required by the tokenless hooks" >&2
    exit 1
fi

DSH_BIN="${DSH_BIN:-}"
if [ -z "$DSH_BIN" ]; then
    DSH_BIN="$(command -v dsh 2>/dev/null || true)"
fi

# --- profile selection -------------------------------------------------------
PROFILE_NAMES=()
if [ -n "${TOKENLESS_DSH_PROFILE:-}" ]; then
    PROFILE_NAMES=("$TOKENLESS_DSH_PROFILE")
elif [ -d "$DSH_HOME/profiles" ]; then
    for profile_dir in "$DSH_HOME/profiles"/*/; do
        [ -d "$profile_dir" ] || continue
        PROFILE_NAMES+=("$(basename "$profile_dir")")
    done
fi

if [ ${#PROFILE_NAMES[@]} -eq 0 ]; then
    echo "[${COMPONENT}] no dsh profiles found under ${DSH_HOME}/profiles — skipping ${AGENT} registration."
    echo "[${COMPONENT}] dsh creates the web/headless profiles on first use; re-run after 'dsh web' (or set TOKENLESS_DSH_PROFILE)."
    exit 0
fi

if [ "${ANOLISA_DRY_RUN:-0}" = "1" ]; then
    for profile in "${PROFILE_NAMES[@]}"; do
        echo "DRY-RUN: ensure ${BRIDGE_PACKAGE} in profile '${profile}'"
        echo "DRY-RUN: register tokenless hook block in ${DSH_HOME}/profiles/${profile}/cordis.patch.yml"
    done
    exit 0
fi

# --- helpers -------------------------------------------------------------------

bridge_present() {
    # $1 = profile name
    [ -d "$DSH_HOME/profiles/$1/node_modules/${BRIDGE_PACKAGE}" ]
}

ensure_bridge_package() {
    # $1 = profile name. Best effort: warn on failure, caller re-checks.
    local profile="$1"
    if bridge_present "$profile"; then
        return 0
    fi
    if [ -n "$DSH_BIN" ] && { [ -x "$DSH_BIN" ] || command -v "$DSH_BIN" >/dev/null 2>&1; }; then
        echo "[${COMPONENT}] installing ${BRIDGE_PACKAGE} into profile '${profile}'..."
        if "$DSH_BIN" plugin --profile "$profile" add "$BRIDGE_PACKAGE" >&2; then
            return 0
        fi
        echo "[${COMPONENT}] WARN: 'dsh plugin --profile ${profile} add ${BRIDGE_PACKAGE}' failed (offline?)." >&2
        return 1
    fi
    echo "[${COMPONENT}] WARN: dsh launcher not found — cannot install ${BRIDGE_PACKAGE} into profile '${profile}'." >&2
    echo "[${COMPONENT}]       Install dsh first, then re-run: dsh plugin --profile ${profile} add ${BRIDGE_PACKAGE}" >&2
    return 1
}

register_patch_block() {
    # $1 = profile name. Idempotent: replaces any previous managed block.
    local profile="$1"
    local patch_file="$DSH_HOME/profiles/$profile/cordis.patch.yml"
    local tmp

    mkdir -p "$DSH_HOME/profiles/$profile"
    touch "$patch_file"
    tmp="$(mktemp "${patch_file}.XXXXXX")"
    awk -v begin="$MARK_BEGIN" -v end="$MARK_END" '
        $0 == begin { skipping = 1; next }
        $0 == end   { skipping = 0; next }
        !skipping   { print }
    ' "$patch_file" > "$tmp"
    {
        cat "$tmp"
        printf '%s\n' "$MARK_BEGIN"
        printf -- '- id: tokenless-deepseek-harness-hooks\n'
        printf '  name: %s\n' "'${BRIDGE_PACKAGE}'"
        printf '  config:\n'
        printf '    configPath: %s\n' "$PLUGIN_SRC/hooks/hooks.json"
        printf '    pluginRoot: %s\n' "$PLUGIN_SRC"
        printf '%s\n' "$MARK_END"
    } > "${tmp}.new"
    mv "${tmp}.new" "$patch_file"
    rm -f "$tmp"
}

# --- main --------------------------------------------------------------------

REGISTERED=0
for profile in "${PROFILE_NAMES[@]}"; do
    if [ -n "${TOKENLESS_DSH_PROFILE:-}" ] && [ ! -d "$DSH_HOME/profiles/$profile" ]; then
        echo "[${COMPONENT}] ERROR: profile '${profile}' not found under ${DSH_HOME}/profiles" >&2
        exit 1
    fi
    if ! ensure_bridge_package "$profile"; then
        # Never write a plugin row the loader cannot resolve: an
        # unresolvable entry fails dsh boot, and a typo must not take the
        # agent down (same containment contract the bridge itself keeps).
        echo "[${COMPONENT}] skipping patch registration for profile '${profile}' (bridge package unresolved)." >&2
        continue
    fi
    register_patch_block "$profile"
    echo "[${COMPONENT}] registered tokenless hooks in profile '${profile}' (${DSH_HOME}/profiles/${profile}/cordis.patch.yml)."
    REGISTERED=$((REGISTERED + 1))
done

if [ "$REGISTERED" -eq 0 ]; then
    echo "[${COMPONENT}] ${AGENT}: no profile registered the tokenless hook bridge." >&2
    exit 1
fi

echo "[${COMPONENT}] ${AGENT} hook bridge registered in ${REGISTERED} profile(s). Restart dsh to activate."
echo "[${COMPONENT}] NOTE: the CC bridge honors tool-ready/stats today; rewrite and"
echo "[${COMPONENT}] response compression take effect once dsh honors updatedInput/updatedToolOutput."
