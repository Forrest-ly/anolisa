/**
 * Unit tests for the MCP stdio client.
 *
 * Covers:
 * - `resolveMcpToolName` (the real TOOL_NAME_MAP via its exported wrapper)
 * - `buildChildEnv` allowlist behaviour
 * - `McpStdioClient.stop()` safety on an unstarted client
 * - worker release after a failed `initialize` handshake
 * - `callTool` rejection when the binary can't spawn
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

/**
 * Fake MCP worker used by the failed-handshake tests.
 *
 * Every spawn appends its pid to `pidsPath` so a test can assert on the
 * workers that were created, including one that never answered a tool
 * call. The first spawn reads `modePath`: `error` answers `initialize`
 * with a JSON-RPC error, `empty` with a null result, and the file is
 * consumed either way so the next spawn handshakes normally.
 */
function failingHandshakeWorker(modePath: string, pidsPath: string): string {
  return `#!${process.execPath}
const fs = require("node:fs");
const readline = require("node:readline");
const MODE_PATH = ${JSON.stringify(modePath)};
const PIDS_PATH = ${JSON.stringify(pidsPath)};
fs.appendFileSync(PIDS_PATH, process.pid + "\\n");
setInterval(() => {}, 1000);
process.stdin.on("end", () => process.exit(0));
readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const request = JSON.parse(line);
  if (request.id === undefined) return;
  if (request.method === "initialize") {
    let mode = "";
    try {
      mode = fs.readFileSync(MODE_PATH, "utf8").trim();
      fs.rmSync(MODE_PATH);
    } catch {}
    if (mode === "error") {
      const error = { code: -32603, message: "handshake refused by test worker" };
      process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, error }) + "\\n");
      return;
    }
    if (mode === "empty") {
      const msg = { jsonrpc: "2.0", id: request.id, result: null };
      process.stdout.write(JSON.stringify(msg) + "\\n");
      return;
    }
  }
  const result = request.method === "initialize" ? {} : {
    content: [{ type: "text", text: String(process.pid) }],
  };
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, result }) + "\\n");
});
`;
}

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

  // Regression for the orphaned worker: `initialized` stays false after a
  // failed handshake, so the next tool call spawns a replacement and
  // overwrites `proc`. Without a release in doStart the first worker is
  // untracked — stop() can never reach it and it outlives the plugin.
  for (const mode of ["error", "empty"] as const) {
    const handshake = mode === "error" ? "a JSON-RPC error" : "an empty result";

    it(`releases the worker when the handshake returns ${handshake}`, {
      timeout: 20_000,
    }, async (context) => {
      const directory = await mkdtemp(join(tmpdir(), "memory-init-fail-test-"));
      const binaryPath = join(directory, "mcp-server");
      const modePath = join(directory, "mode");
      const pidsPath = join(directory, "pids");
      const spawned: number[] = [];
      context.after(async () => {
        for (const pid of spawned) {
          try {
            process.kill(pid, "SIGKILL");
          } catch (error) {
            assert.equal((error as NodeJS.ErrnoException).code, "ESRCH");
          }
        }
        await rm(directory, { recursive: true, force: true });
      });

      await writeFile(binaryPath, failingHandshakeWorker(modePath, pidsPath), { mode: 0o700 });
      await writeFile(modePath, mode);
      const client = new McpStdioClient({ ...cfg, binaryPath });
      const readPids = async (): Promise<number[]> =>
        (await readFile(pidsPath, "utf8").catch(() => "")).split("\n").filter(Boolean).map(Number);

      // The handshake failure — not the teardown — is what reaches the caller.
      await assert.rejects(client.callTool("memory_get_context", {}), (error: Error) => {
        assert.match(
          error.message,
          mode === "error" ? /JSON-RPC error -32603/ : /handshake returned no result/,
        );
        return true;
      });

      const [firstPid] = await readPids();
      assert.ok(Number.isSafeInteger(firstPid) && firstPid > 0);
      spawned.push(firstPid);
      // stop() resolves only after the exit event, so the worker is already
      // reaped by the time the rejection surfaces.
      assert.throws(() => process.kill(firstPid, 0), { code: "ESRCH" });

      // The same client must still be usable: the abort left no stale
      // `proc`, and the deliberate teardown did not burn the respawn quota.
      const secondPid = Number(await client.callTool("memory_get_context", {}));
      assert.ok(Number.isSafeInteger(secondPid) && secondPid > 0);
      assert.notEqual(secondPid, firstPid);
      spawned.push(secondPid);

      await client.stop();
      assert.throws(() => process.kill(secondPid, 0), { code: "ESRCH" });
      assert.deepEqual(await readPids(), [firstPid, secondPid]);
    });
  }

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
