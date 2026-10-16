pub mod journald;

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::sync::Mutex;

use chrono::Utc;
use serde::{Deserialize, Serialize};

use crate::error::Result;

/// One line of the JSONL audit log written to `<mount>/.anolisa/audit.log`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuditEntry {
    /// RFC3339 UTC timestamp.
    pub ts: String,
    /// Tool name, e.g. `mem_write`.
    pub tool: &'static str,
    /// Path relative to mount root (or empty if not applicable).
    #[serde(skip_serializing_if = "String::is_empty", default)]
    pub path: String,
    /// Whether the call succeeded.
    pub ok: bool,
    /// Bytes read or written, when applicable.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bytes: Option<u64>,
    /// Estimated token count for the response (bytes / 4 approximation).
    /// Only populated for search and context retrieval tools.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tokens: Option<u64>,
    /// Error message if `ok == false`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// Optional trace id for cross-tool correlation.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
}

impl AuditEntry {
    pub fn new(tool: &'static str) -> Self {
        Self {
            ts: Utc::now().to_rfc3339(),
            tool,
            path: String::new(),
            ok: true,
            bytes: None,
            tokens: None,
            error: None,
            trace_id: None,
        }
    }

    pub fn path(mut self, p: impl Into<String>) -> Self {
        self.path = p.into();
        self
    }

    pub fn ok(mut self, v: bool) -> Self {
        self.ok = v;
        self
    }

    pub fn bytes(mut self, n: u64) -> Self {
        self.bytes = Some(n);
        self
    }

    pub fn tokens(mut self, n: u64) -> Self {
        self.tokens = Some(n);
        self
    }

    pub fn error(mut self, msg: impl Into<String>) -> Self {
        self.error = Some(msg.into());
        self.ok = false;
        self
    }
}

/// True when a search tool's audit payload is a sanitized query marker
/// (`len=<digits>`) rather than a recorded query.
///
/// `memory_search` redacts the query text and logs only its byte length, so
/// audit readers must treat the marker as "no query recorded": the original
/// text is unrecoverable, and persisting the marker yields junk interest facts
/// and profile topics. Legacy rows and `mem_grep` paths still carry real text.
pub fn is_sanitized_query_marker(payload: &str) -> bool {
    let Some(digits) = payload.strip_prefix("len=") else {
        return false;
    };
    !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit())
}

/// Append-only JSONL logger with a held file handle.
///
/// Each `log()` call writes to a persistent `File` handle guarded by a
/// process-local mutex, avoiding repeated open/close syscalls. When
/// `journald_enabled = true`, each entry is also sent to systemd-journald
/// (Linux only; no-op elsewhere).
pub struct AuditLogger {
    path: PathBuf,
    file: Mutex<File>,
    journald_enabled: bool,
}

impl AuditLogger {
    pub fn new(path: PathBuf) -> Result<Self> {
        Self::new_with_journald(path, false)
    }

    pub fn new_with_journald(path: PathBuf, journald_enabled: bool) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        // O_NOFOLLOW refuses to open the path if its final component is a
        // symlink, so a co-tenant process that swapped `audit.log` for
        // `→ /tmp/evil` cannot redirect our audit stream off-mount. The
        // audit log is the trust anchor for tamper-evidence (see CLAUDE.md
        // "信任链传导") so this open path matters as much as safe_fs does.
        let f = OpenOptions::new()
            .create(true)
            .append(true)
            .custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_CLOEXEC)
            .open(&path)?;
        if journald_enabled {
            journald::probe();
        }
        Ok(Self {
            path,
            file: Mutex::new(f),
            journald_enabled,
        })
    }

    pub fn log(&self, entry: AuditEntry) -> Result<()> {
        let line = serde_json::to_string(&entry)? + "\n";
        {
            let mut f = self.file.lock().unwrap_or_else(|e| e.into_inner());
            f.write_all(line.as_bytes())?;
            f.sync_all()?;
        }
        if self.journald_enabled {
            journald::fanout(&entry);
        }
        Ok(())
    }

    pub fn path(&self) -> &PathBuf {
        &self.path
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn appends_jsonl_lines() {
        let tmp = tempdir().unwrap();
        let p = tmp.path().join("audit.log");
        let log = AuditLogger::new(p.clone()).unwrap();

        log.log(AuditEntry::new("mem_write").path("notes/a.md").bytes(10))
            .unwrap();
        log.log(AuditEntry::new("mem_read").path("notes/a.md").error("nope"))
            .unwrap();

        let contents = std::fs::read_to_string(&p).unwrap();
        let lines: Vec<&str> = contents.lines().collect();
        assert_eq!(lines.len(), 2);
        let v: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(v["tool"], "mem_write");
        assert_eq!(v["path"], "notes/a.md");
        assert_eq!(v["ok"], true);
        let v: serde_json::Value = serde_json::from_str(lines[1]).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["error"], "nope");
    }

    #[test]
    fn recognizes_sanitized_query_markers() {
        assert!(is_sanitized_query_marker("len=14"));
        assert!(is_sanitized_query_marker("len=0"));
        // Recorded queries and grep paths are text, however short or odd.
        assert!(!is_sanitized_query_marker("rust ownership"));
        assert!(!is_sanitized_query_marker("notes/"));
        assert!(!is_sanitized_query_marker("len="));
        assert!(!is_sanitized_query_marker("len=1.4"));
        assert!(!is_sanitized_query_marker("len=14 bytes"));
        assert!(!is_sanitized_query_marker(""));
    }
}
