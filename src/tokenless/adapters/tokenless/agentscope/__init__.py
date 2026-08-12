"""Token-Less middleware for the AgentScope SDK.

Integrates tokenless compression strategies into AgentScope's middleware
onion so that tool calls and their results are optimized before they reach
the model context window:

  1. **Tool Ready** — pre-checks the execution environment and blocks calls
     that cannot succeed, returning "Skip retry" guidance.
  2. **Command rewriting** — for shell-style tools, blocks the original
     command and suggests an RTK-rewritten equivalent.
  3. **Response compression** — runs ``tokenless compress-response`` on
     structured tool results.
  4. **TOON encoding** — re-encodes JSON results to TOON format for extra
     token savings.

Usage:

    from agentscope.tool import Toolkit
    from anolisa_tokenless_agentscope import register

    toolkit = Toolkit()
    register(toolkit)

Every path fails open: if ``tokenless`` or ``rtk`` is unavailable, the
middleware passes requests through unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Any, AsyncGenerator, Callable

# Resolve shared hook utilities (common/hooks/) with FHS fallback paths.
# Primary: relative path — realpath needed because install.sh may symlink
# this package into a Python environment, and plain __file__ points to the
# symlink path; resolving .. from the adapter dir hits common/hooks.
# Fallbacks: system and user FHS paths cover copy-installs and RPM layouts.
#
# Trust model (aligned with bash is_trusted_file / Rust is_trusted_path):
# system FHS paths are unconditional; elsewhere the hooks directory, its
# parent, and hook_utils.py must be owned by the current user or root and
# must not be world-writable.
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
    for prefix in ("/usr/share/", "/usr/local/share/", "/usr/libexec/", "/usr/lib/anolisa/"):
        if real.startswith(prefix):
            return None
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


def _resolve_hook_utils() -> tuple[str, list[str]]:
    """Locate a trusted shared hooks directory and make it importable."""
    try:
        import pwd as _pwd

        real_home = _pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        real_home = ""
    if not os.path.isabs(real_home):
        real_home = ""

    candidates = [
        os.path.join(_HERE, "..", "..", "common", "hooks"),
        "/usr/share/anolisa/adapters/tokenless/common/hooks",
        "/usr/local/share/anolisa/adapters/tokenless/common/hooks",
    ]
    xdg_data = os.environ.get("XDG_DATA_HOME", "")
    if xdg_data and os.path.isabs(xdg_data):
        candidates.append(
            os.path.join(xdg_data, "anolisa", "adapters", "tokenless", "common", "hooks")
        )
    if real_home:
        candidates.append(
            os.path.join(
                real_home, ".local", "share", "anolisa", "adapters", "tokenless", "common", "hooks"
            )
        )

    rejections: list[str] = []
    for candidate in candidates:
        reason = _validate_hooks_dir(candidate)
        if reason is None:
            resolved = os.path.realpath(candidate)
            sys.path.insert(0, resolved)
            return resolved, candidates
        rejections.append(f"  - {candidate}: {reason}")

    raise ImportError(
        "tokenless: no trusted shared hook_utils module (common/hooks/) found.\n"
        "Candidates checked (in order):\n" + "\n".join(rejections) + "\n"
        "A candidate may be rejected by the trust policy (ownership or "
        "permissions) even though the path exists."
    )


_HOOK_UTILS_RESOLVED, _HOOK_UTILS_CANDIDATES = _resolve_hook_utils()

from hook_utils import (  # noqa: E402
    _RTK_FALLBACK,
    _RTK_LOCAL_LIB,
    _RTK_LOCAL_SHARE,
    _TOKENLESS_FALLBACK,
    _TOKENLESS_LOCAL_LIB,
    _TOKENLESS_LOCAL_SHARE,
    get_thresholds,
    parse_version,
    resolve_binary,
    run as _run_sync,
    try_parse_json,
    write_context,
    SKIP_TOOLS as _SKIP_TOOLS_SHARED,
    SHELL_TOOLS as _SHELL_TOOLS_SHARED,
)

logger = logging.getLogger(__name__)

AGENT_ID = "agentscope"
_MIN_RESPONSE_LEN = 200
_MIN_RTK_VERSION = (0, 35, 0)

_SKIP_TOOLS: set[str] = _SKIP_TOOLS_SHARED | {
    "session_search",
    "list_sessions",
}
_SHELL_TOOLS: set[str] = _SHELL_TOOLS_SHARED


_RTK_LIB_FALLBACK = "/usr/lib/anolisa/tokenless/rtk"


def _resolve_binary(name: str, fallback: str) -> str | None:
    """Resolve a binary with AgentScope-specific fallback paths."""
    local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", name)
    if name == "rtk":
        return resolve_binary(
            name, fallback, _RTK_LIB_FALLBACK, local_bin, _RTK_LOCAL_LIB, _RTK_LOCAL_SHARE
        )
    return resolve_binary(name, fallback, local_bin, _TOKENLESS_LOCAL_LIB, _TOKENLESS_LOCAL_SHARE)


def _have(name: str, fallback: str) -> bool:
    return _resolve_binary(name, fallback) is not None


def _tool_name(tool_call: Any) -> str:
    """Extract the tool name from a ToolUseBlock (dict or object)."""
    if tool_call is None:
        return ""
    if isinstance(tool_call, dict):
        return tool_call.get("name", "") or ""
    return getattr(tool_call, "name", "") or ""


def _tool_input(tool_call: Any) -> dict[str, Any]:
    """Extract tool arguments from a ToolUseBlock."""
    if tool_call is None:
        return {}
    if isinstance(tool_call, dict):
        inp = tool_call.get("input", {})
        return inp if isinstance(inp, dict) else {}
    inp = getattr(tool_call, "input", {})
    return inp if isinstance(inp, dict) else {}


def _tool_call_id(tool_call: Any) -> str:
    """Extract the tool call id from a ToolUseBlock."""
    if tool_call is None:
        return ""
    if isinstance(tool_call, dict):
        return (
            tool_call.get("id", "")
            or tool_call.get("tool_call_id", "")
            or tool_call.get("tool_use_id", "")
            or ""
        )
    return (
        getattr(tool_call, "id", "")
        or getattr(tool_call, "tool_call_id", "")
        or getattr(tool_call, "tool_use_id", "")
        or ""
    )


async def _run_async(
    cmd: list[str],
    input_data: str,
    timeout: float = 5.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess | None:
    """Run a subprocess asynchronously, returning None on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input_data.encode()), timeout=timeout
        )
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout.decode(), stderr.decode())
    except Exception as exc:
        logger.debug("tokenless: async run failed for %s: %s", cmd[0] if cmd else "?", exc)
        return None


async def _compress_response(
    tool_name: str,
    result: str,
    session_id: str,
    tool_call_id: str,
) -> str | None:
    """Compress a JSON tool result via ``tokenless compress-response``."""
    tokenless_bin = _resolve_binary("tokenless", _TOKENLESS_FALLBACK)
    if not tokenless_bin:
        return None

    parsed = try_parse_json(result)
    if not isinstance(parsed, (dict, list)):
        return None

    thresholds = get_thresholds(tool_name)
    cmd = [
        tokenless_bin,
        "compress-response",
        "--agent-id",
        AGENT_ID,
        "--truncate-strings-at",
        str(thresholds[0]),
        "--truncate-arrays-at",
        str(thresholds[1]),
        "--max-depth",
        str(thresholds[2]),
    ]
    if session_id:
        cmd.extend(["--session-id", session_id])
    if tool_call_id:
        cmd.extend(["--tool-use-id", tool_call_id])

    proc = await _run_async(cmd, result, timeout=3.0)
    if not proc or proc.returncode != 0 or not proc.stdout.strip():
        return None
    compressed = proc.stdout.strip()
    if len(compressed) >= len(result):
        return None
    return compressed


async def _encode_toon(
    data: str,
    session_id: str = "",
    tool_call_id: str = "",
) -> tuple[str, int] | None:
    """Encode a JSON payload to TOON format."""
    tokenless_bin = _resolve_binary("tokenless", _TOKENLESS_FALLBACK)
    if not tokenless_bin:
        return None

    parsed = try_parse_json(data)
    if not isinstance(parsed, (dict, list)):
        return None

    cmd = [tokenless_bin, "compress-toon", "--agent-id", AGENT_ID]
    if session_id:
        cmd.extend(["--session-id", session_id])
    if tool_call_id:
        cmd.extend(["--tool-use-id", tool_call_id])

    proc = await _run_async(cmd, data, timeout=1.0)
    if not proc or proc.returncode != 0 or not proc.stdout.strip():
        return None
    toon_text = proc.stdout.strip()
    if len(toon_text) >= len(data):
        return None
    savings_pct = (len(data) - len(toon_text)) * 100 // len(data) if data else 0
    return toon_text, savings_pct


async def _env_check(tool_name: str) -> str | None:
    """Run tool-ready env-check and return feedback if the tool is not ready."""
    tokenless_bin = _resolve_binary("tokenless", _TOKENLESS_FALLBACK)
    if not tokenless_bin:
        return None

    proc = await _run_async(
        [tokenless_bin, "env-check", "--tool", tool_name, "--json"], "", timeout=5.0
    )
    if not proc or not proc.stdout.strip():
        return None

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    status = parsed.get("status", "UNKNOWN")
    if status in ("UNKNOWN", "READY"):
        return None

    proc = await _run_async(
        [tokenless_bin, "env-check", "--tool", tool_name, "--fix", "--json"], "", timeout=10.0
    )
    if not proc or not proc.stdout.strip():
        return _not_ready_msg(tool_name)

    try:
        fix_parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _not_ready_msg(tool_name)

    if fix_parsed.get("status") == "READY":
        return None

    return fix_parsed.get("diagnostic") or _not_ready_msg(tool_name)


def _not_ready_msg(tool_name: str) -> str:
    return f"[tokenless:ready] {tool_name}: NOT_READY. Skip retry."


async def _try_rewrite(
    args: dict[str, Any],
    session_id: str,
    tool_call_id: str,
) -> dict[str, str] | None:
    """Attempt RTK command rewrite for shell tool calls.

    Calls ``rtk rewrite <command>`` — a pure text substitution that never
    executes the command. On success, returns a block directive suggesting
    the rewritten command.
    """
    rtk_bin = _resolve_binary("rtk", _RTK_FALLBACK)
    if not rtk_bin:
        return None

    command = args.get("command", "")
    if not command:
        return None

    try:
        ver_proc = await _run_async([rtk_bin, "--version"], "", timeout=3.0)
        ver = parse_version(ver_proc.stdout) if ver_proc else None
        if ver and ver < _MIN_RTK_VERSION:
            logger.warning(
                "tokenless: rtk %s too old (need >= 0.35.0), rewrite skipped",
                ver_proc.stdout.strip() if ver_proc else "",
            )
            return None
    except Exception:
        pass

    write_context(AGENT_ID, session_id, tool_call_id)

    env = os.environ.copy()
    env["TOKENLESS_AGENT_ID"] = AGENT_ID
    if session_id:
        env["TOKENLESS_SESSION_ID"] = session_id
    if tool_call_id:
        env["TOKENLESS_TOOL_USE_ID"] = tool_call_id

    proc = await _run_async([rtk_bin, "rewrite", command], "", timeout=5.0, env=env)
    if not proc:
        return None

    # Exit code protocol (from rtk rewrite_cmd.rs):
    #   0 = rewrite available, Allow verdict
    #   1 = no RTK equivalent (passthrough)
    #   2 = deny rule matched (let AgentScope handle)
    #   3 = Ask/Default verdict (rewrite available but permission model requires
    #       user confirmation; in a non-interactive hook context treat as valid)
    if proc.returncode in (1, 2):
        return None
    if proc.returncode not in (0, 3):
        return None

    rewritten = proc.stdout.strip()
    if not rewritten or rewritten == command:
        return None

    logger.info("tokenless: rtk rewrite %s -> %s", command, rewritten)
    return {
        "action": "block",
        "message": f"[tokenless:rewrite] Re-execute as: {rewritten}",
    }


def _block_response(message: str) -> Any:
    """Build a ToolResponse that blocks the current tool call."""
    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse

    return ToolResponse(
        content=[TextBlock(type="text", text=message)],
        is_last=True,
    )


def _extract_text(response: Any) -> str | None:
    """Extract the first text payload from a ToolResponse."""
    if response is None:
        return None
    content = getattr(response, "content", None)
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                return block.get("text", "")
        elif getattr(block, "type", None) == "text":
            return getattr(block, "text", "")
    return None


def _replace_text(response: Any, new_text: str) -> Any:
    """Return a ToolResponse with the first text block replaced by ``new_text``."""
    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse

    content = getattr(response, "content", []) or []
    new_content: list[Any] = []
    replaced = False
    for block in content:
        if not replaced:
            block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if block_type == "text":
                new_content.append(TextBlock(type="text", text=new_text))
                replaced = True
                continue
        new_content.append(block)
    if not replaced:
        new_content = [TextBlock(type="text", text=new_text)]

    return ToolResponse(
        content=new_content,
        id=getattr(response, "id", None),
        metadata=getattr(response, "metadata", None),
        is_last=getattr(response, "is_last", True),
        stream=getattr(response, "stream", False),
    )


async def tokenless_middleware(
    kwargs: dict,
    next_handler: Callable,
) -> AsyncGenerator[Any, None]:
    """AgentScope middleware entry point.

    Applies Tool Ready env-check, RTK command rewriting, response compression,
    and TOON encoding around tool execution.
    """
    tool_call = kwargs.get("tool_call")
    tool_name = _tool_name(tool_call)
    args = _tool_input(tool_call)
    session_id = kwargs.get("session_id", "") or ""
    tool_use_id = kwargs.get("tool_use_id", "") or kwargs.get("tool_call_id", "") or _tool_call_id(tool_call)

    if session_id:
        os.environ["TOKENLESS_SESSION_ID"] = str(session_id)

    # Step 1: Tool Ready env-check
    if _have("tokenless", _TOKENLESS_FALLBACK):
        feedback = await _env_check(tool_name)
        if feedback:
            logger.info("tokenless: tool-ready blocking %s", tool_name)
            yield _block_response(feedback)
            return

    # Step 2: RTK command rewrite (shell tools only)
    if tool_name in _SHELL_TOOLS and _have("rtk", _RTK_FALLBACK):
        rewrite = await _try_rewrite(args, str(session_id), str(tool_use_id))
        if rewrite:
            yield _block_response(rewrite["message"])
            return

    # Step 3: Execute the tool and transform results
    async for response in await next_handler(**kwargs):
        transformed = await _transform_response(response, tool_name, str(session_id), str(tool_use_id))
        yield transformed


async def _transform_response(
    response: Any,
    tool_name: str,
    session_id: str,
    tool_use_id: str,
) -> Any:
    """Apply response compression + TOON encoding to a single ToolResponse."""
    if not _have("tokenless", _TOKENLESS_FALLBACK):
        return response

    if tool_name in _SKIP_TOOLS:
        return response

    # Skip streaming intermediate chunks; only transform final responses.
    if getattr(response, "stream", False) and not getattr(response, "is_last", True):
        return response

    original = _extract_text(response)
    if not original or original in ("{}", "[]"):
        return response

    if len(original) < _MIN_RESPONSE_LEN:
        return response

    parsed = try_parse_json(original)
    if parsed is None:
        return response

    compressed = await _compress_response(tool_name, original, session_id, tool_use_id)
    current = compressed if compressed else original

    toon_result = await _encode_toon(current, session_id, tool_use_id)

    if compressed is None and toon_result is None:
        return response

    if toon_result:
        final_text, savings_pct = toon_result
        label = "response compressed + TOON encoded" if compressed else "TOON encoded"
    else:
        final_text = current
        savings_pct = (len(original) - len(final_text)) * 100 // len(original) if original else 0
        label = "response compressed"

    logger.info(
        "tokenless: %s %s: %d -> %d chars (%d%% reduction)",
        label,
        tool_name,
        len(original),
        len(final_text),
        savings_pct,
    )
    return _replace_text(response, final_text)


def register(toolkit: Any) -> None:
    """Register tokenless middleware with an AgentScope Toolkit."""
    toolkit.register_middleware(tokenless_middleware)

    features: list[str] = []
    if _have("tokenless", _TOKENLESS_FALLBACK):
        features.extend(["response-compression", "toon-encoding", "tool-ready"])
    if _have("rtk", _RTK_FALLBACK):
        features.append("rtk-rewrite")

    logger.info(
        "tokenless: AgentScope middleware registered — active features: %s",
        ", ".join(features) if features else "none (install tokenless/rtk binary)",
    )
