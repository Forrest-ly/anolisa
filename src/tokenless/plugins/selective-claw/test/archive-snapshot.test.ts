import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MockInstance } from "vitest";
import type { DatabaseSync } from "node:sqlite";
import { closeConnection, createConnection } from "../src/db/connection.js";
import { SelectiveContextEngine } from "../src/engine.js";
import { MessageStore } from "../src/store/message-store.js";
import type { MessageRecord } from "../src/store/message-store.js";
import type { AgentMessage } from "../src/openclaw-bridge.js";

function buildHistory(turns: number): AgentMessage[] {
  const messages: AgentMessage[] = [];
  for (let turn = 1; turn <= turns; turn++) {
    messages.push(
      { role: "user", content: `question ${turn}` },
      { role: "assistant", content: `answer ${turn}` },
    );
  }
  return messages;
}

describe("archive snapshot reuse", () => {
  let db: DatabaseSync;
  let engine: SelectiveContextEngine;
  let reads: MockInstance<(sessionId: string) => MessageRecord[]>;

  function materializedRows(): number {
    return reads.mock.results.reduce(
      (total, result) =>
        total + (result.type === "return" ? (result.value as MessageRecord[]).length : 0),
      0,
    );
  }

  beforeEach(() => {
    db = createConnection(":memory:");
    engine = new SelectiveContextEngine(db, {
      freshTailTurns: 3,
      dbPath: ":memory:",
      enabled: true,
    });
    engine.setSummarizeFn(async (text: string) => `Summary: ${text}`);
    reads = vi.spyOn(MessageStore.prototype, "getMessages");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    closeConnection(db);
  });

  it("reads a 1000-message archive once to assemble its last four messages", async () => {
    const messages = buildHistory(500);
    await engine.bootstrap({ sessionId: "big-session", messages });
    reads.mockClear();

    const result = await engine.assemble({
      sessionId: "big-session",
      messages: messages.slice(-4),
      tokenBudget: 1_000_000,
    });

    expect(reads).toHaveBeenCalledTimes(1);
    expect(materializedRows()).toBe(1000);
    expect(result.messages).toEqual(messages.slice(-4));
  });

  it("reads the archive once when assembling from the archive alone", async () => {
    await engine.bootstrap({ sessionId: "small-session", messages: buildHistory(6) });
    reads.mockClear();

    const result = await engine.assemble({
      sessionId: "small-session",
      messages: [],
      tokenBudget: 1_000_000,
    });

    expect(reads).toHaveBeenCalledTimes(1);
    expect(materializedRows()).toBe(12);
    // One summary message plus the three fresh tail turns of two messages each.
    expect(result.messages).toHaveLength(7);
    expect(String(result.messages[0].content)).toContain("[summary] Earlier conversation context:");
    expect(result.messages.slice(1)).toEqual(buildHistory(6).slice(6));
  });

  it("reads the archive once per afterTurn call", async () => {
    await engine.afterTurn({ sessionId: "turn-session", messages: buildHistory(6) });
    expect(reads).toHaveBeenCalledTimes(1);

    reads.mockClear();
    await engine.afterTurn({ sessionId: "turn-session", messages: buildHistory(7) });
    expect(reads).toHaveBeenCalledTimes(1);

    reads.mockClear();
    await engine.afterTurn({ sessionId: "turn-session", messages: [] });
    expect(reads).toHaveBeenCalledTimes(1);
  });

  it("keeps the appended archive and its summary anchors intact", async () => {
    const messages = buildHistory(6);
    await engine.afterTurn({ sessionId: "anchor-session", messages: messages.slice(0, 8) });
    const before = engine.getStore().getMessages("anchor-session");
    expect(before).toHaveLength(8);

    await engine.assemble({ sessionId: "anchor-session", messages, tokenBudget: 1_000_000 });

    const after = engine.getStore().getMessages("anchor-session");
    expect(after).toHaveLength(12);
    expect(after.slice(0, before.length)).toEqual(before);
    expect(after.map((record) => record.seq)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
    expect(after.map((record) => record.turnSeq)).toEqual([1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6]);
    expect(engine.getStore().getTurnSummaries("anchor-session").map((entry) => entry.turnSeq))
      .toEqual([1, 2, 3]);
  });

  it("does not duplicate the archive when the same window is assembled twice", async () => {
    const messages = buildHistory(6);
    await engine.assemble({ sessionId: "replay-session", messages, tokenBudget: 1_000_000 });
    const first = engine.getStore().getMessages("replay-session");

    reads.mockClear();
    await engine.assemble({ sessionId: "replay-session", messages, tokenBudget: 1_000_000 });

    expect(reads).toHaveBeenCalledTimes(1);
    expect(engine.getStore().getMessages("replay-session")).toEqual(first);
    expect(first).toHaveLength(12);
  });
});
