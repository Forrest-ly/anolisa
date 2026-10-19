import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { SelectiveContextEngine } from "../src/engine.js";
import { createConnection, closeConnection } from "../src/db/connection.js";
import type { DatabaseSync } from "node:sqlite";
import type { AgentMessage } from "../src/openclaw-bridge.js";

const ARCHIVE_TURNS = 500;

function conversation(turns: number): AgentMessage[] {
  const messages: AgentMessage[] = [];
  for (let turn = 1; turn <= turns; turn++) {
    messages.push({ role: "user", content: `question ${turn}` });
    messages.push({ role: "assistant", content: `answer ${turn}` });
  }
  return messages;
}

describe("archive snapshot reuse", () => {
  let db: DatabaseSync;
  let engine: SelectiveContextEngine;

  beforeEach(() => {
    db = createConnection(":memory:");
    engine = new SelectiveContextEngine(db, {
      freshTailTurns: 3,
      dbPath: ":memory:",
      enabled: true,
    });
  });

  afterEach(() => {
    closeConnection(db);
  });

  /**
   * Counts full archive reads and the rows each read materializes. Rows are
   * tallied as the read returns, so later in-memory reuse is not recounted.
   */
  function trackArchiveReads() {
    const store = engine.getStore();
    const readArchive = store.getMessages.bind(store);
    const log = { calls: 0, rows: 0 };
    store.getMessages = (sessionId: string) => {
      const records = readArchive(sessionId);
      log.calls += 1;
      log.rows += records.length;
      return records;
    };
    return {
      log,
      stop() {
        delete (store as { getMessages?: unknown }).getMessages;
      },
    };
  }

  it("reads a large archive once to assemble a small live suffix", async () => {
    const archived = conversation(ARCHIVE_TURNS);
    await engine.assemble({ sessionId: "s1", messages: archived });
    expect(engine.getStore().getMessageCount("s1")).toBe(archived.length);

    const suffix = archived.slice(-4);
    const reads = trackArchiveReads();
    const result = await engine.assemble({ sessionId: "s1", messages: suffix });

    expect(reads.log.calls).toBe(1);
    expect(reads.log.rows).toBe(archived.length);
    expect(result.messages).toEqual(suffix);
    expect(engine.getStore().getMessageCount("s1")).toBe(archived.length);
  });

  it("keeps the rows an insert returned instead of rereading the archive", async () => {
    const archived = conversation(ARCHIVE_TURNS);
    await engine.assemble({ sessionId: "s1", messages: archived });

    const extended: AgentMessage[] = [
      ...archived.slice(-4),
      { role: "user", content: `question ${ARCHIVE_TURNS + 1}` },
      { role: "assistant", content: `answer ${ARCHIVE_TURNS + 1}` },
    ];
    const reads = trackArchiveReads();
    const result = await engine.assemble({ sessionId: "s1", messages: extended });

    expect(reads.log.calls).toBe(1);
    expect(reads.log.rows).toBe(archived.length);
    expect(result.messages).toEqual(extended);

    reads.stop();
    const store = engine.getStore();
    expect(store.getMessageCount("s1")).toBe(archived.length + 2);
    expect(store.getMessages("s1").map((message) => message.content)).toEqual([
      ...archived.slice(0, -4).map((message) => message.content as string),
      ...extended.map((message) => message.content as string),
    ]);
  });

  it("reads the archive once per afterTurn with and without a live window", async () => {
    const archived = conversation(6);
    engine.setSummarizeFn(async () => "saved summary");
    await engine.assemble({ sessionId: "s1", messages: archived });

    const withWindow = trackArchiveReads();
    await engine.afterTurn({ sessionId: "s1", messages: archived });
    expect(withWindow.log.calls).toBe(1);
    expect(withWindow.log.rows).toBe(archived.length);
    withWindow.stop();

    const withoutWindow = trackArchiveReads();
    await engine.afterTurn({ sessionId: "s1" });
    expect(withoutWindow.log.calls).toBe(1);
    expect(withoutWindow.log.rows).toBe(archived.length);
    expect(engine.getStore().getMessageCount("s1")).toBe(archived.length);
  });

  it("reads the archive once when assembling a stored session", async () => {
    const archived = conversation(6);
    await engine.assemble({ sessionId: "s1", messages: archived });

    const reads = trackArchiveReads();
    const result = await engine.assemble({ sessionId: "s1", messages: [] });

    expect(reads.log.calls).toBe(1);
    expect(reads.log.rows).toBe(archived.length);
    // Six archived turns: one summary block plus the three fresh tail turns.
    expect(result.messages).toHaveLength(7);
    expect(String(result.messages[0].content)).toContain("[summary]");
    expect(result.messages.slice(1).map((message) => message.content)).toEqual(
      archived.slice(-6).map((message) => message.content),
    );
  });

  it("keeps durable summary anchors bound through the shared snapshot", async () => {
    const archived = conversation(6);
    engine.setSummarizeFn(async () => "saved summary");
    await engine.assemble({ sessionId: "s1", messages: archived });
    const store = engine.getStore();
    const summaries = store.getTurnSummaries("s1");
    expect(summaries.length).toBeGreaterThan(0);

    const suffix = archived.slice(-4);
    const reads = trackArchiveReads();
    const result = await engine.assemble({ sessionId: "s1", messages: suffix });

    expect(reads.log.calls).toBe(1);
    expect(result.messages).toEqual(suffix);
    expect(store.getTurnSummaries("s1")).toEqual(summaries);
    expect(store.getMessageCount("s1")).toBe(archived.length);
  });
});
