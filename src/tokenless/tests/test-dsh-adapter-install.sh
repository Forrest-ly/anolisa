#!/usr/bin/env bash
# Regression tests for the deepseek-harness (dsh) bridge-mode adapter
# lifecycle: detect fail-open, install registration into cordis.patch.yml,
# idempotency, user-content preservation, uninstall, and npm-failure safety.
set -uo pipefail

PASS=0
FAIL=0

pass() { echo "[PASS] $1"; PASS=$((PASS + 1)); }
fail() { echo "[FAIL] $1" >&2; FAIL=$((FAIL + 1)); }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ADAPTER_DIR="$SCRIPT_DIR/../adapters/tokenless"
SANDBOX="$(mktemp -d -t tokenless-dsh-install-test.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT

FAKE_HOME="$SANDBOX/home"
FAKE_BIN="$SANDBOX/bin"
DSH_HOME_DIR="$SANDBOX/dshome"
ADAPTER_DIR="$SANDBOX/adapter root"
mkdir -p "$FAKE_HOME" "$FAKE_BIN" "$DSH_HOME_DIR"
cp -R "$SOURCE_ADAPTER_DIR"/. "$ADAPTER_DIR"/

ADAPTER_ENV=(
    ANOLISA_COMPONENT=tokenless
    ANOLISA_TARGET=deepseek-harness
    ANOLISA_ADAPTER_DIR="$ADAPTER_DIR"
    HOME="$FAKE_HOME"
    DSH_HOME="$DSH_HOME_DIR"
)

# dsh + npm stubs. The npm stub materializes the bridge package under the
# --prefix directory, mirroring `npm --prefix "$DSH_HOME" install <pkg>`.
cat > "$FAKE_BIN/dsh" <<'STUBEOF'
#!/usr/bin/env bash
echo "dsh 0.1.0-rc.5"
STUBEOF
cat > "$FAKE_BIN/npm" <<'STUBEOF'
#!/usr/bin/env bash
if [ "${DSH_NPM_STUB_FAIL:-0}" = "1" ]; then
    echo "npm stub: simulated registry failure" >&2
    exit 1
fi
prefix=""
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) prefix="$2"; shift 2 ;;
        *) shift ;;
    esac
done
[ -n "$prefix" ] || exit 1
mkdir -p "$prefix/node_modules/@deepseek-ai/dsh-hooks-claude-code"
echo '{"name":"@deepseek-ai/dsh-hooks-claude-code","version":"0.1.0"}' \
    > "$prefix/node_modules/@deepseek-ai/dsh-hooks-claude-code/package.json"
exit 0
STUBEOF
chmod 0755 "$FAKE_BIN/dsh" "$FAKE_BIN/npm"

PATCH_FILE="$DSH_HOME_DIR/cordis.patch.yml"
MARKER="anolisa-tokenless:deepseek-harness-bridge"

# --- Static adapter payload checks ------------------------------------------

python3 - "$ADAPTER_DIR" <<'PYEOF' && pass "hooks.json wires the shared hooks with dsh matchers" || fail "hooks.json structure"
import json, sys
root = sys.argv[1]
with open(f"{root}/deepseek-harness/hooks/hooks.json", encoding="utf-8") as f:
    hooks = json.load(f)["hooks"]
pre = hooks["PreToolUse"]
post = hooks["PostToolUse"]
cmds = [h["command"] for group in pre + post for h in group["hooks"]]
assert any("rewrite_hook.py" in c and group["matcher"] == "bash|pwsh"
           for group in pre for c in [h["command"] for h in group["hooks"]])
assert any("tool_ready_hook.sh" in c and group["matcher"] == ""
           for group in pre for c in [h["command"] for h in group["hooks"]])
assert any("compress_response_hook.py" in c for group in post
           for c in [h["command"] for h in group["hooks"]])
assert all("TOKENLESS_AGENT_ID=deepseek-harness" in c for c in cmds)
assert all("${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.sh" in c for c in cmds)
PYEOF

[ -L "$ADAPTER_DIR/deepseek-harness/hooks/run-hook.sh" ] \
    && [ "$(readlink "$ADAPTER_DIR/deepseek-harness/hooks/run-hook.sh")" = "../../common/hooks/run-hook.sh" ] \
    && pass "run-hook.sh delegates to common/hooks/" \
    || fail "run-hook.sh is not the common/hooks symlink"

python3 - "$SOURCE_ADAPTER_DIR" <<'PYEOF' && pass "manifest declares only the tool-ready capability" || fail "manifest capability declaration"
import json, sys
root = sys.argv[1]
with open(f"{root}/manifest.json.in", encoding="utf-8") as f:
    content = f.read().replace("@VERSION@", "0.0.0-test")
target = json.loads(content)["targets"]["deepseek-harness"]
assert target["capabilities"]["hooks"] == ["tool-ready"], target["capabilities"]
assert target["actions"]["detect"] == "deepseek-harness/scripts/detect.sh"
assert target["actions"]["install"] == "deepseek-harness/scripts/install.sh"
assert target["actions"]["uninstall"] == "deepseek-harness/scripts/uninstall.sh"
PYEOF

# PATH with coreutils but neither the dsh nor the npm stub.
NO_DSH_PATH="/usr/bin:/bin"

# --- detect: fail-open even without dsh --------------------------------------

env "${ADAPTER_ENV[@]}" PATH="$NO_DSH_PATH" bash "$ADAPTER_DIR/deepseek-harness/scripts/detect.sh" >/dev/null 2>&1 \
    && pass "detect.sh exits 0 without dsh (fail-open)" \
    || fail "detect.sh must always exit 0"

# --- install: graceful no-op without dsh -------------------------------------

env "${ADAPTER_ENV[@]}" PATH="$NO_DSH_PATH" bash "$ADAPTER_DIR/deepseek-harness/scripts/install.sh" >/dev/null 2>&1
rc=$?
if [ $rc -eq 0 ] && [ ! -f "$PATCH_FILE" ]; then
    pass "install.sh no-ops without dsh (exit 0, nothing registered)"
else
    fail "install.sh without dsh (rc=$rc, patch file present=$([ -f "$PATCH_FILE" ] && echo yes || echo no))"
fi

# --- install: registers the bridge -------------------------------------------

env "${ADAPTER_ENV[@]}" PATH="$FAKE_BIN:/usr/bin:/bin" bash "$ADAPTER_DIR/deepseek-harness/scripts/install.sh" >/dev/null 2>&1 \
    && [ -f "$PATCH_FILE" ] && grep -qF "$MARKER" "$PATCH_FILE" \
    && pass "install.sh registers the managed bridge block" \
    || fail "install.sh registration"

# The managed block is generated verbatim by install.sh, so assert its exact
# shape: a top-level `- insert:` entry whose row fields point at the adapter.
if grep -qE '^- insert:$' "$PATCH_FILE" \
    && grep -qE '^[[:space:]]+- id: tokenless$' "$PATCH_FILE" \
    && grep -qE "^[[:space:]]+name: '@deepseek-ai/dsh-hooks-claude-code'$" "$PATCH_FILE" \
    && grep -q "configPath: $ADAPTER_DIR/deepseek-harness/hooks/hooks.json" "$PATCH_FILE" \
    && grep -q "pluginRoot: $ADAPTER_DIR/deepseek-harness" "$PATCH_FILE"; then
    pass "patch file carries the insert entry with adapter paths"
else
    fail "patch file content"
fi

# --- install: idempotent ------------------------------------------------------

env "${ADAPTER_ENV[@]}" PATH="$FAKE_BIN:/usr/bin:/bin" bash "$ADAPTER_DIR/deepseek-harness/scripts/install.sh" >/dev/null 2>&1
[ "$(grep -cF "$MARKER >>>" "$PATCH_FILE")" = "1" ] \
    && pass "re-install stays a single managed block" \
    || fail "re-install duplicated the managed block"

# --- install over existing user content ---------------------------------------

printf -- '# user comment\n- id: my-plugin\n  name: ./my-plugin.mjs\n' > "$PATCH_FILE"
env "${ADAPTER_ENV[@]}" PATH="$FAKE_BIN:/usr/bin:/bin" bash "$ADAPTER_DIR/deepseek-harness/scripts/install.sh" >/dev/null 2>&1
if grep -q "my-plugin" "$PATCH_FILE" && grep -qE '^- insert:$' "$PATCH_FILE"; then
    pass "install over user patch file keeps both entries"
else
    fail "install over user content"
fi

# --- uninstall: user content survives -----------------------------------------

env "${ADAPTER_ENV[@]}" PATH="$FAKE_BIN:/usr/bin:/bin" bash "$ADAPTER_DIR/deepseek-harness/scripts/uninstall.sh" >/dev/null 2>&1
if ! grep -qF "$MARKER" "$PATCH_FILE" && grep -q "my-plugin" "$PATCH_FILE"; then
    pass "uninstall removes only the managed block (user entries preserved)"
else
    fail "uninstall damaged user content or left the managed block"
fi

# --- uninstall: file deleted when only our block remains ----------------------

rm -f "$PATCH_FILE"
env "${ADAPTER_ENV[@]}" PATH="$FAKE_BIN:/usr/bin:/bin" bash "$ADAPTER_DIR/deepseek-harness/scripts/install.sh" >/dev/null 2>&1
env "${ADAPTER_ENV[@]}" PATH="$FAKE_BIN:/usr/bin:/bin" bash "$ADAPTER_DIR/deepseek-harness/scripts/uninstall.sh" >/dev/null 2>&1
[ ! -f "$PATCH_FILE" ] \
    && pass "uninstall deletes the patch file when nothing else remains" \
    || fail "patch file must be deleted when only the managed block remained"

# --- install: npm failure must not register an unresolvable entry -------------

rm -f "$PATCH_FILE"
rm -rf "$DSH_HOME_DIR/node_modules"
env "${ADAPTER_ENV[@]}" PATH="$FAKE_BIN:/usr/bin:/bin" DSH_NPM_STUB_FAIL=1 \
    bash "$ADAPTER_DIR/deepseek-harness/scripts/install.sh" >/dev/null 2>&1
rc=$?
if [ $rc -eq 0 ] && [ ! -f "$PATCH_FILE" ]; then
    pass "npm failure: install exits 0 and registers nothing (dsh boot safe)"
else
    fail "npm failure handling (rc=$rc, patch file present=$([ -f "$PATCH_FILE" ] && echo yes || echo no))"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]
