#!/usr/bin/env bash
# detect.sh — Report deepseek-harness (dsh) presence and the tokenless
# bridge-registration state. Read-only.
#
# Output: JSON on stdout, e.g.
#   {"installed": true, "dsh": "/usr/local/bin/dsh", "dsh_home": "/home/u/.dsh",
#    "profiles": ["web"], "bridge_registered": true}
# or {"installed": false, ...} with the fields that could be determined.
#
# Exit code: 0 either way (fail-open — plugin activation is controlled by
# capabilities, and dsh is commonly launched through `npx @deepseek-ai/dsh`,
# which a probe must never download or start).

set -euo pipefail

COMPONENT="${ANOLISA_COMPONENT:-tokenless}"
AGENT="${ANOLISA_TARGET:-deepseek-harness}"
ADAPTER_DIR="${ANOLISA_ADAPTER_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
PLUGIN_SRC="$ADAPTER_DIR/deepseek-harness"
DSH_HOME="${DSH_HOME:-${TOKENLESS_DSH_HOME:-$HOME/.dsh}}"

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

DSH_BIN="${DSH_BIN:-}"
if [ -z "$DSH_BIN" ]; then
    DSH_BIN="$(command -v dsh 2>/dev/null || true)"
fi

# Enumerate profiles (dsh keeps one directory per profile under
# $DSH_HOME/profiles; web/headless auto-initialize on first use).
PROFILES=()
if [ -d "$DSH_HOME/profiles" ]; then
    for profile_dir in "$DSH_HOME/profiles"/*/; do
        [ -d "$profile_dir" ] || continue
        PROFILES+=("$(basename "$profile_dir")")
    done
fi

# The adapter marks its cordis.patch.yml block with a stable comment so
# detect/install/uninstall can agree on ownership without parsing YAML.
BRIDGE_REGISTERED=false
for profile in ${PROFILES[@]+"${PROFILES[@]}"}; do
    patch_file="$DSH_HOME/profiles/$profile/cordis.patch.yml"
    if [ -f "$patch_file" ] && grep -q "# >>> ${COMPONENT} deepseek-harness hooks >>>" "$patch_file"; then
        BRIDGE_REGISTERED=true
        break
    fi
done

INSTALLED=false
if [ -n "$DSH_BIN" ] && [ "$BRIDGE_REGISTERED" = "true" ]; then
    INSTALLED=true
fi

emit() {
    # $1=installed $2=dsh_bin $3=profiles_csv
    if command -v jq &>/dev/null; then
        jq -n --argjson installed "$1" --arg dsh "$2" --arg home "$DSH_HOME" \
            --arg profiles "$3" --argjson registered "$BRIDGE_REGISTERED" \
            '{installed: $installed, dsh: $dsh, dsh_home: $home,
              profiles: (if $profiles == "" then [] else ($profiles | split(",")) end),
              bridge_registered: $registered}'
    else
        printf '{"installed": %s, "dsh": "%s", "dsh_home": "%s", "profiles": "%s", "bridge_registered": %s}\n' \
            "$1" "$2" "$DSH_HOME" "$3" "$BRIDGE_REGISTERED"
    fi
}

PROFILES_CSV=""
for profile in ${PROFILES[@]+"${PROFILES[@]}"}; do
    PROFILES_CSV="${PROFILES_CSV:+$PROFILES_CSV,}$profile"
done

emit "$INSTALLED" "${DSH_BIN:-}" "$PROFILES_CSV"
echo "[${COMPONENT}] ${AGENT} detect: installed=${INSTALLED} dsh=${DSH_BIN:-missing} profiles=${PROFILES_CSV:-none} bridge=${BRIDGE_REGISTERED}" >&2
exit 0
