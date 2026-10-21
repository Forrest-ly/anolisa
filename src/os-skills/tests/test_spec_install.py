"""Regression checks for the RPM %install stage of os-skills.spec.in.

rpm runs scriptlets without errexit and only propagates the exit status of
their last command, so a copy that fails in the middle of the skill loop used
to be masked by the copies after it and rpmbuild still produced a "successful"
RPM holding a partial skills tree.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC_TEMPLATE = ROOT / "os-skills.spec.in"


def install_scriptlet() -> str:
    """Return the %install body of the spec template with its macros rendered."""
    lines = SPEC_TEMPLATE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "%install")
    body = []
    for line in lines[start + 1:]:
        if re.match(r"^%[A-Za-z]", line):
            break
        body.append(line)
    return "\n".join(body) + "\n"


class SpecInstallTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        temporary = Path(self._temporary.name)

        self.real_cp = shutil.which("cp")
        self.assertIsNotNone(self.real_cp, "cp is required to run the %install stage")

        # A minimal source tree shaped like src/os-skills: one directory per
        # skill, each identified by a SKILL.md, plus component.toml.
        self.source = temporary / "source"
        self.buildroot = temporary / "buildroot"
        self.skills_root = self.buildroot / "usr/share/anolisa/skills"
        self.component = (
            self.buildroot / "usr/share/anolisa/components/os-skills/component.toml"
        )
        self.make_skills("ai/alpha", "devops/beta", "system-admin/gamma")
        (self.source / "component.toml").write_text('[component]\nname = "os-skills"\n')

        self.stub_dir = temporary / "stub"
        self.stub_dir.mkdir()
        os.environ["PATH"] = str(self.stub_dir) + os.pathsep + os.environ["PATH"]

    def make_skills(self, *relative_dirs):
        for relative in relative_dirs:
            skill = self.source / relative
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# %s\n" % Path(relative).name)
            (skill / "helper.sh").write_text("#!/bin/sh\nexit 0\n")

    def write_cp_stub(self, failing_suffix):
        """Shadow cp with a stub that fails for source paths ending in a suffix."""
        stub = self.stub_dir / "cp"
        stub.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '    case "$arg" in\n'
            "        */%s) echo \"cp: cannot stat './%s': simulated failure\" >&2\n"
            "              exit 1 ;;\n"
            "    esac\n"
            "done\n"
            'exec %s "$@"\n' % (failing_suffix, failing_suffix, self.real_cp)
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run_install(self):
        script = install_scriptlet().replace(
            "%{buildroot}", str(self.buildroot)
        ).replace("%{_datadir}", "/usr/share")
        rendered = Path(self._temporary.name) / "install.sh"
        rendered.write_text(script)
        # rpm executes scriptlets as "/bin/sh -e <file>" (see the
        # "Executing(%install): /bin/sh -e ..." line in the rpmbuild log), so
        # the harness has to pass errexit the same way.
        return subprocess.run(
            ["/bin/sh", "-e", str(rendered)],
            cwd=str(self.source),
            capture_output=True,
            text=True,
        )

    def test_installs_every_skill(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.component.is_file())
        installed = sorted(p.name for p in self.skills_root.iterdir())
        self.assertEqual(installed, ["alpha", "beta", "gamma"])
        for name in installed:
            self.assertTrue((self.skills_root / name / "SKILL.md").is_file())
        # .sh payloads stay executable, everything else is world readable.
        self.assertEqual((self.skills_root / "alpha" / "helper.sh").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.skills_root / "alpha" / "SKILL.md").stat().st_mode & 0o777, 0o644)

    def test_mid_loop_copy_failure_fails_the_build(self):
        self.write_cp_stub("beta")
        result = self.run_install()
        self.assertNotEqual(
            result.returncode,
            0,
            "a copy failure in the middle of the skill loop must abort %install",
        )
        self.assertIn("ERROR", result.stderr)
        self.assertIn("beta", result.stderr)

    def test_skills_sharing_a_directory_name_fail_the_build(self):
        # Flattening two skills onto one installed directory silently drops
        # one of them, so the installed count must not match the found count.
        self.make_skills("others/gamma")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("ERROR", result.stderr)


if __name__ == "__main__":
    unittest.main()
