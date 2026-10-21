# OS Skills

OS Skills is a system management and DevOps skill library for AI Agents. It provides pre-built skills that enable Agents to perform common system administration and automation tasks.

---

## Overview

OS Skills groups its skills into six categories:

- **AI Tools** — install and configure agent tooling (Claude Code, OpenClaw, QwenPaw, Hermes, Tokenless, MCP)
- **System Admin** — Alibaba Cloud Linux system management, storage resize, kernel upgrade and tuning, backup and restore, shell and regex authoring
- **DevOps** — GitHub workflows, kernel development, SysOM diagnosis, AgentSight token and audit queries
- **Alibaba Cloud** — ECS lifecycle management through the Alibaba Cloud CLI
- **Security** — Alibaba Cloud Linux CVE queries
- **Others** — product guides, skill management, document and image tooling

---

## Installation

```bash
anolisa install os-skills
```

---

## Quick Start

Once installed, OS Skills are available to any ANOLISA-compatible Agent runtime. The Agent can invoke skills via natural language:

```
> "Check whether a CVE affects my Alibaba Cloud Linux instance"
> "Expand the XFS filesystem after growing the data disk"
> "Install and configure Claude Code with a custom API endpoint"
```

---

## Skill Categories

Skills are grouped by the category directories under `src/os-skills/`. The name shown for each skill is its installed directory name; the authoritative definition of a skill lives in the `SKILL.md` of that directory.

### AI Tools (`ai/`)

| Skill | Description |
|-------|-------------|
| `install-claude-code` | Install and configure Claude Code on RPM-based Linux, with fallback install methods and a DashScope/Qwen API endpoint template |
| `install-hermes` | Install Hermes Agent and configure its LLM providers, messaging platforms and web workspace |
| `install-openclaw` | Install OpenClaw non-interactively with Alibaba Cloud Model Studio credentials and billing options |
| `install-qwenpaw` | Deploy the QwenPaw AI assistant on Alibaba Cloud Linux 4, including Model Studio API key and DingTalk channel setup |
| `install-tokenless` | Install Tokenless (LLM token optimization) and enable it for a supported agent framework |
| `qwenpaw-usage` | QwenPaw CLI operations: heartbeat tasks, scheduled jobs, Docker packaging, workspace export |
| `setup-mcp` | Add and configure MCP servers in cosh, converting pasted JSON into a valid configuration |

### System Admin (`system-admin/`)

| Skill | Description |
|-------|-------------|
| `alinux-admin` | Alibaba Cloud Linux 4 system management: shell, systemd units, SSH, firewalld, NetworkManager, permissions, processes, disks |
| `backup-restore` | Backup and restore for workspaces, user data, the operating system, and Alibaba Cloud snapshots |
| `ktuner` | Kernel parameter tuning: analyzes the system, proposes tunings, applies them on request, and rolls back automatically |
| `regex-mastery` | Write, debug and optimize regular expressions |
| `shell-scripting` | Portable bash/zsh scripting: argument parsing, error handling, CLI automation |
| `storage-resize` | Grow partitions and XFS/EXT4/Btrfs filesystems after a cloud disk expansion |
| `upgrade-alinux-kernel` | Upgrade the Linux kernel on Alibaba Cloud Linux instances |

### DevOps (`devops/`)

| Skill | Description |
|-------|-------------|
| `github` | GitHub workflows through the `gh` CLI: issues, pull requests, CI runs, API queries |
| `kernel-dev` | Alibaba Cloud Linux kernel development automation: SRPM and upstream builds, module development environment, dependencies (x86_64 and aarch64) |
| `sysom-agentsight` | Query AgentSight token usage, token savings, audit events and session interruption events |
| `sysom-diagnosis` | SysOM diagnosis for CPU, memory, disk IO and network problems; reports recommendations without applying fixes |

### Alibaba Cloud (`aliyun/`)

| Skill | Description |
|-------|-------------|
| `aliyun-ecs` | Manage ECS through the Alibaba Cloud CLI: instances, disks, snapshots, images, security groups, key pairs, elastic network interfaces |

### Security (`security/`)

| Skill | Description |
|-------|-------------|
| `alinux-cve-query` | Query Alibaba Cloud Linux CVE information: whether a CVE applies, whether it is fixed, and the related security advisories |

### Others (`others/`)

| Skill | Description |
|-------|-------------|
| `anolisa-guide` | Answer questions about ANOLISA (Alibaba Cloud Linux 4 Agentic Edition) and its components: usage, configuration, commands, quota and billing |
| `anolisa-register` | Manage Agentic OS registration: query the status, join the co-build program, opt out, review the privacy policy |
| `clawhub-skill-mng` | Search, install, uninstall, update and list agent skills through the ClawHub CLI |
| `cosh-guide` | Copilot Shell (cosh) CLI user guide: usage, command reference, configuration, features |
| `humanizer` | Remove signs of AI-generated writing from text |
| `image-gen` | Generate images from text prompts through DashScope/Qwen |
| `pdf-reader` | Extract text from PDF files |
| `xlsx` | Open, create, read, analyze, edit and validate spreadsheet files (`.xlsx`, `.xlsm`, `.csv`, `.tsv`) |

---

## Usage with Agent Runtimes

OS Skills integrates with cosh and other ANOLISA-compatible runtimes automatically. Skills are discovered at startup and made available to the Agent's tool inventory.

```bash
# Verify skills are loaded
anolisa status os-skills
```

Skills install as plain directories under the runtime skill path — `/usr/share/anolisa/skills/` for the system profile, `~/.copilot-shell/skills/` for the user profile — and are defined entirely by their `SKILL.md`. os-skills reads no configuration file: `anolisa install os-skills` installs the skill set and `anolisa uninstall os-skills` removes it.

---

## See Also

- [Copilot Shell](copilot-shell/QUICKSTART.md)
- [anolisa CLI](anolisa-cli.md)
