import { describe, it, expect } from "vitest";
import {
  estimateTokens,
  estimateTokenUnits,
  truncateTextToEstimatedTokens,
} from "../src/estimate-tokens.js";

describe("estimateTokens", () => {
  it("estimates ASCII text at ~0.25 tokens per char", () => {
    const result = estimateTokens("hello world");
    expect(result).toBe(3);
  });

  it("estimates CJK text at ~1.5 tokens per char", () => {
    const result = estimateTokens("你好世界");
    expect(result).toBe(6);
  });

  it("returns 0 for empty string", () => {
    expect(estimateTokens("")).toBe(0);
  });
});

describe("estimateTokenUnits", () => {
  it("returns the un-rounded sum estimateTokens ceils", () => {
    expect(estimateTokenUnits("hello world")).toBe(2.75);
    expect(estimateTokens("hello world")).toBe(Math.ceil(estimateTokenUnits("hello world")));
  });

  it("is additive over concatenation so segments can be summed", () => {
    const left = "摘要 \"quoted\"\n\t";
    const right = "path\\to\\file \u{1F600}\u0007";
    expect(estimateTokenUnits(left + right)).toBe(
      estimateTokenUnits(left) + estimateTokenUnits(right),
    );
  });
});

describe("truncateTextToEstimatedTokens", () => {
  it("truncates to fit within token limit", () => {
    const text = "a".repeat(100);
    const truncated = truncateTextToEstimatedTokens(text, 10);
    expect(estimateTokens(truncated)).toBeLessThanOrEqual(10);
    expect(truncated.length).toBe(40);
  });

  it("returns full text when under limit", () => {
    const text = "short";
    expect(truncateTextToEstimatedTokens(text, 100)).toBe(text);
  });
});
