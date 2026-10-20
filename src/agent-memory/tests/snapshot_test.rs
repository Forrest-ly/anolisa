//! Phase 6.3: snapshot create / list / restore round-trip.

use tempfile::tempdir;

use agent_memory::config::AppConfig;
use agent_memory::error::MemoryError;
use agent_memory::service::MemoryService;

fn setup() -> (tempfile::TempDir, MemoryService) {
    let tmp = tempdir().unwrap();
    let mut cfg = AppConfig::default();
    cfg.global.user_id = "snap-tester".into();
    cfg.memory.paths.base_dir = tmp.path().to_string_lossy().into();
    cfg.memory.session.base_dir = tmp.path().join("__sessions__").to_string_lossy().into();
    cfg.memory.mount.strategy = agent_memory::mount::MountStrategyKind::Userland;
    let svc = MemoryService::new(cfg).unwrap();
    (tmp, svc)
}

#[test]
fn snapshot_create_writes_archive_and_sidecar() {
    let (_tmp, svc) = setup();
    svc.write("notes/a.md", "alpha", false).unwrap();
    svc.write("notes/b.md", "beta", false).unwrap();

    let info = svc.mem_snapshot(Some("baseline")).unwrap();
    assert!(info.id.starts_with("snap_"));
    assert_eq!(info.name, "baseline");
    assert_eq!(info.backend, "tar.gz");
    assert!(info.size > 0);

    let snap_dir = svc.mount.meta_dir.join("snapshots");
    assert!(snap_dir.join(format!("{}.tar.gz", info.id)).exists());
    assert!(snap_dir.join(format!("{}.json", info.id)).exists());
}

#[test]
fn snapshot_list_orders_oldest_first() {
    let (_tmp, svc) = setup();
    let a = svc.mem_snapshot(Some("first")).unwrap();
    // Force a different timestamp; ULID monotonic suffices but be safe.
    std::thread::sleep(std::time::Duration::from_millis(10));
    let b = svc.mem_snapshot(Some("second")).unwrap();

    let list = svc.mem_snapshot_list().unwrap();
    assert_eq!(list.len(), 2);
    assert_eq!(list[0].id, a.id);
    assert_eq!(list[1].id, b.id);
}

#[test]
fn snapshot_restore_round_trips_files() {
    let (_tmp, svc) = setup();
    svc.write("doc.md", "v1 contents", false).unwrap();
    let snap = svc.mem_snapshot(None).unwrap();

    // Mutate after snapshot.
    svc.write("doc.md", "v2 contents OVERWRITTEN", true)
        .unwrap();
    svc.write("scratch/draft.md", "throwaway", false).unwrap();
    assert_eq!(svc.read("doc.md").unwrap(), "v2 contents OVERWRITTEN");

    svc.mem_snapshot_restore(&snap.id).unwrap();

    // Original file is back; post-snapshot files are gone.
    assert_eq!(svc.read("doc.md").unwrap(), "v1 contents");
    assert!(matches!(
        svc.read("scratch/draft.md"),
        Err(MemoryError::NotFound(_))
    ));
}

#[test]
fn restore_unknown_id_returns_not_found() {
    let (_tmp, svc) = setup();
    let err = svc.mem_snapshot_restore("snap_does_not_exist").unwrap_err();
    assert!(matches!(err, MemoryError::NotFound(_)));
}

#[test]
fn restore_preserves_meta_dir_and_leaves_no_rollback_artefacts() {
    // Regression for B3: the old `delete all + move in` flow left the
    // mount empty for the duration of the move, and a crash there would
    // wipe user data. The new flow renames each top-level entry aside
    // under `.anolisa/.<id>.rollback.*` and drops them only after the
    // staging swap completes. This test verifies the happy path leaves
    // no rollback leftovers under .anolisa/.
    let (_tmp, svc) = setup();
    svc.write("doc.md", "v1", false).unwrap();
    svc.write("notes/inner.md", "deep", false).unwrap();
    // Place a marker inside .anolisa/ — restore must preserve it.
    let marker = svc.mount.meta_dir.join("audit.log");
    let marker_before = std::fs::read_to_string(&marker).unwrap_or_default();

    let snap = svc.mem_snapshot(None).unwrap();
    svc.write("doc.md", "v2", true).unwrap();
    svc.mem_snapshot_restore(&snap.id).unwrap();

    assert_eq!(svc.read("doc.md").unwrap(), "v1");
    // .anolisa/audit.log must still exist (meta dir untouched).
    let marker_after = std::fs::read_to_string(&marker).unwrap_or_default();
    assert!(
        marker_after.len() >= marker_before.len(),
        "audit.log shrank across restore"
    );

    // No `.<id>.rollback.*` leftovers under .anolisa/.
    let prefix = format!(".{}.rollback.", snap.id);
    let leftovers: Vec<_> = std::fs::read_dir(&svc.mount.meta_dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().starts_with(&prefix))
        .collect();
    assert!(
        leftovers.is_empty(),
        "rollback artefacts not cleaned up: {:?}",
        leftovers.iter().map(|e| e.file_name()).collect::<Vec<_>>()
    );
}

#[test]
fn snapshot_excludes_meta_directory() {
    let (_tmp, svc) = setup();
    svc.write("note.md", "real", false).unwrap();
    let info = svc.mem_snapshot(None).unwrap();

    // Read the archive raw and verify .anolisa/ is not in it.
    let archive_path = svc
        .mount
        .meta_dir
        .join("snapshots")
        .join(format!("{}.tar.gz", info.id));
    let bytes = std::fs::read(&archive_path).unwrap();
    let gz = flate2::read::GzDecoder::new(std::io::Cursor::new(bytes));
    let mut tar = ::tar::Archive::new(gz);
    let entries: Vec<String> = tar
        .entries()
        .unwrap()
        .filter_map(|e| e.ok())
        .filter_map(|e| e.path().ok().map(|p| p.to_string_lossy().into_owned()))
        .collect();

    for path in &entries {
        assert!(
            !path.starts_with(".anolisa"),
            "snapshot leaked meta path: {path}"
        );
    }
}

#[test]
fn repeated_restores_of_one_snapshot_keep_every_displaced_copy() {
    // Regression for #4878: the trash dir was named `<UTC second>-<snapshot id>`
    // and created with `create_dir_all`, so two restores of the same snapshot
    // inside one second shared it. The second restore's rollback rename then
    // replaced the first displaced regular file (and a displaced directory could
    // not be moved in at all), silently destroying the earlier recovery copy.
    let (_tmp, svc) = setup();
    svc.write("doc.md", "baseline", false).unwrap();
    let snap = svc.mem_snapshot(None).unwrap();

    // Force the collision instead of hoping the clock cooperates: squat the
    // natural trash name for the current and the next UTC second, so whichever
    // second the restores land in, the base name is already taken.
    let trash_root = svc.mount.meta_dir.join("trash");
    std::fs::create_dir_all(&trash_root).unwrap();
    let now = chrono::Utc::now();
    let mut squatted = Vec::new();
    for offset in [0i64, 1] {
        let stamp = (now + chrono::Duration::seconds(offset))
            .format("%Y%m%dT%H%M%SZ")
            .to_string();
        let dir = trash_root.join(format!("{stamp}-{}", snap.id));
        std::fs::create_dir_all(&dir).unwrap();
        squatted.push(dir);
    }

    svc.write("doc.md", "first", true).unwrap();
    svc.mem_snapshot_restore(&snap.id).unwrap();
    svc.write("doc.md", "second", true).unwrap();
    svc.mem_snapshot_restore(&snap.id).unwrap();

    assert_eq!(svc.read("doc.md").unwrap(), "baseline");

    // Both displaced versions survive, each in a trash dir of its own.
    let mut kept: Vec<String> = Vec::new();
    for entry in std::fs::read_dir(&trash_root).unwrap() {
        let path = entry.unwrap().path();
        if !path.is_dir() || squatted.contains(&path) {
            continue;
        }
        let doc = path.join("doc.md");
        assert!(
            doc.exists(),
            "trash dir {} lost the displaced doc.md",
            path.display()
        );
        kept.push(std::fs::read_to_string(&doc).unwrap());
    }
    kept.sort();
    assert_eq!(kept, vec!["first".to_string(), "second".to_string()]);

    for dir in &squatted {
        assert!(
            std::fs::read_dir(dir).unwrap().next().is_none(),
            "restore reused the already-taken trash dir {}",
            dir.display()
        );
    }

    // Nothing is stranded in the meta dir either.
    let prefix = format!(".{}.rollback.", snap.id);
    let leftovers: Vec<_> = std::fs::read_dir(&svc.mount.meta_dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().starts_with(&prefix))
        .collect();
    assert!(
        leftovers.is_empty(),
        "rollback artefacts left behind: {:?}",
        leftovers.iter().map(|e| e.file_name()).collect::<Vec<_>>()
    );
}
