import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { DatabaseSync } from "node:sqlite";
import { closeConnection, createConnection } from "../src/db/connection.js";
import { SelectiveContextEngine } from "../src/engine.js";
import type { AgentMessage } from "../src/openclaw-bridge.js";

// Mirrors MAX_CONCURRENT_SUMMARIES in src/engine.ts: the bound is engine-wide
// and deliberately not configurable.
const SUMMARY_BOUND = 4;

interface ProbeState {
  peak: number;
  inFlight: number;
  calls: string[];
}

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

/**
 * Records the peak number of in-flight summarizer calls. Every call yields to
 * the timer queue before settling, so overlapping calls are observable.
 */
function createSummarizer(failing: Set<string> = new Set()) {
  const state: ProbeState = { peak: 0, inFlight: 0, calls: [] };
  const summarize = async (text: string): Promise<string> => {
    state.inFlight += 1;
    state.peak = Math.max(state.peak, state.inFlight);
    state.calls.push(text);
    try {
      await new Promise((resolve) => setTimeout(resolve, 0));
      if (failing.has(text)) throw new Error("provider unavailable");
      return `Summary: ${text}`;
    } finally {
      state.inFlight -= 1;
    }
  };
  return { summarize, state };
}

function turnText(turn: number): string {
  return `user: question ${turn}\nassistant: answer ${turn}`;
}

describe("turn summary concurrency", () => {
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

  it("bounds a 100-turn reopen to the engine-wide summary capacity", async () => {
    const { summarize, state } = createSummarizer();
    engine.setSummarizeFn(summarize);

    await engine.assemble({
      sessionId: "long-session",
      messages: buildHistory(100),
      tokenBudget: 1_000_000,
    });

    expect(state.calls).toHaveLength(97);
    expect(state.peak).toBe(SUMMARY_BOUND);
    expect(state.inFlight).toBe(0);

    const stored = engine.getStore().getTurnSummaries("long-session");
    expect(stored).toHaveLength(97);
    expect(stored[0]).toEqual({ turnSeq: 1, summary: `Summary: ${turnText(1)}` });
    expect(stored[96]).toEqual({ turnSeq: 97, summary: `Summary: ${turnText(97)}` });
  });

  it("shares one capacity between two overlapping sessions", async () => {
    const { summarize, state } = createSummarizer();
    engine.setSummarizeFn(summarize);
    const messages = buildHistory(20);

    await Promise.all([
      engine.assemble({ sessionId: "session-a", messages, tokenBudget: 1_000_000 }),
      engine.assemble({ sessionId: "session-b", messages, tokenBudget: 1_000_000 }),
    ]);

    expect(state.calls).toHaveLength(34);
    expect(state.peak).toBeLessThanOrEqual(SUMMARY_BOUND);
    expect(state.peak).toBeGreaterThan(1);
    expect(engine.getStore().getTurnSummaries("session-a")).toHaveLength(17);
    expect(engine.getStore().getTurnSummaries("session-b")).toHaveLength(17);
  });

  it("releases a rejected slot and still finishes the queued turns", async () => {
    const { summarize, state } = createSummarizer(new Set([turnText(1), turnText(2)]));
    engine.setSummarizeFn(summarize);

    const result = await engine.assemble({
      sessionId: "flaky-session",
      messages: buildHistory(8),
      tokenBudget: 1_000_000,
    });

    expect(state.calls).toHaveLength(5);
    expect(state.peak).toBeLessThanOrEqual(SUMMARY_BOUND);
    expect(state.inFlight).toBe(0);

    // The two rejected turns are not retried and do not block turns 3..5.
    const stored = engine.getStore().getTurnSummaries("flaky-session");
    expect(stored.map((entry) => entry.turnSeq)).toEqual([3, 4, 5]);

    const summaryMessage = String(result.messages[0].content);
    expect(summaryMessage).toContain("Turn 1: [no summary]");
    expect(summaryMessage).toContain("Turn 2: [no summary]");
    expect(summaryMessage).toContain(`Turn 3: Summary: ${turnText(3)}`);
  });

  it("keeps the fresh tail out of the summarizer queue", async () => {
    const { summarize, state } = createSummarizer();
    engine.setSummarizeFn(summarize);

    await engine.afterTurn({ sessionId: "tail-session", messages: buildHistory(3) });

    expect(state.calls).toEqual([]);
    expect(state.peak).toBe(0);
  });
});
