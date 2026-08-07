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
 *   - escaped double quotes in arguments (regression: shellTokenize P1)
 *   - single quotes in RTK path (regression: quoting P1)
 *   - known-limitation: non-wrapper rtk anchored (regression: TS/Python parity)
 *
 * Imports the production helpers from the compiled plugin build so CI
 * catches any drift between the test expectations and the shipped code.
 * Requires ``make build-openclaw-plugin`` before running.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SEGMENT_OPS,
  isEnvAssignment,
  shellTokenize,
  anchorRtkPrefix,
} from "../adapters/tokenless/openclaw/dist/anchor-helpers.js";

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

// ---- Regression tests (PR #2249 review) ------------------------------------

test("escaped double quote inside double-quoted argument", () => {
  // shellTokenize must skip \" inside double quotes instead of treating
  // the backslash-quote as the closing delimiter (P1 review finding).
  const cmd = 'rtk grep "foo\\"bar" src/';
  const tokens = shellTokenize(cmd);
  assert.notEqual(tokens, null, "tokenize must not return null for escaped quote");
  assert.equal(
    anchorRtkPrefix(cmd, RTK),
    `${RTK} grep "foo\\"bar" src/`,
  );
});

test("escaped backslash before closing double quote", () => {
  // \\\\ inside double quotes: the backslash escapes the next backslash,
  // so the closing quote is the one after the second backslash.
  const cmd = 'rtk echo "path\\\\end" done';
  const tokens = shellTokenize(cmd);
  assert.notEqual(tokens, null);
  assert.deepEqual(tokens, ["rtk", "echo", '"path\\\\end"', "done"]);
});

test("rtk path containing single quote is properly escaped", () => {
  // A path like /home/o'brien/rtk must not produce broken shell quoting.
  const trickyRtk = "/home/o'brien/rtk";
  const result = anchorRtkPrefix("rtk grep foo", trickyRtk);
  // Expected: '/home/o'\''brien/rtk' grep foo  (standard shell single-quote escaping)
  assert.equal(
    result,
    `'/home/o'\\''brien/rtk' grep foo`,
  );
});

test("known limitation: rtk as non-wrapper argument is anchored", () => {
  // Matches Python behavior: a bare rtk that appears as a positional
  // argument (not a command) is incorrectly treated as a wrapper.
  // This is accepted because rtk rewrite output never produces such shapes.
  const cmd = "echo rtk done";
  const result = anchorRtkPrefix(cmd, RTK);
  assert.equal(result, `echo ${RTK} done`);
});

test("shellTokenize handles mixed escaped and normal content", () => {
  const cmd = 'rtk grep "normal" "with\\"escape" file';
  const tokens = shellTokenize(cmd);
  assert.notEqual(tokens, null);
  assert.deepEqual(tokens, ["rtk", "grep", '"normal"', '"with\\"escape"', "file"]);
});

test("backslash-escaped quote outside quotes does not start quoted context", () => {
  // Python shlex posix=False treats backslash as escape even outside quotes.
  // rtk grep foo\"bar src/ must tokenize successfully.
  const cmd = 'rtk grep foo\\"bar src/';
  const tokens = shellTokenize(cmd);
  assert.notEqual(tokens, null, "tokenize must not return null for escaped quote outside quotes");
  assert.deepEqual(tokens, ["rtk", "grep", 'foo\\"bar', "src/"]);
  // anchorRtkPrefix must still anchor rtk
  assert.equal(
    anchorRtkPrefix(cmd, RTK),
    `${RTK} grep foo\\"bar src/`,
  );
});
