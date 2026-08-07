"""Token-Less Plugin for Hermes Agent.

Combines multiple context-compression strategies into a single plugin:

  1. **Response compression** — ``transform_tool_result`` : compresses tool
     results via ``tokenless compress-response`` with per-layer thresholds
     (moderate for shell, zero-truncation for API tools), stripping debug
     fields, nulls, empty values.
  2. **TOON encoding** — ``transform_tool_result`` : pipeline step after
     response compression; re-encodes JSON results to TOON format via
     ``tokenless compress-toon`` for additional token savings (15-40%)
     with proper stats recording and size check.
  3. **Tool Ready** — ``pre_tool_call`` : environment readiness pre-check
     with auto-fix and skip-retry feedback for missing dependencies.
  4. **Command rewriting** — ``pre_tool_call`` : blocks shell commands
     and suggests RTK-rewritten equivalents.  Hermes's hook cannot modify
     arguments, so the agent must re-execute with the suggested command
     (one extra round-trip).  Safe: ``rtk rewrite`` only does text
     substitution, never executes the command.
  5. **Session tracking** — ``on_session_start`` : propagates agent/session
     IDs to tokenless stats recording.

Not available in Hermes: schema compression (Hermes hooks do not expose
tool schemas).

Every hook degrades gracefully: if ``tokenless`` is not installed, all
hooks are silently skipped.

Activation is controlled by the Hermes plugin system — list ``tokenless`` in
``plugins.enabled`` in ``config.yaml``, or enable via
``hermes plugins enable tokenless``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any

# Resolve shared hook utilities (common/hooks/) with FHS fallback paths.
# Primary: relative path — realpath needed because install.sh symlinks
# __init__.py into ~/.hermes/plugins/, and plain __file__ points to the
# symlink path; resolving .. from the adapter dir hits common/hooks.
# Fallbacks: system and user FHS paths — needed when the plugin bundle is
# *copied* into ~/.hermes/plugins/tokenless/ (e.g. by the anolisa driver)
# instead of symlinked, so the relative path resolves nowhere.  User-scope
# candidates honor XDG_DATA_HOME (anolisa FsLayout::user prefers it over
# ~/.local/share).
#
# Trust model (aligned with codex/scripts/rewrite-hook, bash
# is_trusted_file, and Rust is_trusted_path): system FHS paths are
# unconditional; elsewhere the hooks directory, its parent, and the
# hook_utils.py file itself must be owned by the current user or root and
# must not be world-writable.  A candidate that exists but is rejected or
# incomplete does not stop the search — later candidates are still tried,
# and every rejection reason is kept for the final diagnostic.
_HERE = os.path.dirname(os.path.realpath(__file__))


def _validate_hooks_dir(path: str) -> str | None:
    """Validate a candidate hooks directory for importing hook_utils.

    Returns None when the directory is trusted and contains an importable
    hook_utils.py, otherwise a human-readable rejection reason.
    """
    if not path or not os.path.isabs(path):
        return "not an absolute path"
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        return "directory does not exist"
    module = os.path.join(real, "hook_utils.py")
    if not os.path.isfile(module):
        return "hook_utils.py missing (incomplete or residual install)"
    # System FHS prefixes are always trusted (checked on the realpath so a
    # symlink pointing outside a system prefix cannot bypass the check).
    for prefix in ("/usr/share/", "/usr/local/share/", "/usr/libexec/", "/usr/lib/anolisa/"):
        if real.startswith(prefix):
            return None
    # Outside system prefixes: the hooks dir, its parent, and the module
    # file must be owned by the current uid or root and not world-writable
    # (mirrors bash is_trusted_file / Rust is_trusted_path).
    uid = os.getuid()
    for p in (real, os.path.dirname(real), module):
        try:
            st = os.stat(p)
        except OSError as exc:
            return f"stat failed for {p}: {exc}"
        if st.st_uid != uid and st.st_uid != 0:
            return f"{p} not owned by current user or root (uid {st.st_uid})"
        if st.st_mode & 0o002:
            return f"{p} is world-writable"
    return None


# Symbols that must exist in the shared hook_utils module.  When a candidate
# passes the trust check but ships an older hook_utils.py that lacks these
# symbols (e.g. a stale install from a previous adapter version), the
# candidate is rejected and the search continues to later paths.
_HOOK_UTILS_REQUIRED_SYMBOLS = ("_anchor_rtk_prefix",)


def _check_api_compat(candidate_dir: str) -> str | None:
    """Trial-import hook_utils from *candidate_dir* and verify required symbols.

    Returns ``None`` when the module loads and exposes every symbol in
    :data:`_HOOK_UTILS_REQUIRED_SYMBOLS`, otherwise a human-readable
    rejection reason.  On rejection the ``sys.path`` mutation and the
    partially-imported module are cleaned up so later candidates start
    from a clean state.
    """
    sys.path.insert(0, candidate_dir)
    saved = sys.modules.pop("hook_utils", None)
    try:
        import hook_utils as _trial  # type: ignore[import-not-found]
        missing = [s for s in _HOOK_UTILS_REQUIRED_SYMBOLS
                   if not hasattr(_trial, s)]
        if missing:
            return f"API mismatch: missing {', '.join(missing)}"
        return None
    except Exception as exc:
        return f"import failed: {exc}"
    finally:
        sys.path.pop(0)
        if saved is not None:
            sys.modules["hook_utils"] = saved
        else:
            sys.modules.pop("hook_utils", None)


def _resolve_hook_utils() -> tuple[str, list[str]]:
    """Locate a trusted shared hooks directory and make it importable.

    Returns ``(resolved_path, candidate_list)``.  The resolved path is
    inserted at the front of ``sys.path`` so the shared ``hook_utils``
    module can be imported.  Raises :exc:`ImportError` when no candidate
    passes both the trust policy and the API compatibility check.
    """
    # Resolve real home from passwd DB for user-install fallback path
    # (NOT $HOME — env-controllable).
    try:
        import pwd as _pwd
        real_home = _pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        real_home = ""
    if not os.path.isabs(real_home):
        real_home = ""

    candidates = [
        os.path.join(_HERE, "..", "common", "hooks"),                          # source-tree / symlink install
        "/usr/share/anolisa/adapters/tokenless/common/hooks",                  # RPM system
        "/usr/local/share/anolisa/adapters/tokenless/common/hooks",            # manual system
    ]
    # XDG user data dir first (anolisa FsLayout::user precedence), then the
    # passwd-home default. XDG_DATA_HOME is env-controllable, but candidates
    # still pass the full ownership/permission validation above.
    xdg_data = os.environ.get("XDG_DATA_HOME", "")
    if xdg_data and os.path.isabs(xdg_data):
        candidates.append(
            os.path.join(xdg_data, "anolisa", "adapters", "tokenless", "common", "hooks"))
    if real_home:
        candidates.append(
            os.path.join(real_home, ".local", "share",
                         "anolisa", "adapters", "tokenless", "common", "hooks"))

    rejections: list[str] = []
    for candidate in candidates:
        reason = _validate_hooks_dir(candidate)
        if reason is not None:
            rejections.append(f"  - {candidate}: {reason}")
            continue
        # Trust check passed — verify API compat (version mismatch guard).
        resolved = os.path.realpath(candidate)
        api_reason = _check_api_compat(resolved)
        if api_reason is not None:
            rejections.append(f"  - {candidate}: {api_reason}")
            continue
        sys.path.insert(0, resolved)
        return resolved, candidates

    raise ImportError(
        "tokenless: no trusted shared hook_utils module (common/hooks/) found.\n"
        "Candidates checked (in order):\n" + "\n".join(rejections) + "\n"
        "Note: a candidate may be rejected by the trust policy (ownership or "
        "permissions) even though the path exists — see the reason next to "
        "each path. Install the tokenless common hooks (anolisa install "
        "tokenless) or re-run adapters/tokenless/hermes/scripts/install.sh "
        "from a complete adapter tree."
    )


try:
    _HOOK_UTILS_RESOLVED, _HOOK_UTILS_CANDIDATES = _resolve_hook_utils()

    from hook_utils import (
        _TOKENLESS_FALLBACK,
        _TOKENLESS_LOCAL_SHARE,
        _TOKENLESS_LOCAL_LIB,
        _RTK_FALLBACK,
        _RTK_LOCAL_SHARE,
        _RTK_LOCAL_LIB,
        _anchor_rtk_prefix,
        resolve_binary,
        warn as _warn_shared,
        try_parse_json as _try_parse_json,
        is_skill_file as _is_skill_file,
        write_context as _write_context,
        run as _run,
        parse_version as _parse_version,
        SKIP_TOOLS as _SKIP_TOOLS_SHARED,
        SHELL_TOOLS as _SHELL_TOOLS_SHARED,
        get_thresholds,
    )
    _HOOK_UTILS_AVAILABLE = True
except ImportError:
    _HOOK_UTILS_AVAILABLE = False
    _HOOK_UTILS_RESOLVED = ""
    _HOOK_UTILS_CANDIDATES = []

    _TOKENLESS_FALLBACK = "/usr/libexec/anolisa/tokenless/tokenless"
    _TOKENLESS_LOCAL_SHARE = ""
    _TOKENLESS_LOCAL_LIB = ""
    _RTK_FALLBACK = "/usr/libexec/anolisa/tokenless/rtk"
    _RTK_LOCAL_SHARE = ""
    _RTK_LOCAL_LIB = ""

    # Minimal fallbacks — the plugin gracefully skips features that
    # require shared utilities (compression, TOON, env-check, skill-file
    # detection).  RTK rewrite still works because _anchor_rtk_prefix and
    # _parse_version are provided locally below.

    def resolve_binary(name: str, *fallbacks: str) -> str | None:  # type: ignore[misc]
        import shutil
        found = shutil.which(name)
        if found:
            return found
        for fb in fallbacks:
            if fb and os.path.isfile(fb) and os.access(fb, os.X_OK):
                return fb
        return None

    def _warn_shared(msg: str) -> None:  # type: ignore[misc]
        pass

    def _try_parse_json(text: str):  # type: ignore[misc]
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None

    def _is_skill_file(text: str) -> bool:  # type: ignore[misc]
        return False

    def _write_context(agent_id: str, session_id: str, tool_call_id: str) -> None:  # type: ignore[misc]
        pass

    def _run(cmd: list[str], stdin_data: str, timeout: int = 5):  # type: ignore[misc]
        try:
            return subprocess.run(
                cmd, input=stdin_data, capture_output=True, text=True, timeout=timeout,
            )
        except Exception:
            return None

    def get_thresholds(tool_name: str) -> tuple[int, int, int]:  # type: ignore[misc]
        return (65536, 128, 8)

    _SKIP_TOOLS_SHARED: set[str] = {
        "Read", "Glob", "Grep", "WebFetch", "WebSearch",
        "read_file", "list_files", "search_files",
    }
    _SHELL_TOOLS_SHARED: set[str] = {"Bash", "Shell", "terminal"}

    import re as _re

    def _parse_version(version_str: str) -> tuple[int, ...] | None:  # type: ignore[misc]
        m = _re.match(r"(\d+)\.(\d+)\.(\d+)", version_str)
        if not m:
            return None
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    import shlex as _shlex

    _SEGMENT_OPS_FB = frozenset({"&&", "||", ";", "|", "&"})

    def _is_env_assignment_fb(token: str) -> bool:
        name, sep, _ = token.partition("=")
        if not sep or not name:
            return False
        if not (name[0].isalpha() or name[0] == "_"):
            return False
        return all(c.isalnum() or c == "_" for c in name)

    def _anchor_rtk_prefix(rewritten: str, rtk_bin: str) -> str:  # type: ignore[misc]
        """Local fallback when shared hook_utils is unavailable or too old."""
        lexer = _shlex.shlex(rewritten, posix=False)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            tokens = list(lexer)
        except ValueError:
            return rewritten
        quoted = _shlex.quote(rtk_bin)
        result = list(tokens)
        wrapped = False
        for i, token in enumerate(tokens):
            if token in _SEGMENT_OPS_FB:
                wrapped = False
                continue
            if _is_env_assignment_fb(token):
                continue
            if not wrapped and token == "rtk":
                result[i] = quoted
                wrapped = True
        return " ".join(result)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "hermes-agent"
_MIN_RESPONSE_LEN = 200

_SKIP_TOOLS: set[str] = _SKIP_TOOLS_SHARED | {
    "session_search", "list_sessions",
}

# Use shared SHELL_TOOLS directly - all tools (including "terminal") are now
# defined in the unified tool_categories.json
_SHELL_TOOLS: set[str] = _SHELL_TOOLS_SHARED

_MIN_RTK_VERSION = (0, 35, 0)

# ---------------------------------------------------------------------------
# Binary resolution (thin wrapper over shared cached resolve_binary)
# ---------------------------------------------------------------------------

# Hermes-specific fallback paths for the RTK binary.
_RTK_LIB_FALLBACK = "/usr/lib/anolisa/tokenless/rtk"


def _resolve_binary(name: str, fallback: str) -> str | None:
    """Resolve binary with hermes-specific fallback paths (cached via shared)."""
    local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", name)
    if name == "rtk":
        return resolve_binary(name, fallback, _RTK_LIB_FALLBACK, local_bin, _RTK_LOCAL_LIB, _RTK_LOCAL_SHARE)
    return resolve_binary(name, fallback, local_bin, _TOKENLESS_LOCAL_LIB, _TOKENLESS_LOCAL_SHARE)


def _have(name: str, fallback: str) -> bool:
    return _resolve_binary(name, fallback) is not None


# ---------------------------------------------------------------------------
# Helpers (shared via hook_utils)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 1. Response Compression (via tokenless compress-response)
# ---------------------------------------------------------------------------


def _compress_response(
    tool_name: str,
    result: str,
    session_id: str,
    tool_call_id: str,
) -> str | None:
    tokenless_bin = _resolve_binary("tokenless", _TOKENLESS_FALLBACK)
    if not tokenless_bin:
        return None

    parsed = _try_parse_json(result)
    if not isinstance(parsed, (dict, list)):
        return None

    # 3-layer dispatch: shell tools use moderate truncation, API tools use zero-truncation
    thresholds = get_thresholds(tool_name)

    cmd = [
        tokenless_bin, "compress-response",
        "--agent-id", AGENT_ID,
        "--truncate-strings-at", str(thresholds[0]),
        "--truncate-arrays-at", str(thresholds[1]),
        "--max-depth", str(thresholds[2]),
    ]
    if session_id:
        cmd.extend(["--session-id", session_id])
    if tool_call_id:
        cmd.extend(["--tool-use-id", tool_call_id])

    proc = _run(cmd, result)
    if not proc or proc.returncode != 0 or not proc.stdout.strip():
        return None

    compressed = proc.stdout.strip()
    if len(compressed) >= len(result):
        return None
    return compressed


# ---------------------------------------------------------------------------
# 2. TOON Encoding (via tokenless compress-toon)
# ---------------------------------------------------------------------------


def _encode_toon(data: str, session_id: str = "", tool_call_id: str = "") -> tuple[str, int] | None:
    tokenless_bin = _resolve_binary("tokenless", _TOKENLESS_FALLBACK)
    if not tokenless_bin:
        return None

    parsed = _try_parse_json(data)
    if not isinstance(parsed, (dict, list)):
        return None

    cmd = [tokenless_bin, "compress-toon", "--agent-id", AGENT_ID]
    if session_id:
        cmd.extend(["--session-id", session_id])
    if tool_call_id:
        cmd.extend(["--tool-use-id", tool_call_id])

    proc = _run(cmd, data, timeout=1)
    if not proc or proc.returncode != 0 or not proc.stdout.strip():
        return None

    toon_text = proc.stdout.strip()
    if len(toon_text) >= len(data):
        return None

    savings_pct = 0
    if len(data) > 0:
        savings_pct = (len(data) - len(toon_text)) * 100 // len(data)

    return toon_text, savings_pct


# ---------------------------------------------------------------------------
# 3. Tool Ready (via tokenless env-check)
# ---------------------------------------------------------------------------


def _env_check(tool_name: str) -> str | None:
    """Run tool-ready env-check and return feedback if tool is not ready."""
    tokenless_bin = _resolve_binary("tokenless", _TOKENLESS_FALLBACK)
    if not tokenless_bin:
        return None

    proc = _run([tokenless_bin, "env-check", "--tool", tool_name, "--json"], "", timeout=5)
    if not proc or not proc.stdout.strip():
        return None

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    status = parsed.get("status", "UNKNOWN")
    if status in ("UNKNOWN", "READY"):
        return None

    # Attempt auto-fix
    proc = _run([tokenless_bin, "env-check", "--tool", tool_name, "--fix", "--json"], "", timeout=10)
    if not proc or not proc.stdout.strip():
        return _not_ready_msg(tool_name)

    try:
        fix_parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _not_ready_msg(tool_name)

    if fix_parsed.get("status") == "READY":
        return None

    diagnostic = fix_parsed.get("diagnostic", "")
    return diagnostic or _not_ready_msg(tool_name)


def _not_ready_msg(tool_name: str) -> str:
    return f"[tokenless:ready] {tool_name}: NOT_READY. Skip retry."


# ---------------------------------------------------------------------------
# 4. Command Rewriting (via rtk rewrite)
# ---------------------------------------------------------------------------


def _try_rewrite(
    args: Any,
    session_id: str,
    tool_call_id: str,
) -> dict[str, str] | None:
    """Attempt RTK command rewrite for terminal tool calls.

    Calls ``rtk rewrite <command>`` — a pure text substitution that never
    executes the command.  On success, returns a block directive suggesting
    the rewritten command so the agent re-executes with the optimized version.
    """
    rtk_bin = _resolve_binary("rtk", _RTK_FALLBACK)
    if not rtk_bin:
        return None

    if not isinstance(args, dict):
        return None

    command = args.get("command", "")
    if not command:
        return None

    # Version guard — non-fatal
    try:
        ver_proc = subprocess.run(
            [rtk_bin, "--version"], capture_output=True, text=True, timeout=3,
        )
        ver = _parse_version(ver_proc.stdout)
        if ver and ver < _MIN_RTK_VERSION:
            logger.warning("tokenless: rtk %s too old (need >= 0.35.0), rewrite skipped", ver_proc.stdout.strip())
            return None
    except Exception:
        pass

    # Write context file so rtk (running as proxy later) can recover IDs
    _write_context(AGENT_ID, session_id, tool_call_id)

    # Set env vars for rtk stats context
    env = os.environ.copy()
    env["TOKENLESS_AGENT_ID"] = AGENT_ID
    if session_id:
        env["TOKENLESS_SESSION_ID"] = session_id
    if tool_call_id:
        env["TOKENLESS_TOOL_USE_ID"] = tool_call_id

    proc = subprocess.run(
        [rtk_bin, "rewrite", command],
        capture_output=True, text=True, timeout=5, env=env,
    )

    # Exit code protocol (from rtk rewrite_cmd.rs):
    #   0 = rewrite available, Allow verdict (auto-allow by permission rule)
    #   1 = no RTK equivalent (passthrough)
    #   2 = deny rule matched (let Hermes handle)
    #   3 = Ask/Default verdict (rewrite available but permission model requires
    #       user confirmation; in non-interactive hook context, treat as valid
    #       rewrite since the intent is token optimization, not permission gating)
    if proc.returncode == 1 or proc.returncode == 2:
        return None
    if proc.returncode != 0 and proc.returncode != 3:
        return None

    rewritten = proc.stdout.strip()
    if not rewritten or rewritten == command:
        return None

    rewritten = _anchor_rtk_prefix(rewritten, rtk_bin)

    logger.info("tokenless: rtk rewrite %s → %s", command, rewritten)
    return {
        "action": "block",
        "message": f"[tokenless:rewrite] Re-execute as: {rewritten}",
    }


# ---------------------------------------------------------------------------
# Hook callbacks
# ---------------------------------------------------------------------------


def on_session_start(**kwargs: Any) -> None:
    """Record session mapping for stats context."""
    session_id = kwargs.get("session_id", "")
    if session_id:
        os.environ["TOKENLESS_SESSION_ID"] = str(session_id)
        logger.debug("tokenless: session_start session_id=%s", session_id)


def on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **kwargs: Any,
) -> dict[str, str] | None:
    """Tool Ready + RTK rewrite pre-check.

    Step 1: env-check blocks when the tool's environment is not ready.
    Step 2: for ``terminal`` calls, blocks and suggests RTK-rewritten
    command (one extra round-trip; safe — rtk rewrite never executes).
    """
    # Step 1: env-check (all tools, needs tokenless + shared hook_utils).
    # Skipped in degraded mode — _env_check depends on shared get_thresholds
    # and tokenless compress-response which require a compatible hook_utils.
    if _HOOK_UTILS_AVAILABLE and _have("tokenless", _TOKENLESS_FALLBACK):
        if session_id:
            os.environ["TOKENLESS_SESSION_ID"] = str(session_id)
        feedback = _env_check(tool_name)
        if feedback:
            logger.info("tokenless: tool-ready blocking %s — %s", tool_name, feedback)
            return {"action": "block", "message": feedback}

    # Step 2: RTK rewrite (terminal only, needs rtk)
    if tool_name in _SHELL_TOOLS and _have("rtk", _RTK_FALLBACK):
        result = _try_rewrite(args, str(session_id), str(tool_call_id))
        if result:
            return result

    return None


def on_transform_tool_result(
    tool_name: str = "",
    args: Any = None,
    result: str = "",
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    duration_ms: int = 0,
    **kwargs: Any,
) -> str | None:
    """Response compression + TOON encoding pipeline.

    Replaces the tool result string with a compressed/TOON-encoded version.
    Runs after post_tool_call; first valid string return wins.

    Content retrieval tools (Read/Glob/Grep) are skipped entirely.
    Shell/exec tools (Bash/Shell) use moderate truncation (64K/128/8).
    All other tools use zero-truncation compress-response + TOON.
    """
    if not _HOOK_UTILS_AVAILABLE:
        return None
    if not _have("tokenless", _TOKENLESS_FALLBACK):
        return None

    # Content retrieval — skip entirely (preserve integrity)
    if tool_name in _SKIP_TOOLS:
        return None

    if not result or result in ("{}", "[]"):
        return None

    # Skip skill files (YAML frontmatter)
    if _is_skill_file(result):
        return None

    # Skip small responses
    if len(result) < _MIN_RESPONSE_LEN:
        return None

    # Validate it's JSON
    parsed = _try_parse_json(result)
    if parsed is None:
        return None

    # Normalize: result is already a JSON string (Hermes tool contract)
    original = result
    original_len = len(original)

    # Step 1: Response compression (per-layer thresholds via get_thresholds)
    compressed = _compress_response(tool_name, result,
                                     str(session_id), str(tool_call_id))
    current = compressed if compressed else result

    # Step 2: TOON encoding
    toon_result = _encode_toon(current, str(session_id), str(tool_call_id))
    used_compression = compressed is not None
    used_toon = toon_result is not None

    if not used_compression and not used_toon:
        return None

    # Build final output
    if used_toon:
        toon_text, savings_pct = toon_result
        final = toon_text
        final_len = len(toon_text)
        savings_label = (
            "response compressed + TOON encoded"
            if used_compression
            else "TOON encoded"
        )
    else:
        final = current  # type: ignore[assignment]
        final_len = len(final)
        savings_pct = (original_len - final_len) * 100 // original_len if original_len else 0
        savings_label = "response compressed"

    logger.info(
        "tokenless: %s %s: %d -> %d chars (%d%% reduction)",
        savings_label, tool_name, original_len, final_len, savings_pct,
    )

    return final


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Register all tokenless hooks with the Hermes plugin system."""

    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("transform_tool_result", on_transform_tool_result)

    # Log what's active
    features: list[str] = []
    if _HOOK_UTILS_AVAILABLE and _have("tokenless", _TOKENLESS_FALLBACK):
        features.append("response-compression")
        features.append("toon-encoding")
        features.append("tool-ready")
    if _have("rtk", _RTK_FALLBACK):
        features.append("rtk-rewrite")

    logger.info(
        "tokenless: Hermes plugin registered — active features: %s",
        ", ".join(features) if features else "none (install tokenless/rtk binary)",
    )
    if not _HOOK_UTILS_AVAILABLE:
        logger.warning(
            "tokenless: running in degraded mode — shared hook_utils not available, "
            "response compression and TOON encoding disabled"
        )
