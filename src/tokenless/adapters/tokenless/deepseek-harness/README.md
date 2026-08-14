# DeepSeek Harness (dsh) adapter

Phase-1 bridge-mode adapter. It wires the shared tokenless hooks into
[deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) through
the upstream `@deepseek-ai/dsh-hooks-claude-code` plugin, which maps
Claude-Code-dialect `hooks.json` command hooks onto the harness's canonical
interception points.

## Layout

- `hooks/hooks.json` — CC-dialect hook config (`${CLAUDE_PLUGIN_ROOT}` is
  substituted by the bridge from its `pluginRoot` setting). PreToolUse
  dispatches `rewrite_hook.py` (Bash) and `tool_ready_hook.sh` (all tools);
  PostToolUse dispatches `compress_response_hook.py`.
- `hooks/run-hook.sh` — symlink to the shared dispatcher in `common/hooks/`.
- `scripts/detect.sh` — fail-open JSON status probe (always exits 0).
- `scripts/install.sh` — installs the bridge package into each dsh profile
  (`$DSH_HOME/profiles/<name>`) and appends a marked managed block to the
  profile's `cordis.patch.yml`.
- `scripts/uninstall.sh` — removes the managed block (and best-effort the
  bridge package).

## Capability gaps (honest by design)

Per the dsh bridge documentation ("Known Limitations"):

- `PreToolUse`: `updatedInput` is parsed but not honored → `rewrite`
  delivers no savings through the bridge.
- `PostToolUse`: `updatedToolOutput`/`updatedMCPToolOutput` are unsupported
  and `suppressOutput` is not applied → `compress-response`/`compress-toon`
  deliver no savings through the bridge. `compress_response_hook.py`
  therefore routes `TOKENLESS_AGENT_ID=deepseek-harness` through the
  replacement branch: the emission degrades to an untouched pass-through
  instead of duplicating the payload via additive `additionalContext`.
- `SessionStart`, the PreToolUse allow path, and JSON `additionalContext`
  work today, so `tool-ready` wiring and stats attribution
  (`TOKENLESS_AGENT_ID=deepseek-harness`) are live now.

The hooks stay registered so savings start automatically once dsh honors the
fields above. The manifest declares only `tool-ready` until then.

## Risks

dsh is in developer preview and warns about compatibility-breaking changes;
re-verify the bridge contract on each dsh release. dsh hook config is
process-level (`configPath` parsed once at load), so registration is
per-profile rather than per-session.

Phase 2 (future) is a native Cordis plugin on `tools/pre-execute` /
`tools/post-execute` for full-fidelity input rewrite and output replacement.
