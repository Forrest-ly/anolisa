#!/usr/bin/env python3
"""Regression tests for the Claude Code installer token file modes (#4911).

`write_config()` keeps the DashScope API key as ANTHROPIC_AUTH_TOKEN in
cleartext inside ~/.claude/settings.json. That file used to be created by a
plain `cat >`, so it inherited the caller's umask (0644 under the default
022) and every local user could read the token; the timestamped `.bak` copy
of a rotated settings.json was world-readable as well.

The installer is driven end-to-end with `curl`, `rpm`, `dnf` and `sudo`
stubbed on PATH, so these tests are hermetic: no network, no package
manager, no root, and the real ~/.claude is never touched.
"""

import glob
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("install-claude-code.sh")

API_KEY = "sk-test-secret"
OLD_API_KEY = "sk-old-secret"

# Stands in for `curl https://claude.ai/install.sh | bash`: drop a fake
# `claude` binary into ~/.local/bin so the native install path succeeds.
CURL_STUB = """#!/bin/sh
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/claude" <<'EOS'
#!/bin/sh
echo "claude 0.0.0-test"
EOS
chmod +x "$HOME/.local/bin/claude"
"""

# Every other external command the installer may reach for is a no-op stub,
# so the run never installs a package and never needs privileges.
NOOP_STUB = """#!/bin/sh
exit 0
"""


@unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
class ClaudeCodeConfigPermissionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        stubs = root / "stubs"
        stubs.mkdir()
        self._write_stub(stubs / "curl", CURL_STUB)
        for name in ("rpm", "dnf", "sudo"):
            self._write_stub(stubs / name, NOOP_STUB)
        self.stubs = stubs
        self.settings = self.home / ".claude" / "settings.json"

    @staticmethod
    def _write_stub(path, body):
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def install(self):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["PATH"] = f"{self.stubs}{os.pathsep}{env.get('PATH', '')}"
        env["CLAUDE_API_KEY"] = API_KEY
        return subprocess.run(
            ["bash", str(SCRIPT), "--config", "--skip-tokenless"],
            cwd=str(self.home),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

    def mode_of(self, path):
        return stat.S_IMODE(os.stat(str(path)).st_mode)

    def assert_owner_only(self, path):
        mode = self.mode_of(path)
        self.assertEqual(mode, 0o600, f"{path} is {mode:o}, expected 600")

    def test_settings_file_is_owner_only(self):
        """A fresh --config install must leave settings.json at 0600."""
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.settings.is_file(), result.stdout + result.stderr)
        self.assert_owner_only(self.settings)
        self.assertIn(API_KEY, self.settings.read_text(encoding="utf-8"))

    def test_backup_is_owner_only(self):
        """A rotated settings.json and its .bak copy must both be 0600."""
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(
            f'{{"env": {{"ANTHROPIC_AUTH_TOKEN": "{OLD_API_KEY}"}}}}',
            encoding="utf-8",
        )
        # A settings.json left behind by an editor or by an older installer.
        self.settings.chmod(0o644)

        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_owner_only(self.settings)
        self.assertIn(API_KEY, self.settings.read_text(encoding="utf-8"))

        backups = sorted(glob.glob(f"{self.settings}.bak.*"))
        self.assertEqual(len(backups), 1, backups)
        self.assert_owner_only(backups[0])
        with open(backups[0], encoding="utf-8") as handle:
            self.assertIn(OLD_API_KEY, handle.read())


if __name__ == "__main__":
    unittest.main()
