# AgentSecCore V2 Policy and daemon foundations

This workspace slice contains the dependency-light contracts, Policy
Administration Point, first-version PAP daemon protocol, product Policy-template
compiler, protocol-independent Unix-domain-socket service framework, and runnable
foreground process bootstrap, together with the first AgentSight file-deletion
target Adapter, and its independent deployment Client used by later AgentSecCore
V2 work packages. It deliberately contains no durable persistence, Policy runtime,
reconciliation worker, or outbox.

The Rust `agent-sec-cli` exposes all 15 Policy, Scope and Binding CRUD commands through
an explicit daemon socket. Its Cargo package and source directory remain `asc-cli`;
the executable target is `agent-sec-cli`. See the [CLI reference](../../../docs/user-guide/en/agent-security/agent-sec-core/policy-cli.md)
and [CLI acceptance record](../docs/design/POLICY_CLI_ACCEPTANCE_zh.md).

The current crates are:

- `asc-foundation-types`: bounded transport-independent identifiers and revisions.
- `asc-policy-types`: authored Policy and immutable prepared Policy/Scope/Binding
  snapshots, backend-independent IR, and target Adapter contracts.
- `asc-policy-engine`: deterministic `prevent_file_deletion` authoring-template
  compiler with a frozen Canonical Policy IR golden. Other template kinds remain
  explicitly unsupported until their lowering and Adapter evidence are defined.
  The implemented template covers path-entry deletion only; rename, move, and
  other namespace mutations are outside its contract.
- `asc-policy-adapter-agentsight`: deterministic file-deletion and PID-Scope
  translation into an AgentSight/ActPlane plan, with semantic and encoding checks.
  Compiler acceptance belongs to the deployed target, not an embedded compiler.
- `asc-agentsight-client`: health-gated AgentSight apply/delete transport for
  one configured endpoint, with process identity resolution and complete HTTP
  fixtures. It does not depend on a reconciliation framework.
- `asc-policy-target-contracts`: shared, PEP-neutral `TargetBindingAdapter` and
  `TargetDeploymentClient` ports; data lives in `asc-policy-types::target`.
- `asc-pap`: transport-independent current-record Policy/Scope/Binding CRUD with
  monotonic revisions over explicit compiler and repository ports.
- `asc-pap-repository-memory`: explicitly temporary process-local Repository
  adapter used only to keep daemon/PAP integration runnable before durable
  persistence lands; its implementation is outside the current review scope.
- `asc-daemon-protocol`: strict request/response contracts and an explicit
  allowlist for 15 Policy, Scope, and Binding administration methods.
- `asc-daemon-handler`: inbound protocol adapter that decodes daemon requests,
  applies server-owned authorization, routes PAP methods, and projects protocol
  responses without depending on a concrete Repository or compiler.
- `asc-daemon-core`: trusted Principal construction boundary and the
  `PolicyAdministration` application port. `PapService<R, C>` implements this
  port directly, so repository/compiler generics do not leak into dispatch.
- `asc-daemon-service`: bounded UDS admission, one-request framing, kernel peer
  credentials, dispatcher/rejection-encoder injection, connection isolation,
  dispatch cancellation, and controlled drain.
- `asc-daemon-client`: synchronous UDS client preserving complete responses, with
  a single connect/write/read deadline and no retries or local fallback. It uses
  standard-library blocking I/O and `socket2` for bounded connect; neither it nor
  the CLI binary requires a Tokio runtime.
- `asc-cli`: command parsing, typed Policy request construction and Policy output;
  server dependencies are test-only. `commands.rs` registers and dispatches the
  top-level commands; `commands/{policy,scope,binding}.rs` own their arguments and
  request mappings, with pagination and encoding helpers in `commands/common.rs`.
- `asc-daemon`: foreground process and composition root that configures and
  injects concrete adapters into the daemon service.

The crate relationships, acceptance types, executable pass/fail matrix,
compatibility report, direct-consumer evidence, and rollback boundary are recorded
in [`PAP_DAEMON_API_ACCEPTANCE_zh.md`](../docs/design/PAP_DAEMON_API_ACCEPTANCE_zh.md).

## Daemon service boundary

`asc-daemon-service` is a `PARTIAL_MIGRATION` work package. It preserves the V1
one-request-per-connection LF/EOF framing, bounded first-frame read, bounded
connection admission, and socket ownership cleanup. Normal response encoding
belongs to an injected dispatcher; transport rejection encoding belongs to a
separate protocol-only port. Its acceptance type is current-version contract
testing with socket bytes and fake handlers. The V1 Python daemon is discovery
evidence only and is not linked or executed by the Rust runtime.

The service framework does not deserialize a daemon request, generate protocol
request IDs, choose authorization roles, or render protocol errors. The concrete
`asc-daemon-handler::DaemonDispatcher` receives a bounded raw request frame and
owns method routing. Method
allowlist routing is internal to that one dispatcher implementation; it is not a
second service dispatch layer. A separate `RejectionEncoder` receives typed
transport failures and must remain independent of PAP/Repository state.

The PAP request path is:

```text
UDS frame -> DaemonDispatcher -> method metadata authorization
          -> PapHandler -> PolicyAdministration -> PapService<R, C>
```

RPC reuses the domain `PolicyTemplate`, `ScopeSelector`, `PreparedPolicy`,
`PreparedScope`, and `BindingView` types. It does not define result wrappers for
each CRUD operation. `PolicyAdministration` intentionally mirrors the use cases
once: it erases `R`/`C` before dispatch and repeats authorization at the
application boundary; there is no additional `PapServiceAdapter` forwarding
object.

The current bootstrap bounds frame read, application dispatch, rejection
encoding, response write, connection drain, and final Tokio runtime shutdown.
Dispatch timeout releases transport capacity and signals cooperative cancellation;
it cannot forcibly stop an application blocking call that ignores that signal.
The framework also cannot prove that a concrete PAP/Repository avoids global
locks; that remains a required direct-consumer concurrency test at integration.

The current `asc-daemon` executable composes and registers the PAP dispatcher and
protocol rejection encoder from `asc-daemon-handler`. It composes `PapService`
with `PolicyTemplateCompiler`, a
root-managed Principal policy, and an explicitly transitional process-local
Repository. Policy CRUD therefore works during one daemon lifetime, but all
state disappears on restart; this is integration evidence, not durable
persistence or distribution readiness. The process prints that limitation at
startup. It also requires an explicit absolute socket path because
packaging-owned system paths, singleton/stale-socket policy, runtime directory
hardening, and readiness remain later process-integration work.

UID 0 is always a Policy administrator. A deployment operator can add other UIDs
at startup with repeatable `--policy-admin-uid <UID>` options. Omitted means root
only. Configured administrators cannot delegate other UIDs at runtime; that API
still requires root. The allowlist is process-local and must be supplied on each
startup. Configuration-file loading, persistence and management RPCs remain later
work. Authorization does not change OS socket permissions or deployment topology.

Run the independent transport process in the foreground:

```bash
cargo run -p asc-daemon -- serve --socket /absolute/existing-directory/daemon.sock
```

`asc-daemon-handler::DaemonDispatcher` implements `RequestDispatcher` directly
and is injected by the executable composition root together with
`JsonRejectionEncoder`. PAP is one
registered method family inside the dispatcher; the service framework and
rejection path remain independent of PAP, its compiler, and its repository.

## PAP RPC contract

The closed method inventory contains create, update, exact get, bounded list,
and delete for each of Policy, Scope, and Binding. Successful responses are
`{requestId,result}` and failures are `{requestId,error}`. The result is the
domain record itself; list is the sole shared `{items,total}` shape. Exact inputs
and output type names are frozen by
`asc-daemon-protocol/tests/fixtures/pap-methods.json`.

The stateful `asc-daemon-protocol/tests/fixtures/pap-crud-e2e.json` scenario
freezes complete request and response values for all 15 methods, including
Canonical Policy IR, Scope templates, embedded Binding snapshots, revisions,
statuses, and deterministic digests. Server-generated request and resource UUIDs
use named placeholders so the same fixture can assert their format and identity
flow across later requests. A UDS integration E2E always executes the complete
scenario with a server-authorized test principal. The `asc-daemon` bootstrap E2E
also starts the real binary with `--policy-admin-uid` set to the test UID and
executes the complete scenario without root. A separate default-config case
verifies non-root `permission_denied` (or full CRUD when root). CLI process tests
use an in-process daemon service; a combined CLI and daemon binary E2E is deferred.

Binding create/update accepts desired state and returns `PENDING_APPLY`; delete
returns `PENDING_DELETE`. These responses prove PAP acceptance only. They do not
mean target enforcement or deletion completed. LIST is integration-ready but is
not distribution-ready until a server-owned aggregate encoded-byte budget is
passed through Repository, PAP, and transport.

TODO(policy-response-bounds): direct `PreparedPolicy` and `BindingView` mutation
results can exceed the response-frame limit after process-local state has already
changed, while embedded snapshots can also make Binding GET/LIST oversized.
Before a durable Repository or distribution gate, converge the public result and
storage shapes and enforce server-owned encoded-size budgets for mutations,
single-record GET, and LIST; increasing the transport limit alone is not the fix.

## Current-record revision boundary

Policy, Scope, and Binding each retain one current record per stable identity.
Changed writes advance a positive, never-reused revision and atomically replace
the previous current content. An exact GET for an older revision returns
not-found, and LIST returns at most one current record per identity.

Deleting current Policy or Scope content retains its allocation head as a
tombstone, so a later update of the same identity advances rather than reuses a
revision. A `PreparedBinding` embeds complete Policy and Scope snapshots; an
existing Binding therefore remains deterministic after either source record is
updated or deleted. A new Binding can select only a currently retained source
revision. PAP does not expose historical resource-version CRUD; durable
operation/audit history belongs to later work packages.

## Binding spec and lifecycle boundary

`PreparedBinding` is an immutable snapshot. The pair
`(binding_id, binding_revision)` identifies exactly one complete Policy/Scope
snapshot and must never be reused for a different desired-state operation.
Only the current Binding snapshot is retained. Mutable status is deliberately
outside that spec:

- `BindingStatus` contains only the lifecycle state; it carries no duplicated
  Binding ID or revision.
- `BindingView { spec, status }` joins one immutable spec with its status for
  GET/LIST responses.
- `bindingRevision` advances for every accepted, non-idempotent Apply or Delete
  intent, including reapplying identical content after failure or deletion.
- Reconciler claim, retry, completion, and failure transitions do not advance
  the revision.

All legal lifecycle states are shared in `asc-policy-types::binding`, next to
`PreparedBinding`, so PAP, the future outbox, and the future reconciler use one
contract:

| State | Meaning | Written by | Terminal without a new request? |
|---|---|---|---|
| `PENDING_APPLY` | Apply request accepted but not claimed | PAP | no |
| `APPLYING` | Apply work claimed and running | reconciler | no |
| `READY` | referenced spec applied successfully | reconciler | yes, success |
| `APPLY_FAILED` | Apply permanently failed or exhausted retries | reconciler | yes, failure |
| `PENDING_DELETE` | Delete request accepted but not claimed | PAP | no |
| `DELETING` | detach work claimed and running | reconciler | no |
| `DELETED` | detach completed successfully | reconciler | yes, success |
| `DELETE_FAILED` | detach permanently failed or exhausted retries | reconciler | yes, failure |

“Terminal” means that no automatic transition remains. A later user request can
still move lifecycle from a terminal state to a new pending state.

The successful creation path is:

```text
none --CREATE--> PENDING_APPLY --claim--> APPLYING --success--> READY
```

The successful deletion path is:

```text
apply-side state --DELETE--> PENDING_DELETE --claim--> DELETING --success--> DELETED
```

The complete legal transition set is:

| Current | Event | Next | Revision rule |
|---|---|---|---|
| none | CREATE valid spec | `PENDING_APPLY` | allocate revision 1 |
| `PENDING_APPLY`, `APPLYING`, `READY` | UPDATE identical spec | no-op | unchanged |
| `APPLYING`, `DELETING` | UPDATE changed spec | `OPERATION_IN_PROGRESS` | unchanged |
| `DELETING` | UPDATE identical spec | `OPERATION_IN_PROGRESS` | unchanged |
| any other state | UPDATE accepted Apply intent | `PENDING_APPLY` | allocate next revision and replace current record |
| `APPLYING` | DELETE | `OPERATION_IN_PROGRESS` | unchanged |
| `PENDING_APPLY`, `READY`, `APPLY_FAILED`, `DELETE_FAILED` | DELETE | `PENDING_DELETE` | allocate next revision and replace current record |
| `PENDING_DELETE`, `DELETING`, `DELETED` | DELETE | no-op | unchanged |
| `PENDING_APPLY` | worker claim | `APPLYING` | unchanged |
| `APPLYING` | success | `READY` | unchanged |
| `APPLYING` | retryable failure | `PENDING_APPLY` | unchanged |
| `APPLYING` | permanent/retry-exhausted failure | `APPLY_FAILED` | unchanged |
| `PENDING_DELETE` | worker claim | `DELETING` | unchanged |
| `DELETING` | success | `DELETED` | unchanged |
| `DELETING` | retryable failure | `PENDING_DELETE` | unchanged |
| `DELETING` | permanent/retry-exhausted failure | `DELETE_FAILED` | unchanged |

There are no other legal transitions. In particular, a user request may reverse
`PENDING_APPLY` or `PENDING_DELETE` because target-side work has not been
claimed, but it cannot interrupt `APPLYING` or `DELETING`. There is no
`APPLYING -> PENDING_DELETE` or `DELETING -> PENDING_APPLY` transition within
one revision.

Repositories atomically replace the single current Binding snapshot and status
when PAP accepts a new desired-state revision; no older Binding record remains.
A status-only worker transition does not rewrite spec content. Status CAS APIs
identify the current target by `binding_id` plus the revision contained in the
Binding and require the expected current status. Repository implementations
must repeat the `APPLYING`/`DELETING` admission gate inside the atomic update so
a worker claim cannot race a PAP pre-check.

The shared state machine is defined and tested now, but the PAP-only phase
implements no outbox, delivery dispatcher, or reconciler. Therefore
PAP writes only `PENDING_APPLY` and `PENDING_DELETE`; nothing in this phase
advances them. TODO(policy-reconciliation): persist each accepted current
Binding replacement and its reconcile intent atomically, then let the future
Reconciler consume one complete `BindingView` whose embedded revision fences
claim, retry, completion, failure, restart recovery, and cancellation.

Durable persistence, Policy runtime, reconciliation worker, and
outbox belong to later work packages and are intentionally absent
from this slice. The compiler included here is limited to the one golden-backed
`prevent_file_deletion` lowering described above.

Dependency sources, TLS/unsafe boundaries and release audit requirements are
recorded in [DEPENDENCIES.md](DEPENDENCIES.md).

Run the branch-owned validation from this directory:

```bash
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all -- --check
```
