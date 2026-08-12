#!/usr/bin/env bash
# detect.sh — Inspect tokenless AgentScope integration. Read-only.
#
# Reports Python, AgentScope SDK, tokenless binary, and adapter resource
# availability. Exit codes:
#   0 = installed and ready
#   1 = not installed but installable
#   2 = missing prerequisites
set -euo pipefail

AGENT="${ANOLISA_TARGET:-agentscope}"
COMPONENT="${ANOLISA_COMPONENT:-tokenless}"
ADAPTER_DIR="${ANOLISA_ADAPTER_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 2>/dev/null || true)}"

PLUGIN_ID="tokenless"
PLUGIN_SRC="$ADAPTER_DIR/agentscope"

line()  { printf '[%s] %s\n' "$COMPONENT" "$*"; }
field() { printf '[%s]   %-26s %s\n' "$COMPONENT" "$1" "$2"; }

PREREQ_MISSING=()
INSTALL_MISSING=()
note_prereq_missing() { PREREQ_MISSING+=("$1"); }
note_install_missing() { INSTALL_MISSING+=("$1"); }

line "${AGENT} detect"

if [ -n "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ]; then
    field "python3" "present (${PYTHON_BIN})"
else
    field "python3" "missing"
    note_prereq_missing "python3"
fi

if [ -n "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ]; then
    if "$PYTHON_BIN" -c "import agentscope" 2>/dev/null; then
        field "AgentScope SDK" "present"
    else
        field "AgentScope SDK" "missing (pip install agentscope)"
        note_prereq_missing "agentscope package"
    fi
else
    note_prereq_missing "agentscope package (no python3 to probe)"
fi

runtime_bin="$(command -v tokenless 2>/dev/null || true)"
if [ -n "$runtime_bin" ]; then
    field "tokenless binary" "present (${runtime_bin})"
else
    field "tokenless binary" "missing"
    note_prereq_missing "tokenless binary"
fi

if [ -d "$PLUGIN_SRC" ] && [ -f "$PLUGIN_SRC/__init__.py" ]; then
    field "plugin resource" "present (${PLUGIN_SRC})"
else
    field "plugin resource" "missing (${PLUGIN_SRC})"
    note_prereq_missing "plugin resource"
fi

# The Python package may be installed via pip or via install.sh symlink.
installed=0
if [ -n "$PYTHON_BIN" ] && [ -x "$PYTHON_BIN" ]; then
    if "$PYTHON_BIN" -c "import anolisa_tokenless_agentscope" 2>/dev/null; then
        installed=1
    fi
fi
if [ "$installed" -eq 1 ]; then
    field "${PLUGIN_ID} middleware" "installed"
else
    field "${PLUGIN_ID} middleware" "missing"
    note_install_missing "${PLUGIN_ID} middleware"
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
