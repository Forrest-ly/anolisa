#!/usr/bin/env python3
"""Regression tests for module parameter passing in test-module.sh (#3744).

Drives the real ``src/os-skills/devops/kernel-dev/scripts/test-module.sh``
against stubbed ``insmod`` / ``lsmod`` / ``rmmod`` / ``modinfo`` / ``dmesg`` /
``sleep`` binaries placed first on ``PATH``, so the suite needs no kernel, no
real module, no root-owned filesystem and no network.  The stub ``insmod``
records the argv it receives (unit-separator per argument, one line per call),
which is exactly what the assertions read back.

``test-module.sh`` requires ``EUID == 0``.  When this test process is not
root, the script is run under ``unshare -r`` where user namespaces are
available; otherwise a copy of the script whose *root check alone* is
neutralised is used, leaving the parameter handling under test byte-identical.
If neither is possible the dynamic cases skip with a reason.

Baseline on unchanged main: ``test_parameter_with_space_reaches_insmod_intact``
and ``test_glob_characters_are_not_expanded`` fail -- insmod receives
``greeting=Hello`` and ``World`` as two separate arguments (the kernel rejects
the unknown parameter ``World``) and ``pattern=*`` arrives glob-expanded
against the working directory; ``test_no_parameter_load`` passes.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "src"
    / "os-skills"
    / "devops"
    / "kernel-dev"
    / "scripts"
    / "test-module.sh"
)

MODULE_NAME = "param_module"
# The single line the fallback runner is allowed to rewrite.
ROOT_CHECK = 'if [ "$EUID" -ne 0 ]; then'
ARG_SEP = "\x1f"

STUBS = {
    "insmod": """#!/bin/sh
for arg in "$@"; do printf '%s\\037' "$arg" >> "$STUB_INSMOD_LOG"; done
printf '\\n' >> "$STUB_INSMOD_LOG"
: > "$STUB_MODULE_STATE"
exit 0
""",
    "rmmod": """#!/bin/sh
rm -f "$STUB_MODULE_STATE"
exit 0
""",
    "lsmod": """#!/bin/sh
echo "Module                  Size  Used by"
if [ -f "$STUB_MODULE_STATE" ]; then
    echo "$STUB_MODULE_NAME       16384  0"
fi
exit 0
""",
    "modinfo": """#!/bin/sh
echo "filename:       $1"
echo "description:    stub module for the test-module.sh regression test"
exit 0
""",
    "dmesg": """#!/bin/sh
echo "$STUB_MODULE_NAME: stub kernel log line"
exit 0
""",
    "sleep": """#!/bin/sh
exit 0
""",
}


def unshare_prefix():
    """Return a command prefix that makes the child see EUID 0, or None."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    unshare = shutil.which("unshare")
    if not unshare:
        return None
    try:
        probe = subprocess.run(
            [unshare, "-r", "true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError:
        return None
    return [unshare, "-r"] if probe.returncode == 0 else None


class TestModuleParameterPassing(unittest.TestCase):
    """Run test-module.sh against stub kernel tools and inspect insmod argv."""

    def setUp(self):
        if not SCRIPT.is_file():
            self.skipTest("test-module.sh not found at {}".format(SCRIPT))
        self.tmp = Path(tempfile.mkdtemp(prefix="test-module-params-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # Mirror the skill layout the script derives SCRIPT_DIR/BASE_DIR from.
        self.skill_dir = self.tmp / "kernel-dev"
        self.script_dir = self.skill_dir / "scripts"
        self.script_dir.mkdir(parents=True)
        example_dir = self.skill_dir / "examples" / MODULE_NAME
        example_dir.mkdir(parents=True)
        self.module_file = example_dir / (MODULE_NAME + ".ko")
        self.module_file.write_bytes(b"stub kernel object\n")

        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        for name, body in STUBS.items():
            stub = self.bin_dir / name
            stub.write_text(body, encoding="utf-8")
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # A cwd holding files an unquoted glob would expand to: '*' matches
        # 'glob-marker', and the whole word 'pattern=*' matches the second
        # marker, which is the re-globbing the kernel command line must not see.
        self.work_dir = self.tmp / "work"
        self.work_dir.mkdir()
        (self.work_dir / "glob-marker").write_text("x\n", encoding="utf-8")
        (self.work_dir / "pattern=glob-marker").write_text("x\n", encoding="utf-8")

        self.insmod_log = self.tmp / "insmod-argv.log"
        self.module_state = self.tmp / "module-loaded"
        self.runner = unshare_prefix()
        self.script_path = self._install_script(neutralize_root=self.runner is None)
        if self.runner is None and not self._root_check_neutralised():
            self.skipTest("no root, no usable 'unshare -r', and the root check is unchanged")

    def _install_script(self, neutralize_root):
        source = SCRIPT.read_text(encoding="utf-8")
        if neutralize_root:
            self.assertEqual(
                source.count(ROOT_CHECK),
                1,
                "the root check moved; update ROOT_CHECK before neutralising it",
            )
            source = source.replace(ROOT_CHECK, "if false; then", 1)
        target = self.script_dir / "test-module.sh"
        target.write_text(source, encoding="utf-8")
        target.chmod(0o755)
        return target

    def _root_check_neutralised(self):
        text = self.script_path.read_text(encoding="utf-8")
        return ROOT_CHECK not in text

    def run_suite(self, *params):
        env = dict(os.environ)
        env["PATH"] = "{}{}{}".format(self.bin_dir, os.pathsep, env.get("PATH", ""))
        env["STUB_INSMOD_LOG"] = str(self.insmod_log)
        env["STUB_MODULE_STATE"] = str(self.module_state)
        env["STUB_MODULE_NAME"] = MODULE_NAME
        cmd = list(self.runner or []) + [str(self.script_path), MODULE_NAME] + list(params)
        return subprocess.run(
            cmd,
            cwd=str(self.work_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

    def insmod_calls(self):
        if not self.insmod_log.is_file():
            return []
        calls = []
        for line in self.insmod_log.read_text(encoding="utf-8").splitlines():
            if not line.strip(ARG_SEP):
                continue
            args = [a for a in line.split(ARG_SEP)]
            while args and args[-1] == "":
                args.pop()
            calls.append(args)
        return calls

    def assert_single_load(self, proc, expected_params):
        self.assertEqual(
            proc.returncode,
            0,
            "test-module.sh exited {}\nstdout:\n{}\nstderr:\n{}".format(
                proc.returncode, proc.stdout, proc.stderr
            ),
        )
        calls = self.insmod_calls()
        self.assertEqual(len(calls), 1, "expected exactly one insmod call, got {}".format(calls))
        self.assertEqual(calls[0], [str(self.module_file)] + list(expected_params))
        self.assertIn("Test Complete!", proc.stdout)

    def test_parameter_with_space_reaches_insmod_intact(self):
        proc = self.run_suite("greeting=Hello World", "repeat_count=3")
        self.assert_single_load(proc, ["greeting=Hello World", "repeat_count=3"])

    def test_glob_characters_are_not_expanded(self):
        proc = self.run_suite("pattern=*", "greeting=Hi?")
        self.assert_single_load(proc, ["pattern=*", "greeting=Hi?"])
        for call in self.insmod_calls():
            for arg in call:
                self.assertNotIn("glob-marker", arg)

    def test_no_parameter_load(self):
        proc = self.run_suite()
        self.assert_single_load(proc, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
