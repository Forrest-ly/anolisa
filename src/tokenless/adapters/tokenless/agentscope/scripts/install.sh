#!/usr/bin/env bash
# install.sh — Install tokenless middleware for the AgentScope SDK.
#
# Makes the adapter importable as ``anolisa_tokenless_agentscope`` by
# symlinking the adapter directory into the active Python environment's
# user site-packages directory.
#
# Usage:
#   ./install.sh                    # Interactive / normal
#   ./install.sh --dry-run          # Print actions without changing state
#
# Environment variables:
#   PYTHON_BIN                 Python interpreter to use (default: python3)
#   TOKENLESS_INSTALL_PREFIX   Installation prefix for binaries (default: ~/.local)
#   ANOLISA_ADAPTER_DIR        Adapter source root (default: parent of scripts/)
#   ANOLISA_DRY_RUN            Set to "1" to print actions only
#   TOKENLESS_SOURCE_DIR       Path to anolisa/src/tokenless (dev-only override)

set -euo pipefail

AGENT="${ANOLISA_TARGET:-agentscope}"
COMPONENT="${ANOLISA_COMPONENT:-tokenless}"
ADAPTER_DIR="${ANOLISA_ADAPTER_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
DRY_RUN="${ANOLISA_DRY_RUN:-0}"
PREFIX="${TOKENLESS_INSTALL_PREFIX:-$HOME/.local}"
BINDIR="$PREFIX/bin"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 2>/dev/null || true)}"

PLUGIN_SRC="$ADAPTER_DIR/agentscope"
PACKAGE_NAME="anolisa_tokenless_agentscope"

line() { printf '[%s] %s\n' "$COMPONENT" "$*"; }

if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
    line "ERROR: python3 not found. Install python3 and try again." >&2
    exit 1
fi

line "Installing ${AGENT} middleware..."

if [ ! -d "$PLUGIN_SRC" ] || [ ! -f "$PLUGIN_SRC/__init__.py" ]; then
    line "ERROR: plugin source not found: $PLUGIN_SRC" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Phase 1 — Ensure tokenless binary is available
# ---------------------------------------------------------------------------

TOKENLESS_BIN=""
if command -v tokenless >/dev/null 2>&1; then
    TOKENLESS_BIN="$(command -v tokenless)"
elif [ -x "$BINDIR/tokenless" ]; then
    TOKENLESS_BIN="$BINDIR/tokenless"
elif [ -x /usr/local/bin/tokenless ]; then
    TOKENLESS_BIN="/usr/local/bin/tokenless"
elif [ -x /usr/bin/tokenless ]; then
    TOKENLESS_BIN="/usr/bin/tokenless"
fi

if [ -z "$TOKENLESS_BIN" ]; then
    if [ -n "${TOKENLESS_SOURCE_DIR:-}" ]; then
        SRCDIR="$TOKENLESS_SOURCE_DIR"
    else
        SRCDIR="$(cd "$SCRIPT_DIR/../../../.." && pwd 2>/dev/null || true)"
    fi

    if [ -z "$SRCDIR" ] || [ ! -f "$SRCDIR/Cargo.toml" ]; then
        line "ERROR: tokenless binary not found and no source tree at $SRCDIR" >&2
        exit 1
    fi

    line "Building tokenless from source: $SRCDIR"
    cd "$SRCDIR"
    cargo build --release -p tokenless-cli 2>&1 | tail -3
    mkdir -p "$BINDIR"
    cp "$SRCDIR/target/release/tokenless" "$BINDIR/tokenless"
    chmod 755 "$BINDIR/tokenless"
else
    line "Binary: $TOKENLESS_BIN ($("$TOKENLESS_BIN" --version))"
fi

# ---------------------------------------------------------------------------
# Phase 2 — Symlink package into Python user site-packages
# ---------------------------------------------------------------------------

SITE_DIR="$("$PYTHON_BIN" -m site --user-site)"
if [ -z "$SITE_DIR" ]; then
    line "ERROR: could not determine Python user site-packages directory" >&2
    exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
    line "DRY-RUN: mkdir -p $SITE_DIR"
    line "DRY-RUN: ln -sfn $PLUGIN_SRC $SITE_DIR/$PACKAGE_NAME"
    exit 0
fi

mkdir -p "$SITE_DIR"
ln -sfn "$PLUGIN_SRC" "$SITE_DIR/$PACKAGE_NAME"

if ! "$PYTHON_BIN" -c "import ${PACKAGE_NAME}" 2>/dev/null; then
    line "ERROR: installed package is not importable: $SITE_DIR/$PACKAGE_NAME" >&2
    exit 1
fi

line "${AGENT} middleware linked to $SITE_DIR/$PACKAGE_NAME."
line "Activate in your AgentScope application:"
line "  from ${PACKAGE_NAME} import register"
line "  register(toolkit)"
