//! Phase 6.2: git versioning end-to-end (auto-commit + log + revert).

use tempfile::tempdir;

use agent_memory::config::AppConfig;
use agent_memory::error::MemoryError;
use agent_memory::service::MemoryService;

fn setup(git_enabled: bool, auto_commit: bool) -> (tempfile::TempDir, MemoryService) {
    let tmp = tempdir().unwrap();
    let mut cfg = AppConfig::default();
    cfg.global.user_id = "git-tester".into();
    cfg.memory.paths.base_dir = tmp.path().to_string_lossy().into();
    cfg.memory.session.base_dir = tmp.path().join("__sessions__").to_string_lossy().into();
    cfg.memory.mount.strategy = agent_memory::mount::MountStrategyKind::Userland;
    cfg.memory.git.enabled = git_enabled;
    cfg.memory.git.auto_commit = auto_commit;
    let svc = MemoryService::new(cfg).unwrap();
    (tmp, svc)
}

#[test]
fn git_disabled_means_no_repo() {
    let (_tmp, svc) = setup(false, false);
    assert!(svc.git.is_none());
    assert!(!svc.mount.root.join(".git").exists());

    let err = svc.mem_log(10, None).unwrap_err();
    assert!(matches!(err, MemoryError::NotImplemented(_)));
}

#[test]
fn git_enabled_initializes_repo_with_gitignore() {
    let (_tmp, svc) = setup(true, false);
    assert!(svc.git.is_some());
    let git_dir = svc.mount.root.join(".git");
    assert!(git_dir.is_dir(), "expected .git/ at {}", git_dir.display());

    let gi = std::fs::read_to_string(svc.mount.root.join(".gitignore")).unwrap();
    assert!(gi.contains(".anolisa/"));

    // Initial commit recorded.
    let log = svc.mem_log(10, None).unwrap();
    assert!(!log.is_empty(), "expected at least an initial commit");
}

#[test]
fn auto_commit_records_writes() {
    let (_tmp, svc) = setup(true, true);
    let baseline = svc.mem_log(50, None).unwrap().len();

    svc.write("notes/hello.md", "first", false).unwrap();
    svc.write("notes/world.md", "second", false).unwrap();

    let after = svc.mem_log(50, None).unwrap();
    assert!(
        after.len() >= baseline + 2,
        "expected ≥{} commits, got {} ({:?})",
        baseline + 2,
        after.len(),
        after.iter().map(|e| &e.summary).collect::<Vec<_>>()
    );
    let summaries: Vec<&str> = after.iter().map(|e| e.summary.as_str()).collect();
    assert!(
        summaries
            .iter()
            .any(|s| s.contains("mem_write notes/hello.md")),
        "missing mem_write hello: {summaries:?}"
    );
}

#[test]
fn read_does_not_create_commits() {
    let (_tmp, svc) = setup(true, true);
    svc.write("a.md", "alpha", false).unwrap();
    let before = svc.mem_log(50, None).unwrap().len();
    let _ = svc.read("a.md").unwrap();
    let _ = svc.read("a.md").unwrap();
    let after = svc.mem_log(50, None).unwrap().len();
    assert_eq!(before, after, "reads should not bump HEAD");
}

#[test]
fn mem_revert_restores_committed_content() {
    let (_tmp, svc) = setup(true, true);
    svc.write("doc.md", "v1", false).unwrap();
    // Now uncommitted edit (write through OS, but skip git auto-commit
    // by going through raw fs).
    let p = svc.mount.root.join("doc.md");
    std::fs::write(&p, "v2 uncommitted").unwrap();
    assert_eq!(svc.read("doc.md").unwrap(), "v2 uncommitted");

    svc.mem_revert("doc.md").unwrap();
    assert_eq!(svc.read("doc.md").unwrap(), "v1");
}

#[test]
fn revert_preserves_pending_work_with_auto_commit() {
    assert_revert_scope(true);
}

#[test]
fn revert_preserves_pending_work_without_auto_commit() {
    assert_revert_scope(false);
}

fn assert_revert_scope(auto_commit: bool) {
    let (_tmp, svc) = setup(true, true);
    svc.write("a.md", "original a", false).unwrap();
    svc.write("b.md", "original b", false).unwrap();
    // Commit the seed files first, then reopen with the setting under test;
    // both cases must start from the same tracked HEAD content.
    let mut cfg = svc.config.clone();
    cfg.memory.git.auto_commit = auto_commit;
    drop(svc);
    let svc = MemoryService::new(cfg).unwrap();
    let repo = git2::Repository::open(&svc.mount.root).unwrap();
    let head = repo.head().unwrap().target().unwrap();
    std::fs::write(svc.mount.root.join("a.md"), "edited a").unwrap();
    std::fs::write(svc.mount.root.join("b.md"), "edited b").unwrap();
    std::fs::write(svc.mount.root.join("untracked.md"), "not ready").unwrap();
    // Include deliberately staged work as well as unstaged/untracked work.
    let mut index = repo.index().unwrap();
    index.add_path(std::path::Path::new("a.md")).unwrap();
    index.add_path(std::path::Path::new("b.md")).unwrap();
    index.write().unwrap();
    let index_before = std::fs::read(svc.mount.root.join(".git/index")).unwrap();

    let restored_from = svc.mem_revert("a.md").unwrap();
    assert_eq!(svc.read("a.md").unwrap(), "original a");
    assert_eq!(svc.read("b.md").unwrap(), "edited b");
    assert_eq!(
        restored_from,
        head.to_string(),
        "revert must return the source HEAD hash"
    );
    assert_eq!(repo.head().unwrap().target().unwrap(), head);
    assert_eq!(
        std::fs::read(svc.mount.root.join(".git/index")).unwrap(),
        index_before
    );
    assert!(
        repo.head()
            .unwrap()
            .peel_to_tree()
            .unwrap()
            .get_path(std::path::Path::new("untracked.md"))
            .is_err()
    );

    svc.mem_revert("b.md").unwrap();
    assert_eq!(
        svc.read("b.md").unwrap(),
        "original b",
        "the first revert consumed the second file's undo"
    );
    assert_eq!(repo.head().unwrap().target().unwrap(), head);
    assert_eq!(svc.read("untracked.md").unwrap(), "not ready");
    assert_eq!(
        std::fs::read(svc.mount.root.join(".git/index")).unwrap(),
        index_before
    );
}

#[test]
fn sequential_reverts_restore_each_files_original_content() {
    let (_tmp, svc) = setup(true, true);
    svc.write("a.md", "original a", false).unwrap();
    svc.write("b.md", "original b", false).unwrap();
    std::fs::write(svc.mount.root.join("a.md"), "edited a").unwrap();
    std::fs::write(svc.mount.root.join("b.md"), "edited b").unwrap();
    svc.mem_revert("a.md").unwrap();
    svc.mem_revert("b.md").unwrap();
    assert_eq!(svc.read("a.md").unwrap(), "original a");
    assert_eq!(svc.read("b.md").unwrap(), "original b");
}

#[test]
fn mem_log_filters_by_path() {
    let (_tmp, svc) = setup(true, true);
    svc.write("alpha.md", "a1", false).unwrap();
    svc.write("beta.md", "b1", false).unwrap();
    svc.write("alpha.md", "a2", true).unwrap();

    let alpha_log = svc.mem_log(50, Some("alpha.md")).unwrap();
    let beta_log = svc.mem_log(50, Some("beta.md")).unwrap();
    assert!(
        alpha_log.len() > beta_log.len(),
        "alpha should have more commits: alpha={} beta={}",
        alpha_log.len(),
        beta_log.len()
    );
}

#[test]
fn revert_unknown_path_errors() {
    let (_tmp, svc) = setup(true, true);
    let err = svc.mem_revert("does-not-exist.md").unwrap_err();
    assert!(matches!(err, MemoryError::NotFound(_)));
}

#[test]
fn concurrent_writes_serialize_into_history() {
    // Regression: two write tools called from concurrent tokio tasks both
    // try to drive `commit_all`, racing on git index.lock. Pre-fix the
    // loser was dropped at debug-level and the user lost a commit.
    //
    // Note: commit_all stages with `add_all(["*"])`, so the first
    // writer's commit may sweep up files a concurrent writer has
    // already flushed to disk; subsequent commits then see no tree
    // change and are skipped by the empty-commit guard. So we don't
    // assert one-commit-per-write; instead we verify (a) every file
    // produced by every thread is reflected in *some* committed tree
    // (no silent loss), and (b) at least one new commit landed beyond
    // baseline (the mutex didn't deadlock).
    use std::sync::Arc;
    use std::thread;

    let (_tmp, svc) = setup(true, true);
    let svc = Arc::new(svc);
    let baseline = svc.mem_log(200, None).unwrap().len();

    let n = 8;
    let mut handles = Vec::with_capacity(n);
    for i in 0..n {
        let svc = Arc::clone(&svc);
        handles.push(thread::spawn(move || {
            let path = format!("notes/c{i}.md");
            svc.write(&path, &format!("body {i}"), false).unwrap();
        }));
    }
    for h in handles {
        h.join().unwrap();
    }

    for i in 0..n {
        let p = format!("notes/c{i}.md");
        let touched = svc.mem_log(200, Some(&p)).unwrap_or_default();
        assert!(
            !touched.is_empty(),
            "expected at least one commit touching {p} after concurrent writes",
        );
    }

    let after = svc.mem_log(200, None).unwrap().len();
    assert!(
        after > baseline,
        "expected at least one new commit beyond baseline {baseline}, got {after}",
    );
}

// ── Regression: every mutating tool bumps HEAD ──────────────────────────────
//
// `auto_commit_for` gates on `git_repo::WRITE_TOOLS`. The tools below write
// tracked content but were missing from that list, so their output reached
// disk uncommitted: `mem_log` never showed it, `mem_revert` still answered
// from the pre-write HEAD, and the next listed tool's `commit_all` swept the
// pending changes into a commit named after an unrelated path.

/// Commit summaries currently in the mount's history, newest first.
fn summaries(svc: &MemoryService) -> Vec<String> {
    svc.mem_log(100, None)
        .unwrap()
        .into_iter()
        .map(|e| e.summary)
        .collect()
}

fn assert_committed(svc: &MemoryService, tool: &str) {
    let log = summaries(svc);
    assert!(
        log.iter()
            .any(|s| s == tool || s.starts_with(&format!("{tool} "))),
        "expected a commit attributed to {tool}, got {log:?}"
    );
}

/// Stronger form: the attributed commit must actually *cover* `path`.
///
/// `commit_all` stages the whole workdir, so a commit merely named after a
/// tool could in principle carry only somebody else's content and still
/// satisfy [`assert_committed`]. `mem_log`'s path filter answers from a real
/// tree diff (`commit_touches_path`), so requiring the tool's commit to appear
/// there pins "attributed to the tool" down to "contains the tool's own write".
fn assert_committed_path(svc: &MemoryService, tool: &str, path: &str) {
    assert_committed(svc, tool);
    let covering = summaries_for_path(svc, path);
    assert!(
        covering
            .iter()
            .any(|s| s == tool || s.starts_with(&format!("{tool} "))),
        "no commit attributed to {tool} covers {path}; commits touching {path}: {covering:?}"
    );
}

/// Commit summaries whose diff touches `path`, newest first.
fn summaries_for_path(svc: &MemoryService, path: &str) -> Vec<String> {
    svc.mem_log(100, Some(path))
        .unwrap()
        .into_iter()
        .map(|e| e.summary)
        .collect()
}

#[test]
fn mem_import_bumps_head() {
    use agent_memory::tools::memory_export::{ExportFilter, memory_export};
    use agent_memory::tools::memory_import::{ImportStrategy, memory_import};

    let (_tmp, svc) = setup(true, true);
    svc.write("notes/keep.md", "portable memory", false)
        .unwrap();
    let archive = memory_export(&svc, &ExportFilter::default()).unwrap();

    svc.remove("notes/keep.md", false).unwrap();
    assert!(!svc.mount.root.join("notes/keep.md").exists());

    memory_import(&svc, &archive, ImportStrategy::Merge, false).unwrap();
    assert_eq!(svc.read("notes/keep.md").unwrap(), "portable memory");
    assert_committed_path(&svc, "mem_import", "notes/keep.md");
}

#[test]
fn task_save_and_close_bump_head() {
    use agent_memory::tools::memory_task::{memory_task_close, memory_task_save};

    let (_tmp, svc) = setup(true, true);
    memory_task_save(
        &svc,
        "ship the git gate",
        Some("in-progress"),
        Some(40),
        None,
        None,
        None,
        None,
        None,
        Some("task-git-1"),
    )
    .unwrap();
    assert!(svc.mount.root.join("tasks/task-git-1.md").exists());
    assert_committed_path(&svc, "memory_task_save", "tasks/task-git-1.md");

    memory_task_close(&svc, "task-git-1", Some("landed")).unwrap();
    assert_committed_path(&svc, "memory_task_close", "tasks/task-git-1.md");
}

#[test]
fn index_refresh_bumps_head() {
    let (_tmp, svc) = setup(true, true);
    svc.write("notes/indexed.md", "# Indexed\n\nbody", false)
        .unwrap();

    let n = agent_memory::tools::memory_index::refresh_index(&svc).unwrap();
    assert!(n > 0, "expected MEMORY.md to list the new note");
    assert!(svc.mount.root.join("MEMORY.md").exists());
    assert_committed_path(&svc, "mem_index_refresh", "MEMORY.md");
}

#[test]
fn consolidated_facts_bump_head() {
    let (_tmp, svc) = setup(true, true);
    // Three writes under one directory are what the working-context rule
    // needs to extract a fact, so consolidation really writes to `facts/`.
    for name in ["config.md", "errors.md", "solution.md"] {
        svc.write(&format!("notes/project-a/{name}"), "work log", false)
            .unwrap();
    }

    let written = svc.consolidate();
    assert!(
        written > 0,
        "expected consolidation to write at least one fact"
    );
    assert!(svc.mount.root.join("facts").is_dir());
    // `facts/facts.jsonl` is appended on every fact written, so it is the one
    // consolidation output whose path is stable enough to assert against.
    assert_committed_path(&svc, "consolidate", "facts/facts.jsonl");
}

/// A `dry_run` import reports what *would* happen and writes nothing, so it
/// must not advance HEAD either. Before `AuditEntry::mutated` this produced a
/// commit whose message read `mem_import 0 imported, 0 skipped` while the tree
/// it recorded was somebody else's pending file — the same misattribution the
/// allowlist exists to prevent, reintroduced through a read-only call shape.
#[test]
fn dry_run_import_leaves_head_and_pending_work_alone() {
    use agent_memory::tools::memory_export::{ExportFilter, memory_export};
    use agent_memory::tools::memory_import::{ImportStrategy, memory_import};

    let (_tmp, svc) = setup(true, true);
    svc.write("notes/keep.md", "portable memory", false)
        .unwrap();
    let archive = memory_export(&svc, &ExportFilter::default()).unwrap();

    // Pending work the user has not decided to commit yet — exactly the state
    // `assert_revert_scope` above establishes and protects.
    std::fs::write(svc.mount.root.join("pending.md"), "not ready").unwrap();
    let before = summaries(&svc);

    let out = memory_import(&svc, &archive, ImportStrategy::Merge, true).unwrap();

    assert!(
        out.starts_with("[DRY RUN]"),
        "expected a preview result, got {out:?}"
    );
    assert_eq!(
        summaries(&svc),
        before,
        "a preview import must not create a commit"
    );
    let repo = git2::Repository::open(&svc.mount.root).unwrap();
    assert!(
        repo.head()
            .unwrap()
            .peel_to_tree()
            .unwrap()
            .get_path(std::path::Path::new("pending.md"))
            .is_err(),
        "a preview import must not sweep unrelated pending work into HEAD"
    );
    assert_eq!(svc.read("pending.md").unwrap(), "not ready");

    // The preview is still an audited call: dropping it from the log to avoid
    // the commit would trade one regression for another.
    let audit = std::fs::read_to_string(svc.mount.audit_log_path()).unwrap();
    assert!(
        audit.lines().any(|line| {
            line.contains("\"tool\":\"mem_import\"") && line.contains("\"mutated\":false")
        }),
        "the preview must stay in the audit log, flagged as non-mutating:\n{audit}"
    );

    // And the real call shape still commits.
    svc.remove("notes/keep.md", false).unwrap();
    memory_import(&svc, &archive, ImportStrategy::Merge, false).unwrap();
    assert_committed_path(&svc, "mem_import", "notes/keep.md");
}

/// Each write tool's commit must carry its own output, not leave it pending
/// for the next allowlisted call to sweep into a commit named after an
/// unrelated path.
#[test]
fn a_tool_commit_does_not_carry_an_earlier_tools_output() {
    use agent_memory::tools::memory_task::memory_task_save;

    let (_tmp, svc) = setup(true, true);
    memory_task_save(
        &svc,
        "ship the git gate",
        Some("in-progress"),
        Some(10),
        None,
        None,
        None,
        None,
        None,
        Some("task-sweep"),
    )
    .unwrap();
    svc.write("notes/after.md", "a later, unrelated write", false)
        .unwrap();

    let touching_task = summaries_for_path(&svc, "tasks/task-sweep.md");
    assert!(
        touching_task
            .iter()
            .any(|s| s.starts_with("memory_task_save")),
        "the task save must be committed by its own commit: {touching_task:?}"
    );
    assert!(
        !touching_task
            .iter()
            .any(|s| s.starts_with("mem_write notes/after.md")),
        "the later mem_write commit swept up the task file: {touching_task:?}"
    );
}
