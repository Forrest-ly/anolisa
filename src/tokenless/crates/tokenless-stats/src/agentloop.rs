//! AgentLoop observability data model and writer for tokenless stats.
//!
//! AgentLoop is the agent observability and optimization backend that agent
//! runtimes report their trajectories to. It joins evidence across components
//! by the trajectory correlation identity (`conversation_id`, `tool_call_id`,
//! agent name), which is why tokenless cannot simply hand AgentLoop its own
//! `session_id` / `tool_use_id` names: the join keys have to match the
//! trajectory vocabulary or the savings never attach to the right step.
//!
//! This module provides:
//! - [`AgentLoopRecord`]: one token-savings event projected onto that
//!   vocabulary, carrying metrics only — never the compressed text.
//! - [`AgentLoopWriter`]: append-only JSONL writer with fail-silent
//!   semantics, sharing the hardened collector sink with the SLS channel.
//!
//! The JSONL file is owned and lifecycle-managed by the ANOLISA collector
//! infrastructure (it creates, rotates, and removes it). tokenless never
//! creates, truncates, or deletes the file: on each [`AgentLoopWriter::write`]
//! it appends only if the file already exists, and silently skips when it does
//! not (treated as "AgentLoop collection not active").

use crate::collector_sink::{append_json_line, resolve_collector_path};
use crate::{StatsRecord, VERSION};
use chrono::Utc;
use serde::Serialize;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

/// Default AgentLoop JSONL output path.
///
/// NOTE: the file and its parent directory are owned by the ANOLISA collector
/// infrastructure. tokenless only appends to it when it already exists; it
/// never creates the file or directory. Override the path via
/// [`AGENTLOOP_PATH_ENV`] (must be under `/var/log/` or `/tmp/`, no `..`).
pub const DEFAULT_AGENTLOOP_PATH: &str = "/var/log/anolisa/agentloop/ops/tokenless.jsonl";

/// Environment variable that overrides [`DEFAULT_AGENTLOOP_PATH`].
pub const AGENTLOOP_PATH_ENV: &str = "TOKENLESS_AGENTLOOP_PATH";

/// Event type emitted for every recorded compression.
///
/// Namespaced by component so a shared AgentLoop ingest can tell tokenless
/// savings apart from other components' events without a second field.
pub const AGENTLOOP_EVENT_TYPE: &str = "tokenless.token_savings";

/// Version of the [`AgentLoopRecord`] schema.
///
/// Bumped when a field is renamed, retyped, or removed. Additive optional
/// fields do not require a bump, so consumers can pin the major layout.
pub const AGENTLOOP_SCHEMA_VERSION: u32 = 1;

/// Resolve the AgentLoop output path from an optional env var value.
/// Falls back to [`DEFAULT_AGENTLOOP_PATH`] when the env var is unset, empty,
/// or contains an invalid path. Returns the canonicalized path to narrow the
/// TOCTOU window between validation and write.
fn resolve_agentloop_path(env_val: Option<&str>) -> PathBuf {
    resolve_collector_path(
        "agentloop",
        AGENTLOOP_PATH_ENV,
        env_val,
        DEFAULT_AGENTLOOP_PATH,
    )
}

/// One tokenless token-savings event in AgentLoop's correlation vocabulary.
///
/// Field names are flat snake_case and the three join keys
/// ([`AgentLoopRecord::conversation_id`], [`AgentLoopRecord::tool_call_id`],
/// [`AgentLoopRecord::agent_name`]) use the trajectory names AgentLoop already
/// indexes, so a savings event lands on the same conversation and tool call
/// the runtime recorded.
///
/// Metrics only: the record never carries `before_text` / `after_text`, so an
/// AgentLoop ingest cannot leak tool output or model context.
#[derive(Debug, Clone, Serialize)]
pub struct AgentLoopRecord {
    /// [`AGENTLOOP_SCHEMA_VERSION`] of this record.
    pub schema_version: u32,
    /// [`AGENTLOOP_EVENT_TYPE`]; constant today, kept as a field so a shared
    /// ingest can route without inspecting the file path.
    pub event_type: String,
    /// Record time in RFC 3339 UTC, converted from the local record timestamp
    /// so trajectories collected in different time zones stay comparable.
    pub timestamp: String,

    /// Trajectory conversation identity (tokenless `session_id`). Absent when
    /// the host did not supply one.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub conversation_id: Option<String>,
    /// Trajectory tool-call identity (tokenless `tool_use_id`). Absent for
    /// operations that are not bound to a single tool call.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    /// Agent or adapter that triggered the compression.
    pub agent_name: String,

    /// Emitting component; always `tokenless`.
    pub component_name: String,
    /// Emitting component version.
    pub component_version: String,

    /// Tokenless operation (`compress-schema`, `compress-response`,
    /// `rewrite-command`, `compress-toon`).
    pub operation: String,
    /// Whether the savings were applied (`active`) or only predicted
    /// (`dry-run`). AgentLoop must not bill dry-run savings as real.
    pub mode: String,
    /// PID of the process that performed the compression.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_pid: Option<i64>,

    /// Characters before compression.
    pub before_chars: usize,
    /// Estimated tokens before compression.
    pub before_tokens: usize,
    /// Characters after compression.
    pub after_chars: usize,
    /// Estimated tokens after compression.
    pub after_tokens: usize,
    /// Characters saved (`before_chars - after_chars`).
    pub chars_saved: usize,
    /// Estimated tokens saved (`before_tokens - after_tokens`).
    pub tokens_saved: usize,
    /// Characters saved as a percentage of `before_chars` (0.0-100.0).
    pub chars_saved_percent: f64,
    /// Estimated tokens saved as a percentage of `before_tokens` (0.0-100.0).
    pub tokens_saved_percent: f64,

    /// Detected content taxonomy, when a detector ran.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_type: Option<String>,
    /// Content origin declared by the adapter, when available.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_origin: Option<String>,
    /// Lifecycle operations that shaped the emitted content.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub applied_operations: Option<Vec<String>>,
    /// Recovery state of the emitted content: whether the original can be
    /// retrieved. AgentLoop uses this to tell a lossless saving from a lossy
    /// one.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recoverability: Option<String>,
    /// Token counter identity used for the estimates.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tokenizer_id: Option<String>,
    /// Truncations emitted without a recovery marker.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unrecoverable_truncations: Option<i64>,
}

impl From<&StatsRecord> for AgentLoopRecord {
    fn from(r: &StatsRecord) -> Self {
        Self {
            schema_version: AGENTLOOP_SCHEMA_VERSION,
            event_type: AGENTLOOP_EVENT_TYPE.to_string(),
            timestamp: r.timestamp.with_timezone(&Utc).to_rfc3339(),
            // The trajectory join keys. tokenless names them session_id /
            // tool_use_id internally; AgentLoop names them conversation_id /
            // tool_call_id. Same identity, AgentLoop's spelling.
            conversation_id: r.session_id.clone(),
            tool_call_id: r.tool_use_id.clone(),
            agent_name: r.agent_id.clone(),
            component_name: "tokenless".to_string(),
            component_version: VERSION.to_string(),
            operation: r.operation.as_str().to_string(),
            mode: r.mode.as_str().to_string(),
            source_pid: r.source_pid,
            before_chars: r.before_chars,
            before_tokens: r.before_tokens,
            after_chars: r.after_chars,
            after_tokens: r.after_tokens,
            chars_saved: r.chars_saved(),
            tokens_saved: r.tokens_saved(),
            chars_saved_percent: r.chars_percent(),
            tokens_saved_percent: r.tokens_percent(),
            content_type: r.content_type.clone(),
            content_origin: r.content_origin.clone(),
            applied_operations: r.applied_operations.clone(),
            recoverability: r.recoverability.clone(),
            tokenizer_id: r.tokenizer_id.clone(),
            unrecoverable_truncations: r.unrecoverable_truncations,
        }
    }
}

/// Writes [`AgentLoopRecord`] entries to a JSONL file (one JSON object per
/// line).
///
/// Each [`AgentLoopWriter::write`] call opens the file append-only, writes one
/// JSON line, then closes it (the file handle drops). Fail-silent: errors are
/// printed to stderr at most once and never propagated, so observability can
/// neither slow down nor fail a compression.
pub struct AgentLoopWriter {
    path: PathBuf,
}

impl Default for AgentLoopWriter {
    fn default() -> Self {
        Self::new()
    }
}

impl AgentLoopWriter {
    /// Create a writer using the [`AGENTLOOP_PATH_ENV`] env var, falling back
    /// to [`DEFAULT_AGENTLOOP_PATH`] if the env var is not set, empty, or
    /// contains an invalid path.
    pub fn new() -> Self {
        let env_val = std::env::var(AGENTLOOP_PATH_ENV).ok();
        Self {
            path: resolve_agentloop_path(env_val.as_deref()),
        }
    }

    /// Resolved output path. Exposed so `stats status` can report where
    /// AgentLoop records would land.
    #[must_use]
    pub fn path(&self) -> &std::path::Path {
        &self.path
    }

    /// Create a writer with an explicit path (for testing).
    /// NOTE: This bypasses the collector path validation — the caller is
    /// responsible for ensuring the path is safe. Production code should use
    /// [`AgentLoopWriter::new`].
    #[cfg(test)]
    pub(crate) fn with_path(path: PathBuf) -> Self {
        Self { path }
    }

    /// Convert a [`StatsRecord`] to [`AgentLoopRecord`] and append it as a
    /// JSON line.
    ///
    /// The JSONL file is owned by the ANOLISA collector infrastructure, which
    /// is responsible for creating, rotating, and removing it. tokenless only
    /// appends: when the file does not yet exist the write is silently skipped
    /// (treated as "AgentLoop collection not active"); tokenless never creates,
    /// truncates, or deletes the file or its parent directory.
    pub fn write(&self, record: &StatsRecord) {
        // Skip silently when the collector has not created the file yet.
        if !self.path.exists() {
            return;
        }

        let agentloop_record = AgentLoopRecord::from(record);
        let line = match serde_json::to_string(&agentloop_record) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("tokenless-agentloop: serialization error: {e}");
                return;
            }
        };

        if let Err(e) = append_json_line(&self.path, &line) {
            static WRITE_ERROR_WARNED: AtomicBool = AtomicBool::new(false);
            if !WRITE_ERROR_WARNED.swap(true, Ordering::Relaxed) {
                eprintln!(
                    "tokenless-agentloop: write error to {}: {} \
                     (further write errors suppressed)",
                    self.path.display(),
                    e
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::record::{CompressionMode, OperationType};
    use std::fs;

    fn make_record() -> StatsRecord {
        StatsRecord::new(
            OperationType::CompressResponse,
            "copilot-shell".to_string(),
            1000,
            400,
            500,
            200,
        )
        .with_session_id("conv-123")
        .with_tool_use_id("call_abc")
        .with_source_pid(12345)
    }

    fn make_record_minimal() -> StatsRecord {
        StatsRecord::new(
            OperationType::CompressSchema,
            "test-agent".to_string(),
            200,
            80,
            100,
            40,
        )
    }

    #[test]
    fn test_agentloop_record_maps_trajectory_join_keys() {
        let record = AgentLoopRecord::from(&make_record());

        assert_eq!(record.conversation_id.as_deref(), Some("conv-123"));
        assert_eq!(record.tool_call_id.as_deref(), Some("call_abc"));
        assert_eq!(record.agent_name, "copilot-shell");
        assert_eq!(record.event_type, AGENTLOOP_EVENT_TYPE);
        assert_eq!(record.schema_version, AGENTLOOP_SCHEMA_VERSION);
        assert_eq!(record.component_name, "tokenless");
        assert_eq!(record.component_version, VERSION);
        assert_eq!(record.operation, "compress-response");
        assert_eq!(record.mode, "active");
        assert_eq!(record.source_pid, Some(12345));
    }

    #[test]
    fn test_agentloop_record_carries_savings_metrics() {
        let record = AgentLoopRecord::from(&make_record());

        assert_eq!(record.before_chars, 1000);
        assert_eq!(record.before_tokens, 400);
        assert_eq!(record.after_chars, 500);
        assert_eq!(record.after_tokens, 200);
        assert_eq!(record.chars_saved, 500);
        assert_eq!(record.tokens_saved, 200);
        assert!((record.chars_saved_percent - 50.0).abs() < 0.01);
        assert!((record.tokens_saved_percent - 50.0).abs() < 0.01);
    }

    #[test]
    fn test_agentloop_record_dry_run_mode_is_labelled() {
        let mut stats = make_record();
        stats.mode = CompressionMode::DryRun;
        let record = AgentLoopRecord::from(&stats);

        assert_eq!(record.mode, "dry-run");
    }

    #[test]
    fn test_agentloop_record_mirrors_entry_metadata() {
        let mut stats = make_record();
        stats.content_type = Some("json".to_string());
        stats.content_origin = Some("tool_result".to_string());
        stats.applied_operations = Some(vec!["json_truncation".to_string()]);
        stats.recoverability = Some("recoverable".to_string());
        stats.tokenizer_id = Some("tokenless-estimator".to_string());
        stats.unrecoverable_truncations = Some(2);
        let record = AgentLoopRecord::from(&stats);

        assert_eq!(record.content_type.as_deref(), Some("json"));
        assert_eq!(record.content_origin.as_deref(), Some("tool_result"));
        assert_eq!(
            record.applied_operations.as_deref(),
            Some(["json_truncation".to_string()].as_slice())
        );
        assert_eq!(record.recoverability.as_deref(), Some("recoverable"));
        assert_eq!(record.tokenizer_id.as_deref(), Some("tokenless-estimator"));
        assert_eq!(record.unrecoverable_truncations, Some(2));
    }

    #[test]
    fn test_agentloop_record_timestamp_is_rfc3339_utc() {
        let record = AgentLoopRecord::from(&make_record());

        // RFC 3339 with an explicit UTC offset, e.g. 2026-09-07T01:02:03+00:00
        assert!(
            record.timestamp.ends_with("+00:00") || record.timestamp.ends_with('Z'),
            "timestamp should be normalized to UTC: {}",
            record.timestamp
        );
        assert!(
            chrono::DateTime::parse_from_rfc3339(&record.timestamp).is_ok(),
            "timestamp should parse as RFC 3339: {}",
            record.timestamp
        );
    }

    #[test]
    fn test_agentloop_record_json_omits_absent_optionals() {
        let json = serde_json::to_value(AgentLoopRecord::from(&make_record_minimal())).unwrap();

        assert_eq!(json["schema_version"], AGENTLOOP_SCHEMA_VERSION);
        assert_eq!(json["event_type"], AGENTLOOP_EVENT_TYPE);
        assert_eq!(json["agent_name"], "test-agent");
        assert_eq!(json["operation"], "compress-schema");
        assert!(json.get("conversation_id").is_none());
        assert!(json.get("tool_call_id").is_none());
        assert!(json.get("source_pid").is_none());
        assert!(json.get("content_type").is_none());
        assert!(json.get("recoverability").is_none());
    }

    #[test]
    fn test_agentloop_record_json_never_carries_text() {
        let mut stats = make_record();
        stats.before_text = Some("secret original payload".to_string());
        stats.after_text = Some("secret compressed payload".to_string());
        let json = serde_json::to_string(&AgentLoopRecord::from(&stats)).unwrap();

        assert!(!json.contains("secret original payload"));
        assert!(!json.contains("secret compressed payload"));
        assert!(!json.contains("before_text"));
        assert!(!json.contains("after_text"));
    }

    #[test]
    fn test_agentloop_writer_default_trait() {
        let _ = AgentLoopWriter::default();
    }

    #[test]
    fn test_agentloop_writer_appends_jsonl() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agentloop.jsonl");
        fs::write(&path, "").unwrap();

        let writer = AgentLoopWriter::with_path(path.clone());
        writer.write(&make_record());
        writer.write(&make_record_minimal());

        let lines: Vec<String> = fs::read_to_string(&path)
            .unwrap()
            .lines()
            .map(str::to_string)
            .collect();
        assert_eq!(lines.len(), 2);

        let first: serde_json::Value = serde_json::from_str(&lines[0]).unwrap();
        let second: serde_json::Value = serde_json::from_str(&lines[1]).unwrap();
        assert_eq!(first["event_type"], AGENTLOOP_EVENT_TYPE);
        assert_eq!(first["conversation_id"], "conv-123");
        assert_eq!(first["tool_call_id"], "call_abc");
        assert_eq!(second["agent_name"], "test-agent");
    }

    #[test]
    fn test_agentloop_writer_skips_when_file_missing() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("agentloop.jsonl");

        AgentLoopWriter::with_path(path.clone()).write(&make_record());

        assert!(
            !path.exists(),
            "tokenless must not create the AgentLoop file"
        );
    }

    #[test]
    fn test_agentloop_writer_fail_silent_on_invalid_path() {
        // A path in a directory that does not exist: the write must not panic
        // and must not create anything.
        let writer = AgentLoopWriter::with_path(PathBuf::from("/nonexistent-dir-xyz/out.jsonl"));
        writer.write(&make_record());
    }

    #[test]
    fn test_agentloop_writer_refuses_symlink_final_component() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("target.jsonl");
        fs::write(&target, "").unwrap();
        let link = dir.path().join("link.jsonl");
        std::os::unix::fs::symlink(&target, &link).unwrap();

        let before = fs::read_to_string(&target).unwrap();
        AgentLoopWriter::with_path(link).write(&make_record());
        let after = fs::read_to_string(&target).unwrap();

        assert_eq!(before, after, "symlink target must not be modified");
    }

    #[test]
    fn test_resolve_agentloop_path_default() {
        assert_eq!(
            resolve_agentloop_path(None),
            PathBuf::from(DEFAULT_AGENTLOOP_PATH)
        );
    }

    #[test]
    fn test_resolve_agentloop_path_empty() {
        assert_eq!(
            resolve_agentloop_path(Some("")),
            PathBuf::from(DEFAULT_AGENTLOOP_PATH)
        );
    }

    #[test]
    fn test_resolve_agentloop_path_rejects_parent_traversal() {
        assert_eq!(
            resolve_agentloop_path(Some("/var/log/../etc/tokenless.jsonl")),
            PathBuf::from(DEFAULT_AGENTLOOP_PATH)
        );
    }

    #[test]
    fn test_resolve_agentloop_path_rejects_non_whitelisted_prefix() {
        assert_eq!(
            resolve_agentloop_path(Some("/etc/tokenless.jsonl")),
            PathBuf::from(DEFAULT_AGENTLOOP_PATH)
        );
    }

    #[test]
    fn test_resolve_agentloop_path_accepts_valid_path() {
        // Valid paths under /var/log/ or /tmp/ are accepted.
        assert_eq!(
            resolve_agentloop_path(Some("/var/log/custom/tokenless.jsonl")),
            PathBuf::from("/var/log/custom/tokenless.jsonl")
        );
        assert_eq!(
            resolve_agentloop_path(Some("/tmp/tokenless-agentloop.jsonl")),
            PathBuf::from("/tmp/tokenless-agentloop.jsonl")
        );
    }
}
