#!/usr/bin/env bash
# Regression tests for the deepseek-harness (dsh) hook-bridge lifecycle.
#
# Sandboxes a fake $DSH_HOME with dsh profiles and a stub `dsh` launcher,
# then exercises detect/install/uninstall idempotency, managed-block
# ownership, and the boot-safety rule (never write a patch entry whose
# bridge package is unresolved).
set -euo pipefail

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
FAKE_DSH_HOME="$SANDBOX/dsh-home"
ADAPTER_DIR="$SANDBOX/adapter root"
mkdir -p "$FAKE_HOME" "$FAKE_BIN" \
         "$ADAPTER_DIR/deepseek-harness/hooks" \
         "$ADAPTER_DIR/deepseek-harness/scripts"
cp "$SOURCE_ADAPTER_DIR/deepseek-harness/hooks/hooks.json" \
   "$ADAPTER_DIR/deepseek-harness/hooks/hooks.json"
cp -P "$SOURCE_ADAPTER_DIR/deepseek-harness/scripts/"*.sh \
   "$ADAPTER_DIR/deepseek-harness/scripts/"

# Stub dsh launcher: `dsh plugin --profile <p> add <pkg>` materializes the
# package under the profile's node_modules; DSH_STUB_FAIL_ADD=1 makes add fail.
cat > "$FAKE_BIN/dsh" <<'STUBEOF'
#!/usr/bin/env bash
if [ "${1:-}" = "plugin" ] && [ "${2:-}" = "--profile" ]; then
    profile="$3"
    action="$4"
    pkg="$5"
    case "$action" in
        add)
            if [ "${DSH_STUB_FAIL_ADD:-0}" = "1" ]; then
                echo "stub: simulated network failure" >&2
                exit 1
            fi
            mkdir -p "${DSH_HOME:-$HOME/.dsh}/profiles/${profile}/node_modules/${pkg}"
            exit 0
            ;;
        remove)
            rm -rf "${DSH_HOME:-$HOME/.dsh}/profiles/${profile}/node_modules/${pkg}"
            exit 0
            ;;
    esac
fi
exit 0
STUBEOF
chmod +x "$FAKE_BIN/dsh"

export HOME="$FAKE_HOME"
export PATH="$FAKE_BIN:$PATH"
export ANOLISA_ADAPTER_DIR="$ADAPTER_DIR"
export DSH_HOME="$FAKE_DSH_HOME"

DETECT_SH="$ADAPTER_DIR/deepseek-harness/scripts/detect.sh"
INSTALL_SH="$ADAPTER_DIR/deepseek-harness/scripts/install.sh"
UNINSTALL_SH="$ADAPTER_DIR/deepseek-harness/scripts/uninstall.sh"

detect_field() {
    # $1 = JSON field; runs detect.sh and extracts a top-level scalar.
    bash "$DETECT_SH" 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"
}

# --- detect before anything exists: fail-open exit 0 ------------------------
if bash "$DETECT_SH" >/dev/null 2>&1; then
    pass "detect exits 0 with no dsh home present (fail-open)"
else
    fail "detect must exit 0 even without dsh"
fi
if [ "$(detect_field installed)" = "False" ]; then
    pass "detect reports installed=false before installation"
else
    fail "detect should report installed=false before installation"
fi

# --- install with no profiles: graceful skip --------------------------------
if bash "$INSTALL_SH" >/dev/null 2>&1; then
    pass "install exits 0 when no dsh profiles exist yet"
else
    fail "install should exit 0 (graceful skip) when no profiles exist"
fi

# --- normal install across profiles ------------------------------------------
mkdir -p "$FAKE_DSH_HOME/profiles/web" "$FAKE_DSH_HOME/profiles/headless"
printf '%s\n' "# user's own patch layer" "- id: user-plugin" "  name: '@example/user-plugin'" \
    > "$FAKE_DSH_HOME/profiles/web/cordis.patch.yml"

if bash "$INSTALL_SH" >/dev/null 2>&1; then
    pass "install succeeds with two profiles"
else
    fail "install failed with two profiles"
fi

for profile in web headless; do
    pkg_dir="$FAKE_DSH_HOME/profiles/$profile/node_modules/@deepseek-ai/dsh-hooks-claude-code"
    if [ -d "$pkg_dir" ]; then
        pass "bridge package installed into profile '$profile'"
    else
        fail "bridge package missing from profile '$profile'"
    fi
    patch_file="$FAKE_DSH_HOME/profiles/$profile/cordis.patch.yml"
    if grep -q "configPath: $ADAPTER_DIR/deepseek-harness/hooks/hooks.json" "$patch_file" \
        && grep -q "pluginRoot: $ADAPTER_DIR/deepseek-harness" "$patch_file"; then
        pass "managed block registered in profile '$profile'"
    else
        fail "managed block missing or wrong in profile '$profile'"
    fi
done

if grep -q "@example/user-plugin" "$FAKE_DSH_HOME/profiles/web/cordis.patch.yml"; then
    pass "existing user patch entries preserved"
else
    fail "install clobbered existing user patch entries"
fi

if [ "$(detect_field installed)" = "True" ]; then
    pass "detect reports installed=true after installation"
else
    fail "detect should report installed=true after installation"
fi

# --- idempotency --------------------------------------------------------------
if bash "$INSTALL_SH" >/dev/null 2>&1; then
    pass "repeated install succeeds (idempotent)"
else
    fail "repeated install failed"
fi
blocks="$(grep -c "tokenless-deepseek-harness-hooks" "$FAKE_DSH_HOME/profiles/web/cordis.patch.yml" || true)"
if [ "$blocks" -eq 1 ]; then
    pass "managed block not duplicated on reinstall"
else
    fail "managed block duplicated on reinstall (found $blocks)"
fi

# --- profile selection --------------------------------------------------------
rm -rf "$FAKE_DSH_HOME/profiles/headless/cordis.patch.yml" \
       "$FAKE_DSH_HOME/profiles/headless/node_modules"
if TOKENLESS_DSH_PROFILE=headless bash "$INSTALL_SH" >/dev/null 2>&1 \
    && grep -q "tokenless-deepseek-harness-hooks" "$FAKE_DSH_HOME/profiles/headless/cordis.patch.yml"; then
    pass "TOKENLESS_DSH_PROFILE restricts registration to the named profile"
else
    fail "TOKENLESS_DSH_PROFILE registration failed"
fi
if TOKENLESS_DSH_PROFILE=missing bash "$INSTALL_SH" >/dev/null 2>&1; then
    fail "install must reject a nonexistent TOKENLESS_DSH_PROFILE"
else
    pass "nonexistent TOKENLESS_DSH_PROFILE rejected"
fi

# --- boot safety: unresolved bridge package => no patch entry -----------------
rm -rf "$FAKE_DSH_HOME/profiles/web/cordis.patch.yml" \
       "$FAKE_DSH_HOME/profiles/web/node_modules" \
       "$FAKE_DSH_HOME/profiles/headless/cordis.patch.yml" \
       "$FAKE_DSH_HOME/profiles/headless/node_modules"
if DSH_STUB_FAIL_ADD=1 bash "$INSTALL_SH" >/dev/null 2>&1; then
    fail "install must fail when no profile can resolve the bridge package"
else
    pass "install fails when the bridge package stays unresolved"
fi
if [ ! -e "$FAKE_DSH_HOME/profiles/headless/cordis.patch.yml" ] \
    || ! grep -q "tokenless-deepseek-harness-hooks" "$FAKE_DSH_HOME/profiles/headless/cordis.patch.yml" 2>/dev/null; then
    pass "no patch entry written for unresolved bridge package (boot safety)"
else
    fail "patch entry written despite unresolved bridge package"
fi

# --- dry run -------------------------------------------------------------------
rm -rf "$FAKE_DSH_HOME/profiles/headless/cordis.patch.yml"
if ANOLISA_DRY_RUN=1 bash "$INSTALL_SH" >/dev/null 2>&1 \
    && [ ! -e "$FAKE_DSH_HOME/profiles/headless/cordis.patch.yml" ]; then
    pass "ANOLISA_DRY_RUN=1 makes no changes"
else
    fail "ANOLISA_DRY_RUN=1 must not write anything"
fi

# --- uninstall ------------------------------------------------------------------
# Restore a user-owned patch layer alongside the managed block so the
# uninstall assertions run against a realistic mixed file.
printf '%s\n' "# user's own patch layer" "- id: user-plugin" "  name: '@example/user-plugin'" \
    > "$FAKE_DSH_HOME/profiles/web/cordis.patch.yml"
bash "$INSTALL_SH" >/dev/null 2>&1

if bash "$UNINSTALL_SH" >/dev/null 2>&1; then
    pass "uninstall succeeds"
else
    fail "uninstall failed"
fi
if ! grep -q "tokenless-deepseek-harness-hooks" "$FAKE_DSH_HOME/profiles/web/cordis.patch.yml" \
    && ! grep -q "tokenless deepseek-harness hooks" "$FAKE_DSH_HOME/profiles/web/cordis.patch.yml"; then
    pass "managed block removed from profile 'web'"
else
    fail "managed block still present in profile 'web'"
fi
if grep -q "@example/user-plugin" "$FAKE_DSH_HOME/profiles/web/cordis.patch.yml"; then
    pass "user patch entries survive uninstall"
else
    fail "uninstall clobbered user patch entries"
fi
if bash "$UNINSTALL_SH" >/dev/null 2>&1; then
    pass "uninstall is idempotent"
else
    fail "repeated uninstall failed"
fi
if [ "$(detect_field installed)" = "False" ]; then
    pass "detect reports installed=false after uninstall"
else
    fail "detect should report installed=false after uninstall"
fi

echo ""
echo "deepseek-harness adapter install tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
