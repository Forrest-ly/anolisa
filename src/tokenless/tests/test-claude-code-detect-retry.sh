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
# it. Scenarios 3-6 pin the plugin probe's retry semantics. GH #3082 showed
# that a successful `plugin list` omitting the plugin is not always
# definitive: right after `claude plugin install` the CLI's plugin registry
# index still lags the manifests on disk, so the first listing can omit a
# plugin that is in fact installed. detect.sh now treats that omission as
# retryable whenever the adapter's own marketplace.json + plugin.json payload
# is fully staged (scenario 5) and as definitive only when nothing is staged
# (scenario 3). A failing listing stays retryable (scenario 4), and a
# genuinely absent plugin still terminates inside the bounded budget —
# including the zero-retry opt-out `make claude-code-install` uses (scenario
# 6).

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
LATE_MARKER="$TEST_DIR/late-list-marker"
PROVISIONER_PID=""

# detect.sh derives PLUGIN_SRC from ANOLISA_ADAPTER_DIR and its plugin probe
# keys off the manifests staged there, so point it at a fixture adapter rather
# than the real tree: a repo checkout has marketplace.json but no stamped
# plugin.json, and `make stamp-adapter-templates` would silently change which
# branch of the probe the scenarios exercise.
FAKE_ADAPTER="$TEST_DIR/adapters/tokenless"
FAKE_PLUGIN_SRC="$FAKE_ADAPTER/claude-code"

cleanup() {
    if [ -n "$PROVISIONER_PID" ]; then
        kill "$PROVISIONER_PID" 2>/dev/null || true
        wait "$PROVISIONER_PID" 2>/dev/null || true
    fi
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

mkdir -p "$FAKE_BIN" "$SHARED_HOOKS"

# Fixture adapter payload. stage_plugin_payload reproduces a fully installed
# adapter resource tree (both manifests present); unstage_plugin_manifest
# reproduces the unstamped dev checkout (plugin.json missing). The hook
# dispatcher is staged once — its absence would be an unrelated prerequisite
# failure (exit 2) and would mask the plugin-probe assertions.
stage_plugin_payload() {
    mkdir -p "$FAKE_PLUGIN_SRC/.claude-plugin" "$FAKE_PLUGIN_SRC/hooks"
    cat >"$FAKE_PLUGIN_SRC/.claude-plugin/marketplace.json" <<'JSON'
{
  "name": "anolisa-tokenless",
  "plugins": [
    { "name": "tokenless", "source": "./" }
  ]
}
JSON
    cat >"$FAKE_PLUGIN_SRC/.claude-plugin/plugin.json" <<'JSON'
{
  "name": "tokenless",
  "version": "9.9.9-test"
}
JSON
    printf '#!/usr/bin/env bash\nexit 0\n' >"$FAKE_PLUGIN_SRC/hooks/run-hook.sh"
    chmod +x "$FAKE_PLUGIN_SRC/hooks/run-hook.sh"
}

unstage_plugin_manifest() {
    rm -f "$FAKE_PLUGIN_SRC/.claude-plugin/plugin.json"
}

stage_plugin_payload

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
#   ready     — the list succeeds and contains the tokenless plugin (default)
#   absent    — the list succeeds but never contains the plugin
#   flaky     — the first `plugin list` call fails while the registry
#               initializes; later calls succeed
#   late-list — the first `plugin list` call succeeds but omits the plugin
#               (registry index not rescanned since the install); later calls
#               list it
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
    if [ "$mode" = "late-list" ] && [ ! -f "$LATE_MARKER" ]; then
        : >"$LATE_MARKER"
        mkdir -p "$HOME/.claude"
        echo "NAME                          STATUS"
        exit 0
    fi
    if [ "$mode" = "flaky" ] && [ ! -f "$FLAKY_MARKER" ]; then
        : >"$FLAKY_MARKER"
        echo "initializing plugin registry" >&2
        exit 1
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
    rm -f "$FAKE_BIN/claude" "$FLAKY_MARKER" "$LATE_MARKER"
    stage_plugin_payload
    echo ready >"$STUB_MODE_FILE"
}

run_detect() { # run_detect <retries> <retry-delay>
    : >"$CALL_LOG"
    rm -f "$FLAKY_MARKER" "$LATE_MARKER"
    # Inherit only /usr/local/bin:/usr/bin:/bin (detect.sh itself prepends
    # $HOME/.local/bin): a claude installed elsewhere in the CI PATH must
    # not leak into the window while the stub is not yet provisioned.
    HOME="$FAKE_HOME" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    CALL_LOG="$CALL_LOG" \
    ANOLISA_ADAPTER_DIR="$FAKE_ADAPTER" \
    STUB_MODE_FILE="$STUB_MODE_FILE" \
    FLAKY_MARKER="$FLAKY_MARKER" \
    LATE_MARKER="$LATE_MARKER" \
    TOKENLESS_DETECT_RETRIES="$1" \
    TOKENLESS_DETECT_RETRY_DELAY="$2" \
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
schedule_claude 0.2
if ! out="$(run_detect 3 0.5)"; then
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
schedule_claude 2
set +e
out="$(run_detect 0 0)"
rc=$?
set -e
cancel_provisioner
[ "$rc" -eq 2 ] \
    || fail "without retries the first execution should exit 2 (missing prerequisites), got $rc" "$out"
grep -qF "claude CLI" <<<"$out" \
    || fail "the first execution without retries should mention the claude CLI" "$out"
grep -qF "missing prerequisites" <<<"$out" \
    || fail "the first execution without retries should report missing prerequisites" "$out"

# --- Scenario 3: unstaged payload + omitted plugin must not retry ----------
# The CLI is installed and `plugin list` works, the tokenless plugin is not
# registered, and the adapter has no stamped plugin.json that a stale registry
# index could be hiding — the pre-install state of an unstamped dev checkout.
# Such an omission is definitive: detect.sh must report "not installed" after
# exactly one `plugin list` invocation, without sleeping out the retry budget.
reset_env
install_claude_stub
unstage_plugin_manifest
echo absent >"$STUB_MODE_FILE"
mkdir -p "$FAKE_HOME/.claude"
set +e
out="$(run_detect 3 1)"
rc=$?
set -e
[ "$rc" -eq 1 ] \
    || fail "plugin-absent detection should exit 1 (installable), got $rc" "$out"
grep -qF "not installed" <<<"$out" \
    || fail "plugin should be reported not installed" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 1 ] \
    || fail "definitive plugin-absent must not retry: plugin list ran $calls times" "$out"
grep -qF "missing (run: make stamp-adapter-templates)" <<<"$out" \
    || fail "the unstamped manifest that makes the omission definitive should be reported" "$out"

# --- Scenario 4: transient plugin-list failures are still retried ---------
# A `plugin list` call that fails outright (as opposed to one that succeeds
# without listing the plugin) is not definitive — the CLI may still be
# initializing — so settle() retries it.
reset_env
install_claude_stub
echo flaky >"$STUB_MODE_FILE"
if ! out="$(run_detect 3 0)"; then
    fail "detect.sh should exit 0 after retrying a transient plugin-list failure" "$out"
fi
grep -qF "installed ($PLUGIN_ID)" <<<"$out" \
    || fail "plugin should be reported installed after the transient failure" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 2 ] \
    || fail "the transient plugin-list failure should be retried once (saw $calls calls)" "$out"

# --- Scenario 5: stale registry index after install is retried (GH #3082) --
# The nightly failure path: the host has just installed the plugin, so the
# adapter payload is fully staged on disk, but the CLI has not rescanned its
# plugin registry yet — the first `plugin list` succeeds (exit 0) and omits
# tokenless@anolisa-tokenless. detect.sh must ride out that window on its very
# first execution and report ready (exit 0), instead of declaring the plugin
# "not installed" and failing the framework-ready assertion.
reset_env
install_claude_stub
echo late-list >"$STUB_MODE_FILE"
mkdir -p "$FAKE_HOME/.claude"
if ! out="$(run_detect 3 0)"; then
    fail "detect.sh should exit 0 (ready) once the registry index catches up with the staged payload" "$out"
fi
grep -qF "installed ($PLUGIN_ID)" <<<"$out" \
    || fail "plugin should be reported installed after the stale-index window" "$out"
grep -qF "claude-code: ready" <<<"$out" \
    || fail "claude-code should be reported ready after the stale-index window" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 2 ] \
    || fail "the stale-index omission should be retried once (saw $calls plugin list calls)" "$out"

# --- Scenario 6: a genuinely absent plugin stays bounded -------------------
# Retrying the omission must not turn the pre-install probe into an unbounded
# one: with the payload staged and the plugin never listed, detect.sh stops
# after 1 + TOKENLESS_DETECT_RETRIES listings and still reports "not
# installed" (exit 1). TOKENLESS_DETECT_RETRIES=0 — the opt-out
# `make claude-code-install` uses for its informational pre-install probe —
# keeps that path to a single listing.
reset_env
install_claude_stub
echo absent >"$STUB_MODE_FILE"
mkdir -p "$FAKE_HOME/.claude"
set +e
out="$(run_detect 2 0)"
rc=$?
set -e
[ "$rc" -eq 1 ] \
    || fail "an absent plugin should still exit 1 (installable), got $rc" "$out"
grep -qF "not installed" <<<"$out" \
    || fail "plugin should be reported not installed once the bounded retries are spent" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 3 ] \
    || fail "stale-index retries must stay bounded at 1 + TOKENLESS_DETECT_RETRIES listings (saw $calls)" "$out"

reset_env
install_claude_stub
echo absent >"$STUB_MODE_FILE"
mkdir -p "$FAKE_HOME/.claude"
set +e
out="$(run_detect 0 0)"
rc=$?
set -e
[ "$rc" -eq 1 ] \
    || fail "the zero-retry opt-out should still exit 1 (installable), got $rc" "$out"
calls="$(plugin_list_calls)"
[ "$calls" -eq 1 ] \
    || fail "TOKENLESS_DETECT_RETRIES=0 must skip the stale-index retries (saw $calls listings)" "$out"

echo "claude-code detect retry test passed"
