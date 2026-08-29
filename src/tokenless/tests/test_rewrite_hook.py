#!/usr/bin/env python3
"""Regression tests for rewrite_hook.py rtk-prefix anchoring.

rtk emits rewritten commands with a bare `rtk` prefix, which only resolves
when the shell executing the tool call has the rtk location on its PATH.
Agent runtimes with a trimmed PATH (e.g. IDE tool environments without
~/.local/bin) would fail every rewritten command with exit 127. The hook
must anchor the rewrite to the resolved absolute rtk binary so the command
is self-contained — without touching quoting, globs, or any other part of
the command text.

The tests stage a fake rtk/tokenless pair in the top-priority user
layout under a sandboxed HOME (so the fakes win even on hosts with a
real rtk install) and run the hook with a PATH that deliberately lacks
the rtk location — the exact shape of the affected environments.  The
sandboxed HOME also carries an rtk config.toml with configured
transparent_prefixes, covering the RTK v0.43 transparent-prefix protocol.
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

# Mirrors real rtk: --version answers; rewrite maps each input command to
# the shape real rtk would emit, with a bare `rtk` prefix at wrapper
# positions (including after `sudo`, env assignments, and connectives).
FAKE_RTK = """#!/usr/bin/env python3
import sys

REWRITES = {
    "grep foo bar && git status": "rtk grep --cached foo && rtk git status",
    "grep foo bar": "rtk grep foo bar",
    "sudo git status": "sudo rtk git status",
    "RUST_BACKTRACE=1 cargo test": "RUST_BACKTRACE=1 rtk cargo test",
    "git status & grep foo": "git status & rtk grep foo",
    "grep -E 'foo|rtk bar' src/": "rtk grep -E 'foo|rtk bar' src/",
    "grep foo *.txt": "rtk grep foo *.txt",
    "grep foo #include src/": "rtk grep foo #include src/",
    "git log 2>&1 | head": "rtk git log 2>&1 | rtk head",
    "git status 2>/dev/null": "rtk git status 2>/dev/null",
    "echo $(date)": "rtk echo $(date)",
    "git status\\ncargo test": "rtk git status\\nrtk cargo test",
    "grep foo\\\\; file": "rtk grep foo\\\\; rtk file",
    # RTK v0.43 transparent-prefix protocol: built-in prefixes (noglob,
    # command, builtin, exec, nocorrect, uv run) and configured multi-word
    # prefixes are stripped before routing and re-prepended in front of the
    # inserted rtk wrapper.
    "noglob git status": "noglob rtk git status",
    "command git status": "command rtk git status",
    "builtin git status": "builtin rtk git status",
    "exec git status": "exec rtk git status",
    "nocorrect git status": "nocorrect rtk git status",
    "uv run pytest tests/": "uv run rtk pytest tests/",
    "PYTHONPATH=. uv run pytest tests/": "PYTHONPATH=. uv run rtk pytest tests/",
    "sudo noglob git status": "sudo noglob rtk git status",
    "shadowenv exec -- git status": "shadowenv exec -- rtk git status",
    "docker exec c1 git status": "docker exec c1 rtk git status",
    "shadowenv exec -- FOO=bar git status": "shadowenv exec -- FOO=bar rtk git status",
    # Partial configured prefix: only "shadowenv exec --" is configured, so
    # a bare "shadowenv" wrapper must not be treated as transparent.
    "shadowenv git status": "shadowenv rtk git status",
    # Ignored command carrying a bare rtk argument plus rewritten segment.
    "echo rtk done && git status": "echo rtk done && rtk git status",
    # Wrapper options consume the command position: the `-u` operand (a
    # sudo username) or `command -v` argument named rtk is passed through
    # untouched while the next segment is rewritten.
    "sudo -u rtk true && git status": "sudo -u rtk true && rtk git status",
    "env -u rtk && git status": "env -u rtk && rtk git status",
    "command -v rtk && git status": "command -v rtk && rtk git status",
    "git status && sudo -u rtk true": "rtk git status && sudo -u rtk true",
}

if len(sys.argv) > 1 and sys.argv[1] == "--version":
    print("rtk 0.43.0")
    sys.exit(0)
if len(sys.argv) > 2 and sys.argv[1] == "rewrite" and sys.argv[2] in REWRITES:
    print(REWRITES[sys.argv[2]])
    sys.exit(0)
sys.exit(1)
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
        # Top-priority user layout (probed before /usr/local/bin & co), so
        # the fakes win even on machines that carry a real rtk/tokenless
        # install — the PATH below still lacks this directory, preserving
        # the affected trimmed-PATH shape.
        user_bin = self.home / ".local" / "bin"
        _write_exec(user_bin / "rtk", FAKE_RTK)
        _write_exec(user_bin / "tokenless", FAKE_TOKENLESS)
        self.rtk = str(user_bin / "rtk")
        # RTK v0.43 reads [hooks].transparent_prefixes from
        # <config dir>/rtk/config.toml; stage a user config so anchoring
        # must cover configured multi-word prefixes too.
        cfg = self.home / ".config" / "rtk"
        cfg.mkdir(parents=True)
        (cfg / "config.toml").write_text(
            "[hooks]\n"
            'transparent_prefixes = ["shadowenv exec --", "docker exec c1"]\n'
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _rewrite(self, command: str) -> str:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        # Resolve rtk's config.toml under the sandboxed HOME regardless of
        # any host-level XDG_CONFIG_HOME.
        env["XDG_CONFIG_HOME"] = str(self.home / ".config")
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
        out = json.loads(proc.stdout or "{}")
        rewritten = (
            out.get("hookSpecificOutput", {}).get("tool_input", {}).get("command", "")
        )
        self.assertTrue(rewritten, f"hook did not rewrite: {out}")
        return rewritten

    def test_rewrite_anchored_to_resolved_rtk_path(self) -> None:
        command = self._rewrite("grep foo bar && git status")
        # Every segment starts with the absolute rtk binary, not bare `rtk`.
        self.assertEqual(command, f"{self.rtk} grep --cached foo && {self.rtk} git status")
        # Self-contained: the resolved first word is an executable file even
        # though PATH lacks its directory.
        first_word = command.split(" ", 1)[0]
        self.assertTrue(os.path.isfile(first_word), command)
        self.assertTrue(os.access(first_word, os.X_OK), command)

    def test_updated_input_matches_tool_input(self) -> None:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["XDG_CONFIG_HOME"] = str(self.home / ".config")
        env["PATH"] = "/usr/bin:/bin"
        env.pop("TOKENLESS_AGENT_ID", None)
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "grep foo bar"}}),
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        hook_out = json.loads(proc.stdout).get("hookSpecificOutput", {})
        command = hook_out.get("tool_input", {}).get("command", "")
        self.assertEqual(command, f"{self.rtk} grep foo bar")
        self.assertEqual(command, hook_out.get("updatedInput", {}).get("command", ""))

    def test_anchor_after_wrapper(self) -> None:
        self.assertEqual(
            self._rewrite("sudo git status"),
            f"sudo {self.rtk} git status",
        )

    def test_anchor_after_env_assignment(self) -> None:
        self.assertEqual(
            self._rewrite("RUST_BACKTRACE=1 cargo test"),
            f"RUST_BACKTRACE=1 {self.rtk} cargo test",
        )

    def test_anchor_after_single_ampersand(self) -> None:
        self.assertEqual(
            self._rewrite("git status & grep foo"),
            f"git status & {self.rtk} grep foo",
        )

    def test_quoted_rtk_pattern_untouched(self) -> None:
        # Only the leading wrapper is anchored; the `rtk` inside the quoted
        # regex pattern must survive byte-for-byte.
        self.assertEqual(
            self._rewrite("grep -E 'foo|rtk bar' src/"),
            f"{self.rtk} grep -E 'foo|rtk bar' src/",
        )

    def test_unquoted_glob_preserved(self) -> None:
        # The glob must stay an unquoted glob — re-quoting tokens would
        # produce '*.txt' and neuter expansion.
        self.assertEqual(
            self._rewrite("grep foo *.txt"),
            f"{self.rtk} grep foo *.txt",
        )

    def test_hash_argument_preserved(self) -> None:
        # `#` must not be treated as a comment starter — the argument and
        # everything after it stays in the command.
        self.assertEqual(
            self._rewrite("grep foo #include src/"),
            f"{self.rtk} grep foo #include src/",
        )

    def test_fd_merging_preserved(self) -> None:
        # `2>&1` must stay one unsplit token — splitting it into
        # `2 >& 1` would turn `2` into an argument and break the merge.
        self.assertEqual(
            self._rewrite("git log 2>&1 | head"),
            f"{self.rtk} git log 2>&1 | {self.rtk} head",
        )

    def test_fd_redirection_preserved(self) -> None:
        # `2>/dev/null` must stay attached — `2 > /dev/null` would make `2`
        # an argument and redirect stdout instead of stderr.
        self.assertEqual(
            self._rewrite("git status 2>/dev/null"),
            f"{self.rtk} git status 2>/dev/null",
        )

    def test_command_substitution_preserved(self) -> None:
        # `$(...)` must not be split into `$ ( ... )`, which would destroy
        # the substitution.
        self.assertEqual(
            self._rewrite("echo $(date)"),
            f"{self.rtk} echo $(date)",
        )

    def test_newline_separated_commands_anchor_both_segments(self) -> None:
        # Newline terminates a command exactly like `;`: the rtk after
        # the newline starts a fresh segment and must be anchored too.
        self.assertEqual(
            self._rewrite("git status\ncargo test"),
            f"{self.rtk} git status\n{self.rtk} cargo test",
        )

    def test_escaped_semicolon_is_not_a_boundary(self) -> None:
        # `\;` is an escaped argument character, not a command
        # separator: the trailing `rtk` is grep's argument and must
        # stay bare instead of being anchored.
        self.assertEqual(
            self._rewrite("grep foo\\; file"),
            f"{self.rtk} grep foo\\; rtk file",
        )

    # -- RTK v0.43 transparent-prefix protocol -----------------------------

    def test_anchor_after_rtk_builtin_transparent_prefixes(self) -> None:
        # rtk strips its built-in single-word prefixes and re-prepends them
        # in front of the rtk wrapper; the rtk behind each must anchor.
        for wrapper in ("noglob", "command", "builtin", "exec", "nocorrect"):
            with self.subTest(wrapper=wrapper):
                self.assertEqual(
                    self._rewrite(f"{wrapper} git status"),
                    f"{wrapper} {self.rtk} git status",
                )

    def test_anchor_after_uv_run_multiword_builtin(self) -> None:
        # `uv run` is a two-word built-in transparent prefix.
        self.assertEqual(
            self._rewrite("uv run pytest tests/"),
            f"uv run {self.rtk} pytest tests/",
        )

    def test_anchor_after_env_assignment_then_uv_run(self) -> None:
        # Env assignments compose in front of transparent prefixes.
        self.assertEqual(
            self._rewrite("PYTHONPATH=. uv run pytest tests/"),
            f"PYTHONPATH=. uv run {self.rtk} pytest tests/",
        )

    def test_anchor_after_nested_wrapper_and_builtin(self) -> None:
        # Shell wrappers and RTK built-ins nest: sudo + noglob.
        self.assertEqual(
            self._rewrite("sudo noglob git status"),
            f"sudo noglob {self.rtk} git status",
        )

    def test_anchor_after_configured_transparent_prefix(self) -> None:
        # Configured multi-word prefix from [hooks].transparent_prefixes.
        self.assertEqual(
            self._rewrite("shadowenv exec -- git status"),
            f"shadowenv exec -- {self.rtk} git status",
        )

    def test_anchor_after_second_configured_prefix(self) -> None:
        self.assertEqual(
            self._rewrite("docker exec c1 git status"),
            f"docker exec c1 {self.rtk} git status",
        )

    def test_anchor_after_configured_prefix_with_inner_env(self) -> None:
        # Env assignments may also appear between prefix and command.
        self.assertEqual(
            self._rewrite("shadowenv exec -- FOO=bar git status"),
            f"shadowenv exec -- FOO=bar {self.rtk} git status",
        )

    def test_partial_configured_prefix_not_matched(self) -> None:
        # Only the full configured sequence is transparent: a bare
        # `shadowenv` consumes the command position, so the rtk behind it
        # is a positional argument and must stay bare.
        self.assertEqual(
            self._rewrite("shadowenv git status"),
            f"shadowenv rtk git status",
        )

    def test_echo_rtk_argument_plus_rewritten_segment(self) -> None:
        # The rtk passed to echo is an argument (stays bare); only the
        # command-position rtk of the rewritten segment is anchored.
        self.assertEqual(
            self._rewrite("echo rtk done && git status"),
            f"echo rtk done && {self.rtk} git status",
        )

    # -- wrapper option operands must not be anchored ------------------------

    def test_sudo_user_option_operand_not_anchored(self) -> None:
        # `sudo -u rtk true`: the word after `-u` is the username operand,
        # not a command — rewriting it into an executable path would break
        # the user switch.  Only the second segment's rtk is anchored.
        self.assertEqual(
            self._rewrite("sudo -u rtk true && git status"),
            f"sudo -u rtk true && {self.rtk} git status",
        )

    def test_env_unset_option_operand_not_anchored(self) -> None:
        # `env -u NAME` unsets a variable; the NAME operand must stay bare.
        self.assertEqual(
            self._rewrite("env -u rtk && git status"),
            f"env -u rtk && {self.rtk} git status",
        )

    def test_command_v_argument_not_anchored(self) -> None:
        # `command -v rtk` queries rtk's resolution — anchoring the word
        # would change the query (and the exit status on trimmed PATH).
        self.assertEqual(
            self._rewrite("command -v rtk && git status"),
            f"command -v rtk && {self.rtk} git status",
        )

    def test_option_operand_segment_later_position_not_anchored(self) -> None:
        # The operand negative also holds when the option-bearing segment
        # is not the first one.
        self.assertEqual(
            self._rewrite("git status && sudo -u rtk true"),
            f"{self.rtk} git status && sudo -u rtk true",
        )


if __name__ == "__main__":
    unittest.main()
