// Copyright 2026 Alibaba Cloud
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

//! End-to-end pipeline retention tests.
//!
//! Verifies that canonical payloads traversing the compression pipeline
//! (ResponseCompressor/SchemaCompressor → TOON encode → TOON decode) retain
//! their semantic fields while noise is stripped.
//!
//! Decode outcomes are pinned, never swallowed: the pipeline helper returns
//! the raw `Result` of the TOON decode, and each test asserts the outcome it
//! expects for its compressor configuration. The canonical fixture exercises
//! three shapes, each pinned here:
//!
//! - default config (truncation marker BETWEEN head and tail items): TOON
//!   decode fails entirely — pinned by
//!   `response_pipeline_default_tail_preserve_decode_failure_is_pinned`;
//! - default config with a stash store: the quoted stash marker round-trips
//!   intact — pinned by `response_pipeline_stash_marker_roundtrips_intact`;
//! - head-only config (marker appended after the last kept item): decode
//!   succeeds but loses root-level keys after the array and truncates the
//!   unquoted marker text — pinned by
//!   `response_toon_roundtrip_known_limitation`. The retention assertions
//!   below use this shape so they inspect real decoded output.
//!
//! A pipeline change that moves any of these behaviors fails loudly instead
//! of passing silently on a fallback value.

use std::sync::Arc;

use serde_json::{Value, json};
use tokenless_bench::{response_canonical, schema_canonical};
use tokenless_ccr::{InMemoryStore, StashStore};
use tokenless_schema::{ResponseCompressor, SchemaCompressor};

/// Compress a response value (stage 1 of the pipeline).
fn response_compressed(value: &Value) -> Value {
    ResponseCompressor::new().compress(value)
}

/// Run a response value through compress → TOON encode → TOON decode and
/// return the raw decode outcome.
///
/// Uses non-strict decode because the compressor's truncation marker (a
/// string inside an object array) produces a mixed-type array whose TOON
/// text is ambiguous under strict validation. The outcome is returned to the
/// caller instead of being swallowed: each test pins whether decode must
/// succeed or fail for its configuration, so a regression in the combined
/// pipeline surfaces as a test failure in both directions.
fn response_pipeline(
    value: &Value,
    compressor: ResponseCompressor,
) -> Result<Value, toon_format::ToonError> {
    let compressed = compressor.compress(value);
    let encoded = toon_format::encode_default(&compressed).expect("TOON encode");
    let opts = toon_format::DecodeOptions::default().with_strict(false);
    toon_format::decode::<Value>(&encoded, &opts)
}

/// Run a schema value through compress → TOON encode → TOON decode.
fn schema_pipeline(value: &Value) -> Value {
    let compressed = SchemaCompressor::new().compress(value);
    let encoded = toon_format::encode_default(&compressed).expect("TOON encode");
    toon_format::decode_default::<Value>(&encoded).expect("TOON decode")
}

#[test]
fn response_pipeline_preserves_tool_and_status() {
    // Tool and status are top-level scalar keys that the TOON decoder does
    // not recover when they follow the large `results` list — with the
    // default configuration decode fails entirely, and even the head-only
    // shape that decodes loses them (both pinned below). Verify them on the
    // compressed value: the compression stage is what must preserve them.
    let compressed = response_compressed(&response_canonical());
    assert_eq!(compressed["tool"], "search_code");
    assert_eq!(compressed["status"], "ok");
}

#[test]
fn response_pipeline_preserves_result_item_fields() {
    // Head-only truncation (array_tail_preserve = 0) appends the marker
    // after the last kept item; that shape round-trips through non-strict
    // TOON decode, so the assertions below inspect real decoded output.
    let decoded = response_pipeline(
        &response_canonical(),
        ResponseCompressor::new().with_array_tail_preserve(0),
    )
    .expect("head-only truncation must round-trip through TOON decode");
    let results = decoded["results"]
        .as_array()
        .expect("results array exists after pipeline");
    // The canonical response has 60 items; head-only truncation keeps
    // 32 head + 1 trailing marker = 33 items.
    assert_eq!(results.len(), 33, "32 head items + trailing marker");
    let first = &results[0];
    assert!(first["id"].is_number(), "id preserved");
    assert!(first["name"].is_string(), "name preserved");
    assert!(first["path"].is_string(), "path preserved");
    assert!(first["status"].is_string(), "status preserved");
    assert!(first["score"].is_number(), "score preserved");
    // The marker position survives; its text is truncated by the round-trip
    // (pinned in response_toon_roundtrip_known_limitation).
    assert!(
        results
            .last()
            .and_then(Value::as_str)
            .is_some_and(|s| s.starts_with("<...")),
        "truncation marker survives as the last item"
    );
}

#[test]
fn response_pipeline_drops_noise_fields() {
    let decoded = response_pipeline(
        &response_canonical(),
        ResponseCompressor::new().with_array_tail_preserve(0),
    )
    .expect("head-only truncation must round-trip through TOON decode");
    let obj = decoded.as_object().expect("decoded response is an object");
    // Top-level noise fields dropped by the compressor.
    for k in ["debug", "trace", "logs"] {
        assert!(
            !obj.contains_key(k),
            "{k} should be dropped by the pipeline"
        );
    }
    // Per-item debug field also stripped from kept result entries.
    if let Some(results) = decoded["results"].as_array() {
        for item in results.iter().take(5) {
            if item.is_object() {
                assert!(
                    item.get("debug").is_none(),
                    "debug should be dropped from result items"
                );
            }
        }
    }
}

#[test]
fn response_pipeline_default_tail_preserve_decode_failure_is_pinned() {
    // With the default configuration the truncation marker sits BETWEEN the
    // head and tail items of `results`. The TOON encoder emits the plain
    // marker unquoted and the decoder rejects the scalar row in the middle
    // of the object list, so non-strict decode fails deterministically on
    // the canonical fixture. Pin the failure: if a future TOON or compressor
    // change makes this shape decode, this test fails so the pipeline
    // expectations are revisited deliberately — a silent fallback to the
    // compressed value used to hide exactly this kind of shift.
    let outcome = response_pipeline(&response_canonical(), ResponseCompressor::new());
    outcome.expect_err("pinned known limitation: mid-array marker breaks TOON decode");
}

#[test]
fn response_pipeline_stash_marker_roundtrips_intact() {
    // With a stash store attached, the marker carries the retrieval key and
    // the TOON encoder quotes it, so the full combined pipeline round-trips:
    // decode succeeds, the marker text (including the stash key) survives
    // intact, and the root-level keys after the array are recovered too.
    // This is the reversible-production shape; pin it end to end.
    let store = Arc::new(InMemoryStore::new());
    let decoded = response_pipeline(
        &response_canonical(),
        ResponseCompressor::new().with_stash_store(store.clone()),
    )
    .expect("stash-marker shape must round-trip through TOON decode");
    let results = decoded["results"]
        .as_array()
        .expect("results array exists after pipeline");
    // 32 head + 1 marker + 8 tail = 41 items.
    assert_eq!(results.len(), 41, "head + marker + tail preserved");
    let marker = results[32]
        .as_str()
        .expect("marker sits between head and tail");
    assert!(
        marker.contains("tokenless:"),
        "stash key survives the round-trip intact: {marker}"
    );
    assert_eq!(store.len(), 1, "one stash entry for the dropped middle");
    assert_eq!(decoded["tool"], "search_code", "root keys recovered");
    assert_eq!(decoded["status"], "ok", "root keys recovered");
}

#[test]
fn response_toon_roundtrip_known_limitation() {
    // Pins, with assertions on real decoded output, two known TOON
    // limitations of the head-only truncation shape:
    //
    // 1. root-level scalar keys (`tool`, `status`) appearing AFTER the large
    //    `results` array in the TOON text are not recovered by the decoder;
    // 2. the plain truncation marker is emitted unquoted by the TOON encoder
    //    and its text does not fully survive the round-trip — only the
    //    `<...` prefix is decoded, so retrieval cannot rely on a TOON
    //    round-trip keeping the plain marker intact (the quoted stash marker
    //    shape is unaffected; see
    //    response_pipeline_stash_marker_roundtrips_intact).
    let compressed = response_compressed(&response_canonical());
    assert_eq!(
        compressed["tool"], "search_code",
        "compressor preserves tool"
    );
    assert_eq!(compressed["status"], "ok", "compressor preserves status");
    let decoded = response_pipeline(
        &response_canonical(),
        ResponseCompressor::new().with_array_tail_preserve(0),
    )
    .expect("head-only truncation must round-trip through TOON decode");
    assert!(
        decoded["results"].is_array(),
        "the array preceding the scalar keys round-trips"
    );
    let tool_missing = decoded.get("tool").is_none() || decoded["tool"].is_null();
    let status_missing = decoded.get("status").is_none() || decoded["status"].is_null();
    assert!(
        tool_missing && status_missing,
        "pinned known limitation: root keys after the large array are lost; \
         if this fails, TOON recovered them and these tests need an update"
    );
    let marker = decoded["results"]
        .as_array()
        .and_then(|r| r.last().cloned());
    assert_eq!(
        marker.as_ref().and_then(Value::as_str),
        Some("<..."),
        "pinned known limitation: unquoted marker text is truncated by the \
         TOON round-trip; if this fails, the encoder quoting changed and \
         these tests need an update"
    );
}

#[test]
fn schema_pipeline_preserves_function_name_and_properties() {
    let decoded = schema_pipeline(&schema_canonical());
    assert_eq!(decoded["function"]["name"], "search_code");
    assert!(
        decoded["function"]["parameters"]["properties"].is_object(),
        "properties preserved"
    );
    assert_eq!(
        decoded["function"]["parameters"]["type"], "object",
        "type preserved"
    );
}

#[test]
fn schema_pipeline_preserves_semantic_fields() {
    // The canonical schema does not carry required/enum/default/const, so use
    // a synthetic schema that does — same pattern as schema_retention.rs.
    let schema = json!({
        "function": {
            "name": "my_function",
            "parameters": {
                "type": "object",
                "required": ["field1"],
                "properties": {
                    "field1": {
                        "type": "string",
                        "enum": ["a", "b", "c"],
                        "default": "a",
                        "const": "fixed"
                    }
                }
            }
        }
    });
    let decoded = schema_pipeline(&schema);
    assert_eq!(decoded["function"]["name"], "my_function");
    let params = &decoded["function"]["parameters"];
    assert_eq!(params["type"], "object");
    assert!(params["required"].is_array());
    let f1 = &params["properties"]["field1"];
    assert!(f1["enum"].is_array());
    assert_eq!(f1["default"], "a");
    assert_eq!(f1["const"], "fixed");
}
