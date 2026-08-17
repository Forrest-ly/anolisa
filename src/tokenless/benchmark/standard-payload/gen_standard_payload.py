#!/usr/bin/env python3
# Copyright 2026 Alibaba Cloud
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Standard test payloads for verifying tokenless compression behavior.

These payloads are the documented reference inputs for the user-guide page
"Compression rates and applicable scenarios". They are fully deterministic
(no RNG, no timestamps, no environment input) so that anyone can regenerate
byte-identical files and compare measured compression rates against the
published reference table.

Outputs (pretty-printed, UTF-8, natural key order):
    schema_tools.json         Function-calling schema array (schema compression)
    response_api_records.json Structured API/tool response with repetitive
                              records, null/empty values and debug/trace/logs
                              fields (response compression, high-savings case)
    response_code.json        Code-search results whose content is source code
                              (response compression, medium/low-savings case)
    response_prose.json       Document-search results whose content is natural
                              language prose (response compression, low-savings
                              boundary case)

Regenerate with:  python3 gen_standard_payload.py

The generated files are committed so that running the check script does not
require Python. If you change this generator, regenerate the files and update
the reference table in the user-guide page in the same change.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent


def _tool(name: str, description: str, properties: dict, required: list[str],
          title: str | None = None, examples: list | None = None) -> dict:
    parameters: dict = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    if examples is not None:
        parameters["examples"] = examples
    function: dict = {"name": name, "description": description, "parameters": parameters}
    if title is not None:
        function["title"] = title
    return {"type": "function", "function": function}


def build_schema_tools() -> list[dict]:
    return [
        _tool(
            name="search_codebase",
            title="Codebase Search Tool",
            description=(
                "Search the entire codebase for symbols, definitions and free-text matches. "
                "Use this tool first whenever you need to locate where a function, class or "
                "configuration key is defined or referenced. The search index covers all "
                "committed files and is refreshed at the start of every session.\n\n"
                "Results are ranked by relevance and include the file path, line range and a "
                "short snippet for every match. Prefer a narrow `query` over a broad one: "
                "queries with more than three words rarely improve recall and make the result "
                "set harder to read.\n\n"
                "```python\n"
                "# Example: find every caller of the retry helper\n"
                "search_codebase(query=\"with_retry\", file_pattern=\"*.py\")\n"
                "```\n\n"
                "Do not use this tool for files that were created after the session started; "
                "they are not indexed yet. Read such files directly with `read_file` instead."
            ),
            properties={
                "query": {
                    "type": "string",
                    "description": (
                        "The search expression. Supports plain text, symbol names and simple "
                        "regular expressions. Keep it short and specific; the engine returns "
                        "at most 50 matches ranked by relevance."
                    ),
                },
                "file_pattern": {
                    "type": "string",
                    "description": (
                        "Optional glob that restricts the search to matching paths, for "
                        "example `src/**/*.rs` or `*.test.ts`. When omitted every indexed "
                        "file is searched."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matches to return, between 1 and 50. Defaults to 20.",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            required=["query"],
            examples=[
                {"query": "parse_config", "file_pattern": "src/**/*.rs"},
                {"query": "TODO(.*timeout)", "max_results": 10},
            ],
        ),
        _tool(
            name="read_file",
            title="File Reader",
            description=(
                "Read the content of a single file from the workspace. The tool returns the "
                "file as text together with its size and last modification time. Binary files "
                "are rejected with an explicit error instead of being returned.\n\n"
                "For large files prefer a line range over reading the whole file; responses "
                "above the context budget are truncated and annotated. When you only need to "
                "know whether a symbol exists, use `search_codebase` first and read exactly "
                "the matching range afterwards.\n\n"
                "```\n"
                "read_file(path=\"src/server/router.rs\", start_line=120, end_line=180)\n"
                "```"
            ),
            properties={
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path of the file to read. Symbolic links are resolved.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to read, 1-based. Optional; defaults to the first line.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to read, inclusive. Optional; defaults to the last line.",
                },
            },
            required=["path"],
        ),
        _tool(
            name="run_shell_command",
            title="Shell Command Runner",
            description=(
                "Run a shell command inside the workspace sandbox and return its combined "
                "standard output and standard error. The command runs with a configurable "
                "timeout and is killed when the timeout is exceeded.\n\n"
                "Use this tool for builds, tests, linters and version-control commands. "
                "Commands that require network access must be declared in the session "
                "manifest, otherwise they fail with a permission error.\n\n"
                "```bash\n"
                "# Example: run the unit tests for one crate\n"
                "cargo test -p tokenless-stats\n"
                "```\n\n"
                "Never run destructive commands (force push, recursive delete of shared "
                "directories) through this tool without an explicit user confirmation."
            ),
            properties={
                "command": {
                    "type": "string",
                    "description": "The shell command line to execute. It is passed to `bash -c` unchanged.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Kill the command after this many seconds. Defaults to 120, maximum 1800.",
                    "default": 120,
                },
                "working_directory": {
                    "type": "string",
                    "description": "Optional workspace-relative directory the command starts in.",
                },
            },
            required=["command"],
            examples=[{"command": "cargo fmt --check", "timeout_seconds": 60}],
        ),
        _tool(
            name="list_directory",
            title="Directory Lister",
            description=(
                "List the entries of a directory in the workspace. Each entry includes its "
                "name, type (file or directory), size in bytes and last modification time. "
                "Hidden entries are included only when requested.\n\n"
                "The result is sorted alphabetically. Use `recursive` with care: deep trees "
                "produce very large responses and are truncated beyond 5000 entries."
            ),
            properties={
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path of the directory to list.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "When true, list the whole subtree instead of a single level.",
                    "default": False,
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "When true, include entries whose name starts with a dot.",
                    "default": False,
                },
            },
            required=["path"],
        ),
        _tool(
            name="create_merge_request",
            title="Merge Request Creator",
            description=(
                "Create a merge request from the current branch to a target branch. The tool "
                "pushes the branch if it has not been pushed yet, fills in the title and "
                "description, and returns the merge request number and URL.\n\n"
                "The description supports Markdown. Keep the title under 80 characters and "
                "start it with a conventional-commit type such as `feat:` or `fix:` so the "
                "release tooling can classify the change automatically.\n\n"
                "```\n"
                "create_merge_request(\n"
                "    title=\"fix(stats): record dry-run predictions\",\n"
                "    description=\"Dry-run records were dropped from the summary.\",\n"
                "    target_branch=\"main\",\n"
                "    labels=[\"tokenless\", \"bug\"],\n"
                ")\n"
                "```"
            ),
            properties={
                "title": {
                    "type": "string",
                    "description": "One-line summary of the change, under 80 characters.",
                    "maxLength": 80,
                },
                "description": {
                    "type": "string",
                    "description": "Markdown body explaining motivation, approach and testing.",
                },
                "target_branch": {
                    "type": "string",
                    "description": "Branch the change should merge into. Defaults to `main`.",
                    "default": "main",
                },
                "labels": {
                    "type": "array",
                    "description": "Optional labels to attach. Unknown labels are created on demand.",
                    "items": {"type": "string"},
                },
                "reviewers": {
                    "type": "array",
                    "description": "Optional list of reviewer usernames.",
                    "items": {"type": "string"},
                },
                "draft": {
                    "type": "boolean",
                    "description": "Create the merge request as a draft that cannot be merged.",
                    "default": False,
                },
            },
            required=["title", "description"],
        ),
        _tool(
            name="query_database",
            title="Analytics Database Query",
            description=(
                "Run a read-only SQL query against the analytics replica and return the rows "
                "as JSON. Write statements are rejected before execution. Queries that return "
                "more than 1000 rows are truncated and flagged so downstream consumers can "
                "detect the limit.\n\n"
                "Prefer explicit column lists over `SELECT *` and always include a `LIMIT` "
                "clause; the replica enforces a 30 second statement timeout.\n\n"
                "```sql\n"
                "SELECT day, active_users, p95_latency_ms\n"
                "FROM service_health_daily\n"
                "WHERE day >= current_date - interval '7 days'\n"
                "ORDER BY day DESC;\n"
                "```"
            ),
            properties={
                "sql": {
                    "type": "string",
                    "description": "The read-only SQL statement to execute on the analytics replica.",
                },
                "database": {
                    "type": "string",
                    "description": "Logical database name. One of `analytics`, `billing_readonly`, `events`.",
                    "enum": ["analytics", "billing_readonly", "events"],
                },
                "parameters": {
                    "type": "object",
                    "description": (
                        "Optional named parameters bound into the statement, which avoids "
                        "quoting issues and injection risk."
                    ),
                    "additionalProperties": {"type": ["string", "number", "boolean"]},
                },
            },
            required=["sql", "database"],
            examples=[{"sql": "SELECT 1", "database": "analytics"}],
        ),
    ]


_REGIONS = ["cn-hangzhou", "cn-shanghai", "us-west-1", "eu-central-1"]
_ZONES = ["a", "b", "c"]
_STATUSES = ["active", "active", "active", "pending", "stopped"]


def _record(i: int) -> dict:
    region = _REGIONS[i % len(_REGIONS)]
    zone = _ZONES[i % len(_ZONES)]
    status = _STATUSES[i % len(_STATUSES)]
    hour = i % 24
    return {
        "id": f"i-{20260000 + i}",
        "name": f"worker-node-{i:03d}",
        "region": region,
        "zone": f"{region}-{zone}",
        "status": status,
        "cpu_percent": round(12.5 + (i * 7) % 80, 1),
        "memory_mb": 1024 + (i * 256) % 15360,
        "disk_mb": 20480 + (i * 512) % 81920,
        "ip_address": f"192.0.2.{10 + (i % 240)}",
        "created_at": f"2026-07-{(i % 28) + 1:02d}T{hour:02d}:15:00Z",
        "updated_at": f"2026-08-{(i % 14) + 1:02d}T{hour:02d}:45:00Z",
        "tags": ["pool:batch", f"tier:{'standard' if i % 3 else 'highmem'}"],
        "labels": {"team": "data-platform", "cost-center": f"cc-{1000 + i % 7}"},
        "description": (
            f"Batch worker node {i:03d} scheduled by the capacity planner; "
            f"runs nightly extraction jobs for shard {i % 16}."
        ),
        "last_error": None if i % 5 else "heartbeat timeout after 3 retries",
        "metadata": {},
        "health_checks": [],
    }


def build_api_records() -> dict:
    records = [_record(i) for i in range(48)]
    return {
        "status": "success",
        "request_id": "req-20260801-000123",
        "took_ms": 187,
        "page": 1,
        "page_size": 48,
        "total": 1234,
        "debug": {
            "cache_hit": False,
            "shards_scanned": 16,
            "query_plan": "index_scan on instances_by_region",
            "internal_notes": "planner fallback disabled",
        },
        "trace": [
            {
                "span_id": f"span-{1000 + i}",
                "name": stage,
                "duration_ms": 4 + i * 3,
                "attributes": {"shard": i % 16, "attempt": 1},
            }
            for i, stage in enumerate(
                [
                    "auth.check",
                    "quota.check",
                    "planner.plan",
                    "index.open",
                    "index.scan",
                    "index.scan",
                    "rows.decode",
                    "response.encode",
                ]
            )
        ],
        "logs": [
            f"2026-08-01T00:00:0{i % 10}Z INFO stage={stage} ok"
            for i, stage in enumerate(
                [
                    "auth",
                    "quota",
                    "planner",
                    "index",
                    "scan",
                    "decode",
                    "encode",
                    "respond",
                    "metrics",
                    "cleanup",
                ]
            )
        ],
        "records": records,
    }


_CODE_SNIPPETS = [
    (
        "rust",
        "src/net/retry.rs",
        12,
        """\
pub async fn with_retry<F, T, E>(mut op: F, policy: &RetryPolicy) -> Result<T, E>
where
    F: FnMut() -> futures::future::BoxFuture<'static, Result<T, E>>,
    E: IsTransient,
{
    let mut attempt = 0usize;
    loop {
        match op().await {
            Ok(value) => return Ok(value),
            Err(err) if err.is_transient() && attempt < policy.max_attempts => {
                attempt += 1;
                let delay = policy.delay_for(attempt);
                tokio::time::sleep(delay).await;
            }
            Err(err) => return Err(err),
        }
    }
}""",
    ),
    (
        "python",
        "pipeline/retry.py",
        34,
        """\
def with_backoff(func, *, attempts=5, base=0.5, factor=2.0, retry_on=(TimeoutError,)):
    delay = base
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retry_on as exc:
            if attempt == attempts:
                raise
            logging.warning("attempt %d failed: %s; sleeping %.2fs", attempt, exc, delay)
            time.sleep(delay)
            delay *= factor""",
    ),
    (
        "go",
        "internal/httpclient/retry.go",
        21,
        """\
func DoWithRetry(ctx context.Context, client *http.Client, req *http.Request, max int) (*http.Response, error) {
    var resp *http.Response
    var err error
    for attempt := 0; attempt <= max; attempt++ {
        resp, err = client.Do(req.Clone(ctx))
        if err == nil && resp.StatusCode < 500 {
            return resp, nil
        }
        if attempt < max {
            time.Sleep(backoff(attempt))
        }
    }
    return resp, err
}""",
    ),
    (
        "typescript",
        "src/client/fetchRetry.ts",
        8,
        """\
export async function fetchRetry(url: string, init: RequestInit, attempts = 4): Promise<Response> {
  let lastError: unknown;
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      const response = await fetch(url, init);
      if (response.status < 500) return response;
      lastError = new Error(`server error ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await sleep(2 ** attempt * 250);
  }
  throw lastError;
}""",
    ),
    (
        "bash",
        "scripts/wait_for_endpoint.sh",
        3,
        """\
attempt=0
until curl --fail --silent "$ENDPOINT/health" > /dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "endpoint never became healthy" >&2
    exit 1
  fi
  sleep 2
done
echo "endpoint healthy after $attempt retries\"""",
    ),
    (
        "rust",
        "src/net/timeout.rs",
        40,
        """\
pub async fn with_timeout<F>(future: F, limit: Duration) -> Result<F::Output, Elapsed>
where
    F: Future,
{
    match tokio::time::timeout(limit, future).await {
        Ok(output) => Ok(output),
        Err(_) => Err(Elapsed { limit }),
    }
}""",
    ),
    (
        "python",
        "pipeline/circuit_breaker.py",
        12,
        """\
class CircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_timeout=30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.opened_at = None

    def allow(self) -> bool:
        if self.failures < self.failure_threshold:
            return True
        if self.opened_at is None:
            return False
        return time.monotonic() - self.opened_at >= self.recovery_timeout""",
    ),
    (
        "go",
        "internal/jitter/jitter.go",
        9,
        """\
func BackoffWithJitter(attempt int, base, cap time.Duration) time.Duration {
    exp := base << attempt
    if exp > cap || exp <= 0 {
        exp = cap
    }
    jitter := time.Duration(rand.Int63n(int64(exp)/2 + 1))
    return exp/2 + jitter
}""",
    ),
    (
        "typescript",
        "src/client/sleep.ts",
        1,
        """\
export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export const defaultRetryConfig = {
  attempts: 4,
  baseDelayMs: 250,
  maxDelayMs: 8000,
  jitter: true,
} as const;""",
    ),
    (
        "yaml",
        "deploy/retry-policy.yaml",
        1,
        """\
retry_policy:
  max_attempts: 5
  initial_backoff: 500ms
  max_backoff: 30s
  backoff_multiplier: 2.0
  retry_on:
    - connection_reset
    - upstream_timeout
    - status_503
  non_retryable:
    - status_400
    - status_401""",
    ),
]


def build_code_response() -> dict:
    results = []
    for index, (language, path, start_line, snippet) in enumerate(_CODE_SNIPPETS):
        lines = snippet.splitlines()
        results.append(
            {
                "path": path,
                "language": language,
                "start_line": start_line,
                "end_line": start_line + len(lines) - 1,
                "score": round(0.98 - index * 0.07, 2),
                "snippet": snippet,
                "error": None,
                "warnings": [],
            }
        )
    return {
        "tool": "code_search",
        "query": "retry backoff",
        "total_matches": 128,
        "returned": len(results),
        "results": results,
        "truncated": False,
        "index_version": None,
    }


_PROSE_DOCS = [
    {
        "title": "Capacity Planning Basics for Agent Platforms",
        "url": "https://example.com/articles/capacity-planning-basics",
        "published_at": "2026-03-18",
        "summary": (
            "A practical introduction to capacity planning for platforms that serve "
            "agent workloads, covering workload characterization, headroom policy and "
            "percentile-based latency budgets."
        ),
        "content": (
            "Capacity planning starts with characterizing the workload. For agent "
            "platforms this means measuring not only request rates but also the shape "
            "of each session: how many tool calls a typical session makes, how large "
            "the tool responses are, and how long sessions stay active. Two platforms "
            "with identical requests-per-second can differ by an order of magnitude in "
            "token throughput if their session shapes differ.\n\n"
            "Once the workload is characterized, define a headroom policy. A common "
            "starting point is to provision for the 95th percentile day plus thirty "
            "percent spare capacity, then adjust after observing at least two full "
            "weekly cycles. Headroom is cheapest when bought before the traffic "
            "arrives; emergency capacity during an incident always costs more in both "
            "money and reliability.\n\n"
            "Finally, track latency budgets by percentile rather than by average. "
            "Average latency hides the long sessions that matter most, because agent "
            "sessions are heavy-tailed: a small number of sessions consume most of "
            "the tokens. Budgeting against the 90th or 95th percentile keeps the "
            "system sized for the sessions users actually notice."
        ),
        "highlights": [],
    },
    {
        "title": "Observability for Batch Pipelines",
        "url": "https://example.com/articles/batch-pipeline-observability",
        "published_at": "2026-05-02",
        "summary": (
            "How to instrument batch pipelines so failures are attributable to a "
            "single stage, with guidance on metrics, structured logs and trace "
            "propagation across queued work."
        ),
        "content": (
            "Batch pipelines fail differently from request-driven services. A web "
            "service either answers or times out, but a batch pipeline can silently "
            "drop work, duplicate work, or fall behind without any visible error. "
            "Observability for batch systems therefore starts with accounting: every "
            "unit of work that enters the pipeline must be traceable to exactly one "
            "terminal state, either completed, failed or expired.\n\n"
            "Structured logs are the backbone of that accounting. Each stage should "
            "emit one structured record per unit of work, carrying a stable work "
            "identifier, the stage name, and the outcome. Free-form logging makes it "
            "impossible to answer the simplest operational question, which is how many "
            "units are currently stuck between two stages.\n\n"
            "Traces add the time dimension. Propagating a trace identifier through "
            "queue metadata lets operators see how long a unit of work waited in each "
            "queue, which is usually where batch latency actually goes. The metric to "
            "alert on is end-to-end age of the oldest incomplete unit, not the "
            "duration of individual stages."
        ),
        "highlights": [],
    },
    {
        "title": "Load Testing Strategies That Predict Production",
        "url": "https://example.com/articles/load-testing-strategies",
        "published_at": "2026-06-21",
        "summary": (
            "Why constant-rate load tests mislead, and how ramp profiles, soak tests "
            "and saturation-point measurement produce numbers that actually predict "
            "production behavior."
        ),
        "content": (
            "A constant-rate load test answers one question: does the system survive "
            "this exact rate? Production traffic never looks like that. Real traffic "
            "ramps, spikes, and recovers, and most production failures happen on the "
            "ramp rather than at steady state. A useful load test therefore includes "
            "a ramp profile that exceeds the fastest growth observed in production, "
            "so the test exposes queue buildup and cache warm-up behavior before users "
            "do.\n\n"
            "Soak tests answer a different question: does the system leak? Memory "
            "leaks, connection pool exhaustion and file descriptor growth only show "
            "up after hours of steady operation. Running the representative workload "
            "at seventy percent of the target rate for at least eight hours, while "
            "watching resource counters, catches most of these defects.\n\n"
            "The most valuable number a load test produces is the saturation point: "
            "the rate at which latency stops scaling linearly and starts climbing "
            "sharply. Measure it by increasing the rate in steps and recording the "
            "95th percentile latency at each step. The saturation point, not the "
            "failure point, is what capacity plans should be built on."
        ),
        "highlights": [],
    },
]


def build_prose_response() -> dict:
    return {
        "source": "web_search",
        "query": "capacity planning best practices",
        "total_results": 3,
        "results": _PROSE_DOCS,
        "next_cursor": None,
        "suggestions": [],
    }


def _write(name: str, value: object) -> None:
    path = OUT_DIR / name
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path.name} ({path.stat().st_size} bytes)")


def main() -> None:
    _write("schema_tools.json", build_schema_tools())
    _write("response_api_records.json", build_api_records())
    _write("response_code.json", build_code_response())
    _write("response_prose.json", build_prose_response())


if __name__ == "__main__":
    main()
