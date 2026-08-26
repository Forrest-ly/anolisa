#!/usr/bin/env bash
# Regression tests for the release wheel gate (packaging/python/verify-wheels.py).
# Builds synthetic wheels with known metadata versions and checks the gate
# accepts a matching inventory while rejecting the tag/Cargo.toml drift that
# must never reach a GitHub Release.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERIFY="$ROOT/packaging/python/verify-wheels.py"
WORK="$(mktemp -d /tmp/tokenless-wheel-gate-test.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

# make_wheel <dir> <distribution> <version> <wheel-tag-suffix>
make_wheel() {
    local dir="$1" dist="$2" version="$3" suffix="$4"
    python3 - "$dir/${dist}-${version}-${suffix}.whl" "$dist" "$version" <<'PY'
import sys
import zipfile

path, distribution, version = sys.argv[1], sys.argv[2], sys.argv[3]
dist_info = f"{distribution}-{version}.dist-info"
with zipfile.ZipFile(path, "w") as archive:
    archive.writestr(
        f"{dist_info}/METADATA",
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n",
    )
    archive.writestr(
        f"{dist_info}/WHEEL",
        "Wheel-Version: 1.0\nGenerator: tokenless-wheel-gate-test\n",
    )
    archive.writestr(f"{dist_info}/RECORD", "")
PY
}

make_runtime_wheel() {
    make_wheel "$1" anolisa_tokenless "$2" "${3:-cp311-abi3-manylinux_2_28_x86_64}"
}

make_agentscope_wheel() {
    make_wheel "$1" anolisa_tokenless_agentscope "$2" "py3-none-any"
}

expect_ok() {
    local description="$1" dir="$2" version="$3"
    if python3 "$VERIFY" --directory "$dir" --version "$version" \
        >"$WORK/last.log" 2>&1; then
        echo "PASS: $description"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $description (gate rejected a valid inventory)"
        sed 's/^/    /' "$WORK/last.log"
        FAIL=$((FAIL + 1))
    fi
}

expect_reject() {
    local description="$1" dir="$2" version="$3" diagnostic="$4"
    if python3 "$VERIFY" --directory "$dir" --version "$version" \
        >"$WORK/last.log" 2>&1; then
        echo "FAIL: $description (gate accepted the wheels)"
        sed 's/^/    /' "$WORK/last.log"
        FAIL=$((FAIL + 1))
    elif grep -qF -- "$diagnostic" "$WORK/last.log"; then
        echo "PASS: $description"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $description (missing diagnostic: $diagnostic)"
        sed 's/^/    /' "$WORK/last.log"
        FAIL=$((FAIL + 1))
    fi
}

# Matching inventory passes.
SCENARIO="$WORK/matching"
install -d "$SCENARIO"
make_runtime_wheel "$SCENARIO" 0.7.13
make_agentscope_wheel "$SCENARIO" 0.7.13
expect_ok "matching wheel versions pass" "$SCENARIO" 0.7.13

# Reviewer scenario: tag v0.7.14 on a commit still building 0.7.13 wheels.
SCENARIO="$WORK/stale-tag"
install -d "$SCENARIO"
make_runtime_wheel "$SCENARIO" 0.7.13
make_agentscope_wheel "$SCENARIO" 0.7.13
expect_reject "stale wheels under a newer release version are rejected" \
    "$SCENARIO" 0.7.14 "does not match release version 0.7.14"

# Only the AgentScope wheel out of sync.
SCENARIO="$WORK/agentscope-drift"
install -d "$SCENARIO"
make_runtime_wheel "$SCENARIO" 0.7.13
make_agentscope_wheel "$SCENARIO" 0.7.12
expect_reject "AgentScope version drift is rejected" \
    "$SCENARIO" 0.7.13 "does not match release version 0.7.13"

# Metadata version disagrees with the file name.
SCENARIO="$WORK/metadata-drift"
install -d "$SCENARIO"
make_runtime_wheel "$SCENARIO" 0.7.13
make_wheel "$SCENARIO" anolisa_tokenless_agentscope 0.7.12 "py3-none-any"
python3 - "$SCENARIO/anolisa_tokenless_agentscope-0.7.12-py3-none-any.whl" <<'PY'
import sys
import zipfile

path = sys.argv[1]
with zipfile.ZipFile(path) as archive:
    metadata = archive.read(
        "anolisa_tokenless_agentscope-0.7.12.dist-info/METADATA"
    ).decode("utf-8")
rewritten = metadata.replace("Version: 0.7.12", "Version: 0.7.13")
with zipfile.ZipFile(path, "w") as archive:
    archive.writestr(
        "anolisa_tokenless_agentscope-0.7.12.dist-info/METADATA", rewritten
    )
PY
expect_reject "file name and METADATA version disagreement is rejected" \
    "$SCENARIO" 0.7.13 "file name version 0.7.12 does not match"

# Missing AgentScope wheel.
SCENARIO="$WORK/missing-agentscope"
install -d "$SCENARIO"
make_runtime_wheel "$SCENARIO" 0.7.13
expect_reject "missing AgentScope wheel is rejected" \
    "$SCENARIO" 0.7.13 "exactly one anolisa_tokenless_agentscope wheel"

# Runtime wheel without the CPython 3.11 stable ABI tag.
SCENARIO="$WORK/wrong-abi"
install -d "$SCENARIO"
make_runtime_wheel "$SCENARIO" 0.7.13 "cp310-abi3-manylinux_2_28_x86_64"
make_agentscope_wheel "$SCENARIO" 0.7.13
expect_reject "non cp311-abi3 runtime wheel is rejected" \
    "$SCENARIO" 0.7.13 "CPython 3.11 stable ABI"

# Unexpected extra wheel.
SCENARIO="$WORK/stray-wheel"
install -d "$SCENARIO"
make_runtime_wheel "$SCENARIO" 0.7.13
make_agentscope_wheel "$SCENARIO" 0.7.13
make_wheel "$SCENARIO" some_other_package 1.0.0 "py3-none-any"
expect_reject "stray wheel in the release directory is rejected" \
    "$SCENARIO" 0.7.13 "unexpected wheel"

echo
echo "wheel release gate tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
