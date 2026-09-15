#!/usr/bin/env bash
# Regression test for the detect.sh first-run settling retries.
#
# Background: GH #2512 reported test_detect_installed_framework_ready
# [claude-code] flaking in nightly runs. The reported cause was a
# filesystem/PATH initialization timing race on the first detect.sh
# execution right after provisioning: the claude binary under
# $HOME/.local/bin and the $HOME/.claude state dir are transiently
# invisible. #2512 itself was closed as a test-side flaky misclassification
# (issuecomment-5288391676), but the detect.sh-side weakness it exposed is
# real, and only one exit-status-affecting check can actually race: the
# claude binary lookup. A binary that is not yet visible makes detect.sh
# exit 2 ("missing prerequisites"), which is exactly what turns a
# "framework ready" assertion into a failure. (The $HOME/.claude config-dir
# probe is informational only and never changes the exit status.)
#
# Scenario 1 therefore reproduces that real failure path: the claude binary
# becomes visible in $HOME/.local/bin only after a delay (simulated with a
# background provisioner), and $HOME/.claude does not exist until the CLI's
# first `plugin list`. With settling retries, detect.sh must ride out the
# race and report ready (exit 0); without retries (scenario 2) the same
# first execution must fail with exit 2, proving the retries are what fix
# it. The remaining scenarios pin the plugin probe's retry semantics.
#
# GH #3082 reported a second, distinct first-run race: `claude plugin
# install` writes the marketplace/plugin manifests, but the CLI's plugin
# registry index only picks them up on a later scan, so the very first
# `plugin list` right after provisioning succeeds (exit 0) and still omits
# the just-installed plugin. detect.sh used to treat that omission as
# definitive and report "not installed" (exit 1) on the first run, while the
# second run correctly reported ready. It now re-lists that one case a
# bounded number of times (scenarios 4-5), but only while the local
# manifests are staged — with nothing on disk for the installer to have
# registered, the omission really is definitive and must not spend any
# budget (scenario 6). Scenario 3 pins the cost of that re-listing in the
# ordinary pre-install state, and scenario 7 keeps the pre-existing rule
# that an outright failing `plugin list` is transient and is retried.
#
# GH #3267 is the same race under concurrent load, where the index lag
# outlasts a small fixed re-list budget: the #3085 mitigation reduced the
# frequency but kept returning the false "not installed" verdict (80% of
# runs under load). detect.sh now confirms an omitted plugin against the
# enabledPlugins record that `claude plugin install` writes synchronously to
# ~/.claude/settings.json, which settles the question without waiting for
# the index at all (scenario 8); that record is a positive signal only, so a
# different plugin, a disabled record and a malformed file all still fall
# through to the bounded re-list budget (scenarios 9-11). The budget itself
# is now a wall-clock window with a backing-off gap and a larger attempt
# ceiling, which rides out a longer lag (scenario 12, with the pre-fix
# ceiling as its control) without letting a large ceiling run away
# (scenario 13) and without hammering the CLI in one burst (scenario 14).
#
# detect.sh reads the manifests and the hook dispatcher from
# $ANOLISA_ADAPTER_DIR, so every scenario points it at a synthetic adapter
# tree the test controls; no scenario depends on the state of the checked-out
# source tree (whether plugin.json has been stamped, for instance).

set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DETECT="$SCRIPT_DIR/../adapters/tokenless/claude-code/scripts/detect.sh"
TEST_DIR="$(mktemp -d)"

FAKE_HOME="$TEST_DIR/home"
FAKE_BIN="$FAKE_HOME/.local/bin"
SHARED_HOOKS="$FAKE_HOME/.local/share/anolisa/adapters/tokenless/common/hooks"
PLUGIN_ID="tokenless@anolisa-tokenless"
CALL_LOG="$TEST_DIR/claude-calls.log"
STUB_MODE_FILE="$TEST_DIR/stub-mode"
FLAKY_MARKER="$TEST_DIR/flaky-marker"
# Synthetic adapter tree handed to detect.sh through ANOLISA_ADAPTER_DIR, so
# the scenarios control whether marketplace.json / plugin.json are staged.
FAKE_ADAPTER_DIR="$TEST_DIR/adapters/tokenless"
FAKE_PLUGIN_SRC="$FAKE_ADAPTER_DIR/claude-code"
LATE_COUNT_FILE="$TEST_DIR/late-count"
LATE_OMISSIONS=1
PROVISIONER_PID=""

cleanup() {
    if [ -n "$PROVISIONER_PID" ]; then
        kill "$PROVISIONER_PID" 2>/dev/null || true
        wait "$PROVISIONER_PID" 2>/dev/null || true
    fi
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

mkdir -p "$FAKE_BIN" "$SHARED_HOOKS"

# detect.sh also requires `tokenless` and `rtk` on PATH (their absence is a
# prerequisite failure). Provide isolated stubs so this test is
# self-contained: run_detect restricts PATH to the fake home plus system
# directories, and a fresh runner or detached worktree may not have either
# binary installed globally.
printf '#!/bin/sh\nexit 0\n' >"$FAKE_BIN/tokenless"
printf '#!/bin/sh\nexit 0\n' >"$FAKE_BIN/rtk"
chmod +x "$FAKE_BIN/tokenless" "$FAKE_BIN/rtk"

fail() {
    echo "FAIL: $1" >&2
    printf '%s\n' "${2:-}" >&2
    exit 1
}

# Stub claude CLI. `plugin list` behaviour is steered via $STUB_MODE_FILE:
#   ready  — the list succeeds and contains the tokenless plugin (default)
#   absent — the list succeeds but does not contain the plugin
#   flaky  — the first `plugin list` call fails while the registry
#            initializes; later calls succeed
#   late   — the first $LATE_OMISSIONS `plugin list` calls succeed but
#            omit the plugin (GH #3082: the registry index has not caught up
#            with the manifests the installer just wrote); later calls list it
# Like the real CLI, `plugin list` creates $HOME/.claude when it first
# runs; the config dir does not exist before that. Every invocation is
# appended to $CALL_LOG so the tests can count CLI calls.
install_claude_stub() {
    cat >"$FAKE_BIN/claude" <<'STUB'
#!/bin/sh
printf '%s\n' "${1:-}" >>"$CALL_LOG"
mode=ready
[ -f "$STUB_MODE_FILE" ] && mode=$(cat "$STUB_MODE_FILE")
case "${1:-}" in
--version)
    echo "claude 9.9.9-test"
    ;;
plugin)
    if [ "$mode" = "absent" ]; then
        echo "NAME                          STATUS"
        exit 0
    fi
    if [ "$mode" = "flaky" ] && [ ! -f "$FLAKY_MARKER" ]; then
        : >"$FLAKY_MARKER"
        echo "initializing plugin registry" >&2
        exit 1
    fi
    if [ "$mode" = "late" ]; then
        n=$(cat "$LATE_COUNT_FILE" 2>/dev/null || echo 0)
        n=$((n + 1))
        echo "$n" >"$LATE_COUNT_FILE"
        if [ "$n" -le "$LATE_OMISSIONS" ]; then
            mkdir -p "$HOME/.claude"
            echo "NAME                          STATUS"
            exit 0
        fi
    fi
    mkdir -p "$HOME/.claude"
    echo "NAME                          STATUS"
    echo "tokenless@anolisa-tokenless   enabled"
    ;;
*)
    exit 0
    ;;
esac
STUB
    chmod +x "$FAKE_BIN/claude"
}

stage_adapter() { # stage_adapter <marketplace:yes|no> <plugin-json:yes|no>
    # Build the synthetic adapter tree detect.sh inspects. The hook dispatcher
    # is always present (its absence is a prerequisite failure that would mask
    # the plugin-probe behaviour under test).
    rm -rf "$FAKE_ADAPTER_DIR"
    mkdir -p "$FAKE_PLUGIN_SRC/.claude-plugin" "$FAKE_PLUGIN_SRC/hooks"
    printf '#!/bin/sh\nexit 0\n' >"$FAKE_PLUGIN_SRC/hooks/run-hook.sh"
    chmod +x "$FAKE_PLUGIN_SRC/hooks/run-hook.sh"
    if [ "$1" = yes ]; then
        printf '{\n  "name": "anolisa-tokenless",\n  "plugins": []\n}\n' \
            >"$FAKE_PLUGIN_SRC/.claude-plugin/marketplace.json"
    fi
    if [ "$2" = yes ]; then
        printf '{\n  "name": "tokenless",\n  "version": "0.0.0-test"\n}\n' \
            >"$FAKE_PLUGIN_SRC/.claude-plugin/plugin.json"
    fi
}

write_settings() { # write_settings <enabled|disabled|other|malformed>
    # Write ~/.claude/settings.json, the state `claude plugin install` leaves
    # behind (and that uninstall.sh cleans up when the CLI is gone). "other"
    # registers this adapter's marketplace but enables a different plugin, so
    # it also pins that a marketplace-name match is not mistaken for the
    # plugin record.
    mkdir -p "$FAKE_HOME/.claude"
    local settings="$FAKE_HOME/.claude/settings.json"
    case "$1" in
    enabled)
        printf '{\n  "enabledPlugins": {\n    "%s": true\n  }\n}\n' \
            "$PLUGIN_ID" >"$settings"
        ;;
    disabled)
        printf '{\n  "enabledPlugins": {\n    "%s": false\n  }\n}\n' \
            "$PLUGIN_ID" >"$settings"
        ;;
    other)
        printf '{\n  "enabledPlugins": {\n    "other@anolisa-other": true\n  },\n' \
            >"$settings"
        printf '  "extraKnownMarketplaces": {\n    "anolisa-tokenless": {}\n  }\n}\n' \
            >>"$settings"
        ;;
    malformed)
        printf '{\n  "enabledPlugins": {\n' >"$settings"
        ;;
    *)
        fail "write_settings: unknown shape '$1'"
        ;;
    esac
}

schedule_claude() { # schedule_claude <delay-seconds>
    # Simulate provisioning: the binary becomes visible only after the delay.
    (
        sleep "$1"
        install_claude_stub
    ) &
    PROVISIONER_PID=$!
}

finish_provisioner() {
    wait "$PROVISIONER_PID"
    PROVISIONER_PID=""
}

cancel_provisioner() {
    kill "$PROVISIONER_PID" 2>/dev/null || true
    wait "$PROVISIONER_PID" 2>/dev/null || true
    PROVISIONER_PID=""
}

reset_env() {
    rm -rf "$FAKE_HOME/.claude"
    rm -f "$FAKE_BIN/claude" "$FLAKY_MARKER" "$LATE_COUNT_FILE"
    echo ready >"$STUB_MODE_FILE"
    LATE_OMISSIONS=1
}

run_detect() { # run_detect <retries> <retry-delay> <plugin-relists> [settle-window]
    : >"$CALL_LOG"
    rm -f "$FLAKY_MARKER" "$LATE_COUNT_FILE"
    # Inherit only /usr/local/bin:/usr/bin:/bin (detect.sh itself prepends
    # $HOME/.local/bin): a claude installed elsewhere in the CI PATH must
    # not leak into the window while the stub is not yet provisioned.
    HOME="$FAKE_HOME" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    CALL_LOG="$CALL_LOG" \
    STUB_MODE_FILE="$STUB_MODE_FILE" \
    FLAKY_MARKER="$FLAKY_MARKER" \
    LATE_COUNT_FILE="$LATE_COUNT_FILE" \
    LATE_OMISSIONS="$LATE_OMISSIONS" \
    ANOLISA_ADAPTER_DIR="$FAKE_ADAPTER_DIR" \
    TOKENLESS_DETECT_RETRIES="$1" \
    TOKENLESS_DETECT_RETRY_DELAY="$2" \
    TOKENLESS_DETECT_PLUGIN_RELISTS="$3" \
    TOKENLESS_DETECT_PLUGIN_SETTLE_SECONDS="${4:-15}" \
        bash "$DETECT" 2>&1
}

plugin_list_calls() {
    grep -c '^plugin$' "$CALL_LOG" || true
}

# --- Scenario 1: the #2512 failure path, with settling retries ------------
# The claude binary is not yet visible in $HOME/.local/bin when detect.sh
# starts (first execution right after provisioning); it appears ~0.2s later
# while detect.sh is still settling (retry delay 0.5s). $HOME/.claude does
# not exist until the CLI's first `plugin list`. Expect ready (exit 0).
reset_env
stage_adapter yes yes
schedule_claude 0.2
if ! out="$(run_detect 3 0.5 2)"; then
    fail "detect.sh should exit 0 (ready) once the settling retries find the claude binary" "$out"
fi
finish_provisioner
grep -qF "installed ($PLUGIN_ID)" <<<"$out" \
    || fail "plugin should be reported installed after the race settles" "$out"
grep -qF "claude-code: ready" <<<"$out" \
    || fail "claude-code should be reported ready after the race settles" "$out"
# Self-containment: detect.sh must have resolved the isolated stubs above,
# not any runner-installed binaries.
grep -qF "present ($FAKE_BIN/tokenless)" <<<"$out" \
    || fail "detect.sh should resolve the isolated tokenless stub, not a host binary" "$out"
grep -qF "present ($FAKE_BIN/rtk)" <<<"$out" \
    || fail "detect.sh should resolve the isolated rtk stub, not a host binary" "$out"
# The $HOME/.claude half of the reported race: the config dir is still
# invisible at probe time. detect.sh must report it as missing without
# letting that affect readiness (the probe is informational only).
grep -qF "missing (created on first claude run)" <<<"$out" \
    || fail "the config-dir probe should have observed the not-yet-created ~/.claude" "$out"

# --- Scenario 2 (control): same first execution, retries disabled ---------
# Without settling retries detect.sh checks once, does not see the binary
# (provisioning completes only after the check), and must fail exactly like
# the nightly framework-ready check did: exit 2, missing prerequisites.
reset_env
stage_adapter yes yes
schedule_claude 2
set +e
out="$(run_detect 0 0 2)"
rc=$?
set -e
cancel_provisioner
[ "$rc" -eq 2 ] \
    || fail "without retries the first execution should exit 2 (missing prerequisites), got $rc" "$out"
grep -qF "claude CLI" <<<"$out" \
    || fail "the first execution without retries should mention the claude CLI" "$out"
grep -qF "missing prerequisites" <<<"$out" \
    || fail "the first execution without retries should report missing prerequisites" "$out"

# --- Scenario 3: absent plugin is reported after bounded re-lists ---------
# The CLI is installed and `plugin list` works, but the tokenless plugin is
# not registered — the ordinary pre-install state on a host whose adapter
# manifests are staged. detect.sh cannot tell that from the GH #3082 registry
# lag on the first list, so it re-lists up to the budget and then reports
# "not installed" (exit 1). The re-lists are bounded: exactly
# 1 + TOKENLESS_DETECT_PLUGIN_RELISTS `plugin list` invocations.
reset_env
install_claude_stub
stage_adapter yes yes
echo absent >"$STUB_MODE_FILE"
mkdir -p "$FAKE_HOME/.claude"
set +e
out="$(run_detect 3 0 2)"
rc=$?
set -e
[ "$rc" -eq 1 ] \
    || fail "plugin-absent detection should exit 1 (installable), got $rc" "$out"
grep -qF "not installed" <<<"$out" \
    || fail "plugin should be reported not installed" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 3 ] \
    || fail "plugin-absent detection should stop after 1 + 2 re-lists (saw $calls calls)" "$out"

# --- Scenario 4: GH #3082, registry index lags behind the installer -------
# The first `plugin list` right after provisioning succeeds but omits the
# just-installed plugin (the CLI's registry index has not rescanned the
# staged manifests yet); the second one lists it. detect.sh must ride out
# that window and report ready (exit 0) on this very first execution.
reset_env
install_claude_stub
stage_adapter yes yes
echo late >"$STUB_MODE_FILE"
LATE_OMISSIONS=1
if ! out="$(run_detect 3 0 2)"; then
    fail "detect.sh should exit 0 (ready) once a re-list sees the just-installed plugin" "$out"
fi
grep -qF "installed ($PLUGIN_ID)" <<<"$out" \
    || fail "plugin should be reported installed after the registry index catches up" "$out"
grep -qF "claude-code: ready" <<<"$out" \
    || fail "claude-code should be reported ready after the registry index catches up" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 2 ] \
    || fail "the omitted plugin should be re-listed once (saw $calls calls)" "$out"

# --- Scenario 5 (control): same first execution, re-lists disabled --------
# Without the re-list budget the very same lagging-index first execution
# reports "not installed" (exit 1) after a single `plugin list`, which is
# exactly the GH #3082 nightly failure. Proves the re-lists are what fix it.
reset_env
install_claude_stub
stage_adapter yes yes
echo late >"$STUB_MODE_FILE"
LATE_OMISSIONS=1
set +e
out="$(run_detect 3 0 0)"
rc=$?
set -e
[ "$rc" -eq 1 ] \
    || fail "without re-lists the lagging-index first execution should exit 1, got $rc" "$out"
grep -qF "not installed" <<<"$out" \
    || fail "without re-lists the plugin should be reported not installed" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 1 ] \
    || fail "without a re-list budget plugin list must run exactly once (saw $calls calls)" "$out"

# --- Scenario 6: unstaged manifests make the omission definitive ---------
# marketplace.json is present but plugin.json is not (an unstamped adapter
# tree), so `claude plugin install` never had a manifest to register and the
# registry index has nothing to catch up with: the omission is definitive and
# must not spend the re-list budget.
reset_env
install_claude_stub
stage_adapter yes no
echo absent >"$STUB_MODE_FILE"
mkdir -p "$FAKE_HOME/.claude"
set +e
out="$(run_detect 3 0 2)"
rc=$?
set -e
[ "$rc" -eq 1 ] \
    || fail "unstaged-manifest plugin-absent detection should exit 1, got $rc" "$out"
grep -qF "not installed" <<<"$out" \
    || fail "plugin should be reported not installed when nothing is staged" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 1 ] \
    || fail "definitive plugin-absent must not re-list: plugin list ran $calls times" "$out"

# --- Scenario 7: transient plugin-list failures are still retried ---------
# A `plugin list` call that fails outright (as opposed to one that succeeds
# without listing the plugin) is not definitive — the CLI may still be
# initializing — so settle() retries it under its own budget.
reset_env
install_claude_stub
stage_adapter yes yes
echo flaky >"$STUB_MODE_FILE"
if ! out="$(run_detect 3 0 2)"; then
    fail "detect.sh should exit 0 after retrying a transient plugin-list failure" "$out"
fi
grep -qF "installed ($PLUGIN_ID)" <<<"$out" \
    || fail "plugin should be reported installed after the transient failure" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 2 ] \
    || fail "the transient plugin-list failure should be retried once (saw $calls calls)" "$out"

# --- Scenario 8: GH #3267, a stale index must not decide the verdict ------
# `claude plugin list` succeeds but still omits the plugin (the registry index
# has not rescanned), while ~/.claude/settings.json already carries the
# enabledPlugins record the installer wrote. That record is authoritative, so
# detect.sh must report installed (exit 0) on the very first list — no
# re-lists, no waiting out the index — and name the signal that confirmed it.
# Before the fix this exact state returned "not installed" (exit 1) whenever
# the re-list budget ran out first, which under load was ~80% of runs.
reset_env
install_claude_stub
stage_adapter yes yes
echo absent >"$STUB_MODE_FILE"
write_settings enabled
if ! out="$(run_detect 3 0 5)"; then
    fail "a stale registry index must not report an installed plugin as missing" "$out"
fi
grep -qF "installed ($PLUGIN_ID)" <<<"$out" \
    || fail "the settings record should confirm the plugin the index omitted" "$out"
grep -qF "plugin registry index" <<<"$out" \
    || fail "detect.sh should surface that the registry index was stale" "$out"
grep -qF "claude-code: ready" <<<"$out" \
    || fail "claude-code should be reported ready despite the stale index" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 1 ] \
    || fail "the settings record confirms the plugin without re-listing (saw $calls calls)" "$out"

# --- Scenario 9 (control): another plugin's record is not a confirmation --
# The marketplace this adapter registers is known to claude, but the enabled
# plugin is a different one: nothing confirms tokenless, so the omission stays
# definitive-ish and the bounded re-list budget decides, exactly as before.
reset_env
install_claude_stub
stage_adapter yes yes
echo absent >"$STUB_MODE_FILE"
write_settings other
set +e
out="$(run_detect 3 0 2)"
rc=$?
set -e
[ "$rc" -eq 1 ] \
    || fail "a different plugin's record must not confirm tokenless, got rc=$rc" "$out"
grep -qF "not installed" <<<"$out" \
    || fail "the plugin should stay not installed when another plugin is enabled" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 3 ] \
    || fail "an unconfirmed omission should still use the re-list budget (saw $calls calls)" "$out"

# --- Scenario 10 (control): a disabled record is not a confirmation -------
# enabledPlugins carries the plugin but set to false, i.e. the user (or a
# failed activation) turned it off. detect.sh must keep reporting "not
# installed" so the install flow re-enables it, instead of using the record to
# claim a ready adapter whose hooks will never fire.
reset_env
install_claude_stub
stage_adapter yes yes
echo absent >"$STUB_MODE_FILE"
write_settings disabled
set +e
out="$(run_detect 3 0 2)"
rc=$?
set -e
[ "$rc" -eq 1 ] \
    || fail "a disabled plugin record must not be reported as installed, got rc=$rc" "$out"
grep -qF "not installed" <<<"$out" \
    || fail "a disabled plugin should be reported not installed" "$out"

# --- Scenario 11 (control): a malformed settings.json is not a confirmation
# The record cannot be read, so it proves nothing: detect.sh must neither
# crash nor guess, and must fall back to the re-list budget.
reset_env
install_claude_stub
stage_adapter yes yes
echo absent >"$STUB_MODE_FILE"
write_settings malformed
set +e
out="$(run_detect 3 0 2)"
rc=$?
set -e
[ "$rc" -eq 1 ] \
    || fail "a malformed settings.json must fall back to the re-list budget, got rc=$rc" "$out"
grep -qF "not installed" <<<"$out" \
    || fail "an unreadable settings record should leave the plugin not installed" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 3 ] \
    || fail "an unreadable settings record should still use the re-list budget (saw $calls calls)" "$out"

# --- Scenario 12: GH #3267, the index lag outlasts the old budget ---------
# The registry index needs four lists before it shows the just-installed
# plugin — the shape of the loaded nightly run, where the lag outgrew the
# fixed budget of 2 re-lists. The larger ceiling rides it out (exit 0).
reset_env
install_claude_stub
stage_adapter yes yes
echo late >"$STUB_MODE_FILE"
LATE_OMISSIONS=4
if ! out="$(run_detect 3 0 5)"; then
    fail "detect.sh should ride out an index lag longer than the GH #3085 budget" "$out"
fi
grep -qF "installed ($PLUGIN_ID)" <<<"$out" \
    || fail "the plugin should be reported installed once the index catches up" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 5 ] \
    || fail "a four-list lag should be settled by the fifth list (saw $calls calls)" "$out"

# --- Scenario 12b (control): the pre-fix budget fails the same run --------
# With the GH #3085 ceiling of 2 re-lists the very same lag ends in the false
# "not installed" verdict reported by GH #3267. Proves the raised ceiling (and
# not something incidental) is what fixes it.
reset_env
install_claude_stub
stage_adapter yes yes
echo late >"$STUB_MODE_FILE"
LATE_OMISSIONS=4
set +e
out="$(run_detect 3 0 2)"
rc=$?
set -e
[ "$rc" -eq 1 ] \
    || fail "the pre-fix re-list ceiling should still miss a four-list lag, got rc=$rc" "$out"
grep -qF "not installed" <<<"$out" \
    || fail "the pre-fix ceiling should report the plugin not installed" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 3 ] \
    || fail "the pre-fix ceiling stops after 1 + 2 lists (saw $calls calls)" "$out"

# --- Scenario 13: the settle window bounds a large attempt ceiling --------
# The re-list budget is a wall-clock window with the attempt count as a
# ceiling, not the other way round: a generous ceiling must not turn a
# genuinely absent plugin into a minutes-long probe loop. With a 1s window the
# loop stops after the first backoff step even though 50 attempts are allowed.
reset_env
install_claude_stub
stage_adapter yes yes
echo absent >"$STUB_MODE_FILE"
set +e
out="$(run_detect 3 1 50 1)"
rc=$?
set -e
[ "$rc" -eq 1 ] \
    || fail "an absent plugin should be reported not installed once the window closes, got rc=$rc" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -le 3 ] \
    || fail "the settle window must bound the re-lists (saw $calls calls with a 1s window)" "$out"

# --- Scenario 14: the re-list gap backs off ------------------------------
# Pin the schedule directly: the base retry delay doubled per re-list and
# capped, so a loaded host spends its settle window on a few well-spaced
# probes instead of burning it in one burst (and a fractional base delay, as
# scenario 1 uses, still works).
eval "$(sed -n '/^plugin_relist_delay()/,/^}/p' "$DETECT")"
# Both variables are read by the eval'd function, which shellcheck cannot see.
# shellcheck disable=SC2034
DETECT_RETRY_DELAY=1
# shellcheck disable=SC2034
DETECT_PLUGIN_RELIST_DELAY_MAX=4
schedule="$(plugin_relist_delay 1) $(plugin_relist_delay 2) $(plugin_relist_delay 3)"
schedule="$schedule $(plugin_relist_delay 4) $(plugin_relist_delay 6)"
[ "$schedule" = "1 2 4 4 4" ] \
    || fail "the re-list backoff should double and cap at 4s, got: $schedule"
# shellcheck disable=SC2034
DETECT_RETRY_DELAY=0.5
schedule="$(plugin_relist_delay 1) $(plugin_relist_delay 2) $(plugin_relist_delay 3)"
[ "$schedule" = "0.5 1 2" ] \
    || fail "the backoff should honour a fractional base delay, got: $schedule"
unset -f plugin_relist_delay

echo "claude-code detect retry test passed"
