//! OpenCode local-plugin registration through a receipt-owned symlink.
//!
//! OpenCode loads plugins at startup. Filesystem registration is verifiable;
//! whether an already-running host loaded the plugin is not.
//!
//! Which OpenCode releases can load a bundle is a property of the bundle, not
//! of the framework, so the driver owns no version floor of its own: a
//! component declares one with `[adapters.compat].framework_version` and
//! enable refuses a host that does not satisfy it.

use std::path::{Component, Path, PathBuf};
use std::time::Duration;

use semver::{Version, VersionReq};

use super::AdapterError;
use super::claim::{
    AdapterClaim, CLAIM_SCHEMA_VERSION, ClaimResource, ClaimResourceKind, ClaimStatus,
    DRIVER_SCHEMA_VERSION, DriverPayload, OpenCodeClaim, validate_plugin_id,
};
use super::driver::{
    AdapterBundle, AdapterCondition, AdapterConditionKind, AdapterStatusReport, AdapterSummary,
    ClaimResourceRef, ConditionStatus, DetectResult, DisableReport, DriverCtx, DriverPlan,
    EnableProgress, FrameworkCommand, FrameworkDriver, HostEnv, PreparedEnable,
    find_binary_in_path, is_executable,
};
use super::util::{bool_status, cli_failure_reason, now_iso8601, symlink_matches};

/// Hard timeout for the read-only `opencode --version` probe. Same budget the
/// other CLI-driving adapters use, so a slow host fails identically everywhere.
const CLI_TIMEOUT: Duration = Duration::from_secs(60);

const RES_LINK: &str = "opencode_plugin_link";

/// Manage local OpenCode plugins without editing the host's JSON configuration.
pub struct OpenCodeDriver;

impl OpenCodeDriver {
    /// Construct the stateless driver.
    pub fn new() -> Self {
        Self
    }

    /// Enforce the adapter's declared OpenCode version requirement. A missing
    /// requirement is a no-op and never probes the CLI, so an adapter that
    /// declares none keeps the driver's original filesystem-only behavior.
    ///
    /// The requirement is a semver constraint, which excludes prereleases
    /// unless the constraint names one: `>=1.3.4` refuses `1.4.0-rc.1`.
    /// Declare the prerelease explicitly when a bundle supports one.
    ///
    /// Fails closed — a requirement the host version cannot be read for is a
    /// refusal, not a pass — because the alternative registers a plugin the
    /// host's loader cannot initialize, which is invisible until OpenCode
    /// starts and reports nothing about the plugin it skipped.
    ///
    /// # Errors
    ///
    /// [`AdapterError::InvalidAdapterInput`] when the declared requirement is
    /// empty or is not a parseable semver constraint, or when `OPENCODE_BIN`
    /// cannot name a program to run; [`AdapterError::FrameworkCli`] when the
    /// probe cannot spawn, times out, exits non-zero, or prints no parseable
    /// version; [`AdapterError::FrameworkVersionMismatch`] when the host
    /// version does not satisfy the requirement.
    fn ensure_supported_version(&self, ctx: &DriverCtx) -> Result<(), AdapterError> {
        // A present-but-empty requirement is a declaration error, not a silent
        // no-op: the author wrote the field and meant to constrain the host.
        let Some(declared) = ctx.framework_version_req.as_deref() else {
            return Ok(());
        };
        let required = declared.trim();
        if required.is_empty() {
            return Err(AdapterError::InvalidAdapterInput {
                component: ctx.component.clone(),
                framework: ctx.framework.clone(),
                reason: "adapter framework_version requirement is present but empty".to_string(),
            });
        }
        let constraint =
            VersionReq::parse(required).map_err(|error| AdapterError::InvalidAdapterInput {
                component: ctx.component.clone(),
                framework: ctx.framework.clone(),
                reason: format!(
                    "invalid adapter framework_version requirement '{required}': {error}"
                ),
            })?;
        let detected = opencode_version(ctx)?;
        if constraint.matches(&detected) {
            return Ok(());
        }
        Err(AdapterError::FrameworkVersionMismatch {
            framework: self.name().to_string(),
            detected: detected.to_string(),
            required: required.to_string(),
        })
    }
}

impl Default for OpenCodeDriver {
    fn default() -> Self {
        Self::new()
    }
}

impl FrameworkDriver for OpenCodeDriver {
    fn name(&self) -> &'static str {
        "opencode"
    }

    fn detect(&self, _env: &HostEnv) -> DetectResult {
        let program = std::env::var_os("OPENCODE_BIN")
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "opencode".into());
        let path = Path::new(&program);
        let found = if path.is_absolute() {
            (path.is_file() && is_executable(path)).then(|| path.to_path_buf())
        } else {
            program.to_str().and_then(find_binary_in_path)
        };
        DetectResult {
            detected: found.is_some(),
            reason: found.map_or_else(
                || format!("OpenCode CLI not found: {}", path.display()),
                |path| format!("OpenCode CLI found at {}", path.display()),
            ),
        }
    }

    fn probe_bundle(&self, root: &Path, declared_entry: Option<&str>) -> bool {
        let entry = Path::new(declared_entry.unwrap_or("plugin.js"));
        valid_entry(entry) && root.join(entry).is_file()
    }

    fn allowed_external_roots(&self, ctx: &DriverCtx) -> Vec<PathBuf> {
        config_dir(ctx.user_home.as_deref()).into_iter().collect()
    }

    fn read_bundle(&self, ctx: &DriverCtx) -> Result<AdapterBundle, AdapterError> {
        let entry = Path::new(ctx.declared_bundle_entry.as_deref().unwrap_or("plugin.js"));
        if !self.probe_bundle(&ctx.resource_root, ctx.declared_bundle_entry.as_deref()) {
            return Err(AdapterError::BundleInvalid {
                root: ctx.resource_root.clone(),
                reason: format!(
                    "OpenCode entry must be an existing relative .js or .ts file: {}",
                    entry.display()
                ),
            });
        }
        let plugin_id = ctx.declared_plugin_id.as_deref().unwrap_or(&ctx.component);
        validate_plugin_id(plugin_id)?;
        Ok(AdapterBundle {
            resource_root: ctx.resource_root.clone(),
            plugin_id: Some(plugin_id.to_string()),
        })
    }

    fn plan_enable(
        &self,
        bundle: &AdapterBundle,
        ctx: &DriverCtx,
    ) -> Result<DriverPlan, AdapterError> {
        // `--dry-run` reaches only this method, so the gate has to live here
        // as well: a preview that plans a link the real run would refuse
        // reports a change that cannot happen.
        self.ensure_supported_version(ctx)?;
        let (link, target) = plugin_paths(bundle, ctx)?;
        let action = if symlink_matches(&link, &target)? {
            "adopt"
        } else {
            "create"
        };
        Ok(DriverPlan {
            framework: self.name().to_string(),
            component: ctx.component.clone(),
            actions: vec![
                format!(
                    "{action} OpenCode plugin link {} -> {} (refuse conflicting paths)",
                    link.display(),
                    target.display()
                ),
                "restart OpenCode to load the plugin".to_string(),
            ],
            register_command: None,
        })
    }

    fn prepare_enable(
        &self,
        bundle: &AdapterBundle,
        ctx: &DriverCtx,
    ) -> Result<(AdapterClaim, PreparedEnable), AdapterError> {
        // Before the Manager persists a receipt: an unsupported host must not
        // leave behind a claim that a later `disable` has to clean up.
        self.ensure_supported_version(ctx)?;
        let (link, target) = plugin_paths(bundle, ctx)?;
        Ok((
            AdapterClaim {
                claim_schema: CLAIM_SCHEMA_VERSION,
                component: ctx.component.clone(),
                framework: self.name().to_string(),
                plugin_id: bundle.plugin_id.clone(),
                adapter_type: ctx.adapter_type.clone(),
                enabled_at: now_iso8601(),
                resource_root: bundle.resource_root.clone(),
                bundle_digest: None,
                source_revision: None,
                materialized_files: Vec::new(),
                driver_schema: DRIVER_SCHEMA_VERSION,
                // The Manager saves this before apply. A process exit must
                // leave a retryable receipt even without an error return.
                status: ClaimStatus::CleanupFailed,
                notices: Vec::new(),
                resources: vec![ClaimResource {
                    id: RES_LINK.to_string(),
                    purpose: "opencode_local_plugin".to_string(),
                    kind: ClaimResourceKind::Symlink { link, target },
                }],
                driver_payload: DriverPayload::OpenCode(OpenCodeClaim {
                    symlink_resource: RES_LINK.to_string(),
                }),
            },
            PreparedEnable::None,
        ))
    }

    fn plan_reenable_cleanup(
        &self,
        prior: &AdapterClaim,
        ctx: &DriverCtx,
    ) -> Result<Vec<String>, AdapterError> {
        let bundle = self.read_bundle(ctx)?;
        let (next_link, next_target) = plugin_paths(&bundle, ctx)?;
        claimed_link(prior)?;
        let mut actions = Vec::new();
        for resource in &prior.resources {
            if let ClaimResourceKind::Symlink { link, target } = &resource.kind
                && (link, target) != (&next_link, &next_target)
            {
                let action = if link == &next_link {
                    "replace prior matching"
                } else {
                    "after registering the replacement, remove prior matching"
                };
                actions.push(format!(
                    "{action} OpenCode plugin link {} -> {}",
                    link.display(),
                    target.display()
                ));
            }
        }
        Ok(actions)
    }

    fn preserve_reenable_facts(
        &self,
        prior: &AdapterClaim,
        next: &mut AdapterClaim,
    ) -> Result<(), AdapterError> {
        claimed_link(prior)?;
        // Keep every outstanding link in the write-ahead receipt until the
        // replacement is installed. Repeated failed upgrades must not lose
        // ownership of an earlier working entry.
        for resource in &prior.resources {
            if !next.resources.iter().any(|r| r.kind == resource.kind) {
                let mut retained = resource.clone();
                retained.id = format!("opencode_retired_{}", next.resources.len());
                next.resources.push(retained);
            }
        }
        Ok(())
    }

    fn validate_prepared_enable(&self, claim: &AdapterClaim) -> Result<(), AdapterError> {
        let (link, target) = claimed_link(claim)?;
        if symlink_matches(link, target)? {
            return Ok(());
        }
        for resource in &claim.resources {
            if let ClaimResourceKind::Symlink {
                link: old_link,
                target: old_target,
            } = &resource.kind
                && old_link == link
                && symlink_matches(old_link, old_target)?
            {
                return Ok(());
            }
        }
        match std::fs::symlink_metadata(link) {
            Err(source) if source.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(source) => Err(AdapterError::Io {
                path: link.to_path_buf(),
                source,
            }),
            Ok(_) => Err(AdapterError::Io {
                path: link.to_path_buf(),
                source: std::io::Error::new(
                    std::io::ErrorKind::AlreadyExists,
                    "refusing conflicting OpenCode plugin path; use the recorded source path without directory aliases",
                ),
            }),
        }
    }

    fn apply_enable(
        &self,
        claim: &mut AdapterClaim,
        _prepared: &PreparedEnable,
        ctx: &DriverCtx,
        _progress: &mut dyn EnableProgress,
    ) -> Result<(), AdapterError> {
        if ctx.dry_run {
            return Ok(());
        }
        let (link, target) = claimed_link(claim)?;
        let mut links = std::collections::BTreeMap::<&Path, Vec<&Path>>::new();
        for resource in &claim.resources {
            if let ClaimResourceKind::Symlink { link, target } = &resource.kind {
                links.entry(link).or_default().push(target);
            }
        }
        // Persisted targets authorize journal replay even when the public entry
        // disappeared during cleanup. Install the new path before retiring old ones.
        if !ctx
            .ops
            .reconcile_symlink(link, &links[link], Some(target))?
        {
            return Err(AdapterError::ReenableCleanupIncomplete {
                component: claim.component.clone(),
                framework: claim.framework.clone(),
                reason: format!("preserving changed OpenCode plugin path {}", link.display()),
            });
        }
        for (old_link, targets) in links {
            if old_link != link && !ctx.ops.reconcile_symlink(old_link, &targets, None)? {
                return Err(AdapterError::ReenableCleanupIncomplete {
                    component: claim.component.clone(),
                    framework: claim.framework.clone(),
                    reason: format!(
                        "preserving changed OpenCode plugin path {}",
                        old_link.display()
                    ),
                });
            }
        }
        claim.resources.retain(|resource| resource.id == RES_LINK);
        claim.status = ClaimStatus::Enabled;
        Ok(())
    }

    fn status(
        &self,
        claim: &AdapterClaim,
        ctx: &DriverCtx,
    ) -> Result<AdapterStatusReport, AdapterError> {
        let (link, target) = claimed_link(claim)?;
        let detected = self.detect(&HostEnv {
            user_home: ctx.user_home.clone(),
        });
        let (status, reason) = match symlink_matches(link, target) {
            Ok(matches) => (
                bool_status(matches),
                format!("{} -> {}", link.display(), target.display()),
            ),
            Err(error) => (ConditionStatus::Unknown, error.to_string()),
        };
        Ok(AdapterStatusReport {
            summary: if claim.status == ClaimStatus::CleanupFailed {
                AdapterSummary::CleanupFailed
            } else if !detected.detected || status == ConditionStatus::False {
                AdapterSummary::Degraded
            } else {
                AdapterSummary::Unknown
            },
            conditions: vec![
                AdapterCondition {
                    kind: AdapterConditionKind::FrameworkDetected,
                    status: bool_status(detected.detected),
                    reason: Some(detected.reason),
                    resource: None,
                },
                AdapterCondition {
                    kind: AdapterConditionKind::SymlinkPresent,
                    status,
                    reason: Some(reason),
                    resource: Some(ClaimResourceRef { id: RES_LINK.to_string() }),
                },
                AdapterCondition {
                    kind: AdapterConditionKind::PluginResourcesLoaded,
                    status: ConditionStatus::Unknown,
                    reason: Some("Restart OpenCode to load the plugin; running-host verification is unavailable".to_string()),
                    resource: None,
                },
                AdapterCondition {
                    kind: AdapterConditionKind::VerificationSupported,
                    status: ConditionStatus::False,
                    reason: Some("Only local plugin registration can be verified".to_string()),
                    resource: None,
                },
            ],
        })
    }

    fn disable(
        &self,
        claim: &AdapterClaim,
        ctx: &DriverCtx,
    ) -> Result<DisableReport, AdapterError> {
        claimed_link(claim)?;
        let mut links = std::collections::BTreeMap::<&Path, Vec<&Path>>::new();
        for resource in &claim.resources {
            if let ClaimResourceKind::Symlink { link, target } = &resource.kind {
                links.entry(link).or_default().push(target);
            }
        }
        let mut report = DisableReport {
            cleanup_complete: true,
            messages: Vec::new(),
        };
        for (link, targets) in links {
            if ctx.dry_run {
                report.messages.push(format!(
                    "would remove matching OpenCode plugin link {}",
                    link.display()
                ));
                continue;
            }
            if !ctx.ops.reconcile_symlink(link, &targets, None)? {
                report.cleanup_complete = false;
                report.messages.push(format!(
                    "preserving changed path {}; restore the recorded link or remove the conflict, then retry disable",
                    link.display()));
            }
        }
        if report.cleanup_complete && !ctx.dry_run {
            report.messages.push(
                "OpenCode plugin links removed or already absent; restart OpenCode to unload it"
                    .to_string(),
            );
        }
        Ok(report)
    }
}

/// The OpenCode CLI to run: `$OPENCODE_BIN` when set, else `opencode`.
///
/// [`FrameworkDriver::detect`] accepts a non-UTF-8 `OPENCODE_BIN` because it
/// only stats the path, while [`FrameworkCommand::program`] is a `String`.
/// Such a value is reported rather than replaced with `opencode` from `PATH`:
/// probing a different binary than the one detection accepted would gate the
/// enable on an unrelated host.
///
/// # Errors
///
/// [`AdapterError::InvalidAdapterInput`] when `OPENCODE_BIN` holds a value
/// that is not valid UTF-8.
fn opencode_program(ctx: &DriverCtx) -> Result<String, AdapterError> {
    match std::env::var_os("OPENCODE_BIN").filter(|value| !value.is_empty()) {
        Some(value) => value
            .into_string()
            .map_err(|value| AdapterError::InvalidAdapterInput {
                component: ctx.component.clone(),
                framework: ctx.framework.clone(),
                reason: format!(
                    "OPENCODE_BIN is not valid UTF-8: {}",
                    value.to_string_lossy()
                ),
            }),
        None => Ok("opencode".to_string()),
    }
}

/// Build the read-only `opencode --version` probe.
///
/// # Errors
///
/// As [`opencode_program`].
fn build_version_cmd(ctx: &DriverCtx) -> Result<FrameworkCommand, AdapterError> {
    Ok(FrameworkCommand {
        program: opencode_program(ctx)?,
        args: vec!["--version".to_string()],
        stdin: None,
        // Keep ANSI escapes out of the token stream the parser scans.
        env_set: vec![("NO_COLOR".to_string(), "1".to_string())],
        env_remove: Vec::new(),
        path_prepend: Vec::new(),
        timeout: CLI_TIMEOUT,
    })
}

/// Probe the host's OpenCode version.
///
/// # Errors
///
/// [`AdapterError::FrameworkCli`] when the probe cannot spawn, times out,
/// exits non-zero, or prints no parseable version.
fn opencode_version(ctx: &DriverCtx) -> Result<Version, AdapterError> {
    let command = build_version_cmd(ctx)?;
    let program = command.program.clone();
    let output = ctx.ops.run_framework_cli(command)?;
    if output.timed_out || !output.success() {
        return Err(AdapterError::FrameworkCli {
            program,
            reason: cli_failure_reason("opencode --version", &output),
        });
    }
    // A host may write its banner to either stream; scanning both keeps the
    // parse independent of that choice.
    let text = format!("{} {}", output.stdout, output.stderr);
    parse_opencode_version(&text).ok_or_else(|| AdapterError::FrameworkCli {
        program,
        reason: format!(
            "cannot parse an OpenCode version from `opencode --version` output '{}'",
            text.trim()
        ),
    })
}

/// First whitespace-delimited token that parses as semver, tolerating a
/// leading `v` and surrounding punctuation, so `opencode v1.3.4 (build 9)`
/// and `1.3.4,` both yield `1.3.4`. The first match wins, which assumes the
/// CLI does not print an unrelated dotted-number token before its own version.
fn parse_opencode_version(output: &str) -> Option<Version> {
    output.split_whitespace().find_map(|token| {
        let trimmed = token.trim_matches(|character: char| {
            !character.is_ascii_alphanumeric() && !matches!(character, '.' | '-' | '+')
        });
        let candidate = trimmed.strip_prefix('v').unwrap_or(trimmed);
        Version::parse(candidate).ok()
    })
}

fn valid_entry(entry: &Path) -> bool {
    entry
        .components()
        .all(|part| matches!(part, Component::Normal(_)))
        && matches!(
            entry.extension().and_then(|s| s.to_str()),
            Some("js" | "ts")
        )
}

fn config_dir(home: Option<&Path>) -> Option<PathBuf> {
    let path = std::env::var_os("OPENCODE_CONFIG_DIR")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("XDG_CONFIG_HOME")
                .filter(|v| !v.is_empty())
                .map(|p| PathBuf::from(p).join("opencode"))
        })
        .or_else(|| home.map(|p| p.join(".config/opencode")))?;
    // Relative config roots would make receipt authority depend on the CWD.
    path.is_absolute().then_some(path)
}

fn plugin_paths(
    bundle: &AdapterBundle,
    ctx: &DriverCtx,
) -> Result<(PathBuf, PathBuf), AdapterError> {
    let config =
        config_dir(ctx.user_home.as_deref()).ok_or_else(|| AdapterError::InvalidAdapterInput {
            component: ctx.component.clone(),
            framework: "opencode".to_string(),
            reason:
                "OpenCode requires an absolute OPENCODE_CONFIG_DIR, XDG_CONFIG_HOME, or user home"
                    .to_string(),
        })?;
    let entry = Path::new(ctx.declared_bundle_entry.as_deref().unwrap_or("plugin.js"));
    let plugin_id = bundle.plugin_id.as_deref().unwrap_or(&ctx.component);
    validate_plugin_id(plugin_id)?;
    let extension = entry
        .extension()
        .and_then(|s| s.to_str())
        .filter(|_| valid_entry(entry))
        .ok_or_else(|| AdapterError::BundleInvalid {
            root: bundle.resource_root.clone(),
            reason: "OpenCode entry must be a relative .js or .ts path".to_string(),
        })?;
    Ok((
        config
            .join("plugins")
            .join(format!("{plugin_id}.{extension}")),
        bundle.resource_root.join(entry),
    ))
}

/// Resolve the receipt's validated link and target for lifecycle and trust persistence.
pub(crate) fn claimed_link(claim: &AdapterClaim) -> Result<(&Path, &Path), AdapterError> {
    if let DriverPayload::OpenCode(payload) = &claim.driver_payload
        && let Some(resource) = claim.resource(&payload.symlink_resource)
        && let ClaimResourceKind::Symlink { link, target } = &resource.kind
    {
        return Ok((link, target));
    }
    Err(AdapterError::BundleInvalid {
        root: claim.resource_root.clone(),
        reason: "OpenCode receipt is missing its plugin symlink resource".to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::parse_opencode_version;
    use semver::Version;

    #[test]
    fn parses_bare_and_labelled_versions() {
        for (output, expected) in [
            ("1.3.4", "1.3.4"),
            ("v1.3.4", "1.3.4"),
            ("opencode 2.0.29", "2.0.29"),
            ("opencode v1.19.2", "1.19.2"),
            ("opencode v2.1.0 (build 99)", "2.1.0"),
            ("1.3.4,\n", "1.3.4"),
            ("1.2.3-test", "1.2.3-test"),
        ] {
            assert_eq!(
                parse_opencode_version(output),
                Version::parse(expected).ok(),
                "output {output:?}"
            );
        }
    }

    #[test]
    fn rejects_output_without_a_version() {
        for output in [
            "",
            "opencode",
            "version",
            "v2",
            "build 99",
            "opencode: command not found",
        ] {
            assert_eq!(parse_opencode_version(output), None, "output {output:?}");
        }
    }

    #[test]
    fn skips_a_diagnostic_line_before_the_version() {
        assert_eq!(
            parse_opencode_version("warning: deprecated config key\nopencode 1.3.4"),
            Version::parse("1.3.4").ok()
        );
    }
}
