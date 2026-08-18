//! Contract test for the cosh extension PreToolUse rewrite matcher.
//!
//! The matcher string shipped in `adapters/tokenless/common/cosh-extension.json`
//! is compiled at runtime by cosh-ng's hook system (`HookSystem::matches_tool`)
//! with the Rust `regex` crate and applied as an *unanchored*
//! `Regex::is_match` search (≈ Python's `re.search`). A matcher that fails
//! `Regex::new` does not raise an error in cosh-ng: it silently degrades to
//! exact string comparison, which an anchored alternation never satisfies, so
//! the rewrite hook never fires and rtk rewriting is skipped.
//!
//! The Python twin of this contract (`tests/test_cosh_extension_matcher.py`)
//! pins the matcher against Python's `re`, which accepts syntax the Rust
//! `regex` crate rejects (e.g. lookahead). Only this side of the contract
//! exercises the engine that actually compiles the matcher; both sides must
//! stay in sync with the manifest.

use std::fs;
use std::path::PathBuf;

use regex::Regex;
use serde_json::Value;

/// Tool names cosh-ng fires PreToolUse with. Must stay in sync with
/// `MATCHING_TOOLS` in `tests/test_cosh_extension_matcher.py`.
const MATCHING_TOOLS: &[&str] = &[
    "shell",
    "run_shell_command",
    "Bash",
    "Shell",
    "terminal",
    "exec",
    "process",
];

/// cosh-ng tools (and common foreign shapes) that must never be rewritten:
/// only shell-execution tools may reach rtk. Must stay in sync with
/// `NON_MATCHING_TOOLS` in `tests/test_cosh_extension_matcher.py`.
const NON_MATCHING_TOOLS: &[&str] = &[
    "read_file",
    "write_file",
    "edit",
    "grep",
    "todo",
    "glob",
    "web_search",
    "shell_prompt", // anchored: prefix overlap must not match
    "my_shell",     // anchored: suffix overlap must not match
    "",
];

fn manifest_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../adapters/tokenless/common/cosh-extension.json")
}

/// Return the matcher of the PreToolUse rtk rewrite hook group, mirroring
/// the lookup in `CoshExtensionMatcherTest._rewrite_matcher`.
fn rewrite_matcher() -> String {
    let path = manifest_path();
    let raw = fs::read_to_string(&path).unwrap_or_else(|err| {
        panic!(
            "cosh-extension.json must be readable at {}: {err}",
            path.display()
        )
    });
    let manifest: Value =
        serde_json::from_str(&raw).expect("cosh-extension.json must be valid JSON");
    let groups = manifest
        .pointer("/hooks/PreToolUse")
        .and_then(Value::as_array)
        .expect("hooks.PreToolUse must be an array of matcher groups");
    for group in groups {
        let Some(hooks) = group.get("hooks").and_then(Value::as_array) else {
            continue;
        };
        let is_rewrite = hooks
            .iter()
            .any(|hook| hook.get("name").and_then(Value::as_str) == Some("tokenless-rewrite"));
        if !is_rewrite {
            continue;
        }
        let matcher = group.get("matcher").and_then(Value::as_str).expect(
            "tokenless-rewrite group must declare an explicit string matcher \
             so non-shell tools never reach rtk",
        );
        assert!(
            !matcher.is_empty(),
            "an empty matcher matches every tool, including non-shell tools"
        );
        return matcher.to_string();
    }
    panic!("tokenless-rewrite hook not found in PreToolUse groups");
}

#[test]
fn matcher_compiles_with_rust_regex_crate() {
    // The gap this contract closes: cosh-ng compiles the matcher with
    // Regex::new. Python's `re` accepts patterns (e.g. lookahead) that this
    // engine rejects, so a matcher can pass the Python tests yet fail here
    // and silently disable the hook in cosh-ng.
    let matcher = rewrite_matcher();
    Regex::new(&matcher).unwrap_or_else(|err| {
        panic!(
            "matcher {matcher:?} must be valid Rust regex syntax \
             (cosh-ng compiles it with Regex::new): {err}"
        )
    });
}

#[test]
fn rust_regex_rejects_python_only_syntax() {
    // Sanity check for the contract itself: lookahead is valid Python `re`
    // syntax but unsupported by the Rust `regex` crate, proving that only
    // this Rust-side test can catch such a matcher.
    assert!(Regex::new("(?=shell)").is_err());
}

#[test]
fn matcher_hits_cosh_shell_tool_name() {
    // The regression from the original fix: cosh-ng names its shell tool
    // `shell`, and the matcher must hit it directly without relying on
    // host-side tool-name aliasing.
    let re = Regex::new(&rewrite_matcher()).expect("matcher must compile");
    assert!(
        re.is_match("shell"),
        "matcher must match cosh-ng's lowercase 'shell' tool name directly"
    );
}

#[test]
fn matcher_hits_all_shell_family_names() {
    let re = Regex::new(&rewrite_matcher()).expect("matcher must compile");
    for name in MATCHING_TOOLS {
        assert!(
            re.is_match(name),
            "matcher must match shell-family tool name {name:?}"
        );
    }
}

#[test]
fn matcher_rejects_non_shell_tools() {
    let re = Regex::new(&rewrite_matcher()).expect("matcher must compile");
    for name in NON_MATCHING_TOOLS {
        assert!(
            !re.is_match(name),
            "matcher must not match non-shell tool name {name:?}"
        );
    }
}
