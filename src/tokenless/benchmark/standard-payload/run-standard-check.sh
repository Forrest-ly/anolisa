#!/usr/bin/env bash
# Copyright 2026 Alibaba Cloud
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Run the installed tokenless CLI over the standard payloads in this
# directory and print the recorded savings for each case as
# `stats summary --json` output. Compare the numbers against the reference
# table in the user-guide page "Compression rates and applicable scenarios"
# (docs/user-guide/<lang>/token-saving/tokenless/compression-scenarios.md).
#
# Usage:
#   ./run-standard-check.sh                 # uses `tokenless` from PATH
#   TOKENLESS_BIN=/path/to/tokenless ./run-standard-check.sh
#
# Each case runs with an isolated TOKENLESS_DATA_DIR, so your real
# statistics and stash databases are never touched.

set -euo pipefail
cd "$(dirname "$0")"

TOKENLESS_BIN="${TOKENLESS_BIN:-tokenless}"
if ! command -v "$TOKENLESS_BIN" >/dev/null 2>&1; then
  echo "error: tokenless binary not found on PATH; install tokenless or set TOKENLESS_BIN" >&2
  exit 1
fi

echo "binary: $("$TOKENLESS_BIN" --version)"
echo

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

run_case() {
  label="$1"
  input="$2"
  shift 2
  dir="$workdir/$label"
  mkdir -p "$dir"
  if ! TOKENLESS_DATA_DIR="$dir" TOKENLESS_SLS_ENABLED=0 \
    "$TOKENLESS_BIN" "$@" -f "$input" > "$dir/output.txt" 2> "$dir/stderr.txt"; then
    echo "=== case $label: FAILED ==="
    cat "$dir/stderr.txt" >&2
    return 1
  fi
  echo "=== case $label: $* -f $input ==="
  TOKENLESS_DATA_DIR="$dir" "$TOKENLESS_BIN" stats summary --json
  echo
}

run_case schema schema_tools.json compress-schema --batch --session-id stdpay-schema
run_case api response_api_records.json compress-response --session-id stdpay-api
run_case code response_code.json compress-response --session-id stdpay-code
run_case prose response_prose.json compress-response --session-id stdpay-prose
run_case toon response_api_records.json compress-toon --session-id stdpay-toon

echo "All cases finished. If a case records 0 records, the payload produced no"
echo "estimated token savings on this tokenless version and the original input"
echo "was emitted unchanged."
