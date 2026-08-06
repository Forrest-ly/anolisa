#!/usr/bin/env node
/**
 * Unit tests for the OpenClaw anchorRtkPrefix helper.
 *
 * Covers the same case matrix as test_rewrite_hook.py:
 *   - wrapper position (sudo)
 *   - env assignments
 *   - single & connective
 *   - quoted patterns left intact
 *   - unquoted globs preserved
 *   - fd redirections preserved
 *   - command substitutions preserved
 *   - multiple pipeline segments
 *
 * These functions are duplicated here from index.ts because the plugin does
 * not export them (it is a self-contained OpenClaw plugin).  The logic under
 * test is pure text manipulation with no I/O, so duplication is acceptable
 * and avoids introducing a build step into the test runner.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

// ---- Inline copy of the anchor helpers from openclaw/index.ts ---------------
// Keep in sync with adapters/tokenless/openclaw/index.ts.

const SEGMENT_OPS = new Set(["&&", "||", ";", "|", "&"]);

function isEnvAssignment(token) {
  const eq = token.indexOf("=");
  if (eq <= 0) return false;
  const name = token.slice(0, eq);
  if (!/^[A-Za-z_]/.test(name)) return false;
  return /^[A-Za-z0-9_]+$/.test(name);
}

function shellTokenize(cmd) {
  const tokens = [];
  let i = 0;
  const n = cmd.length;
  while (i < n) {
    while (i < n && (cmd[i] === " " || cmd[i] === "\t")) i++;
    if (i >= n) break;
    let tok = "";
    while (i < n && cmd[i] !== " " && cmd[i] !== "\t") {
      const ch = cmd[i];
      if (ch === "'") {
        const end = cmd.indexOf("'", i + 1);
        if (end === -1) return null;
        tok += cmd.slice(i, end + 1);
        i = end + 1;
      } else if (ch === '"') {
        const end = cmd.indexOf('"', i + 1);
        if (end === -1) return null;
        tok += cmd.slice(i, end + 1);
        i = end + 1;
      } else {
        tok += ch;
        i++;
      }
    }
    if (tok) tokens.push(tok);
  }
  return tokens;
}

function anchorRtkPrefix(rewritten, resolvedRtkPath) {
  const tokens = shellTokenize(rewritten);
  if (!tokens) return rewritten;
  const needsQuote = /[ \t'"\\$`!*?{}[\]|;&<>()#]/.test(resolvedRtkPath);
  const quoted = needsQuote ? `'${resolvedRtkPath}'` : resolvedRtkPath;
  const result = [...tokens];
  let wrapped = false;
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (SEGMENT_OPS.has(token)) { wrapped = false; continue; }
    if (isEnvAssignment(token)) continue;
    if (!wrapped && token === "rtk") { result[i] = quoted; wrapped = true; }
  }
  return result.join(" ");
}

// ---- Tests ------------------------------------------------------------------

const RTK = "/home/user/.local/share/anolisa/tokenless/rtk";

test("simple single command", () => {
  assert.equal(
    anchorRtkPrefix("rtk grep foo bar", RTK),
    `${RTK} grep foo bar`,
  );
});

test("multiple pipeline segments separated by &&", () => {
  assert.equal(
    anchorRtkPrefix("rtk grep --cached foo && rtk git status", RTK),
    `${RTK} grep --cached foo && ${RTK} git status`,
  );
});

test("wrapper before rtk (sudo)", () => {
  assert.equal(
    anchorRtkPrefix("sudo rtk git status", RTK),
    `sudo ${RTK} git status`,
  );
});

test("leading env assignment", () => {
  assert.equal(
    anchorRtkPrefix("RUST_BACKTRACE=1 rtk cargo test", RTK),
    `RUST_BACKTRACE=1 ${RTK} cargo test`,
  );
});

test("single & connective", () => {
  assert.equal(
    anchorRtkPrefix("git status & rtk grep foo", RTK),
    `git status & ${RTK} grep foo`,
  );
});

test("quoted regex pattern with rtk inside is untouched", () => {
  // The `rtk` inside the single-quoted string must survive byte-for-byte.
  assert.equal(
    anchorRtkPrefix("rtk grep -E 'foo|rtk bar' src/", RTK),
    `${RTK} grep -E 'foo|rtk bar' src/`,
  );
});

test("unquoted glob preserved", () => {
  assert.equal(
    anchorRtkPrefix("rtk grep foo *.txt", RTK),
    `${RTK} grep foo *.txt`,
  );
});

test("hash argument not treated as comment", () => {
  assert.equal(
    anchorRtkPrefix("rtk grep foo #include src/", RTK),
    `${RTK} grep foo #include src/`,
  );
});

test("fd merge token preserved (2>&1)", () => {
  assert.equal(
    anchorRtkPrefix("rtk git log 2>&1 | rtk head", RTK),
    `${RTK} git log 2>&1 | ${RTK} head`,
  );
});

test("fd redirection token preserved (2>/dev/null)", () => {
  assert.equal(
    anchorRtkPrefix("rtk git status 2>/dev/null", RTK),
    `${RTK} git status 2>/dev/null`,
  );
});

test("command substitution preserved $(date)", () => {
  assert.equal(
    anchorRtkPrefix("rtk echo $(date)", RTK),
    `${RTK} echo $(date)`,
  );
});

test("rtk path with spaces is single-quoted", () => {
  const spacedRtk = "/path with spaces/rtk";
  assert.equal(
    anchorRtkPrefix("rtk grep foo", spacedRtk),
    `'${spacedRtk}' grep foo`,
  );
});

test("no rtk token — passthrough unchanged", () => {
  const cmd = "git status && grep foo bar";
  assert.equal(anchorRtkPrefix(cmd, RTK), cmd);
});

test("unparseable input (unmatched quote) — returned untouched", () => {
  const cmd = "rtk grep 'unclosed";
  assert.equal(anchorRtkPrefix(cmd, RTK), cmd);
});
