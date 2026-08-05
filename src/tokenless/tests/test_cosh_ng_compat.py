#!/usr/bin/env python3
from __future__ import annotations

"""Tests for Cosh-NG compatibility in tokenless hooks.

Tests the acceptance criteria from issue #1615:
- Cosh-NG runtime detection from wrapped tool_response
- llmContent extraction (only model-visible content compressed)
- Replacement field emission for PostToolUse
- tool_input field emission for PreToolUse
- Cosh-NG agent ID attribution
- Version detection and fail-open for unsupported versions
- Unsupported runtimes pass through without duplicate injection
"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_HOOKS_DIR = (
    Path(__file__).resolve().parent.parent
    / "adapters"
    / "tokenless"
    / "common"
    / "hooks"
)

_spec = importlib.util.spec_from_file_location(
    "hook_utils", _HOOKS_DIR / "hook_utils.py"
)
assert _spec and _spec.loader
hook_utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook_utils)

COMPRESS_HOOK = _HOOKS_DIR / "compress_response_hook.py"
REWRITE_HOOK = _HOOKS_DIR / "rewrite_hook.py"


def _write_exec(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_large_llm_content(char_target: int = 500) -> str:
    """Return a JSON object string larger than _MIN_RESPONSE_CHARS (200)."""
    return json.dumps({"stdout": "x" * char_target, "exit_code": 0})


def _create_mock_tokenless(tmpdir: Path, behavior: str = "compress") -> Path:
    """Create a mock tokenless binary that simulates compression behavior."""
    mock_script = tmpdir / "tokenless"

    if behavior == "compress":
        script = textwrap.dedent("""\
            #!/usr/bin/env python3
            import json, sys
            if sys.argv[1] == "compress-response":
                data = json.loads(sys.stdin.read())
                compressed = {}
                for k, v in data.items():
                    if isinstance(v, str) and len(v) > 20:
                        compressed[k] = v[:20]
                    else:
                        compressed[k] = v
                print(json.dumps(compressed))
            elif sys.argv[1] == "compress-toon":
                sys.exit(1)
        """)
    elif behavior == "passthrough":
        script = textwrap.dedent("""\
            #!/usr/bin/env python3
            import sys
            print(sys.stdin.read())
        """)
    else:
        raise ValueError(f"Unknown behavior: {behavior}")

    _write_exec(mock_script, script)
    return mock_script


def _create_mock_rtk(tmpdir: Path) -> Path:
    """Create a mock rtk binary that rewrites commands for PreToolUse tests."""
    mock_script = tmpdir / "rtk"
    script = textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        if len(sys.argv) > 1 and sys.argv[1] == "--version":
            print("rtk 0.43.0")
            sys.exit(0)
        if len(sys.argv) > 2 and sys.argv[1] == "rewrite":
            print(f"rtk {sys.argv[2]}")
            sys.exit(0)
        sys.exit(1)
    """)
    _write_exec(mock_script, script)
    return mock_script


class TestCoshNGRuntimeDetection(unittest.TestCase):
    """Test Cosh-NG runtime detection from hook input."""

    def test_detect_cosh_ng_wrapped_response(self):
        """Cosh-NG wraps tool_response as {llmContent, returnDisplay}."""
        input_data = {
            "tool_name": "Bash",
            "tool_response": {
                "llmContent": "command output here",
                "returnDisplay": "Bash completed",
            },
        }
        self.assertTrue(hook_utils.is_cosh_ng_runtime(input_data))

    def test_detect_copilot_shell_string_response(self):
        """Copilot-Shell passes tool_response as a plain string."""
        input_data = {
            "tool_name": "Bash",
            "tool_response": '{"exit_code":0,"stdout":"hello"}',
        }
        self.assertFalse(hook_utils.is_cosh_ng_runtime(input_data))

    def test_detect_empty_response(self):
        """Empty tool_response is not Cosh-NG."""
        input_data = {"tool_name": "Bash", "tool_response": ""}
        self.assertFalse(hook_utils.is_cosh_ng_runtime(input_data))

    def test_detect_missing_response(self):
        """Missing tool_response is not Cosh-NG."""
        input_data = {"tool_name": "Bash"}
        self.assertFalse(hook_utils.is_cosh_ng_runtime(input_data))

    def test_detect_dict_without_llm_content(self):
        """Dict without llmContent is not Cosh-NG wrapper."""
        input_data = {
            "tool_name": "Bash",
            "tool_response": {"data": "some value"},
        }
        self.assertFalse(hook_utils.is_cosh_ng_runtime(input_data))


class TestLLMContentExtraction(unittest.TestCase):
    """Test extraction of model-visible llmContent from Cosh-NG wrapper."""

    def test_extract_llm_content_string(self):
        """Extract llmContent when it's a string."""
        input_data = {
            "tool_response": {
                "llmContent": "model visible content",
                "returnDisplay": "display text",
            }
        }
        self.assertEqual(hook_utils.extract_llm_content(input_data), "model visible content")

    def test_extract_llm_content_missing(self):
        """Return None when llmContent is missing."""
        input_data = {
            "tool_response": {
                "returnDisplay": "display text",
            }
        }
        self.assertIsNone(hook_utils.extract_llm_content(input_data))

    def test_extract_llm_content_empty(self):
        """Return None when llmContent is empty."""
        input_data = {
            "tool_response": {
                "llmContent": "",
                "returnDisplay": "display text",
            }
        }
        self.assertIsNone(hook_utils.extract_llm_content(input_data))

    def test_extract_llm_content_non_dict(self):
        """Return None when tool_response is not a dict."""
        input_data = {"tool_response": "plain string"}
        self.assertIsNone(hook_utils.extract_llm_content(input_data))

    def test_extract_llm_content_non_string(self):
        """Return None when llmContent is not a string."""
        input_data = {
            "tool_response": {
                "llmContent": {"nested": "object"},
                "returnDisplay": "display",
            }
        }
        self.assertIsNone(hook_utils.extract_llm_content(input_data))

    def test_return_display_not_extracted(self):
        """returnDisplay must never be extracted as model-visible content."""
        input_data = {
            "tool_response": {
                "llmContent": "for model",
                "returnDisplay": "for display only",
            }
        }
        result = hook_utils.extract_llm_content(input_data)
        self.assertEqual(result, "for model")
        self.assertNotIn("display", result)


class TestBuildCoshNGPostToolOutput(unittest.TestCase):
    """Test building Cosh-NG-compatible PostToolUse hook output."""

    def test_post_tool_output_with_replacement(self):
        """PostToolUse output includes replacement field."""
        output = hook_utils.build_cosh_ng_post_tool_output(
            replacement="compressed content",
            additional_context="[tokenless:env] error info",
        )
        self.assertIn("hookSpecificOutput", output)
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PostToolUse")
        self.assertEqual(specific["replacement"], "compressed content")
        self.assertEqual(specific["additionalContext"], "[tokenless:env] error info")

    def test_post_tool_output_no_replacement(self):
        """PostToolUse output without replacement (env-only attribution)."""
        output = hook_utils.build_cosh_ng_post_tool_output(
            replacement=None,
            additional_context="env attribution",
        )
        specific = output["hookSpecificOutput"]
        self.assertNotIn("replacement", specific)
        self.assertEqual(specific["additionalContext"], "env attribution")

    def test_post_tool_output_no_additional_context(self):
        """PostToolUse output without additional context."""
        output = hook_utils.build_cosh_ng_post_tool_output(
            replacement="compressed",
            additional_context=None,
        )
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["replacement"], "compressed")
        self.assertNotIn("additionalContext", specific)

    def test_return_display_absent_from_post_output(self):
        """returnDisplay must never appear in the hook output."""
        output = hook_utils.build_cosh_ng_post_tool_output(
            replacement="content",
            additional_context="ctx",
        )
        serialized = json.dumps(output)
        self.assertNotIn("returnDisplay", serialized)


class TestBuildCoshNGPreToolOutput(unittest.TestCase):
    """Test building Cosh-NG-compatible PreToolUse hook output."""

    def test_pre_tool_output_dual_field(self):
        """PreToolUse output includes both tool_input and updatedInput."""
        output = hook_utils.build_cosh_ng_pre_tool_output(
            tool_input={"command": "ls -la"},
            decision="allow",
        )
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertEqual(specific["permissionDecision"], "allow")
        # Cosh-NG reads tool_input
        self.assertEqual(specific["tool_input"], {"command": "ls -la"})
        # Codex reads updatedInput
        self.assertEqual(specific["updatedInput"], {"command": "ls -la"})


class TestVersionDetection(unittest.TestCase):
    """Test Cosh-NG version detection and replacement support."""

    def setUp(self) -> None:
        self._saved_env = os.environ.copy()
        os.environ.pop("COSH_NG_VERSION", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_detect_version_set(self):
        """Detect version from COSH_NG_VERSION env var."""
        os.environ["COSH_NG_VERSION"] = "0.6.0"
        self.assertEqual(hook_utils.detect_cosh_ng_version(), (0, 6, 0))

    def test_detect_version_unset(self):
        """Return None when COSH_NG_VERSION is not set."""
        self.assertIsNone(hook_utils.detect_cosh_ng_version())

    def test_detect_version_unparseable(self):
        """Return None for unparseable version strings."""
        os.environ["COSH_NG_VERSION"] = "not-a-version"
        self.assertIsNone(hook_utils.detect_cosh_ng_version())

    def test_supports_replacement_supported_version(self):
        """Return True for Cosh-NG >= 0.6.0."""
        os.environ["COSH_NG_VERSION"] = "0.6.0"
        self.assertTrue(hook_utils.cosh_ng_supports_replacement())

    def test_supports_replacement_old_version(self):
        """Return False for Cosh-NG < 0.6.0."""
        os.environ["COSH_NG_VERSION"] = "0.5.0"
        self.assertFalse(hook_utils.cosh_ng_supports_replacement())

    def test_supports_replacement_future_version(self):
        """Return True for Cosh-NG >= 1.0.0."""
        os.environ["COSH_NG_VERSION"] = "1.0.0"
        self.assertTrue(hook_utils.cosh_ng_supports_replacement())

    def test_supports_replacement_no_version(self):
        """Return False when version not set."""
        os.environ.pop("COSH_NG_VERSION", None)
        self.assertFalse(hook_utils.cosh_ng_supports_replacement())

    def test_supports_replacement_unparseable_version(self):
        """Return False when version is unparseable."""
        os.environ["COSH_NG_VERSION"] = "abc"
        self.assertFalse(hook_utils.cosh_ng_supports_replacement())


@unittest.skipIf(sys.version_info < (3, 9), "compress_response_hook requires Python 3.9+")
class TestCoshNGCompressResponseIntegration(unittest.TestCase):
    """Integration tests for compress_response_hook.py under Cosh-NG."""

    def setUp(self) -> None:
        self._saved_env = os.environ.copy()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.mock_tokenless = _create_mock_tokenless(self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _run_hook(self, stdin_data: dict, env_overrides: dict | None = None) -> dict:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["PATH"] = str(self.mock_tokenless.parent) + ":" + env.get("PATH", "")
        env["TOKENLESS_AGENT_ID"] = "copilot-shell"
        if env_overrides:
            env.update(env_overrides)
        proc = subprocess.run(
            [sys.executable, str(COMPRESS_HOOK)],
            input=json.dumps(stdin_data),
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stdout = proc.stdout.strip()
        if not stdout or stdout == "{}":
            return {}
        return json.loads(stdout)

    def test_cosh_ng_replacement_field_emitted(self):
        """Cosh-NG path emits replacement with compressed llmContent."""
        llm_content = _make_large_llm_content(500)
        stdin_data = {
            "tool_name": "Bash",
            "tool_response": {
                "llmContent": llm_content,
                "returnDisplay": "Bash completed",
            },
        }
        out = self._run_hook(
            stdin_data,
            env_overrides={"COSH_NG_VERSION": "0.6.0"},
        )
        specific = out.get("hookSpecificOutput", {})
        self.assertEqual(specific.get("hookEventName"), "PostToolUse")
        self.assertIn("replacement", specific)
        # returnDisplay must never leak into the replacement output.
        self.assertNotIn("returnDisplay", json.dumps(specific))

    def test_cosh_ng_fail_open_old_version(self):
        """Old Cosh-NG version disables compression to avoid duplicates."""
        llm_content = _make_large_llm_content(500)
        stdin_data = {
            "tool_name": "Bash",
            "tool_response": {
                "llmContent": llm_content,
                "returnDisplay": "Bash completed",
            },
        }
        out = self._run_hook(
            stdin_data,
            env_overrides={"COSH_NG_VERSION": "0.5.0"},
        )
        self.assertEqual(out, {})

    def test_cosh_ng_fail_open_missing_version(self):
        """Cosh-NG detected without version disables compression."""
        llm_content = _make_large_llm_content(500)
        stdin_data = {
            "tool_name": "Bash",
            "tool_response": {
                "llmContent": llm_content,
                "returnDisplay": "Bash completed",
            },
        }
        out = self._run_hook(
            stdin_data,
            env_overrides={"COSH_NG_VERSION": ""},
        )
        self.assertEqual(out, {})

    def test_cosh_ng_small_response_skipped(self):
        """Small Cosh-NG responses are skipped."""
        stdin_data = {
            "tool_name": "Bash",
            "tool_response": {
                "llmContent": '{"stdout":"hi"}',
                "returnDisplay": "Bash completed",
            },
        }
        out = self._run_hook(
            stdin_data,
            env_overrides={"COSH_NG_VERSION": "0.6.0"},
        )
        self.assertEqual(out, {})

    def test_cosh_ng_env_attribution_only(self):
        """Cosh-NG env attribution is emitted even when response is skipped."""
        stdin_data = {
            "tool_name": "Bash",
            "tool_response": {
                "llmContent": '{"stderr":"command not found: foobar","exit_code":127}',
                "returnDisplay": "Bash failed",
            },
        }
        out = self._run_hook(
            stdin_data,
            env_overrides={"COSH_NG_VERSION": "0.6.0"},
        )
        specific = out.get("hookSpecificOutput", {})
        self.assertNotIn("replacement", specific)
        self.assertIn("additionalContext", specific)
        self.assertIn("ENV_DEPENDENCY_MISSING", specific["additionalContext"])

    def test_cosh_ng_plain_text_llm_content(self):
        """Plain text llmContent is wrapped and compressed."""
        stdin_data = {
            "tool_name": "Bash",
            "tool_response": {
                "llmContent": "x" * 500,
                "returnDisplay": "Bash completed",
            },
        }
        out = self._run_hook(
            stdin_data,
            env_overrides={"COSH_NG_VERSION": "0.6.0"},
        )
        specific = out.get("hookSpecificOutput", {})
        self.assertIn("replacement", specific)


class TestCoshNGRewriteIntegration(unittest.TestCase):
    """Integration tests for rewrite_hook.py under Cosh-NG."""

    def setUp(self) -> None:
        self._saved_env = os.environ.copy()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.mock_rtk = _create_mock_rtk(self.home)
        # tokenless is checked for existence by rewrite_hook.
        _create_mock_tokenless(self.home, behavior="passthrough")

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _run_hook(self, stdin_data: dict, env_overrides: dict | None = None) -> dict:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["PATH"] = str(self.mock_rtk.parent) + ":" + env.get("PATH", "")
        env["TOKENLESS_AGENT_ID"] = "copilot-shell"
        if env_overrides:
            env.update(env_overrides)
        proc = subprocess.run(
            [sys.executable, str(REWRITE_HOOK)],
            input=json.dumps(stdin_data),
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stdout = proc.stdout.strip()
        if not stdout or stdout == "{}":
            return {}
        return json.loads(stdout)

    def test_cosh_ng_pre_tool_emits_tool_input(self):
        """Cosh-NG PreToolUse output uses tool_input patch field."""
        stdin_data = {
            "tool_name": "Bash",
            "hook_event_name": "PreToolUse",
            "tool_input": {"command": "git status"},
        }
        out = self._run_hook(stdin_data, env_overrides={"COSH_NG_VERSION": "0.6.0"})
        specific = out.get("hookSpecificOutput", {})
        self.assertEqual(specific.get("hookEventName"), "PreToolUse")
        self.assertIn("tool_input", specific)
        self.assertIn("updatedInput", specific)
        self.assertEqual(
            specific["tool_input"]["command"],
            specific["updatedInput"]["command"],
        )
        self.assertTrue(
            specific["tool_input"]["command"].endswith("git status")
        )


class TestOutputFormat(unittest.TestCase):
    """Test that hook output JSON is well-formed and complete."""

    def test_post_tool_output_serializable(self):
        """Cosh-NG PostToolUse output is valid JSON."""
        output = hook_utils.build_cosh_ng_post_tool_output(
            replacement="test",
            additional_context="ctx",
        )
        serialized = json.dumps(output, ensure_ascii=False)
        reparsed = json.loads(serialized)
        self.assertEqual(reparsed["hookSpecificOutput"]["replacement"], "test")

    def test_pre_tool_output_serializable(self):
        """Cosh-NG PreToolUse output is valid JSON."""
        output = hook_utils.build_cosh_ng_pre_tool_output(
            tool_input={"command": "echo hello"},
        )
        serialized = json.dumps(output, ensure_ascii=False)
        reparsed = json.loads(serialized)
        self.assertIn("tool_input", reparsed["hookSpecificOutput"])
        self.assertIn("updatedInput", reparsed["hookSpecificOutput"])

    def test_original_sentinel_absent(self):
        """The original response sentinel must not appear in replacement."""
        original = "original full output with lots of data"
        compressed = "compressed"
        output = hook_utils.build_cosh_ng_post_tool_output(
            replacement=compressed,
        )
        serialized = json.dumps(output)
        self.assertNotIn(original, serialized)
        self.assertIn(compressed, serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
