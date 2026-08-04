#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Baseline: all metadata is currently synchronized.
python3 scripts/check-component-versions.py

# Regression: a tokenless catalog version that drifts from Cargo.toml must be
# reported as an error. We temporarily bump only the catalog manifest, run the
# checker, and then restore the original file.
CATALOG="src/anolisa/manifests/components/tokenless/component.toml"
VERSION=$(grep '^version' src/tokenless/Cargo.toml | head -1 | sed 's/.*"\([^"]*\)".*/\1/')
BACKUP=$(mktemp)
MISMATCH_LOG=$(mktemp)
trap 'rm -f "$BACKUP" "$MISMATCH_LOG"; git checkout -- "$CATALOG" 2>/dev/null || true' EXIT

cp "$CATALOG" "$BACKUP"
python3 -c "
import pathlib, re
path = pathlib.Path('$CATALOG')
text = path.read_text()
text = re.sub(r'^(version = \")$VERSION(\")', r'\\g<1>99.99.99\\g<2>', text, flags=re.M)
path.write_text(text)
"

if python3 scripts/check-component-versions.py > "$MISMATCH_LOG" 2>&1; then
    echo "ERROR: check-component-versions.py did not fail on mismatched tokenless catalog version" >&2
    exit 1
fi

if ! grep -qF "$CATALOG" "$MISMATCH_LOG"; then
    echo "ERROR: mismatch output did not mention $CATALOG" >&2
    cat "$MISMATCH_LOG" >&2
    exit 1
fi

echo "Tokenless catalog version mismatch regression test passed"
