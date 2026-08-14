# DeepSeek Harness (dsh) adapter

Phase-1 bridge-mode adapter. It wires the shared tokenless hooks into
[deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) through
the upstream `@deepseek-ai/dsh-hooks-claude-code` plugin, which maps
Claude-Code-dialect `hooks.json` command hooks onto the harness's canonical
interception points.

## Layout

- `hooks/hooks.json` — CC-dialect hook config with dsh tool names
  (`bash|pwsh` rewrite matcher, lowercase content-retrieval exclusions).
  `${CLAUDE_PLUGIN_ROOT}` is substituted by the bridge from its `pluginRoot`
  setting.
- `hooks/run-hook.sh` — symlink to the shared dispatcher in `common/hooks/`.
- `scripts/detect.sh` — fail-open status probe (always exits 0).
- `scripts/install.sh` — installs the bridge package under
  `$DSH_HOME/node_modules` when missing and registers a marked managed
  `insert` block in `$DSH_HOME/cordis.patch.yml`, the home-level user patch
  layer dsh applies over every profile.
- `scripts/uninstall.sh` — removes the managed block and restores the
  pre-install state (user patch entries are preserved).

## Capability gaps (honest by design)

Per the dsh bridge documentation ("Known Limitations"):

- `PreToolUse`: `updatedInput` is parsed but not honored → `rewrite`
  cannot deliver savings through the bridge. `rewrite_hook.py` fails open
  for this agent instead of recording savings that cannot take effect.
- `PostToolUse`: `updatedToolOutput`/`updatedMCPToolOutput` are unsupported
  and `suppressOutput` is not applied → `compress-response`/`compress-toon`
  cannot deliver savings through the bridge. `compress_response_hook.py`
  fails open for this agent: only the genuinely additive environment
  attribution is emitted, never a compressed copy of the payload beside
  the original tool result.
- JSON `additionalContext` and the allow path work today, so `tool-ready`
  wiring and stats attribution (`TOKENLESS_AGENT_ID=deepseek-harness`) are
  live now.

The hooks stay registered so the full pipeline activates without adapter
changes once dsh honors input/output rewriting. The manifest declares only
`tool-ready` until then.

## Risks

dsh is in developer preview and warns about compatibility-breaking changes;
re-verify the bridge contract on each dsh release. dsh hook config is
process-level (`configPath` parsed once at load), so registration is
home-level rather than per-session.

Phase 2 (future) is a native Cordis plugin on `tools/pre-execute` /
`tools/post-execute` for full-fidelity input rewrite and output replacement.
