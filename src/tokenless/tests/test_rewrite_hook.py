#!/usr/bin/env python3
"""Regression tests for rewrite_hook.py rtk-prefix anchoring.

rtk emits rewritten commands with a bare `rtk` prefix, which only resolves
when the shell executing the tool call has the rtk location on its PATH.
Agent runtimes with a trimmed PATH (e.g. IDE tool environments without
~/.local/bin) would fail every rewritten command with exit 127. The hook
must anchor the rewrite to the resolved absolute rtk binary so the command
is self-contained.

The tests stage a fake rtk/tokenless pair in the fallback layout under a
sandboxed HOME and run the hook with a PATH that deliberately lacks the
rtk location — the exact shape of the affected environments.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = (
    Path(__file__).resolve().parent.parent
    / "adapters"
    / "tokenless"
    / "common"
    / "hooks"
    / "rewrite_hook.py"
)

# Mirrors real rtk: --version answers, rewrite prints a compound command
# whose segments each carry a bare `rtk` prefix.
FAKE_RTK = """#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "rtk 0.43.0"
  exit 0
fi
if [ "$1" = "rewrite" ]; then
  echo "rtk grep --cached foo && rtk git status"
  exit 0
fi
exit 1
"""

FAKE_TOKENLESS = """#!/bin/sh
echo "tokenless 0.7.3"
"""


def _write_exec(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class RewriteAnchorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        # Fallback layout resolve_binary probes when PATH lookup fails.
        share = self.home / ".local" / "share" / "anolisa" / "tokenless"
        _write_exec(share / "rtk", FAKE_RTK)
        _write_exec(share / "tokenless", FAKE_TOKENLESS)
        self.rtk = str(share / "rtk")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_hook(self, command: str) -> dict:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        # The affected shape: PATH lacks the rtk location entirely.
        env["PATH"] = "/usr/bin:/bin"
        env.pop("TOKENLESS_AGENT_ID", None)
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout or "{}")

    def test_rewrite_anchored_to_resolved_rtk_path(self) -> None:
        out = self._run_hook("grep foo bar && git status")
        hook_out = out.get("hookSpecificOutput", {})
        command = hook_out.get("tool_input", {}).get("command", "")
        self.assertTrue(command, f"hook did not rewrite: {out}")
        # Every segment starts with the absolute rtk binary, not bare `rtk`.
        self.assertTrue(command.startswith(self.rtk + " "), command)
        self.assertIn(f"&& {self.rtk} ", command)
        # Self-contained: the resolved first word is an executable file even
        # though PATH lacks its directory.
        first_word = command.split(" ", 1)[0]
        self.assertTrue(os.path.isfile(first_word), command)
        self.assertTrue(os.access(first_word, os.X_OK), command)

    def test_updated_input_matches_tool_input(self) -> None:
        out = self._run_hook("grep foo bar")
        hook_out = out.get("hookSpecificOutput", {})
        command = hook_out.get("tool_input", {}).get("command", "")
        self.assertTrue(command, f"hook did not rewrite: {out}")
        self.assertEqual(command, hook_out.get("updatedInput", {}).get("command", ""))


if __name__ == "__main__":
    unittest.main()
