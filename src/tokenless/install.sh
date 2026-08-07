#!/usr/bin/env bash
# install.sh — one-line installer for Token-Less.
#
# Usage (human users):
#   curl -fsSL https://raw.githubusercontent.com/alibaba/anolisa/main/src/tokenless/install.sh | bash
#
# Environment overrides:
#   TOKENLESS_VERSION    npm version tag to install (default: latest)
#   TOKENLESS_NPM_REGISTRY  npm registry override (default: https://registry.npmjs.org/)
#   TOKENLESS_INSTALL_PREFIX  npm prefix for global install (default: none)

set -euo pipefail

VERSION="${TOKENLESS_VERSION:-latest}"
NPM_REGISTRY="${TOKENLESS_NPM_REGISTRY:-https://registry.npmjs.org/}"
INSTALL_PREFIX="${TOKENLESS_INSTALL_PREFIX:-}"

log()  { printf '\033[1;32m%s\033[0m %s\n' "==>" "$*"; }
warn() { printf '\033[1;33m%s\033[0m %s\n' "warn:" "$*" >&2; }
err()  { printf '\033[1;31m%s\033[0m %s\n' "error:" "$*" >&2; exit 1; }

detect_platform() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"

  case "$os" in
    Linux)  OS="linux" ;;
    Darwin) OS="darwin" ;;
    *)      err "unsupported OS: $os (only Linux and macOS are supported)" ;;
  esac

  case "$arch" in
    x86_64|amd64)   ARCH="x64" ;;
    aarch64|arm64)  ARCH="arm64" ;;
    *)              err "unsupported architecture: $arch" ;;
  esac
}

check_musl() {
  [ "$OS" != "linux" ] && return 0
  # Detect musl by absence of glibc loader and presence of musl loader,
  # avoiding ldd which may be missing or unreliable on minimal distros.
  if [ ! -f /lib/x86_64-linux-gnu/libc.so.6 ] && \
     [ ! -f /lib/aarch64-linux-gnu/libc.so.6 ] && \
     [ ! -f /lib64/libc.so.6 ] && \
     [ ! -f /lib/libc.so.6 ]; then
    if ls /lib/ld-musl-*.so* >/dev/null 2>&1; then
      err "musl-based Linux distributions (e.g. Alpine) are not supported by the prebuilt binaries; build from source instead"
    fi
  fi
}

ensure_npm() {
  if command -v npm >/dev/null 2>&1; then
    return 0
  fi

  warn "npm not found; attempting to install Node.js via nvm..."

  if [ -z "${NVM_DIR:-}" ] && [ -d "$HOME/.nvm" ]; then
    export NVM_DIR="$HOME/.nvm"
  fi

  if [ -s "${NVM_DIR:-}/nvm.sh" ]; then
    # shellcheck source=/dev/null
    . "$NVM_DIR/nvm.sh"
    if command -v npm >/dev/null 2>&1; then
      return 0
    fi
    nvm install --lts
    return 0
  fi

  warn "nvm not found; installing nvm and Node.js LTS..."
  if ! command -v curl >/dev/null 2>&1; then
    err "curl is required to install Node.js automatically"
  fi
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
  export NVM_DIR="$HOME/.nvm"
  # shellcheck source=/dev/null
  . "$NVM_DIR/nvm.sh"
  nvm install --lts
}

install_with_npm() {
  local npm_args=("install" "-g" "anolisa-tokenless@${VERSION}" "--registry=${NPM_REGISTRY}")
  if [ -n "$INSTALL_PREFIX" ]; then
    npm_args+=("--prefix=${INSTALL_PREFIX}")
  fi

  log "installing anolisa-tokenless@${VERSION} via npm..."
  if ! npm "${npm_args[@]}"; then
    err "npm install failed"
  fi
}

verify_installation() {
  local bin_dir
  if [ -n "$INSTALL_PREFIX" ]; then
    bin_dir="${INSTALL_PREFIX}/bin"
  else
    bin_dir="$(npm prefix -g)/bin"
  fi

  if command -v tokenless >/dev/null 2>&1; then
    log "tokenless installed: $(tokenless --version)"
  elif [ -x "${bin_dir}/tokenless" ]; then
    log "tokenless installed to ${bin_dir}/tokenless"
    warn "${bin_dir} is not in your PATH; add it with: export PATH=\"${bin_dir}:\$PATH\""
  else
    warn "tokenless binary not found in PATH; you may need to add ${bin_dir} to PATH"
  fi

  log "done"
}

main() {
  detect_platform
  check_musl
  command -v curl >/dev/null 2>&1 || err "curl is required but not found"

  ensure_npm
  install_with_npm
  verify_installation
}

main
