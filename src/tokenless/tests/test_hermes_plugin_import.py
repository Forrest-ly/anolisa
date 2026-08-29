#!/usr/bin/env python3
"""Regression tests for the Hermes plugin hook_utils resolution.

Covers the review findings on PR #2058:
- P1-a: the hooks directory itself, its parent, and hook_utils.py must all
  be rejected when world-writable or foreign-owned (not just the parent).
- P1-b: copy-installs must honor XDG_DATA_HOME (anolisa FsLayout::user
  prefers it over ~/.local/share).
- P1-c: an existing-but-incomplete high-priority candidate must not stop
  the search; later valid candidates are still tried.
- P2: candidate list contains no empty placeholders; _validate_hooks_dir
  rejects relative/empty paths; the ImportError mentions trust-policy
  rejections, not just "missing".
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_SRC = os.path.join(_REPO_ROOT, "adapters", "tokenless", "hermes", "__init__.py")
_HOOKS_SRC = os.path.join(_REPO_ROOT, "adapters", "tokenless", "common", "hooks")


def _load_plugin(path: str, name: str):
    """Load a copy of the Hermes plugin module under a unique name."""
    # Drop any previously imported hook_utils so each load re-resolves it.
    sys.modules.pop("hook_utils", None)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    pre_path = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = pre_path
    return module


def _make_hooks_dir(base: str) -> str:
    """Create a complete, trusted hooks dir under base and return its path."""
    hooks = os.path.join(base, "anolisa", "adapters", "tokenless", "common", "hooks")
    os.makedirs(hooks, mode=0o755)
    for fname in ("hook_utils.py", "tool_categories.json"):
        shutil.copy(os.path.join(_HOOKS_SRC, fname), hooks)
    os.chmod(hooks, 0o755)
    return hooks


class ValidateHooksDirTest(unittest.TestCase):
    """Unit tests for _validate_hooks_dir (loaded from the source tree)."""

    @classmethod
    def setUpClass(cls):
        # Source-tree import: the relative candidate resolves, so loading
        # the real plugin file always succeeds here.
        cls.plugin = _load_plugin(_PLUGIN_SRC, "hermes_plugin_srctree")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hermes-hooks-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_rejects_empty_and_relative_paths(self):
        self.assertIsNotNone(self.plugin._validate_hooks_dir(""))
        self.assertIsNotNone(self.plugin._validate_hooks_dir("relative/hooks"))

    def test_rejects_missing_directory(self):
        reason = self.plugin._validate_hooks_dir(os.path.join(self.tmp, "nope"))
        self.assertIn("does not exist", reason)

    def test_rejects_incomplete_dir_without_hook_utils(self):
        # P1-c: uninstall residue — dir exists but hook_utils.py is gone.
        empty = os.path.join(self.tmp, "hooks")
        os.makedirs(empty)
        reason = self.plugin._validate_hooks_dir(empty)
        self.assertIn("hook_utils.py missing", reason)

    def test_accepts_trusted_complete_dir(self):
        hooks = _make_hooks_dir(self.tmp)
        self.assertIsNone(self.plugin._validate_hooks_dir(hooks))

    def test_rejects_world_writable_hooks_dir(self):
        # P1-a: the hooks dir itself is world-writable.
        hooks = _make_hooks_dir(self.tmp)
        os.chmod(hooks, 0o777)
        reason = self.plugin._validate_hooks_dir(hooks)
        self.assertIn("world-writable", reason)

    def test_rejects_world_writable_hook_utils_file(self):
        # P1-a: hook_utils.py itself is world-writable (0666).
        hooks = _make_hooks_dir(self.tmp)
        os.chmod(os.path.join(hooks, "hook_utils.py"), 0o666)
        reason = self.plugin._validate_hooks_dir(hooks)
        self.assertIn("world-writable", reason)

    def test_rejects_world_writable_parent_dir(self):
        hooks = _make_hooks_dir(self.tmp)
        os.chmod(os.path.dirname(hooks), 0o777)
        reason = self.plugin._validate_hooks_dir(hooks)
        self.assertIn("world-writable", reason)

    def test_candidate_list_has_no_empty_entries(self):
        # P2: no "" placeholder elements in the candidate list.
        for candidate in self.plugin._HOOK_UTILS_CANDIDATES:
            self.assertTrue(candidate, "empty candidate in _HOOK_UTILS_CANDIDATES")
            self.assertTrue(os.path.isabs(candidate) or candidate.startswith(self.plugin._HERE))


class CopyInstallResolutionTest(unittest.TestCase):
    """End-to-end: plugin copied to a bare dir (anolisa driver behavior)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hermes-copy-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        plugin_dir = os.path.join(self.tmp, "plugins", "tokenless")
        os.makedirs(plugin_dir)
        shutil.copy(_PLUGIN_SRC, plugin_dir)
        self.plugin_copy = os.path.join(plugin_dir, "__init__.py")
        self._saved_xdg = os.environ.get("XDG_DATA_HOME")

    def tearDown(self):
        if self._saved_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._saved_xdg

    def test_resolves_via_xdg_data_home(self):
        # P1-b: XDG_DATA_HOME layout must be honored for copy-installs.
        xdg = os.path.join(self.tmp, "xdg-data")
        hooks = _make_hooks_dir(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        plugin = _load_plugin(self.plugin_copy, "hermes_plugin_xdg")
        self.assertEqual(plugin._HOOK_UTILS_RESOLVED, os.path.realpath(hooks))

    def test_incomplete_xdg_candidate_does_not_mask_later_ones(self):
        # P1-c: an existing-but-empty XDG hooks dir must be skipped, and the
        # search must continue to later candidates instead of breaking.
        xdg = os.path.join(self.tmp, "xdg-data")
        empty_hooks = os.path.join(xdg, "anolisa", "adapters", "tokenless", "common", "hooks")
        os.makedirs(empty_hooks)
        os.environ["XDG_DATA_HOME"] = xdg
        try:
            plugin = _load_plugin(self.plugin_copy, "hermes_plugin_incomplete_xdg")
        except ImportError as exc:
            # No later candidate exists on this machine — the diagnostic must
            # name the incomplete dir with its rejection reason (P2 wording).
            self.assertIn("hook_utils.py missing", str(exc))
            self.assertIn(empty_hooks, str(exc))
        else:
            # A later candidate (e.g. passwd-home install) won — but never
            # the incomplete XDG dir.
            self.assertNotEqual(plugin._HOOK_UTILS_RESOLVED, os.path.realpath(empty_hooks))

    def test_import_error_mentions_trust_policy(self):
        # P2: the diagnostic must explain that existing paths can be
        # rejected by the trust policy, not only be "missing".
        xdg = os.path.join(self.tmp, "xdg-data")
        hooks = _make_hooks_dir(xdg)
        os.chmod(hooks, 0o777)  # exists but untrusted
        os.environ["XDG_DATA_HOME"] = xdg
        try:
            plugin = _load_plugin(self.plugin_copy, "hermes_plugin_untrusted_xdg")
        except ImportError as exc:
            self.assertIn("world-writable", str(exc))
            self.assertIn("trust policy", str(exc))
        else:
            # Later candidate won; the untrusted dir must not be selected.
            self.assertNotEqual(plugin._HOOK_UTILS_RESOLVED, os.path.realpath(hooks))


class VersionMismatchTest(unittest.TestCase):
    """Regression tests for shared hook_utils version mismatch (PR #2249 P1).

    When a candidate passes the trust check (hook_utils.py exists, ownership
    and permissions OK) but ships an older hook_utils that lacks the symbols
    this plugin needs (e.g. _anchor_rtk_prefix), the plugin must either
    continue to the next candidate or fall back to a local implementation —
    never crash with ImportError.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hermes-version-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        plugin_dir = os.path.join(self.tmp, "plugins", "tokenless")
        os.makedirs(plugin_dir)
        shutil.copy(_PLUGIN_SRC, plugin_dir)
        self.plugin_copy = os.path.join(plugin_dir, "__init__.py")
        self._saved_xdg = os.environ.get("XDG_DATA_HOME")

    def tearDown(self):
        if self._saved_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._saved_xdg

    def _make_old_hooks_dir(self, base: str) -> str:
        """Create a hooks dir with hook_utils.py that LACKS _anchor_rtk_prefix."""
        hooks = os.path.join(base, "anolisa", "adapters", "tokenless", "common", "hooks")
        os.makedirs(hooks, mode=0o755)
        # Write a minimal hook_utils.py without the required symbol
        with open(os.path.join(hooks, "hook_utils.py"), "w") as f:
            f.write(
                "# Old hook_utils without _anchor_rtk_prefix\n"
                "def resolve_binary(name, *fallbacks): return None\n"
            )
        # Copy tool_categories.json (needed by some imports)
        shutil.copy(
            os.path.join(_HOOKS_SRC, "tool_categories.json"),
            hooks,
        )
        os.chmod(hooks, 0o755)
        return hooks

    def test_old_hooks_rejected_by_api_compat_check(self):
        # A candidate with hook_utils.py that lacks _anchor_rtk_prefix must
        # be rejected by _check_api_compat, not accepted and then crash.
        xdg = os.path.join(self.tmp, "xdg-data")
        old_hooks = self._make_old_hooks_dir(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        try:
            plugin = _load_plugin(self.plugin_copy, "hermes_plugin_old_hooks")
        except ImportError as exc:
            # No compatible candidate found — diagnostic mentions API mismatch.
            self.assertIn("API mismatch", str(exc))
            self.assertIn(old_hooks, str(exc))
        else:
            # A later candidate with the correct version won.
            self.assertNotEqual(plugin._HOOK_UTILS_RESOLVED, os.path.realpath(old_hooks))

    def test_plugin_loads_with_fallback_when_no_compat_candidate(self):
        # When no candidate has the required symbols, the plugin should still
        # load (using local fallbacks) instead of crashing with ImportError.
        xdg = os.path.join(self.tmp, "xdg-data")
        self._make_old_hooks_dir(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        # The plugin should load without raising ImportError
        plugin = _load_plugin(self.plugin_copy, "hermes_plugin_fallback")
        # Verify the fallback _anchor_rtk_prefix works
        self.assertFalse(plugin._HOOK_UTILS_AVAILABLE)
        result = plugin._anchor_rtk_prefix(
            "rtk grep foo", "/usr/bin/rtk",
        )
        self.assertEqual(result, "/usr/bin/rtk grep foo")

    def test_degraded_mode_guards_block_compression_and_env_check(self):
        # In degraded mode, even when _have returns True (tokenless binary
        # exists), the _HOOK_UTILS_AVAILABLE guard must prevent compression,
        # TOON encoding, and env-check from running.
        #
        # Every downstream helper the guard protects is replaced by a
        # recording sentinel that returns a non-None value, so if the guard
        # were removed the callbacks would both invoke the sentinel (count
        # > 0) and return its sentinel value instead of None — the test
        # then fails instead of passing by accident (the real helpers would
        # return None in a test env without tokenless installed).
        xdg = os.path.join(self.tmp, "xdg-data")
        self._make_old_hooks_dir(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        plugin = _load_plugin(self.plugin_copy, "hermes_degraded_guards")
        self.assertFalse(plugin._HOOK_UTILS_AVAILABLE)

        calls = {"env_check": 0, "compress": 0, "toon": 0}

        def sentinel_env_check(tool_name):
            calls["env_check"] += 1
            return "SENTINEL-ENV-CHECK-BLOCK"

        def sentinel_compress(*args, **kwargs):
            calls["compress"] += 1
            return "SENTINEL-COMPRESSED"

        def sentinel_toon(*args, **kwargs):
            calls["toon"] += 1
            return ("SENTINEL-TOON", 50)

        originals = {
            "_have": plugin._have,
            "_env_check": plugin._env_check,
            "_compress_response": plugin._compress_response,
            "_encode_toon": plugin._encode_toon,
        }
        # _have True simulates an installed tokenless binary, so the guard
        # is the only remaining thing that can stop the downstream calls.
        plugin._have = lambda *a, **kw: True
        plugin._env_check = sentinel_env_check
        plugin._compress_response = sentinel_compress
        plugin._encode_toon = sentinel_toon
        try:
            # on_transform_tool_result: must return None despite _have True.
            # Use a payload > _MIN_RESPONSE_LEN (200) to prove the guard
            # fires, not the length check.
            big_result = '{"output": "' + "x" * 300 + '"}'
            self.assertGreater(len(big_result), 200)
            result = plugin.on_transform_tool_result(
                tool_name="Bash",
                result=big_result,
                session_id="test-session",
                tool_call_id="test-call",
            )
            self.assertIsNone(
                result,
                "degraded mode must skip compression even when _have is True",
            )
            self.assertEqual(
                calls["compress"], 0,
                "_compress_response ran despite degraded-mode guard",
            )
            self.assertEqual(
                calls["toon"], 0,
                "_encode_toon ran despite degraded-mode guard",
            )

            # on_pre_tool_call: env-check must be skipped (any tool name).
            pre_result = plugin.on_pre_tool_call(
                tool_name="Read",
                args={"file_path": "/tmp/test"},
                session_id="test-session",
                tool_call_id="test-call",
            )
            self.assertIsNone(
                pre_result,
                "degraded mode must skip env-check even when _have is True",
            )
            self.assertEqual(
                calls["env_check"], 0,
                "_env_check ran despite degraded-mode guard",
            )
        finally:
            for name, fn in originals.items():
                setattr(plugin, name, fn)

    def test_degraded_mode_parse_version_handles_program_prefix(self):
        # _parse_version must use re.search (not re.match) so "rtk 0.34.0"
        # is parsed correctly — the shared parse_version has search semantics.
        xdg = os.path.join(self.tmp, "xdg-data")
        self._make_old_hooks_dir(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        plugin = _load_plugin(self.plugin_copy, "hermes_degraded_ver")
        self.assertFalse(plugin._HOOK_UTILS_AVAILABLE)
        # "rtk 0.34.0" has a program name prefix — re.match would fail
        self.assertEqual(plugin._parse_version("rtk 0.34.0"), (0, 34, 0))
        self.assertEqual(plugin._parse_version("rtk 0.43.0"), (0, 43, 0))
        # Pure version string still works
        self.assertEqual(plugin._parse_version("0.35.0"), (0, 35, 0))
        # Old version must be below the minimum
        ver = plugin._parse_version("rtk 0.34.0")
        self.assertIsNotNone(ver)
        self.assertLess(ver, plugin._MIN_RTK_VERSION)

    def test_degraded_mode_resolve_binary_covers_user_layouts(self):
        # In degraded mode, resolve_binary must cover the same user install
        # layouts as the shared _known_binary_paths — including the legacy
        # share/lib paths — even when PATH is empty.  Every fake binary
        # stays inside self.tmp: "~" is redirected to a tmp home via a
        # patched os.path.expanduser, so the real home is never touched.
        xdg = os.path.join(self.tmp, "xdg-data")
        self._make_old_hooks_dir(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        plugin = _load_plugin(self.plugin_copy, "hermes_degraded_resolve")
        self.assertFalse(plugin._HOOK_UTILS_AVAILABLE)

        # Degraded constants must mirror the shared hook_utils semantics
        # (legacy share/lib layouts), so the _resolve_binary wrapper passes
        # identical fallbacks in shared and degraded mode.
        real_home = os.path.expanduser("~")
        self.assertEqual(
            plugin._RTK_LOCAL_SHARE,
            os.path.join(real_home, ".local", "share", "anolisa", "tokenless", "rtk"),
        )
        self.assertEqual(
            plugin._RTK_LOCAL_LIB,
            os.path.join(real_home, ".local", "lib", "anolisa", "tokenless", "rtk"),
        )
        self.assertEqual(
            plugin._TOKENLESS_LOCAL_SHARE,
            os.path.join(real_home, ".local", "share", "anolisa", "tokenless", "tokenless"),
        )
        self.assertEqual(
            plugin._TOKENLESS_LOCAL_LIB,
            os.path.join(real_home, ".local", "lib", "anolisa", "tokenless", "tokenless"),
        )

        # Unique name that cannot collide with a real installation.
        fake_name = "_test_rtk_degraded_%d" % os.getpid()
        fake_home = os.path.join(self.tmp, "home")
        os.makedirs(fake_home)

        user_layouts = [
            (".local", "bin"),                                          # PATH-less user bin
            (".local", "lib", "anolisa", "libexec", "tokenless"),       # Anolisa CLI user
            (".local", "libexec", "anolisa", "tokenless"),              # Makefile user
            (".local", "share", "anolisa", "tokenless"),                # legacy
            (".local", "lib", "anolisa", "tokenless"),                  # legacy
        ]

        import shutil as _shutil_mod
        original_which = _shutil_mod.which
        original_expanduser = os.path.expanduser
        _shutil_mod.which = lambda name, path=None: None
        os.path.expanduser = lambda p: fake_home + p[1:] if p.startswith("~") else p
        try:
            for layout in user_layouts:
                layout_dir = os.path.join(fake_home, *layout)
                os.makedirs(layout_dir, exist_ok=True)
                fake_bin = os.path.join(layout_dir, fake_name)
                with open(fake_bin, "w") as f:
                    f.write("#!/bin/sh\necho rtk 0.43.0\n")
                os.chmod(fake_bin, 0o755)
                try:
                    found = plugin.resolve_binary(fake_name)
                    self.assertEqual(
                        found, fake_bin,
                        "degraded resolver missed user layout %r" % (layout,),
                    )
                finally:
                    os.unlink(fake_bin)

            # Canonical order check: ~/.local/bin wins over the legacy
            # share layout when both exist.
            bin_fake = os.path.join(fake_home, ".local", "bin", fake_name)
            legacy_fake = os.path.join(
                fake_home, ".local", "share", "anolisa", "tokenless", fake_name,
            )
            for fake_bin in (bin_fake, legacy_fake):
                with open(fake_bin, "w") as f:
                    f.write("#!/bin/sh\necho rtk 0.43.0\n")
                os.chmod(fake_bin, 0o755)
            try:
                self.assertEqual(plugin.resolve_binary(fake_name), bin_fake)
            finally:
                os.unlink(bin_fake)
                os.unlink(legacy_fake)
        finally:
            _shutil_mod.which = original_which
            os.path.expanduser = original_expanduser

    def test_degraded_mode_resolve_binary_covers_system_layouts(self):
        # System install layouts are covered without touching the real
        # filesystem at all: shutil.which reports an empty PATH and
        # os.path.isfile/os.access recognize one fake candidate at a time,
        # so every layout is proven reachable and nothing is written.
        xdg = os.path.join(self.tmp, "xdg-data")
        self._make_old_hooks_dir(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        plugin = _load_plugin(self.plugin_copy, "hermes_degraded_sys")
        self.assertFalse(plugin._HOOK_UTILS_AVAILABLE)

        fake_name = "_test_rtk_degraded_sys_%d" % os.getpid()
        system_layouts = [
            os.path.join("/usr/local/bin", fake_name),
            os.path.join("/usr/local/libexec/anolisa/tokenless", fake_name),
            os.path.join("/usr/bin", fake_name),
            os.path.join("/usr/libexec/anolisa/tokenless", fake_name),
            os.path.join("/usr/lib/anolisa/tokenless", fake_name),
        ]

        import shutil as _shutil_mod
        original_which = _shutil_mod.which
        original_isfile = os.path.isfile
        original_access = os.access
        _shutil_mod.which = lambda name, path=None: None
        try:
            for target in system_layouts:
                os.path.isfile = lambda p, _t=target: p == _t
                os.access = lambda p, mode, _t=target: p == _t
                try:
                    found = plugin.resolve_binary(fake_name)
                    self.assertEqual(
                        found, target,
                        "degraded resolver missed system layout %r" % (target,),
                    )
                finally:
                    os.path.isfile = original_isfile
                    os.access = original_access

            # Explicit fallback paths are still honored after known layouts
            # (the hermes _resolve_binary wrapper relies on this).
            explicit = os.path.join(self.tmp, "explicit", fake_name)
            os.makedirs(os.path.dirname(explicit), exist_ok=True)
            with open(explicit, "w") as f:
                f.write("#!/bin/sh\necho rtk 0.43.0\n")
            os.chmod(explicit, 0o755)
            self.assertEqual(plugin.resolve_binary(fake_name, explicit), explicit)
        finally:
            _shutil_mod.which = original_which
            os.path.isfile = original_isfile
            os.access = original_access


class AnchorRtkPrefixTest(unittest.TestCase):
    """Regression tests for _anchor_rtk_prefix semicolon and newline handling.

    Covers ikunkun-sys's review findings on PR #2249:
    - P1: semicolon-chained ``rtk`` (e.g. ``rtk git status; rtk cargo test``)
      must anchor the second ``rtk`` token.
    - P1: newline-separated commands must preserve the newline, not collapse
      to spaces.
    - P2: _check_api_compat must keep the freshly imported module in
      sys.modules on success, not restore the stale cached copy.
    """

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin(_PLUGIN_SRC, "hermes_anchor_test")
        assert cls.plugin._HOOK_UTILS_AVAILABLE, "shared hook_utils must resolve in source tree"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="anchor-rtk-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Copy plugin for degraded-mode tests (so source-tree candidate
        # doesn't resolve and the plugin falls into degraded mode).
        plugin_dir = os.path.join(self.tmp, "plugins", "tokenless")
        os.makedirs(plugin_dir)
        shutil.copy(_PLUGIN_SRC, plugin_dir)
        self.plugin_copy = os.path.join(plugin_dir, "__init__.py")
        self._saved_xdg = os.environ.get("XDG_DATA_HOME")

    def tearDown(self):
        if self._saved_xdg is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = self._saved_xdg

    def _make_old_hooks_dir_xdg(self, base: str) -> str:
        """Create a hooks dir with old hook_utils.py (no _anchor_rtk_prefix)."""
        hooks = os.path.join(base, "anolisa", "adapters", "tokenless", "common", "hooks")
        os.makedirs(hooks, mode=0o755)
        with open(os.path.join(hooks, "hook_utils.py"), "w") as f:
            f.write(
                "# Old hook_utils without _anchor_rtk_prefix\n"
                "def resolve_binary(name, *fallbacks): return None\n"
            )
        shutil.copy(
            os.path.join(_HOOKS_SRC, "tool_categories.json"),
            hooks,
        )
        os.chmod(hooks, 0o755)
        return hooks

    # -- P1: semicolon-chained rtk (shared impl) ----------------------------

    def test_semicolon_chained_rtk_anchors_both_segments(self):
        # RTK 0.43 compound rewrite: "rtk git status; rtk cargo test"
        # The semicolon is attached to "status" as "status;".  Both rtk
        # tokens must be anchored.
        cmd = "rtk git status; rtk cargo test"
        result = self.plugin._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertEqual(
            result,
            "/usr/bin/rtk git status; /usr/bin/rtk cargo test",
            "both rtk tokens must be anchored",
        )

    # -- P1: newline preservation (shared impl) ------------------------------

    def test_newline_separator_preserved(self):
        # "rtk git status\ncargo build" — the newline must be preserved,
        # not collapsed to a space.  Otherwise "cargo build" would become
        # an argument to "status" and never execute.
        cmd = "rtk git status\ncargo build"
        result = self.plugin._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertIn("\n", result, "newline must be preserved, not collapsed to space")
        self.assertEqual(
            result,
            "/usr/bin/rtk git status\ncargo build",
            "only rtk should be replaced, newline and rest preserved",
        )

    # -- P1: semicolon-chained rtk (degraded/fallback impl) -----------------

    def test_degraded_semicolon_chained_rtk_anchors_both_segments(self):
        xdg = os.path.join(self.tmp, "xdg-data")
        self._make_old_hooks_dir_xdg(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        degraded = _load_plugin(self.plugin_copy, "hermes_anchor_degraded")
        self.assertFalse(degraded._HOOK_UTILS_AVAILABLE)
        cmd = "rtk git status; rtk cargo test"
        result = degraded._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertEqual(
            result,
            "/usr/bin/rtk git status; /usr/bin/rtk cargo test",
            "degraded fallback must anchor both rtk tokens",
        )

    # -- P1: newline preservation (degraded/fallback impl) ------------------

    def test_degraded_newline_separator_preserved(self):
        xdg = os.path.join(self.tmp, "xdg-data")
        self._make_old_hooks_dir_xdg(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        degraded = _load_plugin(self.plugin_copy, "hermes_anchor_degraded_nl")
        self.assertFalse(degraded._HOOK_UTILS_AVAILABLE)
        cmd = "rtk git status\ncargo build"
        result = degraded._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertIn("\n", result, "degraded fallback must preserve newline")

    # -- P2: API compat cache -------------------------------------------------

    def test_check_api_compat_keeps_fresh_module_on_success(self):
        # When _check_api_compat succeeds, the freshly imported module must
        # stay in sys.modules — the old cached module must NOT be restored.
        import types

        # Create a fake "old" module and put it in sys.modules
        old_mod = types.ModuleType("hook_utils")
        old_mod._STALE = True  # marker so we can detect it
        sys.modules["hook_utils"] = old_mod

        try:
            # Trial-import from the real source-tree hooks dir
            hooks_dir = os.path.realpath(_HOOKS_SRC)
            reason = self.plugin._check_api_compat(hooks_dir)
            self.assertIsNone(reason, "compatible candidate must pass API check")

            # The module in sys.modules must be the fresh one, not old_mod
            current = sys.modules.get("hook_utils")
            self.assertIsNotNone(current, "module must remain in sys.modules")
            self.assertFalse(
                getattr(current, "_STALE", False),
                "stale module was restored instead of fresh candidate",
            )
        finally:
            sys.modules.pop("hook_utils", None)

    # -- P1: newline is a real segment boundary (shared impl) ---------------

    def test_newline_separated_rtk_anchors_both_segments(self):
        # Newline terminates a command exactly like `;`: the rtk after
        # the newline starts a fresh segment and must be anchored.
        cmd = "rtk git status\nrtk cargo test"
        result = self.plugin._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertEqual(
            result,
            "/usr/bin/rtk git status\n/usr/bin/rtk cargo test",
            "rtk after newline must be anchored",
        )

    def test_escaped_semicolon_is_not_a_boundary(self):
        # `\;` is an escaped argument character, not a command
        # separator: the trailing `rtk` is grep's argument and must
        # stay bare instead of being anchored.
        cmd = "rtk grep foo\\; rtk file"
        result = self.plugin._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertEqual(
            result,
            "/usr/bin/rtk grep foo\\; rtk file",
            "escaped semicolon must not start a new segment",
        )

    # -- P1: same probes against the degraded/fallback impl -----------------

    def test_degraded_newline_separated_rtk_anchors_both_segments(self):
        xdg = os.path.join(self.tmp, "xdg-data")
        self._make_old_hooks_dir_xdg(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        degraded = _load_plugin(self.plugin_copy, "hermes_anchor_degraded_nl2")
        self.assertFalse(degraded._HOOK_UTILS_AVAILABLE)
        cmd = "rtk git status\nrtk cargo test"
        result = degraded._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertEqual(
            result,
            "/usr/bin/rtk git status\n/usr/bin/rtk cargo test",
            "degraded fallback must anchor rtk after newline",
        )

    def test_degraded_escaped_semicolon_is_not_a_boundary(self):
        xdg = os.path.join(self.tmp, "xdg-data")
        self._make_old_hooks_dir_xdg(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        degraded = _load_plugin(self.plugin_copy, "hermes_anchor_degraded_esc")
        self.assertFalse(degraded._HOOK_UTILS_AVAILABLE)
        cmd = "rtk grep foo\\; rtk file"
        result = degraded._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertEqual(
            result,
            "/usr/bin/rtk grep foo\\; rtk file",
            "degraded fallback must not treat escaped semicolon as boundary",
        )

    # -- P1: command position vs argument position (shared impl) ------------

    def test_rtk_argument_in_ignored_segment_not_anchored(self):
        # "echo rtk && rtk git status" — the first rtk is a positional
        # argument to echo (an ignored segment kept as-is) and must not be
        # anchored; the second rtk is in command position and must be
        # anchored.
        cmd = "echo rtk && rtk git status"
        result = self.plugin._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertEqual(
            result,
            "echo rtk && /usr/bin/rtk git status",
            "only command-position rtk is anchored",
        )

    def test_rtk_argument_alone_not_anchored(self):
        cmd = "echo rtk done"
        result = self.plugin._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertEqual(result, "echo rtk done")

    def test_wrapper_before_rtk_still_anchors(self):
        # Transparent wrappers (e.g. sudo) must not consume command position.
        cmd = "sudo rtk git status"
        result = self.plugin._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertEqual(result, "sudo /usr/bin/rtk git status")

    # -- P1: command position vs argument position (degraded/fallback impl) -

    def test_degraded_rtk_argument_in_ignored_segment_not_anchored(self):
        xdg = os.path.join(self.tmp, "xdg-data")
        self._make_old_hooks_dir_xdg(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        degraded = _load_plugin(self.plugin_copy, "hermes_anchor_argpos")
        self.assertFalse(degraded._HOOK_UTILS_AVAILABLE)
        cmd = "echo rtk && rtk git status"
        result = degraded._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertEqual(
            result,
            "echo rtk && /usr/bin/rtk git status",
            "degraded fallback must not anchor rtk argument",
        )

    def test_degraded_wrapper_before_rtk_still_anchors(self):
        xdg = os.path.join(self.tmp, "xdg-data")
        self._make_old_hooks_dir_xdg(xdg)
        os.environ["XDG_DATA_HOME"] = xdg
        degraded = _load_plugin(self.plugin_copy, "hermes_anchor_wrapper")
        self.assertFalse(degraded._HOOK_UTILS_AVAILABLE)
        cmd = "sudo rtk git status"
        result = degraded._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
        self.assertEqual(result, "sudo /usr/bin/rtk git status")


class AnchorTransparentPrefixTest(unittest.TestCase):
    """Regression tests for RTK's transparent-prefix protocol anchoring.

    Covers ikunkun-sys's 2026-08-14 review finding on PR #2249: the
    command-position state machine only whitelisted fixed shell wrappers,
    so RTK v0.43 outputs that start with a transparent prefix — built-ins
    (``uv run``, ``noglob``, ``command``, ``builtin``, ``exec``,
    ``nocorrect``) or user-configured multi-word
    ``[hooks].transparent_prefixes`` (e.g. ``shadowenv exec --``) — kept a
    bare ``rtk`` that fails with exit 127 in trimmed-PATH environments.

    Every test sandboxes HOME/XDG_CONFIG_HOME under a fresh temp dir with
    a known rtk config.toml so results are deterministic on any host,
    with or without a real rtk installation or user config.
    """

    CONFIGURED = 'transparent_prefixes = ["shadowenv exec --", "docker exec c1", "foo bar"]'

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin(_PLUGIN_SRC, "hermes_anchor_tprefix_test")
        assert cls.plugin._HOOK_UTILS_AVAILABLE, "shared hook_utils must resolve in source tree"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="anchor-tprefix-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved_home = os.environ.get("HOME")
        self._saved_xdg_config = os.environ.get("XDG_CONFIG_HOME")
        self._saved_xdg_data = os.environ.get("XDG_DATA_HOME")
        # Sandboxed HOME with no anolisa installs: degraded loads fall
        # back, and rtk's config.toml resolves under the sandbox.
        os.environ["HOME"] = self.tmp
        cfg = os.path.join(self.tmp, ".config", "rtk")
        os.makedirs(cfg)
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, ".config")
        os.environ["XDG_DATA_HOME"] = os.path.join(self.tmp, "xdg-data")
        with open(os.path.join(cfg, "config.toml"), "w") as f:
            f.write("[hooks]\n" + self.CONFIGURED + "\n")
        # Copy plugin for degraded-mode loads (no hooks candidate under
        # the sandboxed HOME, so the fallback implementation is used).
        plugin_dir = os.path.join(self.tmp, "plugins", "tokenless")
        os.makedirs(plugin_dir)
        shutil.copy(_PLUGIN_SRC, plugin_dir)
        self.plugin_copy = os.path.join(plugin_dir, "__init__.py")

    def tearDown(self):
        for name, saved in (
            ("HOME", self._saved_home),
            ("XDG_CONFIG_HOME", self._saved_xdg_config),
            ("XDG_DATA_HOME", self._saved_xdg_data),
        ):
            if saved is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = saved

    def _degraded(self, name: str):
        degraded = _load_plugin(self.plugin_copy, name)
        assert not degraded._HOOK_UTILS_AVAILABLE, "copy must load in degraded mode"
        return degraded

    # -- shared implementation ------------------------------------------------

    def test_builtin_transparent_prefixes_anchor(self):
        for wrapper in ("noglob", "command", "builtin", "exec", "nocorrect"):
            with self.subTest(wrapper=wrapper):
                result = self.plugin._anchor_rtk_prefix(
                    f"{wrapper} rtk git status", "/usr/bin/rtk",
                )
                self.assertEqual(result, f"{wrapper} /usr/bin/rtk git status")

    def test_uv_run_multiword_builtin_anchors(self):
        result = self.plugin._anchor_rtk_prefix("uv run rtk pytest tests/", "/usr/bin/rtk")
        self.assertEqual(result, "uv run /usr/bin/rtk pytest tests/")

    def test_env_assignment_composes_before_builtin(self):
        result = self.plugin._anchor_rtk_prefix(
            "PYTHONPATH=. uv run rtk pytest tests/", "/usr/bin/rtk",
        )
        self.assertEqual(result, "PYTHONPATH=. uv run /usr/bin/rtk pytest tests/")

    def test_wrapper_nests_with_builtin(self):
        result = self.plugin._anchor_rtk_prefix("sudo noglob rtk git status", "/usr/bin/rtk")
        self.assertEqual(result, "sudo noglob /usr/bin/rtk git status")

    def test_configured_transparent_prefix_anchors(self):
        result = self.plugin._anchor_rtk_prefix(
            "shadowenv exec -- rtk git status", "/usr/bin/rtk",
        )
        self.assertEqual(result, "shadowenv exec -- /usr/bin/rtk git status")

    def test_second_configured_prefix_anchors(self):
        result = self.plugin._anchor_rtk_prefix("docker exec c1 rtk git status", "/usr/bin/rtk")
        self.assertEqual(result, "docker exec c1 /usr/bin/rtk git status")

    def test_env_between_configured_prefix_and_rtk_anchors(self):
        result = self.plugin._anchor_rtk_prefix(
            "shadowenv exec -- FOO=bar rtk git status", "/usr/bin/rtk",
        )
        self.assertEqual(result, "shadowenv exec -- FOO=bar /usr/bin/rtk git status")

    def test_configured_prefix_anchors_in_every_segment(self):
        result = self.plugin._anchor_rtk_prefix(
            "noglob rtk git status; shadowenv exec -- rtk cargo test", "/usr/bin/rtk",
        )
        self.assertEqual(
            result,
            "noglob /usr/bin/rtk git status; shadowenv exec -- /usr/bin/rtk cargo test",
        )

    def test_partial_configured_prefix_not_matched(self):
        # Bare `shadowenv` is not the configured `shadowenv exec --`: the
        # command position is consumed and the rtk stays bare.
        result = self.plugin._anchor_rtk_prefix("shadowenv rtk git status", "/usr/bin/rtk")
        self.assertEqual(result, "shadowenv rtk git status")

    def test_configured_prefix_never_crosses_segment_boundary(self):
        # "foo bar" is configured, but `;` splits the sequence: segment 2
        # starts with `bar`, which alone is not a prefix.
        result = self.plugin._anchor_rtk_prefix("foo; bar rtk git status", "/usr/bin/rtk")
        self.assertEqual(result, "foo; bar rtk git status")

    def test_echo_rtk_argument_still_not_anchored(self):
        result = self.plugin._anchor_rtk_prefix("echo rtk && rtk git status", "/usr/bin/rtk")
        self.assertEqual(result, "echo rtk && /usr/bin/rtk git status")

    # -- wrapper option operands (mixed compound negatives) ------------------

    def test_wrapper_option_operands_not_anchored(self):
        # A wrapper option consumes the command position: the username
        # operand of `sudo -u`, the variable name of `env -u`, and the
        # query argument of `command -v` must never be rewritten into the
        # executable path, while the following segment's command-position
        # rtk is still anchored.
        cases = [
            (
                "sudo -u rtk true && rtk git status",
                "sudo -u rtk true && /usr/bin/rtk git status",
            ),
            ("env -u rtk && rtk git status", "env -u rtk && /usr/bin/rtk git status"),
            (
                "command -v rtk && rtk git status",
                "command -v rtk && /usr/bin/rtk git status",
            ),
        ]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                result = self.plugin._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
                self.assertEqual(result, want)

    def test_wrapper_option_segment_alone_not_anchored(self):
        result = self.plugin._anchor_rtk_prefix("sudo -u rtk true", "/usr/bin/rtk")
        self.assertEqual(result, "sudo -u rtk true")

    def test_option_operand_in_later_segment_not_anchored(self):
        result = self.plugin._anchor_rtk_prefix(
            "rtk git status && sudo -u rtk true", "/usr/bin/rtk",
        )
        self.assertEqual(result, "/usr/bin/rtk git status && sudo -u rtk true")

    def test_bare_wrappers_still_anchor_after_option_fix(self):
        # Regression guard for the operand fix: option-less wrappers and
        # transparent prefixes keep anchoring.
        for cmd, want in (
            ("sudo rtk git status", "sudo /usr/bin/rtk git status"),
            ("sudo noglob rtk git status", "sudo noglob /usr/bin/rtk git status"),
            ("uv run rtk pytest tests/", "uv run /usr/bin/rtk pytest tests/"),
            ("env FOO=bar rtk git status", "env FOO=bar /usr/bin/rtk git status"),
            (
                "shadowenv exec -- rtk git status",
                "shadowenv exec -- /usr/bin/rtk git status",
            ),
        ):
            with self.subTest(cmd=cmd):
                result = self.plugin._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
                self.assertEqual(result, want)

    # -- degraded fallback implementation -------------------------------------

    def test_degraded_builtin_transparent_prefixes_anchor(self):
        degraded = self._degraded("hermes_anchor_tprefix_degraded_builtin")
        for wrapper in ("noglob", "exec"):
            with self.subTest(wrapper=wrapper):
                result = degraded._anchor_rtk_prefix(
                    f"{wrapper} rtk git status", "/usr/bin/rtk",
                )
                self.assertEqual(result, f"{wrapper} /usr/bin/rtk git status")
        result = degraded._anchor_rtk_prefix("uv run rtk pytest tests/", "/usr/bin/rtk")
        self.assertEqual(result, "uv run /usr/bin/rtk pytest tests/")

    def test_degraded_configured_transparent_prefix_anchors(self):
        degraded = self._degraded("hermes_anchor_tprefix_degraded_configured")
        result = degraded._anchor_rtk_prefix(
            "shadowenv exec -- rtk git status", "/usr/bin/rtk",
        )
        self.assertEqual(result, "shadowenv exec -- /usr/bin/rtk git status")

    def test_degraded_partial_configured_prefix_not_matched(self):
        degraded = self._degraded("hermes_anchor_tprefix_degraded_partial")
        result = degraded._anchor_rtk_prefix("shadowenv rtk git status", "/usr/bin/rtk")
        self.assertEqual(result, "shadowenv rtk git status")

    def test_degraded_echo_rtk_argument_still_not_anchored(self):
        degraded = self._degraded("hermes_anchor_tprefix_degraded_echo")
        result = degraded._anchor_rtk_prefix("echo rtk && rtk git status", "/usr/bin/rtk")
        self.assertEqual(result, "echo rtk && /usr/bin/rtk git status")

    def test_degraded_wrapper_option_operands_not_anchored(self):
        degraded = self._degraded("hermes_anchor_tprefix_degraded_optoperand")
        cases = [
            (
                "sudo -u rtk true && rtk git status",
                "sudo -u rtk true && /usr/bin/rtk git status",
            ),
            ("env -u rtk && rtk git status", "env -u rtk && /usr/bin/rtk git status"),
            (
                "command -v rtk && rtk git status",
                "command -v rtk && /usr/bin/rtk git status",
            ),
        ]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                result = degraded._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
                self.assertEqual(result, want)

    def test_degraded_bare_wrappers_still_anchor_after_option_fix(self):
        degraded = self._degraded("hermes_anchor_tprefix_degraded_optguard")
        for cmd, want in (
            ("sudo rtk git status", "sudo /usr/bin/rtk git status"),
            ("noglob rtk git status", "noglob /usr/bin/rtk git status"),
            (
                "shadowenv exec -- rtk git status",
                "shadowenv exec -- /usr/bin/rtk git status",
            ),
        ):
            with self.subTest(cmd=cmd):
                result = degraded._anchor_rtk_prefix(cmd, "/usr/bin/rtk")
                self.assertEqual(result, want)


class DegradedFallbackSyncTest(unittest.TestCase):
    """Drift guard for the degraded-mode fallback (PR #2249 review N2).

    hermes/__init__.py intentionally duplicates the hook_utils rtk
    anchoring logic (the ``_fb`` constants and lexer) because degraded
    mode must keep working when no trusted hook_utils can be imported.
    The copies carry KEEP IN SYNC comments; these tests pin them to the
    shared implementation so a one-sided edit fails here instead of
    drifting silently.
    """

    CONFIGURED = 'transparent_prefixes = ["shadowenv exec --", "docker exec c1"]'

    # Command-position matrix: bare / wrapped / prefixed rtk,
    # argument-position negatives, segment connectives, quoting, fd
    # redirection, and configured prefixes.  Both implementations must
    # return identical output for every entry.
    CORPUS = (
        "rtk",
        "rtk git status",
        "echo rtk",
        "echo rtk && rtk git status",
        "sudo rtk git status",
        "sudo -u rtk true",
        "command -v rtk",
        "env -u FOO rtk ls",
        "time nice rtk ls",
        "FOO=bar rtk ls",
        "FOO=bar sudo rtk ls",
        "rtk ls; rtk git status",
        "rtk ls && rtk git status || rtk make check",
        "rtk ls | rtk grep -q done",
        "ls 2>&1 | rtk grep err",
        "ls &> log && rtk tail log",
        "echo a\\; b && rtk status",
        "echo 'rtk stays quoted'",
        'echo "rtk in double quotes"',
        "rtk ls\nrtk git status",
        "noglob rtk git status",
        "uv run rtk pytest tests/",
        "command builtin rtk ls",
        "PYTHONPATH=. uv run rtk pytest",
        "shadowenv exec -- rtk git status",
        "docker exec c1 rtk git status",
        "shadowenv exec -- echo rtk",
        "foo bar rtk ls",
        "no rtk token at all",
        "",
    )

    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin(_PLUGIN_SRC, "hermes_fb_sync_shared")
        assert cls.plugin._HOOK_UTILS_AVAILABLE, (
            "shared hook_utils must resolve in source tree"
        )
        # Canonical hook_utils loaded straight from the shared hooks dir,
        # independent of the plugin's own resolution, so the constants
        # are compared against the real source of truth.
        sys.modules.pop("hook_utils", None)
        sys.path.insert(0, _HOOKS_SRC)
        try:
            import hook_utils as hook_utils_mod
            cls.hook_utils = hook_utils_mod
        finally:
            sys.path.remove(_HOOKS_SRC)

    def setUp(self):
        # Same sandbox as AnchorTransparentPrefixTest: fresh HOME/XDG with
        # a known rtk config.toml, and a plugin copy that has no hooks
        # candidate, so it loads in degraded mode.
        self.tmp = tempfile.mkdtemp(prefix="fb-sync-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved_home = os.environ.get("HOME")
        self._saved_xdg_config = os.environ.get("XDG_CONFIG_HOME")
        self._saved_xdg_data = os.environ.get("XDG_DATA_HOME")
        os.environ["HOME"] = self.tmp
        cfg = os.path.join(self.tmp, ".config", "rtk")
        os.makedirs(cfg)
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, ".config")
        os.environ["XDG_DATA_HOME"] = os.path.join(self.tmp, "xdg-data")
        with open(os.path.join(cfg, "config.toml"), "w") as f:
            f.write("[hooks]\n" + self.CONFIGURED + "\n")
        plugin_dir = os.path.join(self.tmp, "plugins", "tokenless")
        os.makedirs(plugin_dir)
        shutil.copy(_PLUGIN_SRC, plugin_dir)
        self.plugin_copy = os.path.join(plugin_dir, "__init__.py")

    def tearDown(self):
        for name, saved in (
            ("HOME", self._saved_home),
            ("XDG_CONFIG_HOME", self._saved_xdg_config),
            ("XDG_DATA_HOME", self._saved_xdg_data),
        ):
            if saved is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = saved

    def _degraded(self, name: str):
        degraded = _load_plugin(self.plugin_copy, name)
        assert not degraded._HOOK_UTILS_AVAILABLE, (
            "copy must load in degraded mode"
        )
        return degraded

    def test_mirrored_constants_match_shared_hook_utils(self):
        degraded = self._degraded("hermes_fb_sync_constants")
        pairs = (
            ("_SEGMENT_META_CHARS", "_SEGMENT_META_CHARS_FB"),
            ("_SEGMENT_BOUNDARY_CHARS", "_SEGMENT_BOUNDARY_CHARS_FB"),
            ("_WORD_BREAK_CHARS", "_WORD_BREAK_CHARS_FB"),
            ("_TRANSPARENT_WRAPPERS", "_TRANSPARENT_WRAPPERS_FB"),
            ("_RTK_BUILTIN_TRANSPARENT_PREFIXES",
             "_RTK_BUILTIN_TRANSPARENT_PREFIXES_FB"),
        )
        for shared_name, fb_name in pairs:
            with self.subTest(constant=shared_name):
                self.assertEqual(
                    getattr(self.hook_utils, shared_name),
                    getattr(degraded, fb_name),
                    "%s in hermes/__init__.py drifted from hook_utils.%s"
                    " - update both sides (KEEP IN SYNC)." % (fb_name, shared_name),
                )
        self.assertEqual(
            self.hook_utils._ENV_ASSIGNMENT_RE.pattern,
            degraded._ENV_ASSIGNMENT_RE_FB.pattern,
            "_ENV_ASSIGNMENT_RE_FB in hermes/__init__.py drifted from"
            " hook_utils._ENV_ASSIGNMENT_RE.",
        )

    def test_anchor_output_matches_shared_implementation(self):
        degraded = self._degraded("hermes_fb_sync_anchor")
        for cmd in self.CORPUS:
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    self.plugin._anchor_rtk_prefix(cmd, "/usr/bin/rtk"),
                    degraded._anchor_rtk_prefix(cmd, "/usr/bin/rtk"),
                    "degraded-mode _anchor_rtk_prefix diverged from shared"
                    " hook_utils - update both implementations (KEEP IN SYNC).",
                )


if __name__ == "__main__":
    unittest.main()
