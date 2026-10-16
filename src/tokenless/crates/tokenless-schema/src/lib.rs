pub mod schema_compressor;

pub use schema_compressor::SchemaCompressor;

/// Best-effort stderr warning whose own write failure cannot fail the
/// command. `eprintln!` panics when writing to stderr fails (a full
/// filesystem behind redirected logs, a closed descriptor), which would
/// turn a purely diagnostic stash warning into a process failure — the
/// rollback delete failure is already counted in `stash_errors` and
/// surfaced separately, so the warning must never be the thing that
/// kills an otherwise successful compression. The write errors are
/// discarded: there is no fallback channel to report a failed warning
/// on, and failing the command here is exactly the regression to avoid.
///
/// Same shape as `tokenless_ccr::warn_soft` and `tokenless_stats::warn_stats`.
pub(crate) fn warn_soft(message: &str) {
    use std::io::Write;
    let mut stderr = std::io::stderr();
    let _ = stderr.write_all(message.as_bytes());
    let _ = stderr.write_all(b"\n");
    let _ = stderr.flush();
}
