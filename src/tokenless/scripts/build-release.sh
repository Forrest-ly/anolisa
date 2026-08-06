#!/usr/bin/env bash
# =============================================================================
# Build a tokenless binary release tarball.
#
# Usage:
#   scripts/build-release.sh [distro-suffix]
#
# The optional distro-suffix is appended to the package directory and tarball
# name, e.g.:
#   scripts/build-release.sh          -> dist/tokenless-0.7.4-x86_64.tar.gz
#   scripts/build-release.sh 7u       -> dist/tokenless-0.7.4-7u-x86_64.tar.gz
#
# The script is intended to run on the target Linux distribution so that the
# resulting binaries are linked against the same glibc version. The "7u" suffix
# denotes the Anolis OS 7u / Alibaba Cloud Linux 2 / el7 compatible build.
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

VERSION="$(grep '^version' Cargo.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')"
ARCH="$(rustc -vV 2>/dev/null | sed -n 's/^host: *\([^ -]*\).*/\1/p' || uname -m)"
DISTRO_SUFFIX="${1:-}"

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
log()  { echo -e "\033[0;36m[INFO]\033[0m $*" >&2; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*" >&2; }
err()  { echo -e "\033[0;31m[ERROR]\033[0m $*" >&2; }

# -----------------------------------------------------------------------------
# Pre-flight guards
# -----------------------------------------------------------------------------
check_cargo_config() {
    local fail=0
    local cargo_home="${CARGO_HOME:-${HOME}/.cargo}"
    for cfg in ".cargo/config.toml" ".cargo/config" \
               "${cargo_home}/config.toml" "${cargo_home}/config"; do
        if [ -f "${cfg}" ]; then
            if grep -qE '^\s*target\s*=' "${cfg}" 2>/dev/null; then
                err "Cargo config ${cfg} sets [build] target."
                fail=1
            fi
            if grep -qE '^\s*target-dir\s*=' "${cfg}" 2>/dev/null; then
                err "Cargo config ${cfg} sets [build] target-dir."
                fail=1
            fi
        fi
    done
    if [ "${fail}" -ne 0 ]; then
        exit 1
    fi
}

check_environment() {
    if [ "$(uname -s)" != "Linux" ]; then
        err "release builds are only supported on Linux (current: $(uname -s))."
        exit 1
    fi

    local host_arch
    host_arch="$(rustc -vV 2>/dev/null | sed -n 's/^host: *\([^ -]*\).*/\1/p' || uname -m)"

    if [ -n "${CARGO_BUILD_TARGET:-}" ]; then
        err "CARGO_BUILD_TARGET is set to ${CARGO_BUILD_TARGET}."
        err "release builds always use target/release/ — cross-compilation is not supported."
        exit 1
    fi

    if [ -n "${CARGO_TARGET_DIR:-}" ]; then
        err "CARGO_TARGET_DIR is set to ${CARGO_TARGET_DIR}."
        err "release builds always read from target/release/ — custom target dirs are not supported."
        exit 1
    fi

    if [ "${ARCH}" != "${host_arch}" ]; then
        err "ARCH=${ARCH} does not match build host architecture (${host_arch})."
        err "cross-architecture release builds are not supported."
        exit 1
    fi
}

# -----------------------------------------------------------------------------
# Build
# -----------------------------------------------------------------------------
build_binaries() {
    log "Building tokenless, rtk and openclaw plugin..."
    make build
}

# -----------------------------------------------------------------------------
# Generate install.sh
# -----------------------------------------------------------------------------
write_install_script() {
    local pkg_dir="$1"
    local install_sh="${pkg_dir}/install.sh"

    cat > "${install_sh}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-/usr/local}"

# --- Preflight: PREFIX must be an absolute path ---
case "$PREFIX" in
    /*) ;;
    *)  echo "ERROR: PREFIX must be an absolute path (got: $PREFIX)"; exit 1 ;;
esac

# --- Preflight: ensure BINDIR parent is a directory ---
if [ -e "$PREFIX/bin" ] && [ ! -d "$PREFIX/bin" ]; then
    echo "ERROR: $PREFIX/bin exists but is not a directory"; exit 1
fi

# Path layout — keep in sync with Makefile
BINDIR="$PREFIX/bin"
LIBEXECDIR="$PREFIX/libexec/anolisa/tokenless"
SHARE_DIR="$PREFIX/share/anolisa/adapters/tokenless"
COMPONENTS_DIR="$PREFIX/share/anolisa/components/tokenless"
COSH_EXT_DIR="$PREFIX/share/anolisa/extensions/tokenless"

echo "==> tokenless installer"
echo "    Install prefix : $PREFIX"
echo "    Binaries       : $BINDIR"
echo "    Libexec        : $LIBEXECDIR"
echo "    Adapters       : $SHARE_DIR"
echo "    Cosh extension : $COSH_EXT_DIR"
echo "    Note: this script will OVERWRITE rtk/toon symlinks in $BINDIR,"
echo "          replace all files under $SHARE_DIR,"
echo "          and replace the cosh extension at $COSH_EXT_DIR"
echo "==> Installing tokenless from $SCRIPT_DIR ..."
echo ""

# --- Install binaries ---
install -d -m 0755 "$BINDIR" "$LIBEXECDIR"
install -m 0755 "$SCRIPT_DIR/bin/tokenless" "$BINDIR/"
install -m 0755 "$SCRIPT_DIR/libexec/rtk" "$LIBEXECDIR/"

# rtk symlink
TARGET="$LIBEXECDIR/rtk"
if [ -L "$BINDIR/rtk" ]; then
    RTK_LINK=$(readlink "$BINDIR/rtk" || true)
    case "$RTK_LINK" in
        *"$PREFIX"*) ;;
        *) echo "WARNING: $BINDIR/rtk is an existing symlink pointing to $RTK_LINK — will be overwritten" ;;
    esac
elif [ -e "$BINDIR/rtk" ]; then
    echo "WARNING: $BINDIR/rtk already exists — will be overwritten"
fi
ln -sf "$TARGET" "$BINDIR/"

# toon (conditional)
if [ -f "$SCRIPT_DIR/libexec/toon" ]; then
    install -m 0755 "$SCRIPT_DIR/libexec/toon" "$LIBEXECDIR/"
    TARGET="$LIBEXECDIR/toon"
    if [ -L "$BINDIR/toon" ]; then
        TOON_LINK=$(readlink "$BINDIR/toon" || true)
        case "$TOON_LINK" in
            *"$PREFIX"*) ;;
            *) echo "WARNING: $BINDIR/toon is an existing symlink pointing to $TOON_LINK — will be overwritten" ;;
        esac
    elif [ -e "$BINDIR/toon" ]; then
        echo "WARNING: $BINDIR/toon already exists — will be overwritten"
    fi
    ln -sf "$TARGET" "$BINDIR/"
else
    # Clean up toon from a previous install when this release does not bundle it.
    if [ -e "$LIBEXECDIR/toon" ]; then
        rm -f "$LIBEXECDIR/toon"
    fi
    if [ -L "$BINDIR/toon" ]; then
        TOON_LINK=$(readlink "$BINDIR/toon" 2>/dev/null || true)
        TOON_REAL=$(cd "$BINDIR" && realpath -m "$TOON_LINK" 2>/dev/null || echo "$TOON_LINK")
        TOON_EXPECT="$(realpath -m "$LIBEXECDIR/toon" 2>/dev/null || echo "$LIBEXECDIR/toon")"
        if [ "$TOON_REAL" = "$TOON_EXPECT" ]; then
            rm -f "$BINDIR/toon"
            echo "NOTE: removed $BINDIR/toon symlink (no longer bundled)"
        else
            echo "NOTE: $BINDIR/toon is an external symlink ($TOON_LINK) — left untouched"
        fi
    fi
fi

# --- Adapter resources ---
# Use cp -r (NOT cp -pr) to avoid preserving source file ownership.
if [ -d "$SHARE_DIR" ] && [ "$(ls -A "$SHARE_DIR" 2>/dev/null)" ]; then
    echo "WARNING: $SHARE_DIR is not empty — all contents will be replaced"
fi
rm -rf "$SHARE_DIR"
install -d -m 0755 "$SHARE_DIR"
cp -r "$SCRIPT_DIR/share/adapters/tokenless/." "$SHARE_DIR/"
find "$SHARE_DIR" -type f \( -name '*.py' -o -name '*.sh' \) -exec chmod 0755 {} +

# --- Component contract ---
if [ -f "$SCRIPT_DIR/share/component.toml" ]; then
    install -d -m 0755 "$COMPONENTS_DIR"
    install -m 0644 "$SCRIPT_DIR/share/component.toml" "$COMPONENTS_DIR/"
fi

# --- Cosh extension ---
if [ -d "$COSH_EXT_DIR" ]; then
    echo "NOTE: replacing existing cosh extension at $COSH_EXT_DIR"
fi
rm -rf "$COSH_EXT_DIR"
install -d -m 0755 "$COSH_EXT_DIR/hooks" "$COSH_EXT_DIR/commands"
if [ -d "$SHARE_DIR/common/hooks" ] && [ "$(ls -A "$SHARE_DIR/common/hooks" 2>/dev/null)" ]; then
    cp -r "$SHARE_DIR/common/hooks/." "$COSH_EXT_DIR/hooks/"
fi
if [ -d "$SHARE_DIR/common/commands" ] && [ "$(ls -A "$SHARE_DIR/common/commands" 2>/dev/null)" ]; then
    cp -r "$SHARE_DIR/common/commands/." "$COSH_EXT_DIR/commands/"
fi
if [ -f "$SHARE_DIR/common/cosh-extension.json" ]; then
    install -m 0644 "$SHARE_DIR/common/cosh-extension.json" "$COSH_EXT_DIR/"
fi
find "$COSH_EXT_DIR" -type f \( -name '*.py' -o -name '*.sh' \) -exec chmod 0755 {} +
echo "==> cosh extension installed to $COSH_EXT_DIR"

echo ""
echo "==> Done. Run 'tokenless --version' to verify."
echo "==> Cosh extension deployed to $COSH_EXT_DIR."
echo "==> If cosh does not scan $PREFIX/share/anolisa/extensions by default,"
echo "    you may need to configure cosh to include this extension path."
EOF

    sed -i "s/@VERSION@/${VERSION}/g" "${install_sh}"
    chmod +x "${install_sh}"
}

# -----------------------------------------------------------------------------
# Package
# -----------------------------------------------------------------------------
package_release() {
    local pkg_name="tokenless-${VERSION}"
    if [ -n "${DISTRO_SUFFIX}" ]; then
        pkg_name="${pkg_name}-${DISTRO_SUFFIX}"
    fi
    local pkg_dir="dist/${pkg_name}"

    log "Packaging ${pkg_name} for ${ARCH}..."
    rm -rf "${pkg_dir}"
    mkdir -p "${pkg_dir}/bin" \
             "${pkg_dir}/libexec" \
             "${pkg_dir}/share/adapters/tokenless" \
             "${pkg_dir}/docs"

    install -m 0755 target/release/tokenless "${pkg_dir}/bin/"
    install -m 0755 third_party/rtk/target/release/rtk "${pkg_dir}/libexec/"

    # toon: build into a private staging dir so we package the binary from THIS build.
    local staging
    staging="$(mktemp -d)"
    if cargo install toon-format --version "${TOON_VER:-0.5.0}" --locked --root "${staging}" 2>"${staging}/install.err"; then
        if [ -x "${staging}/bin/toon" ]; then
            install -m 0755 "${staging}/bin/toon" "${pkg_dir}/libexec/toon"
            log "Bundled toon $(${pkg_dir}/libexec/toon --version 2>/dev/null || echo "${TOON_VER:-0.5.0}")"
        fi
    else
        warn "toon install failed — skipping (see ${staging}/install.err)"
        cat "${staging}/install.err" >&2
    fi
    rm -rf "${staging}"

    # Adapter resources (exclude build-only artifacts).
    tar -cf - -C adapters/tokenless \
        --exclude='node_modules' \
        --exclude='.tsbuildinfo' \
        --exclude='*.in' \
        --exclude='tests' \
        . | tar -xf - -C "${pkg_dir}/share/adapters/tokenless/"
    find "${pkg_dir}/share/adapters/tokenless" \
        -type f \( -name '*.py' -o -name '*.sh' \) -exec chmod 0755 {} +

    # Component contract, docs and license.
    install -m 0644 .anolisa/component.toml "${pkg_dir}/share/component.toml"
    install -m 0644 ../../docs/user-guide/en/token-saving/tokenless/user-manual.md \
        "${pkg_dir}/docs/tokenless-user-manual-en.md"
    install -m 0644 ../../docs/user-guide/zh/token-saving/tokenless/user-manual.md \
        "${pkg_dir}/docs/tokenless-user-manual-zh.md"
    install -m 0644 docs/response-compression.md "${pkg_dir}/docs/"
    install -m 0644 LICENSE "${pkg_dir}/"

    # Embedded install script.
    write_install_script "${pkg_dir}"

    rm -f "dist/${pkg_name}-${ARCH}.tar.gz"
    tar czf "dist/${pkg_name}-${ARCH}.tar.gz" -C dist "${pkg_name}"
    rm -rf "${pkg_dir}"

    echo ""
    log "Release tarball: dist/${pkg_name}-${ARCH}.tar.gz $(du -sh "dist/${pkg_name}-${ARCH}.tar.gz" | cut -f1)"
    log "Extract and run ./install.sh to deploy."
}

# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
main() {
    local arg="${1:-}"
    case "${arg}" in
        --check-cargo-config)
            check_cargo_config
            echo "OK: no Cargo config overrides detected"
            exit 0
            ;;
        --install-script-only)
            # Generate install.sh for the current VERSION (used by CI tests).
            DISTRO_SUFFIX=""
            pkg_dir="dist/tokenless-${VERSION}"
            mkdir -p "${pkg_dir}"
            write_install_script "${pkg_dir}"
            exit 0
            ;;
        "")
            DISTRO_SUFFIX=""
            ;;
        -*)
            err "Unknown option: $1"
            exit 1
            ;;
        *)
            DISTRO_SUFFIX="${arg}"
            ;;
    esac

    check_cargo_config
    check_environment
    build_binaries
    package_release
}

main "$@"
