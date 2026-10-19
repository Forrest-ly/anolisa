import { describe, it, expect } from "vitest";
import { Assembler, ContextBudgetError } from "../src/assembler.js";
import { estimateTokens } from "../src/estimate-tokens.js";
import type { AgentMessage } from "../src/openclaw-bridge.js";

function makeTurns(turnCount: number): AgentMessage[] {
  const msgs: AgentMessage[] = [];
  for (let t = 0; t < turnCount; t++) {
    msgs.push({ role: "user", content: `Turn ${t + 1} question about topic ${t + 1}` });
    msgs.push({ role: "assistant", content: `Turn ${t + 1} answer about topic ${t + 1}` });
  }
  return msgs;
}

const SUMMARY_HEADER = "[summary] Earlier conversation context:";

function estimateMessages(...messages: AgentMessage[]): number {
  return messages.reduce((total, message) => total + estimateTokens(JSON.stringify(message)), 0);
}

function summaryBlock(...lines: string[]): AgentMessage {
  return { role: "user", content: [SUMMARY_HEADER, ...lines].join("\n") };
}

function hasSummaryBlock(messages: AgentMessage[]): boolean {
  return messages.some(
    (message) => typeof message.content === "string" && message.content.includes("[summary]"),
  );
}

describe("Assembler", () => {
  const assembler = new Assembler(3);

  it("returns empty messages for empty conversation", () => {
    const result = assembler.assemble({
      messages: [],
      summaries: new Map(),
      tokenBudget: 10000,
      freshTailTurns: 3,
    });
    expect(result.messages).toHaveLength(0);
    expect(result.estimatedTokens).toBe(0);
  });

  it("returns all messages when turns <= freshTailTurns", () => {
    const msgs = makeTurns(3);
    const result = assembler.assemble({
      messages: msgs,
      summaries: new Map(),
      tokenBudget: 100000,
      freshTailTurns: 3,
    });
    expect(result.messages).toHaveLength(6);
    expect(result.stats.totalTurns).toBe(3);
  });

  it("trims to fresh tail when turns > freshTailTurns", () => {
    const msgs = makeTurns(6);
    const result = assembler.assemble({
      messages: msgs,
      summaries: new Map(),
      tokenBudget: 100000,
      freshTailTurns: 3,
    });
    expect(result.stats.freshTailTurnCount).toBe(3);
    expect(result.stats.freshTailMessageCount).toBe(6);
    expect(result.stats.totalTurns).toBe(6);
  });

  it("inserts summary message when summaries exist", () => {
    const msgs = makeTurns(5);
    const summaries = new Map<number, string>([
      [1, "Discussed topic 1"],
      [2, "Discussed topic 2"],
    ]);

    const result = assembler.assemble({
      messages: msgs,
      summaries,
      tokenBudget: 100000,
      freshTailTurns: 3,
    });

    const summaryMsg = result.messages[0];
    expect(summaryMsg.role).toBe("user");
    expect(summaryMsg.content).toContain("[summary]");
    expect(summaryMsg.content).toContain("Turn 1: Discussed topic 1");
    expect(summaryMsg.content).toContain("Turn 2: Discussed topic 2");
    expect(result.stats.summaryCount).toBe(2);
  });

  it("does not include summary for recent turns in summary block", () => {
    const msgs = makeTurns(4);
    const summaries = new Map<number, string>([
      [1, "Old topic"],
      [4, "Recent topic"],
    ]);

    const result = assembler.assemble({
      messages: msgs,
      summaries,
      tokenBudget: 100000,
      freshTailTurns: 3,
    });

    const summaryMsg = result.messages[0];
    expect(summaryMsg.content).toContain("Turn 1: Old topic");
    expect(summaryMsg.content).not.toContain("Recent topic");
  });

  it("includes the inserted summary message in its token estimate", () => {
    const msgs = makeTurns(4);
    const result = assembler.assemble({
      messages: msgs,
      summaries: new Map([[1, "Discussed topic 1"]]),
      tokenBudget: 100000,
      freshTailTurns: 3,
    });

    const tailTokens = estimateMessages(...msgs.slice(2));
    const summaryTokens = estimateTokens(JSON.stringify(result.messages[0]));
    expect(result.estimatedTokens).toBe(tailTokens + summaryTokens);
    for (let index = 0; index < msgs.length - 2; index++) {
      expect(result.messages[index + 1]).toBe(msgs[index + 2]);
    }
  });

  it("counts fallback previews as part of the returned context", () => {
    const msgs = makeTurns(5);
    const result = assembler.assemble({
      messages: msgs,
      summaries: new Map(),
      tokenBudget: 100000,
      freshTailTurns: 3,
    });

    const tailTokens = msgs.slice(4).reduce(
      (total, message) => total + estimateTokens(JSON.stringify(message)),
      0,
    );
    expect(result.estimatedTokens).toBe(
      tailTokens + estimateTokens(JSON.stringify(result.messages[0])),
    );
  });

  it("shows [no summary] for older turns without summaries", () => {
    const msgs = makeTurns(5);
    const result = assembler.assemble({
      messages: msgs,
      summaries: new Map(),
      tokenBudget: 100000,
      freshTailTurns: 3,
    });

    const hasFallback = result.messages.some((m) =>
      typeof m.content === "string" && m.content.includes("[no summary]")
    );
    expect(hasFallback).toBe(true);
    expect(result.stats.droppedTurns).toBe(2);
  });

  it("fresh tail messages preserve original order", () => {
    const msgs = makeTurns(5);
    const result = assembler.assemble({
      messages: msgs,
      summaries: new Map(),
      tokenBudget: 100000,
      freshTailTurns: 3,
    });

    const tailMsgs = result.messages.filter(
      (m) => typeof m.content !== "string" || !m.content.includes("[summary]")
    );
    expect(tailMsgs[0].content).toContain("Turn 3");
    expect(tailMsgs[tailMsgs.length - 1].content).toContain("Turn 5");
  });

  it.each(["assistant", "toolResult"])("gives a leading %s its own summary turn", (role) => {
    const messages: AgentMessage[] = [
      { role, content: "leading context" },
      { role: "user", content: "new question" },
      { role: "assistant", content: "new answer" },
      { role: "user", content: "latest question" },
    ];
    const result = assembler.assemble({
      messages,
      summaries: new Map([[1, "Leading turn summary"], [2, "New question summary"]]),
      tokenBudget: 100000,
      freshTailTurns: 1,
    });
    expect(result.messages[0].content).toContain("Turn 1: Leading turn summary");
    expect(result.messages[0].content).toContain("Turn 2: New question summary");
    expect(result.messages.slice(1)).toEqual(messages.slice(3));
  });
});

describe("Assembler token budget", () => {
  const assembler = new Assembler(3);

  it("omits a historical summary entry that does not fit the remaining budget", () => {
    const msgs = makeTurns(4);
    const tailTokens = estimateMessages(...msgs.slice(2));
    const tokenBudget = tailTokens + 10;

    const result = assembler.assemble({
      messages: msgs,
      summaries: new Map([[1, "Detailed summary ".repeat(200)]]),
      tokenBudget,
      freshTailTurns: 3,
    });

    expect(result.estimatedTokens).toBeLessThanOrEqual(tokenBudget);
    expect(result.estimatedTokens).toBe(tailTokens);
    expect(result.messages).toHaveLength(msgs.length - 2);
    expect(hasSummaryBlock(result.messages)).toBe(false);
    expect(result.stats.summaryCount).toBe(0);
    expect(result.stats.omittedTurns).toBe(1);
    expect(result.stats.droppedTurns).toBe(0);
  });

  it("admits whole summary entries newest-first within the remaining budget", () => {
    const msgs = makeTurns(6);
    const summaries = new Map<number, string>([
      [1, "oldest entry ".repeat(30)],
      [2, "middle entry"],
      [3, "newest entry"],
    ]);
    const tailTokens = estimateMessages(...msgs.slice(6));
    const tokenBudget = tailTokens + estimateTokens(
      JSON.stringify(summaryBlock("Turn 2: middle entry", "Turn 3: newest entry")),
    );

    const result = assembler.assemble({ messages: msgs, summaries, tokenBudget, freshTailTurns: 3 });

    expect(result.estimatedTokens).toBeLessThanOrEqual(tokenBudget);
    expect(result.stats.summaryCount).toBe(2);
    expect(result.stats.omittedTurns).toBe(1);
    const summary = result.messages[0];
    expect(summary.content).toContain("Turn 2: middle entry");
    expect(summary.content).toContain("Turn 3: newest entry");
    expect(summary.content).not.toContain("oldest entry");
    expect(summary.content.indexOf("Turn 2")).toBeLessThan(summary.content.indexOf("Turn 3"));
  });

  it("stops at the first entry that does not fit instead of trimming it", () => {
    const msgs = makeTurns(6);
    const summaries = new Map<number, string>([
      [1, "oldest entry"],
      [2, "middle entry"],
      [3, "newest entry ".repeat(60)],
    ]);
    const tailTokens = estimateMessages(...msgs.slice(6));
    const tokenBudget = tailTokens + estimateTokens(
      JSON.stringify(summaryBlock("Turn 1: oldest entry", "Turn 2: middle entry")),
    );

    const result = assembler.assemble({ messages: msgs, summaries, tokenBudget, freshTailTurns: 3 });

    expect(result.estimatedTokens).toBe(tailTokens);
    expect(result.stats.summaryCount).toBe(0);
    expect(result.stats.omittedTurns).toBe(3);
    expect(hasSummaryBlock(result.messages)).toBe(false);
  });

  it("keeps tool-call and tool-result payloads intact while trimming history", () => {
    const toolCall: AgentMessage = {
      role: "assistant",
      content: [
        { type: "toolCall", id: "call-9", name: "read_file", arguments: { path: "README.md" } },
      ],
    };
    const toolResult: AgentMessage = {
      role: "toolResult",
      toolCallId: "call-9",
      toolName: "read_file",
      isError: false,
      content: [{ type: "tool_result", output: "line one\nline two 文件内容" }],
    };
    const msgs: AgentMessage[] = [
      ...makeTurns(2),
      { role: "user", content: "read the file" },
      toolCall,
      toolResult,
    ];
    const tail = msgs.slice(4);
    const tokenBudget = estimateMessages(...tail) + 4;

    const result = assembler.assemble({
      messages: msgs,
      summaries: new Map([[1, "first turn"], [2, "second turn"]]),
      tokenBudget,
      freshTailTurns: 1,
    });

    expect(result.estimatedTokens).toBeLessThanOrEqual(tokenBudget);
    expect(result.messages).toHaveLength(tail.length);
    tail.forEach((message, index) => expect(result.messages[index]).toBe(message));
    expect(result.messages[1]).toEqual(toolCall);
    expect(result.messages[2]).toEqual(toolResult);
  });

  it("rejects when the protected recent tail alone exceeds the budget", () => {
    const msgs = makeTurns(2);
    const requiredTokens = estimateMessages(...msgs);
    const tokenBudget = requiredTokens - 1;

    let error: unknown;
    try {
      assembler.assemble({ messages: msgs, summaries: new Map(), tokenBudget, freshTailTurns: 3 });
    } catch (caught) {
      error = caught;
    }

    expect(error).toBeInstanceOf(ContextBudgetError);
    const budgetError = error as ContextBudgetError;
    expect(budgetError.name).toBe("ContextBudgetError");
    expect(budgetError.requiredTokens).toBe(requiredTokens);
    expect(budgetError.tokenBudget).toBe(tokenBudget);
    expect(budgetError.freshTailTurnCount).toBe(2);
    expect(budgetError.freshTailMessageCount).toBe(msgs.length);
    expect(budgetError.message).toContain("no message was trimmed");
  });

  it("rejects an over-budget tail even when older turns have no summaries", () => {
    const msgs = makeTurns(5);
    const tail = msgs.slice(6);
    const tokenBudget = estimateMessages(...tail) - 1;

    expect(() =>
      assembler.assemble({ messages: msgs, summaries: new Map(), tokenBudget, freshTailTurns: 2 }),
    ).toThrow(ContextBudgetError);
  });

  it("stays within budget and never regresses as the budget grows", () => {
    const msgs: AgentMessage[] = [];
    for (let turn = 1; turn <= 6; turn++) {
      msgs.push({ role: "user", content: `问题 ${turn}: "quoted" \\ path\nline\t制表 \u{1F600}` });
      msgs.push({ role: "assistant", content: `回答 ${turn} — emoji \u{1F600} and control \u0007 char` });
    }
    const summaries = new Map<number, string>();
    for (let turn = 1; turn <= 3; turn++) {
      summaries.set(turn, `摘要 ${turn}: "quoted"\n\\path \u{1F600} detail `.repeat(turn * 4));
    }

    const tailTokens = estimateMessages(...msgs.slice(6));
    const fullTokens = estimateMessages(...msgs.slice(6)) + 4000;
    let previousSummaryCount = -1;
    for (let tokenBudget = tailTokens; tokenBudget <= fullTokens; tokenBudget += 37) {
      const result = assembler.assemble({ messages: msgs, summaries, tokenBudget, freshTailTurns: 3 });
      expect(result.estimatedTokens).toBeLessThanOrEqual(tokenBudget);
      expect(result.estimatedTokens).toBe(estimateMessages(...result.messages));
      expect(result.stats.summaryCount).toBeGreaterThanOrEqual(previousSummaryCount);
      previousSummaryCount = result.stats.summaryCount;
      expect(result.stats.summaryCount + result.stats.omittedTurns).toBe(3);
      const tail = msgs.slice(6);
      result.messages.slice(-tail.length).forEach((message, index) => expect(message).toBe(tail[index]));
    }
    expect(previousSummaryCount).toBe(3);
  });
});
