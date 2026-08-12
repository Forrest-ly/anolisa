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
//! **Known limitation**: head+tail sampling places the array-truncation marker
//! between the kept head and tail samples, so a truncated array of objects
//! becomes a mixed-type list with a scalar in the middle. TOON 0.5's decoder
//! cannot parse that scalar (even in non-strict mode) and the whole decode
//! fails. The response pipeline tests therefore run with the truncation marker
//! disabled; see `response_toon_roundtrip_known_limitation` for the documented
//! decode failure when the marker is present.

use serde_json::{Value, json};
use tokenless_bench::{response_canonical, schema_canonical};
use tokenless_schema::{ResponseCompressor, SchemaCompressor};

/// Compress a response value (stage 1 of the pipeline).
fn response_compressed(value: &Value) -> Value {
    ResponseCompressor::new().compress(value)
}

/// Run a response value through compress → TOON encode → TOON decode.
///
/// Uses non-strict decode and disables the array-truncation marker: head+tail
/// sampling places the marker between the kept samples, and TOON 0.5 cannot
/// decode a scalar sitting in the middle of an object list (the decode fails
/// outright — see `response_toon_roundtrip_known_limitation`). With the marker
/// disabled, the kept head+tail items are a uniform object list and
/// round-trip correctly, including root-level keys after the array.
fn response_pipeline(value: &Value) -> Value {
    let compressed = ResponseCompressor::new()
        .with_add_truncation_marker(false)
        .compress(value);
    let encoded = toon_format::encode_default(&compressed).expect("TOON encode");
    let opts = toon_format::DecodeOptions::default().with_strict(false);
    toon_format::decode::<Value>(&encoded, &opts).expect("TOON decode")
}

/// Run a schema value through compress → TOON encode → TOON decode.
fn schema_pipeline(value: &Value) -> Value {
    let compressed = SchemaCompressor::new().compress(value);
    let encoded = toon_format::encode_default(&compressed).expect("TOON encode");
    toon_format::decode_default::<Value>(&encoded).expect("TOON decode")
}

#[test]
fn response_pipeline_preserves_tool_and_status() {
    // Tool and status are top-level scalar keys after the large `results`
    // list. Verify them on the compressed value — the compression stage is
    // what must preserve them, independent of TOON codec behavior.
    let compressed = response_compressed(&response_canonical());
    assert_eq!(compressed["tool"], "search_code");
    assert_eq!(compressed["status"], "ok");
}

#[test]
fn response_pipeline_preserves_result_item_fields() {
    let decoded = response_pipeline(&response_canonical());
    let results = decoded["results"]
        .as_array()
        .expect("results array exists after pipeline");
    // The canonical response has 60 items; head+tail sampling keeps 32
    // (16 head + 16 tail; the marker is disabled for the TOON round-trip,
    // see `response_pipeline`). After the round-trip the first element must
    // still be an object carrying the semantic fields.
    let first = &results[0];
    assert!(first["id"].is_number(), "id preserved");
    assert!(first["name"].is_string(), "name preserved");
    assert!(first["path"].is_string(), "path preserved");
    assert!(first["status"].is_string(), "status preserved");
    assert!(first["score"].is_number(), "score preserved");
}

#[test]
fn response_pipeline_drops_noise_fields() {
    let decoded = response_pipeline(&response_canonical());
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

#[test]
fn response_toon_roundtrip_known_limitation() {
    // Documents the known TOON limitation: with the truncation marker between
    // the head and tail samples, a truncated object array becomes a
    // mixed-type list whose mid-list scalar marker TOON 0.5 cannot parse —
    // the whole decode fails, even in non-strict mode. This is expected
    // behavior of the current codec, not a bug in the benchmark suite.
    // Canary: if a future toon-format release learns to parse the mid-list
    // marker, this assertion flips and signals the limitation no longer
    // applies (and the marker can be re-enabled in `response_pipeline`).
    let compressed = ResponseCompressor::new().compress(&response_canonical());
    let encoded = toon_format::encode_default(&compressed).expect("TOON encode");
    let opts = toon_format::DecodeOptions::default().with_strict(false);
    assert!(
        toon_format::decode::<Value>(&encoded, &opts).is_err(),
        "TOON limitation: mid-list truncation marker must fail to decode with current codec"
    );
}
