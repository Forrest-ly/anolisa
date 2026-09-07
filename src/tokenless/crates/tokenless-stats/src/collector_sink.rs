//! Shared hardened JSONL sink for collector-owned observability files.
//!
//! Tokenless ships two external observability channels — the SLS ops feed and
//! the AgentLoop token-savings feed. Both are append-only JSONL files whose
//! lifecycle (create, rotate, remove) is owned by the ANOLISA collector
//! infrastructure, never by tokenless. Because both files can land in
//! world-writable locations, they share one path-validation and append
//! implementation so the security properties cannot drift between channels.
//!
//! Contract shared by every caller:
//! - tokenless appends only when the target file already exists; a missing
//!   file means "collection not active" and is skipped silently.
//! - tokenless never creates, truncates, or removes the file or its parent
//!   directory.
//! - all failures are reported on stderr at most once and never propagated:
//!   observability must not block or fail a compression.

use std::ffi::OsString;
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Component, Path, PathBuf};

/// Allowed path prefixes for a collector sink path override.
///
/// NOTE: `/tmp/` is world-writable on most Unix systems — other local users
/// can read the JSONL file if placed there. Prefer `/var/log/` for production.
pub(crate) const ALLOWED_COLLECTOR_PREFIXES: &[&str] = &["/var/log/", "/tmp/"];

/// Root-owned prefixes where creating a symlink requires privilege. For
/// these, the original (pre-canonicalize) path can be trusted even when
/// canonicalization resolves it elsewhere (e.g. `/var/log` symlinked to
/// another filesystem). World-writable prefixes like `/tmp/` are excluded
/// because an unprivileged user can place a symlink there to escape.
pub(crate) const TRUSTED_COLLECTOR_PREFIXES: &[&str] = &["/var/log/"];

/// Canonicalize a path, walking up the parent chain to resolve symlinks
/// when the path or its ancestors don't exist yet. Returns the best-effort
/// canonicalized path (falls back to the original if the entire chain is
/// unresolvable).
pub(crate) fn canonicalize_or_reconstruct(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| {
        let mut cursor = path.to_path_buf();
        let mut suffix: Vec<OsString> = Vec::new();
        loop {
            match cursor.canonicalize() {
                Ok(canon) => {
                    let mut result = canon;
                    for name in suffix.iter().rev() {
                        result.push(name);
                    }
                    return result;
                }
                Err(_) => {
                    if let Some(name) = cursor.file_name() {
                        suffix.push(name.to_os_string());
                    }
                    match cursor.parent() {
                        Some(p) => cursor = p.to_path_buf(),
                        None => return path.to_path_buf(),
                    }
                }
            }
        }
    })
}

/// Validate a collector sink path: reject `..` traversal and paths outside
/// the allowed prefixes. Returns the canonicalized path if acceptable, or
/// `None`.
///
/// Canonicalizes the path to resolve symlinks before the prefix check, so a
/// symlinked `/var/log` → `/` cannot be used to escape the allowed
/// directories. A path is accepted when the resolved path matches an allowed
/// prefix, OR the original (pre-canonicalize) path matches a root-owned
/// trusted prefix (see [`TRUSTED_COLLECTOR_PREFIXES`]).
///
/// The trusted-prefix fallback covers systems where `/var/log` is itself
/// symlinked to another filesystem: canonicalization resolves it to the real
/// target (no longer starting with `/var/log/`), but since creating that
/// symlink requires root, trusting the original path is safe. The
/// world-writable `/tmp/` prefix is excluded from the fallback, so a
/// user-placed symlink in `/tmp/` cannot escape to an arbitrary location.
///
/// NOTE: callers store the canonicalized path at construction time to narrow
/// the TOCTOU window between validation and write.
pub(crate) fn validate_collector_path(path: &Path) -> Option<PathBuf> {
    if path.components().any(|c| c == Component::ParentDir) {
        return None;
    }

    let resolved = canonicalize_or_reconstruct(path);
    let resolved_str = resolved.to_str().unwrap_or("");
    let original_str = path.to_str().unwrap_or("");
    let resolved_ok = ALLOWED_COLLECTOR_PREFIXES
        .iter()
        .any(|prefix| resolved_str.starts_with(prefix));
    let original_ok = TRUSTED_COLLECTOR_PREFIXES
        .iter()
        .any(|prefix| original_str.starts_with(prefix));
    if resolved_ok || original_ok {
        Some(resolved)
    } else {
        None
    }
}

/// Resolve a collector sink path from an optional env var value.
///
/// Falls back to `default_path` when the env var is unset, empty, or contains
/// an invalid path. Returns the canonicalized path to narrow the TOCTOU
/// window between validation and write. `channel` names the sink in the
/// rejection warning so operators can tell the two channels apart.
pub(crate) fn resolve_collector_path(
    channel: &str,
    env_name: &str,
    env_val: Option<&str>,
    default_path: &str,
) -> PathBuf {
    env_val
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .and_then(|p| match validate_collector_path(&p) {
            Some(resolved) => Some(resolved),
            None => {
                eprintln!(
                    "tokenless-{channel}: {env_name} rejected (must be under \
                     /var/log/ or /tmp/, and must not contain '..'), \
                     falling back to default: {default_path}"
                );
                None
            }
        })
        .unwrap_or_else(|| PathBuf::from(default_path))
}

/// Append one already-serialized line (without a trailing newline) to a
/// collector-owned JSONL file.
///
/// The caller must have decided that the file exists; this helper opens it
/// append-only and refuses to follow a symlink in the final path component.
///
/// # Errors
///
/// Returns the underlying IO error. Callers report it once on stderr and
/// continue — observability never fails a compression.
pub(crate) fn append_json_line(path: &Path, line: &str) -> std::io::Result<()> {
    let mut opts = OpenOptions::new();
    opts.append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        // Refuse to open if the final path component is a symlink. The file
        // is owned by the ANOLISA collector and tokenless never creates one,
        // so a legit target is never a symlink here; O_NOFOLLOW blocks a
        // swap-to-symlink between the existence check and the open (narrowing
        // the TOCTOU window on /tmp/).
        opts.custom_flags(libc::O_NOFOLLOW);
    }
    let mut file = opts.open(path)?;
    file.write_all(line.as_bytes())?;
    file.write_all(b"\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_collector_path_accepts_trusted_var_log() {
        // When /var/log is symlinked to another filesystem, canonicalization
        // resolves it to the real target which no longer starts with
        // /var/log/. Since /var/log is root-owned (creating the symlink needs
        // privilege), the original path is trusted and the path is still
        // accepted. Here /var/log is a real directory (no symlink), so both
        // the original and reconstructed paths match the /var/log/ prefix.
        let result = validate_collector_path(Path::new("/var/log/anolisa/ops/tokenless.jsonl"));
        assert!(result.is_some());
        let resolved = result.unwrap();
        assert!(resolved.to_str().unwrap().starts_with("/var/log/"));
    }

    #[test]
    fn test_validate_collector_path_rejects_parent_traversal() {
        assert!(validate_collector_path(Path::new("/var/log/../../etc/passwd")).is_none());
    }

    #[test]
    fn test_validate_collector_path_rejects_non_whitelisted_prefix() {
        assert!(validate_collector_path(Path::new("/etc/cron.d/evil")).is_none());
    }

    #[cfg(unix)]
    #[test]
    fn test_validate_collector_path_rejects_tmp_symlink_escape() {
        // /tmp/ is world-writable, so an unprivileged user can place a
        // symlink there that escapes the allowed prefixes (e.g. -> /etc).
        // The path must be REJECTED: the resolved path no longer matches an
        // allowed prefix, and /tmp/ is not a trusted prefix, so the original
        // path must not be used as a fallback.
        let dir = tempfile::tempdir().unwrap();
        let link = dir.path().join("escape");
        std::os::unix::fs::symlink("/etc", &link).unwrap();
        let escaped = link.join("cron.d/evil.jsonl");
        assert!(validate_collector_path(&escaped).is_none());
    }

    #[test]
    fn test_resolve_collector_path_rejects_and_falls_back() {
        assert_eq!(
            resolve_collector_path(
                "sls",
                "TOKENLESS_SLS_PATH",
                Some("/etc/evil.jsonl"),
                "/var/log/anolisa/sls/ops/tokenless.jsonl"
            ),
            PathBuf::from("/var/log/anolisa/sls/ops/tokenless.jsonl")
        );
        assert_eq!(
            resolve_collector_path(
                "agentloop",
                "TOKENLESS_AGENTLOOP_PATH",
                Some(""),
                "/var/log/anolisa/agentloop/ops/tokenless.jsonl"
            ),
            PathBuf::from("/var/log/anolisa/agentloop/ops/tokenless.jsonl")
        );
    }

    #[test]
    fn test_append_json_line_adds_newline_and_appends() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sink.jsonl");
        std::fs::write(&path, "").unwrap();

        append_json_line(&path, "{\"a\":1}").unwrap();
        append_json_line(&path, "{\"a\":2}").unwrap();

        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            "{\"a\":1}\n{\"a\":2}\n"
        );
    }

    #[cfg(unix)]
    #[test]
    fn test_append_json_line_refuses_symlink() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("target.jsonl");
        std::fs::write(&target, "original\n").unwrap();
        let link = dir.path().join("link.jsonl");
        std::os::unix::fs::symlink(&target, &link).unwrap();

        assert!(append_json_line(&link, "{\"a\":1}").is_err());
        assert_eq!(std::fs::read_to_string(&target).unwrap(), "original\n");
    }
}
