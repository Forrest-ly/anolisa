#!/usr/bin/env bash
# install.sh — Register tokenless hooks for Kimi Code in ~/.kimi/config.toml.
#
# Kimi Code uses a flat TOML config with [[hooks]] entries rather than a
# plugin manifest. This script injects hook definitions that point to
# the shared tokenless hooks via the run-hook.sh dispatcher.
set -euo pipefail

AGENT="${ANOLISA_TARGET:-kimicode}"
COMPONENT="${ANOLISA_COMPONENT:-tokenless}"
ADAPTER_DIR="${ANOLISA_ADAPTER_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"

KIMI_HOME="${HOME}/.kimi"
CONFIG_FILE="${KIMI_HOME}/config.toml"

DRY_RUN="${ANOLISA_DRY_RUN:-0}"

echo "[${COMPONENT}] Installing ${AGENT} adapter..."

if [ ! -d "$KIMI_HOME" ]; then
    mkdir -p "$KIMI_HOME"
    echo "[${COMPONENT}] created ${KIMI_HOME}"
fi

# Determine the absolute path to the run-hook.sh dispatcher
HOOK_DISPATCHER="${ADAPTER_DIR}/kimicode/hooks/run-hook.sh"
if [ ! -f "$HOOK_DISPATCHER" ]; then
    echo "[${COMPONENT}] ERROR: hook dispatcher not found: $HOOK_DISPATCHER" >&2
    echo "[${COMPONENT}]        Ensure the kimicode adapter directory is intact." >&2
    exit 1
fi

# Make dispatcher executable
chmod +x "$HOOK_DISPATCHER"

# Convert to absolute path for TOML embedding
HOOK_DISPATCHER_ABS="$(cd "$(dirname "$HOOK_DISPATCHER")" && pwd)/$(basename "$HOOK_DISPATCHER")"

# Define the hooks to install
declare -a HOOK_EVENTS=(
    "PreToolUse"
    "PreToolUse"
    "PostToolUse"
)

declare -a HOOK_MATCHERS=(
    "^(Bash|Shell|run_shell_command|terminal|execute_command)$"
    ""
    "^(?!(?:Read|Glob|Grep|NotebookRead)$).+"
)

declare -a HOOK_SCRIPTS=(
    "rewrite_hook.py"
    "tool_ready_hook.sh"
    "compress_response_hook.py"
)

declare -a HOOK_TIMEOUTS=(
    "10"
    "15"
    "15"
)

declare -a HOOK_DESCRIPTIONS=(
    "tokenless-rewrite: Rewrites shell commands via rtk for token savings"
    "tokenless-tool-ready: Pre-checks tool environment readiness"
    "tokenless-compress-response: Compresses tool responses for token savings"
)

# Python script to safely merge hooks into config.toml
python3 - "$CONFIG_FILE" "$HOOK_DISPATCHER_ABS" "$DRY_RUN" <<'PYTHON_SCRIPT'
import sys
import os
from pathlib import Path

config_path = Path(sys.argv[1])
dispatcher = sys.argv[2]
dry_run = sys.argv[3] == "1"

# Hook definitions passed from bash
hook_events = ["PreToolUse", "PreToolUse", "PostToolUse"]
hook_matchers = [
    "^(Bash|Shell|run_shell_command|terminal|execute_command)$",
    "",
    "^(?!(?:Read|Glob|Grep|NotebookRead)$).+"
]
hook_scripts = [
    "rewrite_hook.py",
    "tool_ready_hook.sh",
    "compress_response_hook.py"
]
hook_timeouts = [10, 15, 15]
hook_descriptions = [
    "tokenless-rewrite: Rewrites shell commands via rtk for token savings",
    "tokenless-tool-ready: Pre-checks tool environment readiness",
    "tokenless-compress-response: Compresses tool responses for token savings"
]

# Read existing config or start fresh
try:
    with open(config_path) as f:
        content = f.read()
except FileNotFoundError:
    content = ""

# Check for existing tokenless hooks and remove them
lines = content.split('\n')
new_lines = []
skip_until_next_hook = False

for i, line in enumerate(lines):
    if line.strip().startswith("[[hooks]]"):
        # Look ahead to see if this is a tokenless hook
        is_tokenless = False
        for j in range(i+1, min(i+10, len(lines))):
            if lines[j].strip().startswith("[["):
                break
            if "tokenless-" in lines[j]:
                is_tokenless = True
                break
        
        if is_tokenless:
            skip_until_next_hook = True
            continue
    
    if skip_until_next_hook:
        if line.strip().startswith("[[hooks]]"):
            skip_until_next_hook = False
            new_lines.append(line)
        elif line.strip().startswith("["):
            skip_until_next_hook = False
            new_lines.append(line)
        continue
    
    new_lines.append(line)

content = '\n'.join(new_lines).rstrip()

# Build new hook entries
new_hooks = []
for event, matcher, script, timeout, desc in zip(
    hook_events, hook_matchers, hook_scripts, hook_timeouts, hook_descriptions
):
    command = f'bash "{dispatcher}" {script}'
    hook_entry = f"""
[[hooks]]
event = "{event}"
matcher = "{matcher}"
command = """ + f'"{command}"' + f"""
timeout = {timeout}
# {desc}
"""
    new_hooks.append(hook_entry)

# Append new hooks
if content and not content.endswith('\n'):
    content += '\n'

content += '\n# Tokenless adapter hooks (auto-installed by tokenless)\n'
content += ''.join(new_hooks)

if dry_run:
    print(f"[DRY-RUN] Would write to {config_path}:")
    print(content)
    sys.exit(0)

# Write back
with open(config_path, 'w') as f:
    f.write(content)

print(f"[tokenless] Updated {config_path} with {len(new_hooks)} hooks")
PYTHON_SCRIPT

echo "[${COMPONENT}] ${AGENT} adapter installed."
echo "[${COMPONENT}] Restart kimi (or run /hooks) to activate."
