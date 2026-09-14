#!/usr/bin/env bash
# Regression test for the best-effort RTK version probe in
# benchmark/l1-compressor/run-benchmarks.sh (the `rtk_version` field of
# benchmark_identity.json).
#
# The full runner builds and benches the suite, so this test executes only its
# source-identity prelude: the script is truncated just before the
# "Source identity recorded" line, which keeps rtk discovery, the bounded
# --version probe and the identity writer intact while dropping cargo work.
#
# Covered:
#   1. a bare command name ($RTK_BIN=rtk) is resolved through PATH, matching
#      the Rust find_rtk_binary convention
#   2. an explicit path ($RTK_BIN=/abs/rtk) still resolves
#   3. missing / non-executable / empty-output rtk records "unavailable"
#   4. a hanging rtk is bounded by RTK_VERSION_TIMEOUT_SECS and reaped
#   5. only the first --version line is recorded, and a non-zero exit does not
#      corrupt the identity JSON
#   6. the probe still works on hosts without a timeout(1) helper

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="$SCRIPT_DIR/../benchmark/l1-compressor/run-benchmarks.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

L1_DIR="$WORK/benchmark/l1-compressor"
BIN_DIR="$WORK/bin"
mkdir -p "$L1_DIR" "$BIN_DIR"

# The prelude reads the workspace version from ../../Cargo.toml and hashes
# fixtures/*.json, so give it both (an empty fixture glob makes the prelude's
# `|| echo "unknown"` fallback append to the hash and emit invalid JSON).
printf '[workspace.package]\nversion = "0.7.4"\n' > "$WORK/Cargo.toml"
mkdir -p "$L1_DIR/fixtures"
printf '{"probe":"fixture"}\n' > "$L1_DIR/fixtures/sample.json"

awk '/^echo "==> Source identity recorded/{exit} {print}' "$RUNNER" > "$L1_DIR/prelude.sh"
# Same prelude with the timeout(1) helper disabled, to exercise the fallback.
sed 's|^    RTK_TIMEOUT_CMD="timeout"$|    RTK_TIMEOUT_CMD=""|' "$L1_DIR/prelude.sh" \
    > "$L1_DIR/prelude-no-helper.sh"

CURRENT_RUNNER="$L1_DIR/prelude.sh"
IDENTITY_FILE="$L1_DIR/benchmark_identity.json"

make_stub() { # make_stub <path> <body>
    printf '#!/usr/bin/env bash\n%s\n' "$2" > "$1"
    chmod +x "$1"
}

# probe [VAR=value ...] -> recorded rtk_version
probe() {
    rm -f "$IDENTITY_FILE"
    env "$@" bash "$CURRENT_RUNNER" > /dev/null 2>&1 || true
    sed -n 's/^[[:space:]]*"rtk_version": "\(.*\)",[[:space:]]*$/\1/p' "$IDENTITY_FILE"
}

FAILED=0
check() { # check <label> <actual> <expected>
    if [ "$2" = "$3" ]; then
        echo "  ok   $1"
    else
        echo "  FAIL $1: expected '$3', got '$2'"
        FAILED=1
    fi
}

echo "benchmark rtk version probe:"

# 1. Bare command name on PATH, nothing named rtk in the working directory —
#    the case the Rust discovery still resolves and runs.
make_stub "$BIN_DIR/rtk" 'echo "rtk 0.1-test"'
check "bare command name resolved through PATH" \
    "$(probe RTK_BIN=rtk "PATH=$BIN_DIR:$PATH")" "rtk 0.1-test"

# 6. Same binary, no timeout(1) helper available.
CURRENT_RUNNER="$L1_DIR/prelude-no-helper.sh"
check "PATH resolution without a timeout(1) helper" \
    "$(probe RTK_BIN=rtk "PATH=$BIN_DIR:$PATH")" "rtk 0.1-test"
CURRENT_RUNNER="$L1_DIR/prelude.sh"

# 2. Explicit paths: absolute and relative-to-CWD.
make_stub "$BIN_DIR/rtk-abs" 'echo "rtk 0.2-abs"'
check "absolute RTK_BIN path" \
    "$(probe "RTK_BIN=$BIN_DIR/rtk-abs")" "rtk 0.2-abs"

# 3a. Default vendored location absent.
check "missing vendored rtk" "$(probe)" "unavailable"

# 3b. Present but not executable (bash's `command -v` also reports
#     non-executable PATH hits, so the resolved path must be re-checked).
mkdir -p "$WORK/third_party/rtk/target/release"
printf '#!/usr/bin/env bash\necho "rtk 0.3-noexec"\n' \
    > "$WORK/third_party/rtk/target/release/rtk"
chmod 0644 "$WORK/third_party/rtk/target/release/rtk"
check "non-executable vendored rtk" "$(probe)" "unavailable"
check "bare command name absent from PATH" \
    "$(probe RTK_BIN=rtk-not-installed "PATH=$BIN_DIR:$PATH")" "unavailable"

# 3c. Executable but silent on stdout.
make_stub "$BIN_DIR/rtk-silent" 'exit 0'
check "empty --version output" "$(probe "RTK_BIN=$BIN_DIR/rtk-silent")" "unavailable"

# 4. Hanging binary: bounded by the deadline, child reaped.
HANG_PID_FILE="$WORK/hang.pid"
# shellcheck disable=SC2016 # Stub body must stay literal: it is expanded by the
# stub's own shell at probe time, not by make_stub.
make_stub "$BIN_DIR/rtk-hang" 'echo $$ > "$RTK_HANG_PID_FILE"; exec sleep 30'
START=$(date +%s)
check "hanging rtk bounded by RTK_VERSION_TIMEOUT_SECS" \
    "$(probe "RTK_BIN=$BIN_DIR/rtk-hang" RTK_VERSION_TIMEOUT_SECS=1 "RTK_HANG_PID_FILE=$HANG_PID_FILE")" \
    "unavailable"
ELAPSED=$(( $(date +%s) - START ))
if [ "$ELAPSED" -le 5 ]; then
    echo "  ok   probe returned after ${ELAPSED}s (deadline 1s)"
else
    echo "  FAIL probe took ${ELAPSED}s for a 1s deadline"
    FAILED=1
fi
if [ -f "$HANG_PID_FILE" ] && ! kill -0 "$(cat "$HANG_PID_FILE")" 2>/dev/null; then
    echo "  ok   timed-out probe child was reaped"
else
    echo "  FAIL timed-out probe child still running"
    FAILED=1
fi

# 5. Multi-line output and non-zero exit must not corrupt the identity file.
make_stub "$BIN_DIR/rtk-multi" 'echo "rtk 0.4-multi"; echo "build abc123"'
check "only the first --version line recorded" \
    "$(probe "RTK_BIN=$BIN_DIR/rtk-multi")" "rtk 0.4-multi"
make_stub "$BIN_DIR/rtk-nonzero" 'echo "rtk 0.5-nonzero"; exit 3'
check "version kept when --version exits non-zero" \
    "$(probe "RTK_BIN=$BIN_DIR/rtk-nonzero")" "rtk 0.5-nonzero"
if command -v python3 > /dev/null 2>&1; then
    python3 -m json.tool "$IDENTITY_FILE" > /dev/null
    echo "  ok   benchmark_identity.json is valid JSON"
fi

if [ "$FAILED" -ne 0 ]; then
    echo "benchmark rtk version probe test FAILED"
    exit 1
fi
echo "benchmark rtk version probe test passed"
