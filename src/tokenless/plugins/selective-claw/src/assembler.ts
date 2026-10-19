import type { AgentMessage } from "./openclaw-bridge.js";
import { estimateTokens, estimateTokenUnits } from "./estimate-tokens.js";

export interface AssembleInput {
  messages: AgentMessage[];
  summaries: Map<number, string>;
  tokenBudget: number;
  freshTailTurns: number;
}

export interface AssembleResult {
  messages: AgentMessage[];
  estimatedTokens: number;
  stats: {
    totalTurns: number;
    freshTailTurnCount: number;
    freshTailMessageCount: number;
    /** Summary lines present in the returned context, `[no summary]` previews included. */
    summaryCount: number;
    /** Admitted older turns that had no summary and were rendered as a preview. */
    droppedTurns: number;
    /** Older turns left out of the context because their entry did not fit the budget. */
    omittedTurns: number;
  };
}

/**
 * Raised when the protected recent tail alone does not fit `tokenBudget`.
 *
 * Recent messages are never trimmed — a cut tool call or tool result breaks the
 * pairing the provider validates — so there is no context to return. Callers get
 * an explicit failure instead of an over-budget one; every raw message stays
 * archived and remains available through `expand_turn`.
 */
export class ContextBudgetError extends Error {
  readonly requiredTokens: number;
  readonly tokenBudget: number;
  readonly freshTailTurnCount: number;
  readonly freshTailMessageCount: number;

  constructor(
    requiredTokens: number,
    tokenBudget: number,
    freshTailTurnCount: number,
    freshTailMessageCount: number,
  ) {
    super(
      `recent context needs ${requiredTokens} estimated tokens, above the ${tokenBudget} token budget ` +
        `(${freshTailTurnCount} protected turn(s), ${freshTailMessageCount} message(s)); ` +
        `raise the budget or lower freshTailTurns — no message was trimmed`,
    );
    this.name = "ContextBudgetError";
    this.requiredTokens = requiredTokens;
    this.tokenBudget = tokenBudget;
    this.freshTailTurnCount = freshTailTurnCount;
    this.freshTailMessageCount = freshTailMessageCount;
  }
}

interface Turn {
  turnSeq: number;
  messages: AgentMessage[];
  tokenCount: number;
}

const SUMMARY_ROLE = "user";
const SUMMARY_HEADER = "[summary] Earlier conversation context:";

// estimateTokens() ceils a sum of per-code-point weights and JSON escaping is
// character-local, so the cost of a serialized message can be accumulated one
// segment at a time: measuring JSON.stringify(segment) charges exactly the
// characters the serializer emits. The running total therefore matches
// estimateTokens(JSON.stringify(message)) for the message built from those
// segments, without re-serializing the whole block at every candidate step.
const QUOTE_UNITS = 0.5; // the two ASCII quotes JSON.stringify adds around a string
const SUMMARY_OVERHEAD_UNITS = estimateTokenUnits(
  JSON.stringify({ role: SUMMARY_ROLE, content: "" }),
);

function escapedUnits(text: string): number {
  return estimateTokenUnits(JSON.stringify(text)) - QUOTE_UNITS;
}

const NEWLINE_UNITS = escapedUnits("\n");

function deriveTurns(messages: AgentMessage[]): Turn[] {
  const turns: Turn[] = [];
  let turnSeq = 0;
  let current: Turn | null = null;

  for (const msg of messages) {
    const role = normalizeRole(msg.role);
    if (role === "user" || role === "system") {
      turnSeq++;
      current = { turnSeq, messages: [], tokenCount: 0 };
      turns.push(current);
    }
    if (!current) {
      turnSeq = 1;
      current = { turnSeq, messages: [], tokenCount: 0 };
      turns.push(current);
    }
    const tokens = estimateTokens(JSON.stringify(msg));
    current.messages.push(msg);
    current.tokenCount += tokens;
  }

  return turns;
}

function normalizeRole(role: string): string {
  if (role === "toolResult" || role === "tool_result") return "tool";
  return role;
}

function summaryLine(turn: Turn, summary: string): string {
  return `Turn ${turn.turnSeq}: ${summary}`;
}

function previewLine(turn: Turn): string {
  const preview = turn.messages.map((m) => extractText(m).slice(0, 60)).join(" | ");
  return `Turn ${turn.turnSeq}: [no summary] ${preview}...`;
}

export class Assembler {
  constructor(private defaultFreshTailTurns: number = 3) {}

  assemble(input: AssembleInput): AssembleResult {
    const freshTailTurns = input.freshTailTurns ?? this.defaultFreshTailTurns;
    const tokenBudget = input.tokenBudget;
    const turns = deriveTurns(input.messages);

    if (turns.length === 0) {
      return {
        messages: [],
        estimatedTokens: 0,
        stats: {
          totalTurns: 0,
          freshTailTurnCount: 0,
          freshTailMessageCount: 0,
          summaryCount: 0,
          droppedTurns: 0,
          omittedTurns: 0,
        },
      };
    }

    // A non-positive freshTailTurns keeps every turn verbatim (slice(-0) semantics).
    const tail = freshTailTurns > 0 ? turns.slice(-freshTailTurns) : turns;
    const older = freshTailTurns > 0 ? turns.slice(0, -freshTailTurns) : [];
    const tailMessages = tail.flatMap((turn) => turn.messages);
    const tailTokens = tail.reduce((total, turn) => total + turn.tokenCount, 0);

    if (tailTokens > tokenBudget) {
      throw new ContextBudgetError(tailTokens, tokenBudget, tail.length, tailMessages.length);
    }

    // Admit whole summary entries newest-first: the tail is already paid for, and
    // the first entry that does not fit ends the block, so the admitted history is
    // always the most recent contiguous run. Omitted turns stay archived.
    const lines: string[] = [];
    let summaryUnits = SUMMARY_OVERHEAD_UNITS + escapedUnits(SUMMARY_HEADER);
    let droppedTurns = 0;

    for (let index = older.length - 1; index >= 0; index--) {
      const turn = older[index];
      const summary = input.summaries.get(turn.turnSeq);
      const line = summary ? summaryLine(turn, summary) : previewLine(turn);
      const separatorUnits = lines.length === 0 ? 0 : NEWLINE_UNITS;
      const candidateUnits = summaryUnits + separatorUnits + escapedUnits(line);
      if (tailTokens + Math.ceil(candidateUnits) > tokenBudget) break;

      summaryUnits = candidateUnits;
      lines.unshift(line);
      if (!summary) droppedTurns++;
    }

    const summaryMessage: AgentMessage | null =
      lines.length > 0
        ? { role: SUMMARY_ROLE, content: [SUMMARY_HEADER, ...lines].join("\n") }
        : null;
    const messages = summaryMessage ? [summaryMessage, ...tailMessages] : tailMessages;
    const estimatedTokens = messages.reduce(
      (total, message) => total + estimateTokens(JSON.stringify(message)),
      0,
    );

    return {
      messages,
      estimatedTokens,
      stats: {
        totalTurns: turns.length,
        freshTailTurnCount: tail.length,
        freshTailMessageCount: tailMessages.length,
        summaryCount: lines.length,
        droppedTurns,
        omittedTurns: older.length - lines.length,
      },
    };
  }
}

function extractText(msg: AgentMessage): string {
  if (typeof msg.content === "string") return msg.content;
  if (Array.isArray(msg.content)) {
    return msg.content
      .map((block: any) => {
        if (typeof block === "string") return block;
        if (block?.type === "text" && typeof block.text === "string") return block.text;
        return "";
      })
      .join(" ");
  }
  return "";
}
