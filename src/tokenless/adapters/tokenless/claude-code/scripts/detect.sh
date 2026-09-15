#!/usr/bin/env bash
# detect.sh — Inspect Claude Code presence and the tokenless plugin state.
# Read-only. Tri-state exit aligns with openclaw/hermes detect.sh:
#   0 = installed and ready
#   1 = not installed but installable (prereqs OK)
#   2 = missing prerequisites
set -euo pipefail

COMPONENT="${ANOLISA_COMPONENT:-tokenless}"
AGENT="${ANOLISA_TARGET:-claude-code}"
ADAPTER_DIR="${ANOLISA_ADAPTER_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"

PLUGIN_ID="${COMPONENT}@anolisa-${COMPONENT}"
PLUGIN_SRC="$ADAPTER_DIR/claude-code"

CLAUDE_BIN="${CLAUDE_BIN:-}"
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# First-run settling retries: right after provisioning, the claude binary or
# the plugin registry may be transiently invisible on the very first detect.sh
# execution (filesystem/PATH init timing race). settle() only retries checks
# that report a retryable failure (exit status 1); checks that succeed or
# report a definitive result return immediately, so steady-state runs stay
# fast.
DETECT_RETRIES="${TOKENLESS_DETECT_RETRIES:-3}"
DETECT_RETRY_DELAY="${TOKENLESS_DETECT_RETRY_DELAY:-1}"
# Separate, smaller budget for one result that only looks definitive: a
# `plugin list` that succeeds but omits the plugin. `claude plugin install`
# writes marketplace.json/plugin.json, yet the CLI's plugin registry index
# only picks them up on a later scan, so the very first list right after
# provisioning can succeed and still omit the just-installed plugin (GH
# #3082). Kept apart from DETECT_RETRIES so a genuinely absent plugin pays a
# bounded settle rather than the whole settling budget.
#
# GH #3267: a bare attempt count is not load-adaptive. Under concurrent load
# the registry index lags longer than two cheap re-lists, the count budget is
# exhausted, and the false "not installed" verdict of GH #3082 comes back.
# The budget is therefore expressed as a wall-clock settle window
# (DETECT_PLUGIN_SETTLE_SECONDS, integer seconds measured with $SECONDS) with
# DETECT_PLUGIN_RELISTS kept as a hard attempt ceiling, and the gap between
# re-lists backs off instead of staying constant. A slow host then spends the
# same window on fewer, better-spaced probes instead of burning it on calls
# that all land before the index has refreshed. Both limits are checked
# before each re-list, so the window is a soft ceiling: the loop can overshoot
# it by at most one backoff step plus one in-flight `plugin list`.
DETECT_PLUGIN_RELISTS="${TOKENLESS_DETECT_PLUGIN_RELISTS:-5}"
DETECT_PLUGIN_SETTLE_SECONDS="${TOKENLESS_DETECT_PLUGIN_SETTLE_SECONDS:-15}"
DETECT_PLUGIN_RELIST_DELAY_MAX="${TOKENLESS_DETECT_PLUGIN_RELIST_DELAY_MAX:-4}"

# settle <cmd...> — run cmd once; if it reports a retryable failure (exit
# status 1), sleep DETECT_RETRY_DELAY and retry, up to DETECT_RETRIES retries
# (that is, at most 1 + DETECT_RETRIES attempts in total). Exit status 0 is
# success. Any other exit status is a definitive result and is returned
# immediately without further retries. Returns the final exit status.
# Callers must therefore reserve exit status 1 for conditions that a retry
# may still resolve.
settle() {
    local retry=0 rc=0
    "$@"; rc=$?
    while [ "$rc" -eq 1 ] && [ "$retry" -lt "$DETECT_RETRIES" ]; do
        retry=$((retry + 1))
        sleep "$DETECT_RETRY_DELAY"
        "$@"; rc=$?
    done
    return "$rc"
}

# plugin_relist_delay <n> — backoff before the n-th re-list: the base retry
# delay doubled per attempt and capped at DETECT_PLUGIN_RELIST_DELAY_MAX. awk
# does the arithmetic so a fractional base delay (the retry test uses 0.5)
# keeps working; without awk the constant base delay is used, i.e. the
# pre-GH #3267 behaviour.
plugin_relist_delay() {
    local delay=""
    delay="$(awk -v base="$DETECT_RETRY_DELAY" -v n="$1" \
        -v cap="$DETECT_PLUGIN_RELIST_DELAY_MAX" \
        'BEGIN { d = base * (2 ^ (n - 1)); printf "%g\n", (d > cap ? cap : d) }' \
        2>/dev/null)" || delay=""
    [ -n "$delay" ] || delay="$DETECT_RETRY_DELAY"
    printf '%s\n' "$delay"
}

line()  { printf '[%s] %s\n' "$COMPONENT" "$*"; }
field() { printf '[%s]   %-26s %s\n' "$COMPONENT" "$1" "$2"; }

PREREQ_MISSING=()
INSTALL_MISSING=()
note_prereq_missing()  { PREREQ_MISSING+=("$1"); }
note_install_missing() { INSTALL_MISSING+=("$1"); }

# Set when `claude plugin list` omitted the plugin but the installer's own
# settings record confirmed it (GH #3267): the registry index was stale, which
# is worth reporting because it is otherwise invisible and intermittent.
PLUGIN_INDEX_STALE=0

find_claude_bin() {
    CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
    [ -n "$CLAUDE_BIN" ]
}

if [ -z "$CLAUDE_BIN" ]; then
    # PATH/filesystem may still be settling right after install; re-check
    # briefly before declaring the CLI missing. A genuinely absent CLI cannot
    # be distinguished from that settling race, so it spends one retry budget
    # (set TOKENLESS_DETECT_RETRIES=0 to opt out).
    settle find_claude_bin || true
fi

line "${AGENT} detect"
if [ -n "$CLAUDE_BIN" ] && [ -x "$CLAUDE_BIN" ]; then
    CLAUDE_VER="$("$CLAUDE_BIN" --version 2>/dev/null | awk '{print $1}' || echo unknown)"
    field "claude CLI"        "present (${CLAUDE_BIN}, v${CLAUDE_VER})"
else
    field "claude CLI"        "missing"
    note_prereq_missing "claude CLI"
fi

# Informational only: claude creates ~/.claude on first run; absence is
# not a prerequisite failure. Check once, without settling retries: nothing in
# this script creates the directory before `claude plugin list` runs, and that
# call carries its own retry budget and initializes ~/.claude itself, so
# retrying this probe would only delay the report.
if [ -d "$HOME/.claude" ]; then
    field "claude config dir" "present ($HOME/.claude)"
else
    field "claude config dir" "missing (created on first claude run)"
fi

if [ -f "$PLUGIN_SRC/.claude-plugin/marketplace.json" ]; then
    field "marketplace.json"  "present"
else
    field "marketplace.json"  "missing"
    note_prereq_missing "marketplace.json"
fi

if [ -f "$PLUGIN_SRC/.claude-plugin/plugin.json" ]; then
    field "plugin.json"       "present"
else
    field "plugin.json"       "missing (run: make stamp-adapter-templates)"
fi

# plugin_manifests_staged — true when the local manifests that
# `claude plugin install` consumes (the single-plugin marketplace plus the
# stamped plugin manifest) are both on disk. Their presence is what makes an
# omitted plugin ambiguous: the installer had something to register, so the
# CLI's registry index may simply not have caught up with it yet.
plugin_manifests_staged() {
    [ -f "$PLUGIN_SRC/.claude-plugin/marketplace.json" ] \
        && [ -f "$PLUGIN_SRC/.claude-plugin/plugin.json" ]
}

# plugin_enabled_in_settings — positive confirmation from the state that
# `claude plugin install` writes synchronously: the enabledPlugins record in
# ~/.claude/settings.json (the same record uninstall.sh cleans up when the CLI
# is gone). The registry index behind `claude plugin list` is refreshed lazily
# and can lag that write by seconds under load, so when the two disagree the
# settings record is the stronger signal (GH #3267).
#
# Strictly a positive signal: a missing file, a malformed file, an absent jq,
# an absent record or a disabled one proves nothing and must fall through to
# the re-list budget below rather than flip the verdict. A disabled plugin in
# particular stays "not installed" so that the install flow re-enables it.
plugin_enabled_in_settings() {
    local settings="$HOME/.claude/settings.json"
    [ -f "$settings" ] || return 1
    if command -v jq &>/dev/null; then
        jq -e --arg id "$PLUGIN_ID" '(.enabledPlugins // {})[$id] == true' \
            "$settings" &>/dev/null
        return $?
    fi
    # No jq: match the enabledPlugins record literally. $PLUGIN_ID
    # ("$COMPONENT@anolisa-$COMPONENT") is quoted in the record and differs
    # from the marketplace name this adapter registers, so neither that name
    # nor any other adapter's plugin can produce a false match; requiring the
    # `true` value keeps this path as strict as the jq one.
    grep -Eq "\"$PLUGIN_ID\"[[:space:]]*:[[:space:]]*true" "$settings"
}

# claude_plugin_listed — probe the plugin registry, using the settle()
# exit-status contract: 0 = plugin listed; 1 = `claude plugin list` itself
# failed (the CLI may still be initializing ~/.claude on first run, so a
# retry may still succeed); 2 = `plugin list` ran successfully but did not
# list the plugin.
claude_plugin_listed() {
    local listing
    if ! listing="$("$CLAUDE_BIN" plugin list 2>&1)"; then
        return 1
    fi
    if printf '%s\n' "$listing" | grep -qF "$PLUGIN_ID"; then
        return 0
    fi
    # An omission is only *probably* definitive: the index may not have caught
    # up with the installer yet. Confirm against the installer's own record
    # before reporting the plugin absent.
    if plugin_enabled_in_settings; then
        PLUGIN_INDEX_STALE=1
        return 0
    fi
    return 2
}

# settle_plugin_listed — settle() for the plugin probe, extended with a
# bounded re-list of the "list succeeded but omitted the plugin" case.
#
# With nothing staged on disk that omission is definitive — there was no
# plugin for the registry to index — so it is returned immediately (status
# 2) without spending any budget. With the manifests staged it is also the
# shape of the GH #3082 first-run race, where the registry index lags one
# scan behind the installer and the just-installed plugin is missing from an
# otherwise successful list. Re-list to ride out that refresh window, bounded
# by DETECT_PLUGIN_RELISTS attempts and by DETECT_PLUGIN_SETTLE_SECONDS of
# wall clock (GH #3267), with a backing-off gap between attempts; a genuinely
# absent plugin still ends up reported as "not installed", only later.
#
# Like settle(), this must be called in a condition context so that `set -e`
# stays suppressed while the probe reports a non-zero status.
settle_plugin_listed() {
    local relist=0 rc=0 start="${SECONDS:-0}"
    settle claude_plugin_listed; rc=$?
    while [ "$rc" -eq 2 ] && [ "$relist" -lt "$DETECT_PLUGIN_RELISTS" ] \
        && [ "$((${SECONDS:-0} - start))" -lt "$DETECT_PLUGIN_SETTLE_SECONDS" ] \
        && plugin_manifests_staged; do
        relist=$((relist + 1))
        sleep "$(plugin_relist_delay "$relist")"
        settle claude_plugin_listed; rc=$?
    done
    return "$rc"
}

if [ -n "$CLAUDE_BIN" ] && [ -x "$CLAUDE_BIN" ]; then
    # First-run race: `claude plugin list` may transiently fail while the CLI
    # initializes ~/.claude (status 1), and may transiently omit a plugin
    # whose manifests were staged only moments ago (status 2). Both are
    # retried within their own bounded budgets, and "not installed" is
    # reported only once those budgets are exhausted.
    if settle_plugin_listed; then
        field "plugin install"    "installed ($PLUGIN_ID)"
        if [ "$PLUGIN_INDEX_STALE" -eq 1 ]; then
            field "plugin registry index" \
                "stale (confirmed via ~/.claude/settings.json)"
        fi
    else
        field "plugin install"    "not installed"
        note_install_missing "$PLUGIN_ID"
    fi
fi

if [ -f "$PLUGIN_SRC/hooks/run-hook.sh" ]; then
    field "hook dispatcher"   "present"
else
    field "hook dispatcher"   "missing (hooks/run-hook.sh)"
    note_prereq_missing "hook dispatcher"
fi

if command -v python3 &>/dev/null; then
    field "python3"           "present ($(command -v python3))"
else
    field "python3"           "missing"
    note_prereq_missing "python3"
fi

# jq is required by tool_ready_hook.sh; absence disables that hook only
# (rewrite + compress-response still work). Treat as informational.
if command -v jq &>/dev/null; then
    field "jq"                "present ($(command -v jq))"
else
    field "jq"                "missing (tool-ready hook disabled)"
fi

runtime_bin="$(command -v tokenless 2>/dev/null || true)"
if [ -n "$runtime_bin" ]; then
    field "tokenless binary"  "present (${runtime_bin})"
else
    field "tokenless binary"  "missing"
    note_prereq_missing "tokenless binary"
fi

rtk_bin="$(command -v rtk 2>/dev/null || true)"
if [ -n "$rtk_bin" ]; then
    field "rtk binary"        "present (${rtk_bin})"
else
    field "rtk binary"        "missing"
    note_prereq_missing "rtk binary"
fi

# Shared hook scripts live under FHS; warn when missing so user knows to run
# `make install` (or install the RPM) before adapter actually fires.
SHARED_HOOKS_DIR=""
for d in /usr/local/share/anolisa/adapters/tokenless/common/hooks \
         /usr/share/anolisa/adapters/tokenless/common/hooks \
         "$HOME/.local/share/anolisa/adapters/tokenless/common/hooks"; do
    if [ -d "$d" ]; then SHARED_HOOKS_DIR="$d"; break; fi
done
if [ -n "$SHARED_HOOKS_DIR" ]; then
    field "shared hooks dir"  "present ($SHARED_HOOKS_DIR)"
else
    field "shared hooks dir"  "missing (run: make -C src/tokenless install)"
    note_prereq_missing "shared hooks dir"
fi

if [ ${#PREREQ_MISSING[@]} -gt 0 ]; then
    line "${AGENT}: missing prerequisites (${PREREQ_MISSING[*]})"
    exit 2
fi
if [ ${#INSTALL_MISSING[@]} -gt 0 ]; then
    line "${AGENT}: not installed (ready to install)"
    exit 1
fi
line "${AGENT}: ready"
exit 0
