/**
 * Tokenless integration for OpenCode's local plugin API.
 *
 * The adapter translates OpenCode's in-process hooks to the shared Tokenless
 * JSON hook protocol, keeping compression and readiness behavior consistent
 * with the other agent integrations.
 *
 * One module serves both host generations from one hook implementation:
 * OpenCode 2 loads the default definition (`id` + `setup`), OpenCode 1 loads
 * the same default export's `server()` entrypoint, and OpenCode 1 releases
 * older than the object entrypoint keep scanning the named `TokenlessPlugin`
 * export. Only the host's event shapes differ, never Tokenless policy.
 */

import { spawn } from "node:child_process";
import { existsSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";

const AGENT_ID = "opencode";
const PLUGIN_ID = "tokenless";
const HOOK_TIMEOUT_MS = 15_000;
const MAX_HOOK_OUTPUT_BYTES = 1024 * 1024;
const MAX_CACHE_ENTRIES = 256;
const PLUGIN_FILE = fileURLToPath(import.meta.url);
const PLUGIN_DIR = dirname(realpathSync(PLUGIN_FILE));

// OpenCode 1 named its built-in shell tool "bash"; OpenCode 2 names it "shell".
// The shared hooks classify the whole shell family (common/hooks/
// tool_categories.json, layer_2_shell), so both entrypoints gate on that family
// instead of one host's spelling: a "bash"-only gate silently disabled command
// rewriting and shell failure evidence on OpenCode 2.
const SHELL_TOOLS = new Set([
  "bash",
  "shell",
  "exec",
  "terminal",
  "process",
  "run_shell_command",
  "run_in_terminal",
  "get_terminal_output",
  "execute_command",
]);

function isShellTool(name) {
  return typeof name === "string" && SHELL_TOOLS.has(name.trim().toLowerCase());
}

function isFile(path) {
  try {
    return existsSync(path) && statSync(path).isFile();
  } catch {
    // Hook discovery is fail-open so unreadable paths never block OpenCode startup.
    return false;
  }
}

function findHookRunner() {
  const adapterDir = process.env.ANOLISA_ADAPTER_DIR;
  const userHome = process.env.HOME && isAbsolute(process.env.HOME) ? process.env.HOME : homedir();
  const candidates = [
    process.env.TOKENLESS_HOOK_RUNNER,
    adapterDir ? join(adapterDir, "common", "hooks", "run-hook.sh") : "",
    // The installed plugin remains beside the shared adapter tree after its
    // global registration symlink is resolved through PLUGIN_FILE.
    join(PLUGIN_DIR, "..", "common", "hooks", "run-hook.sh"),
    join(
      userHome,
      ".local",
      "share",
      "anolisa",
      "adapters",
      "tokenless",
      "common",
      "hooks",
      "run-hook.sh",
    ),
    "/usr/local/share/anolisa/adapters/tokenless/common/hooks/run-hook.sh",
    "/usr/share/anolisa/adapters/tokenless/common/hooks/run-hook.sh",
  ];
  return candidates.find((candidate) => candidate && isFile(candidate)) ?? null;
}

function parseHookOutput(raw) {
  if (!raw.trim()) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function runHook(runner, hookName, payload) {
  if (!runner) return Promise.resolve(null);
  let serialized;
  try {
    serialized = JSON.stringify(payload);
  } catch {
    return Promise.resolve(null);
  }

  return new Promise((resolve) => {
    let stdout = "";
    let stdoutBytes = 0;
    let settled = false;
    let child;
    let timer;
    const ownProcessGroup = process.platform !== "win32";
    try {
      child = spawn("bash", [runner, hookName], {
        env: {
          ...process.env,
          TOKENLESS_AGENT_ID: AGENT_ID,
        },
        detached: ownProcessGroup,
        stdio: ["pipe", "pipe", "ignore"],
      });
    } catch {
      resolve(null);
      return;
    }

    const finish = (value, terminate = false) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (terminate) {
        // A hook can exit while its descendants still hold stdout open. Kill
        // only our detached group, and do not let inherited pipes pin the host.
        if (ownProcessGroup && child.pid) {
          try {
            process.kill(-child.pid, "SIGKILL");
          } catch {
            child.kill("SIGKILL");
          }
        } else {
          child.kill("SIGKILL");
        }
        child.stdin.destroy();
        child.stdout.destroy();
        child.unref();
      }
      resolve(value);
    };
    timer = setTimeout(() => finish(null, true), HOOK_TIMEOUT_MS);

    child.on("error", () => finish(null));
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      if (settled) return;
      stdoutBytes += Buffer.byteLength(chunk, "utf8");
      if (stdoutBytes > MAX_HOOK_OUTPUT_BYTES) {
        finish(null, true);
        return;
      }
      stdout += chunk;
    });
    child.on("close", (code) => {
      finish(code === 0 ? parseHookOutput(stdout) : null);
    });

    child.stdin.on("error", () => {
      finish(null, true);
    });
    child.stdin.end(serialized);
  });
}

function toolPayload(input, args, toolResponse) {
  const payload = {
    session_id: input.sessionID,
    tool_use_id: input.callID,
    tool_call_id: input.callID,
    tool_name: input.tool,
    tool_input: args ?? {},
  };
  if (toolResponse !== undefined) payload.tool_response = toolResponse;
  return payload;
}

function hookSpecificOutput(result) {
  const output = result?.hookSpecificOutput;
  return output && typeof output === "object" ? output : null;
}

function prependContext(context, content) {
  if (typeof context !== "string" || !context) return content;
  return content ? `${context}\n${content}` : context;
}

// Host codecs carry executable validation; only plain JSON schemas cross the hook boundary.
function isJsonSchema(value) {
  if (typeof value === "boolean") return true;
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

// OpenCode 1 hands the plugin one model-visible string. OpenCode 2 hands it
// either a string or an ordered content-part list, where only `text` parts are
// prose; `file` parts stay untouched. The view exposes the concatenated text
// plus a replacement that restores the host's original shape.
function resultTextView(content) {
  if (typeof content === "string") {
    return { text: content, replace: (text) => text };
  }
  if (!Array.isArray(content)) return null;
  const textIndexes = new Set();
  content.forEach((part, index) => {
    if (part && typeof part === "object" && part.type === "text" && typeof part.text === "string") {
      textIndexes.add(index);
    }
  });
  if (!textIndexes.size) return null;
  const first = Math.min(...textIndexes);
  const text = content
    .filter((_, index) => textIndexes.has(index))
    .map((part) => part.text)
    .join("\n");
  return {
    text,
    replace: (replacement) =>
      content.flatMap((part, index) => {
        if (index === first) return [{ ...part, text: replacement }];
        return textIndexes.has(index) ? [] : [part];
      }),
  };
}

function createHooks() {
  const runner = findHookRunner();
  const readinessContext = new Map();
  const schemaCache = new Map();

  const cacheValue = (cache, key, value) => {
    if (cache.has(key)) {
      cache.delete(key);
    } else if (cache.size >= MAX_CACHE_ENTRIES) {
      cache.delete(cache.keys().next().value);
    }
    cache.set(key, value);
  };

  const cachedValue = (cache, key) => {
    if (!cache.has(key)) return undefined;
    const value = cache.get(key);
    cache.delete(key);
    cache.set(key, value);
    return value;
  };

  /**
   * Runs the readiness gate and, for shell-family tools, the RTK rewrite.
   *
   * `call` is the host-neutral shape `{ tool, sessionID, callID, args }`.
   * Returns the rewritten command, or null when nothing changed. Throws when
   * Tokenless blocks the call; both host generations surface that as a failed
   * tool call.
   */
  async function beforeTool(call) {
    const payload = toolPayload(call, call.args);
    const readyResult = await runHook(runner, "tool_ready_hook.sh", payload);
    if (readyResult?.decision === "block") {
      throw new Error(readyResult.reason || "Tokenless reports that the tool is not ready");
    }

    const readyOutput = hookSpecificOutput(readyResult);
    if (typeof readyOutput?.additionalContext === "string") {
      cacheValue(
        readinessContext,
        `${call.sessionID}:${call.callID}`,
        readyOutput.additionalContext,
      );
    }

    if (!isShellTool(call.tool) || typeof call.args?.command !== "string") return null;
    const rewriteResult = await runHook(runner, "rewrite_hook.py", payload);
    const rewritten = hookSpecificOutput(rewriteResult)?.updatedInput?.command;
    return typeof rewritten === "string" && rewritten ? rewritten : null;
  }

  /**
   * Compresses one model-visible tool result and returns its replacement text,
   * prefixed with the readiness and diagnostic context Core attached.
   */
  async function afterTool(call, content, isError) {
    const key = `${call.sessionID}:${call.callID}`;
    const readyContext = readinessContext.get(key);
    readinessContext.delete(key);

    const payload = toolPayload(call, call.args, content);
    if (isError) payload.is_error = true;
    const result = await runHook(runner, "compress_response_hook.py", payload);
    const hookOutput = hookSpecificOutput(result);
    const compressedOutput = hookOutput?.updatedToolOutput;
    const text = typeof compressedOutput === "string" ? compressedOutput : content;
    const context = [readyContext, hookOutput?.additionalContext]
      .filter((value) => typeof value === "string" && value)
      .join("\n");
    return prependContext(context, text);
  }

  /**
   * Compresses tool declarations in one hook call and returns the results keyed
   * by tool name. A declaration Core did not return, or returned under another
   * name, is omitted so its caller keeps the host's own definition. Unchanged
   * declarations are served from the bounded cache, so a host that re-declares
   * its tools on every model request costs one subprocess per new shape.
   */
  async function compressDeclarations(declarations) {
    const compressed = new Map();
    const pending = [];
    for (const declaration of declarations) {
      let cacheKey;
      try {
        cacheKey = JSON.stringify(declaration);
      } catch {
        continue;
      }
      const cached = cachedValue(schemaCache, cacheKey);
      if (cached) {
        compressed.set(declaration.name, cached);
        continue;
      }
      pending.push({ declaration, cacheKey });
    }
    if (!pending.length) return compressed;

    const result = await runHook(runner, "compress_schema_hook.py", {
      llm_request: { config: { tools: pending.map((entry) => entry.declaration) } },
    });
    const tools = hookSpecificOutput(result)?.llm_request?.config?.tools;
    if (!Array.isArray(tools)) return compressed;
    for (const entry of pending) {
      const name = entry.declaration.name;
      const matches = tools.filter((tool) => tool?.name === name);
      if (matches.length !== 1) continue;
      const value = { description: matches[0].description, parameters: matches[0].parameters };
      cacheValue(schemaCache, entry.cacheKey, value);
      compressed.set(name, value);
    }
    return compressed;
  }

  return { beforeTool, afterTool, compressDeclarations };
}

/** Shell failure evidence both host generations keep outside the model-visible text. */
function isFailedShellCall(tool, metadata) {
  const exit = metadata?.exit;
  return isShellTool(tool) && Number.isInteger(exit) && exit !== 0;
}

export const TokenlessPlugin = async () => {
  const hooks = createHooks();

  return {
    "tool.execute.before": async (input, output) => {
      const rewritten = await hooks.beforeTool({
        tool: input.tool,
        sessionID: input.sessionID,
        callID: input.callID,
        args: output.args,
      });
      if (rewritten) output.args.command = rewritten;
    },

    "tool.execute.after": async (input, output) => {
      // OpenCode keeps the shell exit code outside the model-visible text.
      // Forward failure evidence so Core preserves it and can add diagnostics.
      output.output = await hooks.afterTool(
        {
          tool: input.tool,
          sessionID: input.sessionID,
          callID: input.callID,
          args: input.args,
        },
        output.output,
        isFailedShellCall(input.tool, output.metadata),
      );
    },

    "tool.definition": async (input, output) => {
      const schemaField = output.jsonSchema !== undefined ? "jsonSchema" : "parameters";
      const parameters = output[schemaField];
      // Older hosts expose only a runtime codec. Leave it intact until they expose a JSON schema.
      if (!isJsonSchema(parameters)) return;
      const compressed = await hooks.compressDeclarations([
        { name: input.toolID, description: output.description, parameters },
      ]);
      const declaration = compressed.get(input.toolID);
      if (!declaration) return;

      if (typeof declaration.description === "string") output.description = declaration.description;
      if (isJsonSchema(declaration.parameters)) {
        output[schemaField] = cloneJson(declaration.parameters);
      }
    },
  };
};

/**
 * OpenCode 2 entrypoint. V2 hands each hook one mutable event instead of the
 * V1 `(input, output)` pair: the tool-call id is `event.id`, a completed result
 * carries `content` as a string or a content-part list, and the outgoing model
 * request exposes its declarations as a `name -> { description, input }` record
 * that Core matches by object identity, so entries are edited in place.
 */
async function setup(ctx) {
  const hooks = createHooks();

  await ctx.tool.hook("execute.before", async (event) => {
    const rewritten = await hooks.beforeTool({
      tool: event.tool,
      sessionID: event.sessionID,
      callID: event.id,
      args: event.input,
    });
    if (!rewritten) return;
    try {
      event.input.command = rewritten;
    } catch {
      // A frozen argument object still accepts the replacement Core re-reads.
      event.input = { ...event.input, command: rewritten };
    }
  });

  await ctx.tool.hook("execute.after", async (event) => {
    if (event.status !== "completed" || !event.result) return;
    // A result carrying only structured output has no model-visible text yet:
    // the host renders it after the hooks, so there is nothing to replace.
    const view = resultTextView(event.result.content);
    if (!view) return;
    const text = await hooks.afterTool(
      {
        tool: event.tool,
        sessionID: event.sessionID,
        callID: event.id,
        args: event.input,
      },
      view.text,
      isFailedShellCall(event.tool, event.result.metadata),
    );
    if (typeof text !== "string" || text === view.text) return;
    event.result.content = view.replace(text);
  });

  await ctx.session.hook("context", async (event) => {
    const tools = event.tools;
    if (!tools || typeof tools !== "object") return;
    const entries = [];
    for (const [name, entry] of Object.entries(tools)) {
      // Hosts exposing only a runtime codec keep it: plain JSON schemas only.
      if (!entry || typeof entry !== "object" || !isJsonSchema(entry.input)) continue;
      entries.push({ name, entry });
    }
    if (!entries.length) return;
    const compressed = await hooks.compressDeclarations(
      entries.map(({ name, entry }) => ({
        name,
        description: entry.description,
        parameters: entry.input,
      })),
    );
    for (const { name, entry } of entries) {
      const declaration = compressed.get(name);
      if (!declaration) continue;
      if (typeof declaration.description === "string") entry.description = declaration.description;
      if (isJsonSchema(declaration.parameters)) entry.input = cloneJson(declaration.parameters);
    }
  });
}

// The documented dual shape: OpenCode 2 reads `id` + `setup`, OpenCode 1.4+
// reads `server()`, and older OpenCode 1 releases call the named export.
export default {
  id: PLUGIN_ID,
  setup,
  server: TokenlessPlugin,
};
