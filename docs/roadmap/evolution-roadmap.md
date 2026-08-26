# Tokenless Evolution Roadmap

[中文版](evolution-roadmap_zh.md)

Canonical reference for the evolution of the Tokenless unified compression
pipeline. The section numbers cited across the crates (`§4.1`–`§6`), the
design principles, and the milestone markers (`M1`, `M4`) refer to this
document. It consolidates the roadmap as encoded in the shipped crates and
the merged implementation PRs; status is current as of Tokenless 0.7.13.

## Goal

One shared Rust compression pipeline serving the CLI hooks, the in-process
Runtime, and the framework adapters. A single versioned compatibility
boundary — protocol v1 — replaces adapter-specific payloads: an adapter
copies only the model-visible value into a `CompressionRequest` and gets
back the final content plus the decision facts it needs to build its
host-specific envelope. UI or business objects that must remain unmodified
never enter the protocol.

Compression stays an optional optimization throughout: no pipeline failure
may fail the request, and every non-applied outcome emits the original
content unchanged.

## Design principles

The principles are numbered as the shipped code cites them; numbers without
a shipped citation are intentionally not restated here.

- **Principle 2 — Route by content, constrain by seam.** A compressor is a
  candidate only when it supports the detected content type, runs at the
  request's seam, and every capability it requires is declared by the
  adapter. Response-shaping compressors never reach hosts that cannot
  replace model-visible output; retrievable-lossy compressors never reach
  hosts without a retrieve tool.
- **Principle 3 — Staged escalation.** The ladder is lossless, then
  retrievable-lossy, then bounded truncation. Escalation happens only while
  the configured size policy is unmet, at most one compressor per lossy
  stage; a specialized lossy decision is final.
- **Principle 5 — Explicit reversibility claims.** Every applied
  transformation reports its recovery state — `lossless`, `retrievable`, or
  `unrecoverable` — and required-reversible mode rejects unrecoverable
  candidates outright.
- **Principle 6 — Fail-open, bounded diagnostics.** A failing compressor
  never fails the request; the first failure is kept as a diagnostic bounded
  to `DIAGNOSTIC_MAX_BYTES` (4 KiB), and `output` always carries exactly
  what the adapter must emit.
- **Principle 7 — Static registration.** The registry is a `const` slice
  assembled at compile time; dynamic plugins and configuration-driven
  loading are out of scope. Entries land together with the compressor that
  implements them, never speculatively.

## Architecture (§4)

### §4.1 Protocol v1 boundary

`tokenless-protocol` defines the compatibility boundary:
`CompressionRequest` / `CompressionResponse` with `protocol_version`, the
`Seam`, `Capabilities`, `Disposition`, and `Reversibility` types.
Compatibility rules:

- Readers ignore unknown fields within a supported major version, so
  optional fields may be added without a version bump.
- An incompatible shape requires a new `protocol_version`, never a parallel
  adapter-specific payload.
- `from_json` validates the version before the shape, so a future version
  reports `UnsupportedVersion` instead of a misleading shape error.

### §4.2 Content detection and registry routing

`tokenless-pipeline` carries the content taxonomy (`json_records`,
`search_results`, `build_log`, `stack_trace`, `diff`, `html`, `tabular`,
`source_code`, `plain_text`, `unknown`), the deterministic bounded-cost
detector, and the compile-time registry with the principle-2 routing
filter.

Detection is a pure function of the content, inspecting at most 64 KiB /
200 lines and never fully parsing any format; expensive parsing stays
inside the selected compressor. Checks run from the most distinctive shape
to the most general, and detection is conservative by design: HTML is only
a document that starts as one, source code needs a shebang or several
declaration-keyword lines, and binary-like input is `unknown` (milestone
M4 policy: ambiguous fragments are not classified).

### §4.3 Staged execution and end-to-end arbitration

`tokenless_pipeline::run` takes a request through detection, routing, and
the escalation ladder, then compares the original and the final candidate
once. A candidate that does not remove normalized tokens, violates required
reversibility, or exceeds the overall timeout budget is rejected as a
whole; its newly created Stash writes are rolled back by `(key,
generation)` and the original content is emitted unchanged.

### §4.5 Adapter boundary

Adapters own their private host contracts. Only the model-visible value is
copied into the request; the response carries the final content plus the
decision facts (`disposition`, token counts, `compressor_chain`,
`stash_keys`, bounded `diagnostic`) the adapter needs to build its host
envelope. Adapters need no local fallback logic: `output` is always
emittable.

### §4.6 Seams

Four interception points in the agent loop: `before_model` (e.g. schema
publication), `pre_tool` (e.g. command rewrite), `post_tool` (the primary
compression seam), and `proxy` (a frontend observing model traffic). Only
Stash keys of committed, applied results appear in a response; rolled-back
candidates never leak keys.

## Decisions and contracts (§5)

### §5.1 Token counter decision

All token counts in protocol v1 use the character-class heuristic
`heuristic-v1` (CJK ≈ 1 token per char, other ≈ 1 token per 4 chars),
implemented once in `tokenless-stats` — not a provider tokenizer. Counts
are normalized tokens for arbitration and attribution, not billing
estimates. Any change to the estimator's character classes or ratios
requires a new counter ID, and rows produced under different IDs must never
be merged into one series without an explicit per-counter breakdown.

### §5.2 Routing contract

Unknown or ambiguous content routes to passthrough. Detection routes only
record-shaped JSON (`{...}` / `[...]`) to the JSON cleanup; scalar roots
pass through unchanged. Misclassification degrades to the fail-open
passthrough path by design.

### §5.3 Response cleanup behind the pipeline

The pre-existing JSON response cleanup is registered as `RESPONSE_CLEANUP`
(content `json_records`, seam `post_tool`, stage retrievable-lossy, cost
moderate, requires `replace_output`) and the shared path behind the CLI
`compress-response` command, `TokenlessRuntime::compress_response`, and the
Python binding routes through `tokenless_pipeline::run`. One overall
timeout budget (10 s in-process) guards detection and all stages; on expiry
the original is returned and Stash writes are rolled back. Reversibility is
claimed from what actually happened: no truncation → lossless, all
truncations stashed → retrievable, otherwise → unrecoverable.

### §5.4 Single external-hook entry point

The four decisions previously duplicated across the common Python hooks and
the CLI subcommands — JSON detection, tool threshold selection, TOON
selection, and final size acceptance — move into one shared seam router:

- a protocol-v1 `tokenless compress` subcommand (stdin
  `CompressionRequest` → stdout `CompressionResponse`) and an in-process
  `TokenlessRuntime::compress`, both routed through the same entry point;
- external hooks become envelope-only adapters that build one request,
  spawn at most one `tokenless` subprocess, and translate the response into
  their host's envelope;
- adapter contract fixtures cover the five behavior classes (passthrough /
  replacement / no-savings / timeout / malformed) per migrated agent;
- new routing behavior is gated by a runtime configuration toggle, default
  off, introduced with the wiring change.

Status: in review (PR #2844), migrating the common Python hooks
(`compress_response_hook.py`, `compress_schema_hook.py`) first; codex /
hermes / openclaw / dsh / SDK adapters keep their current paths until
migrated.

### §5.5 Statistics migration

Attribution columns land in the statistics schema, and the legacy dry-run
measurement channel (`CompressResult.compressed_output`, which records the
predicted candidate text) is replaced by measured numbers and removed.
Attribution reaches statistics with the request instead of separately.

### §5.6 Shared vocabulary and parity

CLI, Runtime, and language bindings share one set of disposition names and
wire strings (the protocol `Disposition` enum), and all counting goes
through the same `heuristic-v1` estimator, keeping every arbitration on
identical numbers. The milestone M1 exit gate requires CLI and Runtime to
agree on this vocabulary; behavior parity is asserted across the five
behavior classes for every migrated agent.

## §6 Compressor pack

New content-specific compressors join the pipeline by implementing the
`Compressor` trait against a registry entry. Planned directions include a
`SchemaCompressor` for the schema seam. Per principle 7, entries land
together with the compressor that implements them, never speculatively.

## Milestone markers

- **M1** — exit gate: CLI and Runtime agree on the shared disposition
  vocabulary (§5.6). Met when response compression moved behind the
  registry and the Runtime's pre-protocol disposition enum was retired.
- **M4** — conservative detection policy: ambiguous fragments are not
  classified (§4.2). Encoded in the shipped detector.

## Implementation status

| Section | Deliverable | Status | Reference |
|---------|-------------|--------|-----------|
| §4.1 | `tokenless-protocol` v1 types and wire contract | Shipped in 0.7.13 | PR #2783 |
| §4.2 | Content taxonomy, detector, static registry | Shipped in 0.7.13 | PR #2788 |
| §4.3 | Staged execution and end-to-end arbitration | Shipped in 0.7.13 | PR #2799 |
| §5.3 | Response cleanup routed through the pipeline | Shipped in 0.7.13 | PR #2816 |
| §5.4 | Unified external-hook entry, contract fixtures, runtime toggle | In review | PR #2844 |
| §5.5 | Statistics attribution migration | Planned | follows §5.4 |
| §6 | New compressor pack (incl. schema seam) | Planned | — |

Legacy `compress-response` / `compress-schema` / `compress-toon`
subcommands and the pre-pipeline Python helpers stay until every consumer
has migrated to the unified entry; their removal is a dedicated later step.
