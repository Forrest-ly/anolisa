---
name: install-tokenless
version: 1.0.0
description: Install the Token-Less toolkit (tokenless, rtk, toon) and its framework adapters on Linux or macOS. Use when the user asks to install Token-Less, set up tokenless, install the anolisa tokenless toolkit, or enable token-saving hooks.
layer: application
lifecycle: usage
platforms: [cosh, qoder]
---

# Install Token-Less

Installs the Token-Less CLI toolkit and framework adapters.

## Supported platforms

- Linux (glibc) x86_64 / aarch64
- macOS x86_64 (Intel) / aarch64 (Apple Silicon)

> musl-based Linux (e.g. Alpine) is not supported by the prebuilt binaries; build from source instead.

## Installation workflow

```
Task Progress:
- [ ] Step 1: Detect platform and prerequisites
- [ ] Step 2: Install Token-Less
- [ ] Step 3: Verify installation
- [ ] Step 4: Register framework adapters (optional)
```

### Step 1: Check prerequisites

Ensure the machine is running a supported Linux/macOS platform and has `curl` available.

```bash
curl --version
```

### Step 2: Install Token-Less

Run the installer script. It uses npm when available and falls back to installing Node.js LTS via nvm if npm is missing.

```bash
bash scripts/install.sh
```

Override behavior with environment variables:

| Variable | Default | Description |
|---|---|---|
| `TOKENLESS_VERSION` | `latest` | npm version tag to install |
| `TOKENLESS_NPM_REGISTRY` | `https://registry.npmjs.org/` | npm registry override |
| `TOKENLESS_INSTALL_PREFIX` | none | npm global prefix override |
| `TOKENLESS_INSTALL_REF` | `main` | Git ref for remote installer (set to a version tag to pin) |

### Step 3: Verify installation

```bash
tokenless --version
rtk --version
toon --version
```

### Step 4: Register adapters (optional)

After installation, register Token-Less with the active agent framework.
The adapter paths below assume the default install location
(`~/.local/share/anolisa/adapters/tokenless/`). If you use a custom
`XDG_DATA_HOME`, replace `~/.local/share` with your value.
Alternatively, use the corresponding `make` targets (e.g. `make claude-code-install`,
`make qoder-install`) from the repository root which handle path resolution automatically.

```bash
# Claude Code
bash ~/.local/share/anolisa/adapters/tokenless/claude-code/scripts/install.sh

# Codex
bash ~/.local/share/anolisa/adapters/tokenless/codex/scripts/install.sh

# OpenCode
bash ~/.local/share/anolisa/adapters/tokenless/opencode/scripts/install.sh

# Qoder CLI
bash ~/.local/share/anolisa/adapters/tokenless/qoder/scripts/install.sh

# Hermes Agent
bash ~/.local/share/anolisa/adapters/tokenless/hermes/scripts/install.sh
```

## Uninstall

```bash
npm uninstall -g anolisa-tokenless
rm -rf ~/.local/share/anolisa/adapters/tokenless
```
