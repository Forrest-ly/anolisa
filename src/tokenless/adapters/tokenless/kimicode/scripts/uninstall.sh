#!/usr/bin/env bash
# uninstall.sh — Remove tokenless hooks from Kimi Code config (~/.kimi/config.toml).
# Removes all [[hooks]] entries that reference tokenless hooks.
set -euo pipefail

AGENT="${ANOLISA_TARGET:-kimicode}"
COMPONENT="${ANOLISA_COMPONENT:-tokenless}"

KIMI_HOME="${HOME}/.kimi"
CONFIG_FILE="${KIMI_HOME}/config.toml"

echo "[${COMPONENT}] Uninstalling ${AGENT} adapter..."

if [ ! -f "$CONFIG_FILE" ]; then
    echo "[${COMPONENT}] config.toml not found — nothing to remove."
    exit 0
fi

# Check for python3 dependency
if ! command -v python3 &>/dev/null; then
    echo "[${COMPONENT}] WARNING: python3 not found — falling back to sed-based cleanup."
    echo "[${COMPONENT}] This may leave some tokenless hooks in config.toml."
    echo "[${COMPONENT}] For complete cleanup, install python3 and run this script again."
    
    # Fallback: use sed to remove lines containing tokenless- (less precise)
    sed -i.bak '/^# tokenless-/d; /^# Tokenless adapter hooks/d' "$CONFIG_FILE"
    echo "[${COMPONENT}] Attempted sed-based cleanup (backup: ${CONFIG_FILE}.bak)"
    exit 0
fi

# Python script to remove tokenless hooks from config.toml
python3 - "$CONFIG_FILE" <<'PYTHON_SCRIPT'
import sys
from pathlib import Path

config_path = Path(sys.argv[1])

try:
    with open(config_path) as f:
        content = f.read()
except FileNotFoundError:
    print(f"[tokenless] config.toml not found: {config_path}")
    sys.exit(0)

# Parse and remove tokenless hooks
lines = content.split('\n')
new_lines = []
skip_until_next_hook = False
removed_count = 0

for i, line in enumerate(lines):
    if line.strip().startswith("[[hooks]]"):
        # Look ahead to see if this is a tokenless hook
        is_tokenless = False
        for j in range(i+1, min(i+15, len(lines))):
            if lines[j].strip().startswith("[["):
                break
            if "tokenless-" in lines[j]:
                is_tokenless = True
                break
        
        if is_tokenless:
            skip_until_next_hook = True
            removed_count += 1
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

# Remove the "Tokenless adapter hooks" comment if it exists
lines_after = content.split('\n')
lines_after = [l for l in lines_after if l.strip() != "# Tokenless adapter hooks (auto-installed by tokenless)"]
content = '\n'.join(lines_after)

# Clean up trailing blank lines
while content.endswith('\n\n'):
    content = content[:-1]

if not content.endswith('\n'):
    content += '\n'

# Write back
with open(config_path, 'w') as f:
    f.write(content)

print(f"[tokenless] Removed {removed_count} hook entries from {config_path}")
PYTHON_SCRIPT

echo "[${COMPONENT}] ${AGENT} adapter uninstalled."
