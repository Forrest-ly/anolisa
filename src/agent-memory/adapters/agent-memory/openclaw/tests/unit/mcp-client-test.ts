/**
 * Unit tests for the MCP stdio client.
 *
 * Covers:
 * - `resolveMcpToolName` (the real TOOL_NAME_MAP via its exported wrapper)
 * - `buildChildEnv` allowlist behaviour
 * - `McpStdioClient.stop()` safety on an unstarted client
 * - `callTool` rejection when the binary can't spawn
 * - Unicode fidelity when a real fake worker dribbles replies byte by byte
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { McpStdioClient, buildChildEnv, resolveMcpToolName } from "../../src/mcp-client.js";

describe("resolveMcpToolName", () => {
  it("maps namespaced OpenClaw tools to native MCP names", () => {
    assert.equal(resolveMcpToolName("anolisa_memory_get"), "mem_read");
    assert.equal(resolveMcpToolName("anolisa_memory_search"), "memory_search");
  });

  it("passes native MCP names and identity-mapped contracts through", () => {
    assert.equal(resolveMcpToolName("memory_search"), "memory_search");
    assert.equal(resolveMcpToolName("memory_observe"), "memory_observe");
    assert.equal(resolveMcpToolName("memory_get_context"), "memory_get_context");
  });

  it("passes unknown names through unchanged", () => {
    assert.equal(resolveMcpToolName("future_tool"), "future_tool");
  });
});

describe("buildChildEnv", () => {
  it("keeps allow-listed exact vars", () => {
    const env = buildChildEnv(
      { PATH: "/usr/bin", HOME: "/home/u", FOO: "secret" } as any,
      {},
    );
    assert.equal(env.PATH, "/usr/bin");
    assert.equal(env.HOME, "/home/u");
    assert.equal(env.FOO, undefined);
  });

  it("keeps prefix-matched vars (MEMORY_*, RUST_*) + exact USER_ID", () => {
    const env = buildChildEnv(
      {
        MEMORY_PROFILE: "expert",
        RUST_LOG: "debug",
        USER_ID: "alice",
        AWS_SECRET_KEY: "leak",
      } as any,
      {},
    );
    assert.equal(env.MEMORY_PROFILE, "expert");
    assert.equal(env.RUST_LOG, "debug");
    assert.equal(env.USER_ID, "alice");
    assert.equal(env.AWS_SECRET_KEY, undefined);
  });

  it("does NOT leak USER_ID-prefixed look-alikes (regression for R6-2)", () => {
    // Earlier USER_ID was in the prefix list, so a startsWith match
    // would have let USER_IDX / USER_ID_FOO through. Now USER_ID is
    // an exact-match entry and only the literal name passes.
    const env = buildChildEnv(
      { USER_IDX: "leak", USER_ID_FOO: "leak2", USER_ID: "alice" } as any,
      {},
    );
    assert.equal(env.USER_ID, "alice");
    assert.equal(env.USER_IDX, undefined);
    assert.equal(env.USER_ID_FOO, undefined);
  });

  it("does NOT leak MEMORY-prefixed look-alikes that miss the underscore", () => {
    // MEMORYCACHE has no underscore, so it should not match `MEMORY_`.
    const env = buildChildEnv({ MEMORYCACHE: "leak", MEMORY_PROFILE: "advanced" } as any, {});
    assert.equal(env.MEMORY_PROFILE, "advanced");
    assert.equal(env.MEMORYCACHE, undefined);
  });

  it("plugin env overrides allow-listed parent value", () => {
    const env = buildChildEnv(
      { MEMORY_PROFILE: "basic", PATH: "/usr/bin" } as any,
      { MEMORY_PROFILE: "advanced" },
    );
    assert.equal(env.MEMORY_PROFILE, "advanced");
    assert.equal(env.PATH, "/usr/bin");
  });
});

describe("McpStdioClient", () => {
  const cfg = {
    binaryPath: "/nonexistent/agent-memory-binary",
    userId: "0",
    profile: "advanced" as const,
    maxReadBytes: 1_048_576,
    maxWriteBytes: 16_777_216,
    // Inert here — the binary never spawns — but part of AgentMemoryConfig,
    // which the client forwards as MEMORY_SESSION_ID / MEMORY_SESSION_DIR.
    sessionId: "ses_test",
    sessionDir: "/nonexistent/agent-memory-session-dir",
  };

  it("stop() is safe when the process was never started", async () => {
    const client = new McpStdioClient(cfg);
    await client.stop();
  });

  for (const ignoreEof of [false, true]) {
    it(`stop() waits for ${ignoreEof ? "SIGKILL" : "stdin EOF"} before returning`, {
      timeout: 10_000,
    }, async (context) => {
      const directory = await mkdtemp(join(tmpdir(), "memory-stop-test-"));
      const binaryPath = join(directory, "mcp-server");
      const eofPath = join(directory, "eof");
      let childPid: number | undefined;
      context.after(async () => {
        if (childPid !== undefined) {
          try {
            process.kill(childPid, "SIGKILL");
          } catch (error) {
            assert.equal((error as NodeJS.ErrnoException).code, "ESRCH");
          }
        }
        await rm(directory, { recursive: true, force: true });
      });
      await writeFile(binaryPath, `#!${process.execPath}
const readline = require("node:readline");
const fs = require("node:fs");
setInterval(() => {}, 1000);
process.on("SIGTERM", () => {});
process.stdin.on("end", () => {
  if (${ignoreEof}) return;
  fs.writeFileSync(${JSON.stringify(eofPath)}, "eof");
  process.exit(0);
});
readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const request = JSON.parse(line);
  if (request.id === undefined) return;
  const result = request.method === "initialize" ? {} : {
    content: [{ type: "text", text: String(process.pid) }],
  };
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, result }) + "\\n");
});
`, { mode: 0o700 });
      const client = new McpStdioClient({ ...cfg, binaryPath });
      childPid = Number(await client.callTool("memory_get_context", {}));
      assert.ok(Number.isSafeInteger(childPid) && childPid > 0);

      await client.stop();

      assert.throws(() => process.kill(childPid!, 0), { code: "ESRCH" });
      if (!ignoreEof) {
        assert.equal(await readFile(eofPath, "utf8"), "eof");
      }
      await client.stop();
    });
  }

  // Regression for fragmented-Unicode decoding: the fake worker below is a
  // real child process that answers the initialize handshake and tools/call,
  // but writes each reply ONE BYTE per event-loop turn. Every multibyte code
  // point therefore straddles a pipe chunk boundary, which is exactly what
  // turned Chinese text, accents and emoji into U+FFFD when each stdout
  // Buffer was decoded on its own.
  const UNICODE_PAYLOAD = "记忆 · café · naïve · 中文记忆内容 · 🚀🔍 · émoji · 漢字";

  it("keeps multibyte Unicode intact across byte-by-byte stdout chunks", {
    timeout: 30_000,
  }, async (context) => {
    const directory = await mkdtemp(join(tmpdir(), "memory-unicode-test-"));
    const binaryPath = join(directory, "mcp-server");
    const pidPath = join(directory, "pid");
    context.after(async () => {
      // The worker records its own pid so a failed assertion can't leave an
      // orphan behind. Missing file == never spawned == nothing to kill.
      let pid = NaN;
      try {
        pid = Number(await readFile(pidPath, "utf8"));
      } catch {
        pid = NaN;
      }
      if (Number.isSafeInteger(pid) && pid > 0) {
        try {
          process.kill(pid, "SIGKILL");
        } catch (error) {
          assert.equal((error as NodeJS.ErrnoException).code, "ESRCH");
        }
      }
      await rm(directory, { recursive: true, force: true });
    });
    await writeFile(
      binaryPath,
      `#!${process.execPath}
const fs = require("node:fs");
const readline = require("node:readline");
const UNICODE = ${JSON.stringify(UNICODE_PAYLOAD)};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

fs.writeFileSync(${JSON.stringify(pidPath)}, String(process.pid));

// One byte per turn, so the parent never reads a whole code point at once.
async function dribble(buf) {
  for (let i = 0; i < buf.length; i += 1) {
    process.stdout.write(buf.subarray(i, i + 1));
    await sleep(0);
  }
}

// Serialize replies: two interleaved dribbles would corrupt both frames.
let queue = Promise.resolve();

readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const request = JSON.parse(line);
  if (request.id === undefined) return;
  queue = queue.then(async () => {
    const result = request.method === "initialize"
      ? {
          protocolVersion: "2024-11-05",
          capabilities: {},
          serverInfo: { name: "agent-memory-记忆-fake", version: "0.0.0-test" },
        }
      : {
          content: [{
            type: "text",
            text: request.params.arguments.query + "|" + UNICODE +
              "|" + request.params.arguments.query,
          }],
          isError: false,
        };
    const frame =
      JSON.stringify({ jsonrpc: "2.0", id: request.id, result }) + "\\n";
    await dribble(Buffer.from(frame, "utf8"));
  });
});
`,
      { mode: 0o700 },
    );

    const client = new McpStdioClient({ ...cfg, binaryPath });
    context.after(async () => {
      await client.stop();
    });

    // The handshake reply is dribbled as well, so getting a tool result at
    // all proves the initialize frame survived byte-by-byte delivery too.
    const query = "记忆🚀";
    const expected = `${query}|${UNICODE_PAYLOAD}|${query}`;

    const first = await client.callTool("memory_observe", { query });
    assert.equal(first, expected);
    assert.ok(!first.includes("\uFFFD"), "reply must not contain U+FFFD");

    // A second call on the same child: the decoder must not carry state into
    // the next frame, and the newline parser must still split the two.
    const second = await client.callTool("memory_observe", { query });
    assert.equal(second, expected);
  });

  it("callTool rejects with a real error when the binary cannot spawn", async () => {
    const client = new McpStdioClient(cfg);
    try {
      await client.callTool("memory_search", { query: "x" });
      assert.fail("expected callTool to reject");
    } catch (err: any) {
      // Error surfaces from spawn (ENOENT) or the initialize timeout.
      assert.ok(err instanceof Error);
      assert.ok(err.message.length > 0);
    } finally {
      await client.stop();
    }
  });
});
