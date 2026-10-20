//! Linux user-namespace mount strategy (Phase 2).
//!
//! Pipeline (run once at process startup):
//!
//! 1. `unshare(CLONE_NEWUSER | CLONE_NEWNS)` — enter fresh `(user, mount)`
//!    namespaces. Inside, our uid is 0 (mapped to the real uid on host).
//! 2. Write `/proc/self/setgroups = "deny"` (required before gid_map on
//!    kernels 4.6+) and `/proc/self/{uid_map,gid_map}` with a single
//!    line `0 <real> 1`.
//! 3. `mount("none", "/mnt", "tmpfs", ...)` — overlay a private tmpfs on
//!    the host `/mnt` so we can create directories without touching real
//!    `/mnt`.
//! 4. `mkdir -p /mnt/memory/<ns>` and bind-mount `<base>/<ns>/` onto it.
//!
//! After this, every subsequent file IO that targets `/mnt/memory/<ns>/`
//! is transparently redirected to `<base>/<ns>/` on the home filesystem,
//! and host-side processes see nothing under `/mnt`.

use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};

use nix::mount::{MsFlags, mount};
use nix::sched::{CloneFlags, unshare};
use nix::unistd::{getegid, geteuid};

use crate::error::{MemoryError, Result};
use crate::ns::Namespace;

use super::MountStrategy;

const MNT_ROOT: &str = "/mnt";
const MNT_MEMORY: &str = "/mnt/memory";

/// Records that `unshare(NEWUSER|NEWNS)` has run. The syscall is one-shot
/// per task — a second call EINVALs — so it is set the moment the unshare
/// succeeds, *before* the map writes that can still fail. Read it through
/// [`stage1_action`], never on its own: `UNSHARED` alone says "stage 1
/// started", not "stage 1 finished".
static UNSHARED: AtomicBool = AtomicBool::new(false);
/// Records that the one-shot map writes (setgroups / uid_map / gid_map)
/// all landed — the real stage-1 completion signal. A failed map write
/// leaves the process inside a namespace with no uid mapping, where
/// `geteuid()` reads 65534 and inode creation on a fresh filesystem fails
/// EOVERFLOW, yet the stage-2 mount steps still run (the process holds full
/// capabilities in the namespace it created). A retry that consulted only
/// `UNSHARED` therefore reached those steps and either returned `Ok` from
/// the broken namespace or surfaced a recoverable-looking mount error that
/// `auto` swallowed into a silent userland fallback.
static MAPS_OK: AtomicBool = AtomicBool::new(false);
/// Inner detail of why stage 1 was abandoned, kept so every later `enter()`
/// reports the original failure instead of a contextless "already broken".
static MAPS_FAILURE: OnceLock<String> = OnceLock::new();
/// Tracks completion of the mount steps (private /, tmpfs /mnt, mkdir
/// /mnt/memory). Each mount call is individually idempotent (EBUSY when
/// already done), so a partial failure is safe to retry by calling
/// enter() again — which `auto` strategy needs to fall back cleanly.
static MOUNTS_READY: AtomicBool = AtomicBool::new(false);
/// Serialises concurrent enter() calls so the unshare/maps stage runs
/// exactly once even under contention.
static INIT_LOCK: Mutex<()> = Mutex::new(());

/// What a (re)entrant `enter()` must do with the one-shot stage 1, derived
/// from the recorded flags. Extracted from `enter` so the retry matrix is
/// unit-testable: forcing a real `uid_map` write to fail needs root or
/// userns control of `/proc`, which is not portable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Stage1Action {
    /// Nothing recorded yet — run unshare + maps.
    Run,
    /// Maps landed — stage 1 is complete, go straight to the mount steps.
    Skip,
    /// unshare ran but a map write failed. The process is stuck inside a
    /// half-initialised namespace that can never be repaired from within,
    /// so every retry must fail with `UserNsUnrecoverable`.
    Poisoned,
}

/// Maps success is the only completion signal; `unshared` without it means
/// stage 1 started and never finished. `maps_ok` wins even on its own,
/// because re-running `unshare(NEWUSER)` EINVALs whatever else the flags
/// say.
fn stage1_action(unshared: bool, maps_ok: bool) -> Stage1Action {
    if maps_ok {
        Stage1Action::Skip
    } else if unshared {
        Stage1Action::Poisoned
    } else {
        Stage1Action::Run
    }
}

pub struct LinuxUserNsMount;

impl LinuxUserNsMount {
    /// Enter the new namespace + set up `/mnt` as a private tmpfs.
    /// Idempotent at the process level: if mounts are already set up,
    /// returns Ok. If a prior call failed mid-mount, retry mount steps
    /// without re-running the one-shot unshare/maps stage.
    ///
    /// # Errors
    ///
    /// `UserNsUnrecoverable` when an earlier call unshared but failed a map
    /// write. The process is then permanently inside a half-initialised
    /// namespace, so this keeps failing on every retry instead of letting
    /// the mount steps paper over it with an `Ok`.
    pub fn enter() -> Result<Self> {
        if MOUNTS_READY.load(Ordering::Acquire) {
            return Ok(Self);
        }

        // INIT_LOCK establishes happens-before; Acquire is sufficient for
        // the second-check load (no need for SeqCst).
        let _guard = INIT_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        if MOUNTS_READY.load(Ordering::Acquire) {
            return Ok(Self);
        }

        // Stage 1: unshare + write maps, at most once. `UNSHARED` is marked
        // immediately after the unshare so a maps-stage failure cannot tempt
        // a retry into unshare(NEWUSER) again (EINVAL); `MAPS_OK` is marked
        // only once all three writes landed, and the gap between the two is
        // exactly what `Stage1Action::Poisoned` reports. Maps failures are
        // UserNsUnrecoverable so the auto-fallback path knows the process is
        // in a broken user namespace and refuses to keep running under
        // userland (which would silently produce wrong ownership /
        // permissions on every home-dir syscall).
        match stage1_action(
            UNSHARED.load(Ordering::Acquire),
            MAPS_OK.load(Ordering::Acquire),
        ) {
            Stage1Action::Skip => {}
            Stage1Action::Poisoned => {
                return Err(MemoryError::UserNsUnrecoverable(poisoned_maps_reason()));
            }
            Stage1Action::Run => {
                // Record the launching uid *before* the unshare below makes
                // `geteuid()` report the mapped one, so the per-user session
                // fallback name can still tell host users apart. `main` already
                // did this; it is repeated here because `enter` is also reached
                // from `MemoryService::new` in library consumers.
                crate::host::capture_host_uid();

                let real_uid = geteuid().as_raw();
                let real_gid = getegid().as_raw();

                unshare(CloneFlags::CLONE_NEWUSER | CloneFlags::CLONE_NEWNS)
                    .map_err(|e| MemoryError::Other(format!("unshare(NEWUSER|NEWNS): {e}")))?;
                UNSHARED.store(true, Ordering::Release);

                // From here on we are inside a fresh user namespace; any
                // error is unrecoverable for this process. Record the detail
                // before returning so later retries can quote it.
                if let Err(e) = write_maps(real_uid, real_gid) {
                    if let MemoryError::UserNsUnrecoverable(detail) = &e {
                        let _ = MAPS_FAILURE.set(detail.clone());
                    }
                    return Err(e);
                }
                MAPS_OK.store(true, Ordering::Release);
            }
        }

        // Stage 2: mount setup. Each step is idempotent — EBUSY signals
        // a prior call already established the state — so a failure at
        // any step can be retried by calling enter() again without
        // re-entering the namespace.
        if let Err(e) = mount::<str, str, str, str>(
            None,
            "/",
            None,
            MsFlags::MS_PRIVATE | MsFlags::MS_REC,
            None,
        ) {
            if e != nix::errno::Errno::EBUSY {
                return Err(MemoryError::Other(format!("mount-private /: {e}")));
            }
        }

        match mount::<str, str, str, str>(
            Some("none"),
            MNT_ROOT,
            Some("tmpfs"),
            MsFlags::empty(),
            None,
        ) {
            Ok(()) => {}
            Err(nix::errno::Errno::EBUSY) => {
                tracing::debug!("/mnt tmpfs already established in this namespace");
            }
            Err(e) => return Err(MemoryError::Other(format!("tmpfs /mnt: {e}"))),
        }

        std::fs::create_dir_all(MNT_MEMORY)?;

        MOUNTS_READY.store(true, Ordering::Release);
        Ok(Self)
    }
}

/// The three one-shot map writes, in kernel-mandated order
/// (setgroups=deny must precede gid_map).
fn write_maps(real_uid: u32, real_gid: u32) -> Result<()> {
    write_proc_unrecoverable("/proc/self/setgroups", "deny")?;
    write_proc_unrecoverable("/proc/self/uid_map", &format!("0 {real_uid} 1"))?;
    write_proc_unrecoverable("/proc/self/gid_map", &format!("0 {real_gid} 1"))
}

/// Message for a retry that finds stage 1 poisoned. The detail is always
/// recorded by the path that sets `UNSHARED` without `MAPS_OK`; the fallback
/// keeps the error actionable if that ever stops holding.
fn poisoned_maps_reason() -> String {
    match MAPS_FAILURE.get() {
        Some(detail) => {
            format!("retry after an earlier failed attempt ({detail}); the maps are write-once")
        }
        None => {
            "retry after an earlier attempt that unshared without completing the maps".to_string()
        }
    }
}

/// Write `body` into the procfs path that controls a one-shot user-ns
/// mapping (setgroups / uid_map / gid_map). Any error here means the
/// process is stuck inside a half-initialised user namespace, so the
/// error is wrapped in `UserNsUnrecoverable` to prevent the caller's
/// fallback path from silently downgrading to userland.
fn write_proc_unrecoverable(path: &str, body: &str) -> Result<()> {
    write_proc(path, body).map_err(|e| {
        MemoryError::UserNsUnrecoverable(format!(
            "wrote unshare(NEWUSER) but {path} update failed: {e}"
        ))
    })
}

impl MountStrategy for LinuxUserNsMount {
    fn ensure(&self, ns: &Namespace, base: &Path) -> Result<PathBuf> {
        // 1. Ensure backing data dir exists in the user's home.
        let backing = base.join(ns.dir_name());
        std::fs::create_dir_all(&backing)?;
        // 2. Populate README + manifest in the backing dir BEFORE bind —
        //    bind transparently exposes them at the public path.
        super::populate_mount_dir(&backing, ns)?;

        // 3. Public path inside our namespace: /mnt/memory/<ns>/.
        let public = PathBuf::from(MNT_MEMORY).join(ns.dir_name());
        std::fs::create_dir_all(&public)?;

        // 4. bind-mount backing → public. If already bound (e.g. retried),
        //    `mount` returns EBUSY; treat as success.
        match mount::<Path, Path, str, str>(Some(&backing), &public, None, MsFlags::MS_BIND, None) {
            Ok(()) => {}
            Err(nix::errno::Errno::EBUSY) => {
                tracing::debug!(
                    "bind {} -> {} already mounted",
                    backing.display(),
                    public.display()
                );
            }
            Err(e) => {
                return Err(MemoryError::Other(format!(
                    "bind {} -> {}: {e}",
                    backing.display(),
                    public.display()
                )));
            }
        }

        Ok(public)
    }

    fn name(&self) -> &'static str {
        "linux-userns"
    }
}

fn write_proc(path: &str, body: &str) -> Result<()> {
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .open(path)
        .map_err(|e| MemoryError::Other(format!("open {path}: {e}")))?;
    f.write_all(body.as_bytes())
        .map_err(|e| MemoryError::Other(format!("write {path}: {e}")))?;
    Ok(())
}

/// Read /proc/self/uid_map; a one-id mapping indicates we're in a user
/// namespace we created (vs the init ns which has the full 0..2^32-1
/// range). Used by `info` for diagnostics.
pub fn in_user_namespace() -> bool {
    // Format per line: "inside_uid  outside_uid  range".
    // - Init ns:        "0  0  4294967295"
    // - Rootless unshare with `0 <real> 1`: "0  <real>  1"
    //
    // Pre-fix this checked the second column for `"0"`, which falsely
    // reported "not in userns" when a root user (uid=0) had unshared,
    // since the outside_uid was still 0. Inspecting the range is the
    // reliable signal: anything other than the full 2^32 means we're
    // inside a confined user namespace.
    match std::fs::read_to_string("/proc/self/uid_map") {
        Ok(s) => s
            .lines()
            .next()
            .and_then(|l| l.split_whitespace().nth(2))
            .map(|range| range != "4294967295")
            .unwrap_or(false),
        Err(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::{Stage1Action, stage1_action};

    #[test]
    fn a_fresh_process_runs_stage1() {
        assert_eq!(stage1_action(false, false), Stage1Action::Run);
    }

    #[test]
    fn completed_maps_skip_stage1_on_retry() {
        assert_eq!(stage1_action(true, true), Stage1Action::Skip);
    }

    #[test]
    fn unshare_without_maps_poisons_every_retry() {
        // Regression guard: this combination used to fall through to the
        // stage-2 mount steps inside a namespace with no uid mapping, which
        // either returned `Ok` from the broken namespace or surfaced a
        // recoverable-looking mount error that `auto` downgraded to a silent
        // userland fallback — while the process stayed broken.
        assert_eq!(stage1_action(true, false), Stage1Action::Poisoned);
    }

    #[test]
    fn maps_ok_alone_never_re_unshares() {
        // Unreachable in practice — the maps are only written after the
        // unshare — but the completion flag is authoritative: a second
        // unshare(NEWUSER) would EINVAL.
        assert_eq!(stage1_action(false, true), Stage1Action::Skip);
    }
}
