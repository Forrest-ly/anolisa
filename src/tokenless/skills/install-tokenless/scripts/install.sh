#!/usr/bin/env bash
# install.sh — Agent-facing installer entry point for the install-tokenless skill.
#
# This script delegates to the canonical Token-Less installer. When run from a
# local checkout it uses the bundled install.sh; otherwise it downloads the
# installer from the alibaba/anolisa repository.
#
# Environment overrides:
#   TOKENLESS_INSTALL_REF  Git ref for remote installer download (default: main)
#                           Set to a version tag (e.g. v0.7.0) to pin the installer.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_INSTALLER="${SKILL_DIR}/../../install.sh"
INSTALL_REF="${TOKENLESS_INSTALL_REF:-main}"
REMOTE_INSTALLER="https://raw.githubusercontent.com/alibaba/anolisa/${INSTALL_REF}/src/tokenless/install.sh"

if [ -f "$REPO_INSTALLER" ]; then
  bash "$REPO_INSTALLER" "$@"
else
  command -v curl >/dev/null 2>&1 || {
    echo "error: curl is required but not found" >&2
    exit 1
  }
  curl -fsSL "$REMOTE_INSTALLER" | bash -s -- "$@"
fi
