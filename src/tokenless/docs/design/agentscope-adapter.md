# Tokenless Adapter for AgentScope

[中文版](agentscope-adapter_zh.md)

## Goal

Provide a Tokenless adapter for [AgentScope](https://github.com/agentscope-ai/agentscope)
so that AgentScope-based agents can benefit from Tokenless compression without
changing their application code.

## AgentScope Integration Points

AgentScope exposes several extension points that can be used to implement
compression:

| AgentScope concept | What it intercepts | Tokenless mapping |
|---|---|---|
| `Middleware` hooks | Reply, reasoning, acting, model calling | Run compression before/after model calls and tool execution |
| `Model` wrappers | Raw LLM, embedding, TTS calls | Wrap provider calls to compress request schemas and tool results |
| `Toolkit` / skills | Tool definitions and execution | Compress tool schemas and tool outputs |
| Event system | Streaming reasoning, tool calls, multimodal content | Optional stats / diagnostics |

The preferred integration is through **Middleware** because it is the
framework-native hook layer and keeps the adapter aligned with how Tokenless
integrates with Hermes, Codex, and OpenCode.

## Adapter Responsibilities

| Capability | Where it runs | Notes |
|---|---|---|
| Tool schema compression | Before model call | Compress `tools` passed to the model |
| Response compression | After tool execution | Compress JSON tool results |
| TOON encoding | After response compression | Encode compressed JSON to TOON when beneficial |
| Tool Ready | Before tool execution | Block calls when environment is not ready |
| Command rewriting | Before shell tool execution | Suggest RTK-rewritten command (block + suggest if AgentScope does not allow argument mutation) |
| Session tracking | On session / run start | Propagate IDs to tokenless stats |

## Proposed Adapter Layout

```
adapters/tokenless/agentscope/
├── __init__.py              # Plugin entry point, registers middleware
├── middleware.py            # AgentScope middleware implementation
├── plugin.yaml.in           # AgentScope plugin manifest
├── scripts/
│   ├── detect.sh            # Detect AgentScope installation
│   ├── install.sh           # Install / symlink adapter into AgentScope
│   └── uninstall.sh         # Remove adapter
```

The Python implementation reuses `adapters/tokenless/common/hooks/hook_utils.py`
for binary resolution and the shared hook scripts for the actual compression
logic, just like the Hermes adapter.

## Middleware Hooks

AgentScope middleware can intercept the agent loop at multiple stages. The
Tokenless adapter registers handlers for the stages that map to Tokenless
strategies:

1. `pre_model_call` (or equivalent pre-model stage)
   - Compress `tools` in the model request using `compress_schema_hook.py`.
   - Record agent/session context.

2. `post_tool_execute` / `post_acting`
   - For tool results that are JSON strings, run `compress_response_hook.py`.
   - The hook may additionally apply TOON encoding.

3. `pre_tool_execute` / `pre_acting`
   - Run `tool_ready_hook.sh` to check environment readiness.
   - For shell tools, run `rewrite_hook.py` to obtain an RTK-rewritten command.
   - If AgentScope allows argument mutation, replace the command inline;
     otherwise block the call and return the rewritten command as feedback.

4. `on_session_start`
   - Propagate `agent_id` and `session_id` into environment variables for stats.

## Manifest Entry

`adapters/tokenless/manifest.json.in` gains an `agentscope` target:

```json
"agentscope": {
  "compatibleVersions": ">=0.1.0",
  "capabilities": {
    "hooks": [
      "tool-ready",
      "rewrite",
      "compress-response",
      "compress-toon",
      "compress-schema"
    ]
  },
  "actions": {
    "detect":    "agentscope/scripts/detect.sh",
    "install":   "agentscope/scripts/install.sh",
    "uninstall": "agentscope/scripts/uninstall.sh"
  }
}
```

## Open Design Questions

1. **Exact middleware API names** — AgentScope's middleware stage names and
   signatures need to be verified against the installed SDK version. The
   adapter should version-guard itself and fail open if the API is incompatible.

2. **Argument mutation** — If AgentScope middleware cannot mutate tool
   arguments, command rewriting must use the same "block + suggest" pattern as
   the Hermes adapter.

3. **Tool result shape** — AgentScope may wrap tool results in a custom message
   object. The adapter must extract the JSON payload before compression and
   re-wrap it afterwards.

4. **Installation path** — AgentScope's plugin/middleware discovery path must be
   determined so `install.sh` can place the adapter in the correct user or
   virtual-environment directory.

## Suggested Phase 1 Scope

- Investigate AgentScope middleware signatures and plugin discovery.
- Implement `agentscope/middleware.py` with fail-open hooks for schema
  compression, response compression, and tool ready.
- Add `agentscope/plugin.yaml.in` and install/uninstall scripts.
- Register the `agentscope` target in `manifest.json.in`.
- Add a smoke test that imports the middleware against a pinned AgentScope
  version.

## Relationship to the LLM Provider Proxy

If an AgentScope application cannot use middleware (e.g., an older version or a
restricted runtime), the [LLM Provider proxy](llm-provider-proxy.md) can still
provide compression transparently by changing the model endpoint. The
middleware adapter is the preferred integration when available.
