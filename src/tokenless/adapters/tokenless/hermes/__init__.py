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
    rejection reason.  On success the freshly imported module is kept in
    ``sys.modules`` so the subsequent ``from hook_utils import …`` reuses
    it rather than a stale cached copy.  On rejection the ``sys.path``
    mutation is cleaned up and the previously cached module (if any) is
    restored so later candidates start from a clean state.
    """
    sys.path.insert(0, candidate_dir)
    saved = sys.modules.pop("hook_utils", None)
    try:
        import hook_utils as _trial  # type: ignore[import-not-found]
        missing = [s for s in _HOOK_UTILS_REQUIRED_SYMBOLS
                   if not hasattr(_trial, s)]
        if missing:
            if saved is not None:
                sys.modules["hook_utils"] = saved
            else:
                sys.modules.pop("hook_utils", None)
            return f"API mismatch: missing {', '.join(missing)}"
        # Success — keep the freshly imported module in sys.modules.
        return None
    except Exception as exc:
        if saved is not None:
            sys.modules["hook_utils"] = saved
        else:
            sys.modules.pop("hook_utils", None)
        return f"import failed: {exc}"
    finally:
        sys.path.pop(0)


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

    # Mirror the shared hook_utils constants exactly (KEEP IN SYNC with
    # common/hooks/hook_utils.py): same system fallbacks and same legacy
    # user-install layouts, so degraded mode resolves identical paths.
    _USER_HOME = os.path.expanduser("~")
    if not _USER_HOME or not os.path.isabs(_USER_HOME):
        _USER_HOME = ""

    def _user_path(*parts: str) -> str:
        return os.path.join(_USER_HOME, *parts) if _USER_HOME else ""

    _TOKENLESS_FALLBACK = "/usr/bin/tokenless"
    _TOKENLESS_LOCAL_SHARE = _user_path(
        ".local", "share", "anolisa", "tokenless", "tokenless",
    )
    _TOKENLESS_LOCAL_LIB = _user_path(
        ".local", "lib", "anolisa", "tokenless", "tokenless",
    )
    _RTK_FALLBACK = "/usr/libexec/anolisa/tokenless/rtk"
    _RTK_LOCAL_SHARE = _user_path(
        ".local", "share", "anolisa", "tokenless", "rtk",
    )
    _RTK_LOCAL_LIB = _user_path(".local", "lib", "anolisa", "tokenless", "rtk")

    # Minimal fallbacks — the plugin gracefully skips features that
    # require shared utilities (compression, TOON, env-check, skill-file
    # detection).  RTK rewrite still works because _anchor_rtk_prefix and
    # _parse_version are provided locally below.

    def resolve_binary(name: str, *fallbacks: str) -> str | None:  # type: ignore[misc]
        import shutil
        found = shutil.which(name)
        if found:
            return found
        # Mirror shared hook_utils._known_binary_paths (KEEP IN SYNC) —
        # canonical order: user, /usr/local, /usr, then legacy.  Home is
        # resolved per call, matching the shared resolver.
        home = os.path.expanduser("~")
        user_home = home if home and os.path.isabs(home) else None
        known: list[str] = []
        if user_home:
            known.extend((
                os.path.join(user_home, ".local", "bin", name),
                # Anolisa CLI user mode.
                os.path.join(user_home, ".local", "lib", "anolisa",
                             "libexec", "tokenless", name),
                # Makefile user mode.
                os.path.join(user_home, ".local", "libexec", "anolisa",
                             "tokenless", name),
            ))
        known.extend((
            os.path.join("/usr/local/bin", name),
            # Anolisa CLI system mode.
            os.path.join("/usr/local/libexec/anolisa/tokenless", name),
            os.path.join("/usr/bin", name),
            # Makefile system mode and RPM.
            os.path.join("/usr/libexec/anolisa/tokenless", name),
            # Debian and pre-layout-migration compatibility.
            os.path.join("/usr/lib/anolisa/tokenless", name),
        ))
        if user_home:
            known.extend((
                # Legacy share/lib installs (kept until old installs age out).
                os.path.join(user_home, ".local", "share", "anolisa",
                             "tokenless", name),
                os.path.join(user_home, ".local", "lib", "anolisa",
                             "tokenless", name),
            ))
        for path in (*known, *fallbacks):
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return path
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
        m = _re.search(r"(\d+)\.(\d+)\.(\d+)", version_str)
        if not m:
            return None
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    import shlex as _shlex

    # Word-span lexer inputs (KEEP IN SYNC with shared hook_utils).
    _SEGMENT_META_CHARS_FB = frozenset(";|&")
    _SEGMENT_BOUNDARY_CHARS_FB = frozenset(";|&\n")
    _WORD_BREAK_CHARS_FB = " \t\n\r;|&"

    # Transparent wrappers that delegate to the real command (KEEP IN SYNC).
    _TRANSPARENT_WRAPPERS_FB = frozenset((
        "sudo", "doas", "pkexec",
        "env", "nice", "nohup", "stdbuf", "time", "timeout",
    ))

    # RTK's built-in transparent prefixes, rtk >= 0.43 contract (KEEP IN
    # SYNC with shared hook_utils._RTK_BUILTIN_TRANSPARENT_PREFIXES).
    _RTK_BUILTIN_TRANSPARENT_PREFIXES_FB = (
        "uv run",
        "noglob", "command", "builtin", "exec", "nocorrect",
    )

    _ENV_ASSIGNMENT_RE_FB = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

    _HOOKS_TABLE_RE_FB = _re.compile(r"^\[hooks\][ \t]*(?:[#;].*)?$")
    _ARRAY_RE_FB = _re.compile(
        r"transparent_prefixes[ \t]*=[ \t]*\[(?P<body>[^\]]*)\]", _re.S,
    )
    _STRING_RE_FB = _re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'')

    # Configured-prefix cache keyed by (path, mtime): the plugin is
    # long-lived, so notice config edits without re-parsing every call
    # (KEEP IN SYNC with shared hook_utils behaviour).
    _configured_prefix_cache_fb: dict = {}
    _prefix_word_lists_cache_fb: dict = {}

    def _rtk_config_path_fb() -> str | None:  # type: ignore[misc]
        """rtk's global config.toml path, mirroring rtk's own lookup."""
        home = os.path.expanduser("~")
        if not home or not os.path.isabs(home):
            return None
        if sys.platform == "darwin":
            return os.path.join(home, "Library", "Application Support", "rtk", "config.toml")
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            return os.path.join(appdata, "rtk", "config.toml") if appdata else None
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        base = xdg if os.path.isabs(xdg) else os.path.join(home, ".config")
        return os.path.join(base, "rtk", "config.toml")

    def _parse_hooks_transparent_prefixes_fb(text: str) -> tuple:  # type: ignore[misc]
        """Best-effort ``[hooks].transparent_prefixes`` reader (no tomllib)."""
        body: list = []
        in_hooks = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_hooks = bool(_HOOKS_TABLE_RE_FB.match(stripped))
                continue
            if in_hooks:
                body.append(line)
        if not body:
            return ()
        match = _ARRAY_RE_FB.search("\n".join(body))
        if not match:
            return ()
        values: list = []
        for sm in _STRING_RE_FB.finditer(match.group("body")):
            if sm.group(1) is not None:
                values.append(sm.group(1).replace('\\"', '"').replace("\\\\", "\\"))
            else:
                values.append(sm.group(2))
        return tuple(values)

    def _rtk_transparent_prefixes_fb() -> tuple:  # type: ignore[misc]
        """User-configured rtk transparent prefixes (may be empty)."""
        path = _rtk_config_path_fb()
        if not path:
            return ()
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return ()
        key = (path, mtime)
        cached = _configured_prefix_cache_fb.get(key)
        if cached is not None:
            return cached
        prefixes: tuple = ()
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            try:
                import tomllib

                data = tomllib.loads(text)
                values = data.get("hooks", {}).get("transparent_prefixes", [])
                if isinstance(values, list):
                    prefixes = tuple(v for v in values if isinstance(v, str))
            except ModuleNotFoundError:
                prefixes = _parse_hooks_transparent_prefixes_fb(text)
            except Exception:
                prefixes = ()
        except OSError:
            prefixes = ()
        # Mirror rtk's normalize_transparent_prefixes: trim, drop empties, dedup.
        prefixes = tuple(dict.fromkeys(p.strip() for p in prefixes if p.strip()))
        _configured_prefix_cache_fb.clear()  # keep at most one entry
        _configured_prefix_cache_fb[key] = prefixes
        return prefixes

    def _transparent_prefix_word_lists_fb() -> tuple:  # type: ignore[misc]
        """All transparent prefixes as word tuples, longest match first."""
        configured = _rtk_transparent_prefixes_fb()
        cached = _prefix_word_lists_cache_fb.get(configured)
        if cached is None:
            merged = set(_TRANSPARENT_WRAPPERS_FB)
            merged.update(_RTK_BUILTIN_TRANSPARENT_PREFIXES_FB)
            merged.update(p.strip() for p in configured if p.strip())
            word_lists = [tuple(p.split()) for p in merged if p.strip()]
            word_lists.sort(
                key=lambda words: (len(words), sum(len(w) for w in words)),
                reverse=True,
            )
            cached = tuple(word_lists)
            _prefix_word_lists_cache_fb.clear()  # keep at most one entry
            _prefix_word_lists_cache_fb[configured] = cached
        return cached

    def _match_transparent_prefix_fb(  # type: ignore[misc]
        rewritten: str, spans: list, index: int, prefix_words: tuple,
    ) -> int:
        """Span count of the longest transparent prefix at *index* (0 = none)."""
        total = len(spans)
        for words in prefix_words:
            count = len(words)
            if count == 0 or index + count > total:
                continue
            matched = True
            for offset in range(count):
                span_start, span_end, new_segment = spans[index + offset]
                if offset and new_segment:
                    matched = False
                    break
                if rewritten[span_start:span_end] != words[offset]:
                    matched = False
                    break
            if matched:
                return count
        return 0

    def _is_env_assignment_fb(token: str) -> bool:  # type: ignore[misc]
        """Return True when *token* looks like a leading shell env assignment."""
        return bool(_ENV_ASSIGNMENT_RE_FB.match(token))

    def _is_segment_boundary_fb(cmd: str, i: int) -> bool:  # type: ignore[misc]
        """``&`` inside fd redirections (2>&1, &>) is not a separator."""
        if cmd[i] != "&":
            return True
        if i > 0 and cmd[i - 1] == ">":
            return False
        if i + 1 < len(cmd) and cmd[i + 1] == ">":
            return False
        return True

    def _shell_word_spans_fb(cmd: str):  # type: ignore[misc]
        """Lex cmd into (start, end, new_segment) word spans.

        Mirrors shared hook_utils._shell_word_spans (KEEP IN SYNC):
        unquoted, unescaped ``;`` / ``|`` / ``&`` connectives and
        newlines start a new segment; backslash-escaped operators
        (``\\;``) and operators inside quotes are ordinary word
        material, never boundaries; ``&`` inside fd redirections
        (``2>&1``, ``&>``) stays in its word.  Returns None when a
        quote is never closed.
        """
        spans: list[tuple[int, int, bool]] = []
        i = 0
        n = len(cmd)
        prev_end = -1
        while i < n:
            c = cmd[i]
            if c in _WORD_BREAK_CHARS_FB and (
                c not in _SEGMENT_META_CHARS_FB or _is_segment_boundary_fb(cmd, i)
            ):
                i += 1
                continue
            start = i
            while i < n:
                c = cmd[i]
                if c in _WORD_BREAK_CHARS_FB:
                    if c in _SEGMENT_META_CHARS_FB and not _is_segment_boundary_fb(cmd, i):
                        # fd redirection (& in 2>&1 / &>) stays in the word.
                        i += 1
                        continue
                    break
                if c == "'":
                    j = cmd.find("'", i + 1)
                    if j < 0:
                        return None
                    i = j + 1
                    continue
                if c == '"':
                    i += 1
                    closed = False
                    while i < n:
                        if cmd[i] == '"':
                            closed = True
                            i += 1
                            break
                        if cmd[i] == "\\" and i + 1 < n and cmd[i + 1] in '"$`\\':
                            i += 2
                            continue
                        i += 1
                    if not closed:
                        return None
                    continue
                if c == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            if prev_end < 0:
                new_segment = True
            else:
                gap = cmd[prev_end:start]
                new_segment = any(ch in _SEGMENT_BOUNDARY_CHARS_FB for ch in gap)
            spans.append((start, i, new_segment))
            prev_end = i
        return spans

    def _anchor_rtk_prefix(rewritten: str, rtk_bin: str) -> str:  # type: ignore[misc]
        """Local fallback when shared hook_utils is unavailable or too old.

        Anchors the first bare ``rtk`` word of each segment *in command
        position*; segments are delimited by real (unquoted, unescaped)
        ``;`` / ``|`` / ``&`` / ``&&`` / ``||`` connectives and newlines.
        Escaped operators (``\\;``) and quoted operators never start a
        segment.  Leading environment assignments, transparent wrappers
        such as ``sudo``, and RTK's transparent-prefix protocol — the
        built-in ``uv run`` / ``noglob`` / ``command`` / ``builtin`` /
        ``exec`` / ``nocorrect`` prefixes plus the user-configured
        multi-word ``[hooks].transparent_prefixes`` — do not consume the
        command position; prefix sequences are matched whole, longest
        first, never across a segment boundary.  Any other word — a plain
        command, or a wrapper option such as ``sudo -u`` / ``command -v``
        whose operand or query argument must never be anchored — consumes
        it.
        """
        spans = _shell_word_spans_fb(rewritten)
        if spans is None:
            return rewritten
        quoted = _shlex.quote(rtk_bin)
        prefix_words = _transparent_prefix_word_lists_fb()
        parts: list[str] = []
        pos = 0
        index = 0
        total = len(spans)
        command_pending = True
        while index < total:
            start, end, new_segment = spans[index]
            if new_segment:
                command_pending = True
            if not command_pending:
                index += 1
                continue
            word = rewritten[start:end]
            if _is_env_assignment_fb(word):
                index += 1
                continue
            matched = _match_transparent_prefix_fb(rewritten, spans, index, prefix_words)
            if matched:
                index += matched
                continue
            if word == "rtk":
                parts.append(rewritten[pos:start])
                parts.append(quoted)
                pos = end
                command_pending = False
            else:
                # Any other word consumes the command position — including
                # wrapper options (``-u``, ``-v``, ...) whose operands (e.g.
                # the username in ``sudo -u rtk true``) or query arguments
                # (``command -v rtk``) must never be anchored.  rtk itself
                # never composes a dash option in front of the rtk wrapper
                # it inserts, so this only affects passed-through literals.
                command_pending = False
            index += 1
        if pos == 0:
            return rewritten
        parts.append(rewritten[pos:])
        return "".join(parts)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "hermes-agent"
_MIN_RESPONSE_LEN = 200

# Minimum payload size for the TOON encoding step. TOON on small JSON
# saves only a few characters (observed ~0.3% below ~500 chars) while
# the per-event encode cost stays the same, so payloads under this
# threshold keep the response-compressed form and skip TOON entirely.
# Mirrors tokenless-runtime's MIN_TOON_CHARS, the default the
# compress-toon CLI applies; keep the two values in sync.
_MIN_TOON_CHARS = 500

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

    # Step 2: TOON encoding — only for payloads at or above the minimum
    # threshold; small JSON gains near-zero chars from TOON but would still
    # pay the full encode cost on every tool result.
    toon_result = None
    if len(current) >= _MIN_TOON_CHARS:
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
