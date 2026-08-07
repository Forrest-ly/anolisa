// Shared shell-tokenization and RTK anchor helpers.
// Used by the OpenClaw plugin (index.ts) and its unit tests.

export const SEGMENT_OPS = new Set(["&&", "||", ";", "|", "&"]);

export function isEnvAssignment(token: string): boolean {
  const eq = token.indexOf("=");
  if (eq <= 0) return false;
  const name = token.slice(0, eq);
  if (!/^[A-Za-z_]/.test(name)) return false;
  return /^[A-Za-z0-9_]+$/.test(name);
}

/**
 * Tokenize a shell command string without a shell, preserving quoted strings.
 *
 * Splits on whitespace while keeping single- and double-quoted spans intact.
 * Handles backslash-escaped characters inside double quotes (mirrors Python
 * shlex with ``posix=False``).  Does **not** recognize fd redirections, globs,
 * or command substitutions as special tokens — they pass through as ordinary
 * characters within their enclosing whitespace-delimited token.
 *
 * Returns ``null`` when the input contains an unmatched quote.
 */
export function shellTokenize(cmd: string): string[] | null {
  const tokens: string[] = [];
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
        tok += ch;
        i++;
        while (i < n && cmd[i] !== '"') {
          if (cmd[i] === "\\" && i + 1 < n) {
            tok += cmd[i] + cmd[i + 1];
            i += 2;
          } else {
            tok += cmd[i];
            i++;
          }
        }
        if (i >= n) return null;
        tok += cmd[i];
        i++;
      } else {
        tok += ch;
        i++;
      }
    }
    if (tok) tokens.push(tok);
  }
  return tokens;
}

/**
 * Replace bare `rtk` wrapper tokens with the resolved absolute binary path.
 *
 * Ports the Python ``_anchor_rtk_prefix`` logic: swaps the first unquoted
 * ``rtk`` token of each pipeline segment (at command start or right after a
 * connective like ``&&``/``||``/``;``/``|``/``&``, optionally behind env
 * assignments or wrappers like ``sudo``).  Quoted patterns, globs, fd
 * redirections, and command substitutions are never modified.  Unparseable
 * input is returned untouched.
 *
 * Known limitation (matches Python behavior): a bare ``rtk`` token that
 * appears as an argument rather than a command — e.g. ``echo rtk done`` —
 * is incorrectly anchored.  This is accepted because rtk rewrite output
 * never produces such shapes.
 */
export function anchorRtkPrefix(rewritten: string, resolvedRtkPath: string): string {
  const tokens = shellTokenize(rewritten);
  if (!tokens) return rewritten;

  const needsQuote = /[ \t'"\\$`!*?{}[\]|;&<>()#]/.test(resolvedRtkPath);
  const quoted = needsQuote
    ? `'${resolvedRtkPath.replace(/'/g, "'\\''")}'`
    : resolvedRtkPath;

  const result = [...tokens];
  let wrapped = false;
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (SEGMENT_OPS.has(token)) {
      wrapped = false;
      continue;
    }
    if (isEnvAssignment(token)) continue;
    if (!wrapped && token === "rtk") {
      result[i] = quoted;
      wrapped = true;
    }
  }
  return result.join(" ");
}
