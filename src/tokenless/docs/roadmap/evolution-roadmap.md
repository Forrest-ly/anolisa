# Tokenless Evolution Roadmap — The Unified Compression Pipeline

[中文版](evolution-roadmap_zh.md)

> **Status: living document.** This roadmap consolidates the evolution plan that
> the tokenless crates already cite by section number (§4.x / §5.x). Where
> implementation exists, the code is the source of truth; the protocol contract
> tests in `crates/tokenless-protocol/src/tests/protocol_tests.rs` are the drift
> guard between this document and the wire types.

## 1. Why a unified pipeline

Tokenless compresses model-visible content (tool results, published tool
schemas) before it reaches the model, so agents spend tokens on task content
instead of boilerplate. Before this roadmap, each integration point made its
own compression decisions:

- the Python hooks, the CLI subcommands (`compress-response`,
  `compress-schema`, `compress-toon`), and the in-process runtime each
  re-implemented JSON detection, threshold selection, and final-size
  acceptance;
- hosts that cannot replace model-visible output, or cannot retrieve stashed
  content, had no uniform way to declare that;
- dispositions and statistics were not comparable across entry points.

The goal is one unified compression pipeline:

1. one **protocol boundary** between adapters and the pipeline (§4.1);
2. one **content taxonomy**, one deterministic **detector**, and one static
   **compressor registry** (§4.2);
3. one **staged execution engine** with end-to-end arbitration (§4.3);
4. one **external entry point** (`tokenless compress` subcommand and the
   in-process `TokenlessRuntime::compress`) shared by every adapter (§5.4);
5. comparable **dispositions and statistics** across CLI, runtime, and
   language bindings (§5.5–§5.6).

Adapters shrink to envelope translation: they build one protocol request,
route it through the shared entry, and emit `output` exactly as returned —
no fallback logic of their own.

## 2. Principles

The implementation cites these principles by number. Where a principle is
encoded, the enforcing code is listed.

| # | Principle | Enforced by |
|---|-----------|-------------|
| 2 | **Route by content, constrain by seam.** A compressor is a candidate only when it supports the detected content type, runs at the request's seam, and every capability it requires is declared by the adapter. | `CompressorSpec::matches` / `candidates` (tokenless-pipeline) |
| 3 | **Staged escalation.** Lossless runs before retrievable-lossy, retrievable-lossy before bounded truncation; escalate only while the configured size policy is unmet. | `Stage` ordering + `run` (tokenless-pipeline) |
| 5 | **Explicit reversibility.** Every applied outcome states its recovery state — lossless, retrievable, or unrecoverable; required-reversible mode rejects unrecoverable candidates outright. | `Reversibility` + end-to-end arbitration |
| 6 | **Fail-open, bounded diagnostics.** Compression is an optional optimization: a failing step never fails the request, the first failure is kept as a diagnostic bounded to 4096 bytes, and `output` always holds exactly what the adapter must emit. | `Disposition::Error`, `DIAGNOSTIC_MAX_BYTES`, fail-open contract |
| 7 | **Static registration.** The registry is a compile-time `const` slice; dynamic plugin loading and configuration-driven registration are out of scope. | `REGISTRY` (tokenless-pipeline) |

Principles 1 and 4 belong to the draft numbering but are not yet pinned by any
implementation citation; they are deliberately left unstated here until the
steps that encode them land.

## 3. Target architecture (§4)

### §4.1 Protocol v1 — the compatibility boundary

`tokenless-protocol` defines protocol v1: the boundary between
agent-specific adapters and the shared compression pipeline. It is
deliberately not an OpenAI or Anthropic request shape.

- `CompressionRequest` carries only the model-visible content plus the
  attribution and capability facts the pipeline needs: `protocol_version`,
  `content`, `agent_id`, optional `session_id` / `tool_use_id` /
  `tool_name`, `seam`, and `capabilities`.
- `CompressionResponse` carries the final content plus the decision the
  adapter needs to build its host-specific envelope: `output`,
  `disposition`, optional `content_type`, `compressor_chain`,
  `reversibility`, `before_tokens` / `after_tokens`, `stash_keys`,
  `tokenizer_id`, and an optional bounded `diagnostic`.

Wire example (request):

```json
{
  "protocol_version": 1,
  "content": "...",
  "agent_id": "claude-code",
  "session_id": "...",
  "tool_use_id": "...",
  "tool_name": "Bash",
  "seam": "post_tool",
  "capabilities": {
    "replace_output": true,
    "publish_retrieve_tool": true
  }
}
```

Wire example (response, illustrative compressor IDs):

```json
{
  "protocol_version": 1,
  "output": "...",
  "disposition": "applied",
  "content_type": "build_log",
  "compressor_chain": ["terminal-cleanup", "build-log"],
  "reversibility": "retrievable",
  "before_tokens": 1200,
  "after_tokens": 340,
  "stash_keys": ["0123456789abcdef01234567"]
}
```

Compatibility rules:

- readers ignore unknown fields within a supported major protocol version,
  so optional fields may be added without a version bump;
- an incompatible shape requires a new `protocol_version`, never a parallel
  adapter-specific payload;
- the version is checked before the full parse, so a future version is
  reported as `UnsupportedVersion` rather than a shape error;
- **fail-open contract**: `output` always holds exactly what the adapter
  must emit. On every non-`applied` disposition it is the original
  model-visible content, so adapters never need fallback logic of their own.

The disposition vocabulary is shared by every frontend: `applied`, `dry_run`,
`passthrough`, `no_savings`, `reversibility_unavailable`, `timeout`, `error`.

### §4.2 Content taxonomy, detector, registry

Detection is a pure, deterministic function of the content with bounded
work: only the first 64 KiB / 200 lines are inspected (plus a bounded tail
for the JSON bracket sniff), and no format is fully parsed — expensive
parsing belongs inside the selected compressor, not in a detector that runs
on every input. When no cheap signal is decisive, the detector prefers the
more general class, and ultimately `plain_text` or `unknown`, which the
pipeline routes to passthrough until a conservative fallback compressor
exists.

First taxonomy (stable wire values): `json_records`, `search_results`,
`build_log`, `stack_trace`, `diff`, `html`, `tabular`, `source_code`,
`plain_text`, `unknown`. Checks run from the most distinctive shape to the
most general one, so a build log that merely *contains* a traceback stays
`build_log` while content that *starts as* a traceback is `stack_trace`.
Ambiguous fragments are not classified as HTML (**M4 policy**).

Routing by detected content is the contract: record-shaped JSON
(`{...}`/`[...]`) routes to the JSON cleanup; a scalar JSON root passes
through untouched and waits for its own compressor rather than being
truncated by a compressor that does not understand it.

The compressor registry is static (principle 7): a compile-time `const`
slice of `CompressorSpec { id, content_types, seams, required_capabilities,
stage, cost_class }`. Entries are appended together with the compressor that
implements them, never speculatively. `candidates` filters the registry by
the principle-2 rule — content type ∩ seam ∩ declared capabilities.

### §4.3 Staged execution and end-to-end arbitration

`tokenless_pipeline::run` takes one request through detection, routing, and
the escalation ladder (principle 3):

1. **steps 1–2** — every applicable lossless transformation runs; a result
   that does not remove normalized tokens is dropped outright;
2. **steps 3–4** — escalate only while the configured size policy is unmet,
   one compressor per stage: at most one content-specific retrievable-lossy
   compressor, then at most one bounded truncation. A specialized lossy
   decision is final — no second lossy compressor and no generic lossy
   fallback runs after it; only the bounded truncation stage (step 4, the
   ladder's last resort) may still follow. A stash write whose marker a
   later stage cuts out of the output is rolled back rather than committed;
3. **steps 5–6** — arbitration is end-to-end: the original and the final
   candidate are compared once. A candidate that does not remove normalized
   tokens, violates required reversibility, or exceeds the overall timeout
   budget is rejected as a whole — its newly created stash keys are rolled
   back and the original content is emitted unchanged.

One `PipelineConfig` carries the arbitration policy for a call: the overall
`timeout` budget (checked at stage boundaries), `max_tokens` (the size
policy; `None` never escalates beyond lossless), `require_reversibility`,
and `dry_run`.

### §4.5 Adapters own their private host contracts

Adapters keep their host-specific envelopes and business objects to
themselves; only the model-visible value is copied into a protocol request.
UI or business objects that must remain unmodified never enter the
protocol.

### §4.6 Seams

The `seam` field records where in the agent loop the content was
intercepted: `before_model` (e.g. schema publication), `pre_tool` (e.g.
command rewrite), `post_tool` (tool output after execution — the primary
compression seam), and `proxy` (a proxy frontend observing model traffic).
Stash keys are reported only for applied, emitted results: rolled-back
candidates never leak keys.

## 4. Migration plan (§5)

### §5.1 Token counter identity

All token counts in protocol v1 use one counter identity: `heuristic-v1`,
the character-class heuristic implemented by `tokenless-stats` (CJK ≈ 1
token per character, other content ≈ 1 token per 4 characters) — not a
provider tokenizer. Counts are normalized tokens for arbitration and
attribution, not billing estimates. Any change to the estimator's character
classes or ratios requires a new `tokenizer_id`; rows and responses produced
under different IDs must never be merged into one series without an explicit
per-counter breakdown. A payload missing the field reads as `heuristic-v1`:
the heuristic estimator is the only counter that ever shipped before the
field existed.

### §5.2–§5.3 The shared pipeline crate and its first compressor

`tokenless-pipeline` carries the pieces that sit between the protocol
boundary and the compressors themselves: the taxonomy and detector (§4.2),
the registry and capability filter (§4.2), and the staged execution engine
with end-to-end arbitration (§4.3).

The first production compressor is the existing JSON response cleanup
(`response-cleanup`), moved behind the pipeline's `Compressor` trait:
structural cleanup and bounded truncation of record-shaped JSON, with
truncated content stashed and marker-referenced when a stash store is
attached. Its executable implementation lives with the runtime, which owns
the stash store and the per-call configuration; its `spec()` returns a
reference to the registry entry so the two cannot drift. The runtime honors
the one-budget contract: a single overall pipeline budget (10 s in-process)
replaces per-step timeouts, and timeout is fail-open.

Further compressors join the registry the same way — appended together with
their implementation, never speculatively.

### §5.4 One external entry point

All external hooks converge on a single entry: a protocol-v1
`tokenless compress` subcommand (stdin `CompressionRequest` → stdout
`CompressionResponse`) and the in-process `TokenlessRuntime::compress`, both
routed through one shared seam router in the runtime. The decisions
previously duplicated across the common Python hooks and the CLI
subcommands — JSON detection (including string-unwrap semantics), tool
threshold selection, TOON selection, and the final size acceptance — happen
exactly once, in Rust.

The Python hooks become envelope-only adapters: each builds one request,
spawns at most one tokenless subprocess, and translates the response into
its host's envelope. Matchers, hooks configuration, and extension manifests
stay untouched.

### §5.5 Statistics migration

Statistics follow the winner: at most one row per invocation, keyed by the
winning operation (e.g. TOON win → `compress-toon`, cleanup win →
`compress-response`, schema → `compress-schema`), with savings re-based
from the original input to the final output. The legacy measurement side
channels (the runtime's separate `compressed_output` candidate field and the
pre-protocol disposition types) are retired with this migration.

### §5.6 Disposition parity and adapter contract fixtures

CLI and runtime must agree on one disposition vocabulary — the shared
`Disposition` enum from protocol v1 (**M1 exit gate**) — and on one canonical
passthrough shape, so dispositions stay comparable across entry points.
Adapter behavior is pinned by contract fixtures covering five classes —
passthrough, replacement, no-savings, timeout, malformed — exercised against
a mock protocol binary for every migrated agent, plus a golden parity suite
that replays a shared corpus through the rewritten hooks against envelopes
captured from the pre-rewrite hooks.

### Legacy removal

The legacy `compress-response` / `compress-schema` / `compress-toon`
subcommands and the old Python helpers stay until the removal step, after
every adapter has converged on the unified entry.

## 5. Implementation status

| Step | Content | Section | Status |
|------|---------|---------|--------|
| Protocol v1 request/response types | `tokenless-protocol` crate | §4.1 | Merged ([#2783](https://github.com/alibaba/anolisa/pull/2783)) |
| Content taxonomy, detector, static registry | `tokenless-pipeline` crate | §4.2 | Merged ([#2788](https://github.com/alibaba/anolisa/pull/2788)) |
| Staged pipeline and end-to-end arbitration | `tokenless-pipeline` crate | §4.3 | Merged ([#2799](https://github.com/alibaba/anolisa/pull/2799)) |
| Response compression behind the registry | `tokenless-runtime` | §5.3 | Merged ([#2816](https://github.com/alibaba/anolisa/pull/2816)) |
| Unified external hook entry + contract fixtures | runtime entry router, envelope-only hooks | §5.4 / §5.6 | Open ([#2844](https://github.com/alibaba/anolisa/pull/2844), PR 6 of the sequence) |
| Statistics attribution rework | one row per invocation, winner-keyed | §5.5 | Planned (roadmap PR 7) |
| Legacy subcommand/helper removal | retire `compress-response` / `compress-schema` / `compress-toon` | — | Planned (roadmap PR 18) |

## 6. Related documents

- [Response compression rules](../response-compression.md) — the seven
  cleanup rules behind the `response-cleanup` compressor
- [Stash and reversible compression](../stash-reversible-compression.md) —
  the stash store and marker retrieval contract
- [Runtime library](../design/runtime-library.md) — the stateful runtime
  API shared by frontends
