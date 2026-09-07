use serde_json::json;
use tokenless_protocol::*;

fn attribution() -> Attribution {
    Attribution {
        agent_id: "codex".into(),
        session_id: Some("session-1".into()),
        tool_use_id: Some("call-1".into()),
    }
}

fn requests() -> Vec<RequestEnvelope> {
    vec![
        RequestEnvelope {
            attribution: attribution(),
            request: Request::BeforeModel(BeforeModelRequest {
                tools: vec![json!({"name": "read"})],
                visible_context: json!({"messages": []}),
                capabilities: BeforeModelCapabilities {
                    replace_tools: true,
                    recovery: tokenless_protocol::RecoveryMethod::Shell,
                },
            }),
        },
        RequestEnvelope {
            attribution: attribution(),
            request: Request::PreTool(PreToolRequest {
                tool_name: "Bash".into(),
                arguments: json!({"command": "git status"}),
                command_field: "command".into(),
                capabilities: PreToolCapabilities {
                    replace_arguments: true,
                    block_and_suggest: false,
                },
            }),
        },
        RequestEnvelope {
            attribution: attribution(),
            request: Request::PostTool(PostToolRequest {
                result_kind: ResultKind::Tool,
                tool_name: "Bash".into(),
                content: "{}".into(),
                status: ToolResultStatus::Success,
                content_origin: ContentOrigin::CommandOutput,
                output_optimization: OutputOptimization::None,
                capabilities: PostToolCapabilities {
                    replace_output: true,
                    recovery: tokenless_protocol::RecoveryMethod::None,
                    replace_with_text: true,
                },
            }),
        },
        RequestEnvelope {
            attribution: attribution(),
            request: Request::Retrieve(RetrieveRequest {
                hash_or_marker: "0123456789abcdef01234567".into(),
                visible_markers: vec!["0123456789abcdef01234567".into()],
            }),
        },
    ]
}

#[test]
fn all_request_operations_round_trip_with_fixed_envelope() {
    for request in requests() {
        let json = request.to_json().unwrap();
        let value: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(value["protocol_version"], 2);
        assert_eq!(value["operation"], request.request.operation().wire_str());
        assert!(value.get("input").is_some());
        assert!(value.get("result").is_none());
        assert_eq!(RequestEnvelope::from_json(&json).unwrap(), request);
    }
}

#[test]
fn all_response_operations_round_trip_with_fixed_envelope() {
    let responses = [
        Response::BeforeModel(BeforeModelResponse {
            tools: vec![],
            visible_markers: vec![],
        }),
        Response::PreTool(PreToolResponse {
            arguments: json!({}),
            action: PreToolAction::Passthrough,
            output_optimization: OutputOptimization::None,
        }),
        Response::PostTool(PostToolResponse {
            output: "{}".into(),
            disposition: Disposition::Applied,
            content_type: Some(ContentType::Json),
            applied_operations: vec![
                AppliedOperation::TerminalCleanup,
                AppliedOperation::BuildLogReduction,
                AppliedOperation::JsonCleanup,
                AppliedOperation::JsonRecordReduction,
                AppliedOperation::JsonTruncation,
                AppliedOperation::Toon,
            ],
            recoverability: Recoverability::Lossless,
            before_tokens: 10,
            after_tokens: 4,
            stash_keys: vec![],
            tokenizer_id: TOKENIZER_ID.into(),
            additional_context: None,
        }),
        Response::Retrieve(RetrieveResponse {
            hash: "0123456789abcdef01234567".into(),
            payload: "payload".into(),
        }),
    ];
    for response in responses {
        let envelope = ResponseEnvelope {
            attribution: attribution(),
            response,
        };
        let json = envelope.to_json().unwrap();
        let value: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert!(value.get("result").is_some());
        assert!(value.get("input").is_none());
        assert_eq!(ResponseEnvelope::from_json(&json).unwrap(), envelope);
    }
}

#[test]
fn record_reduction_has_a_stable_wire_name() {
    assert_eq!(
        serde_json::to_string(&AppliedOperation::JsonRecordReduction).unwrap(),
        r#""json_record_reduction""#
    );
    assert_eq!(
        serde_json::to_string(&AppliedOperation::TerminalCleanup).unwrap(),
        r#""terminal_cleanup""#
    );
    assert_eq!(
        serde_json::to_string(&AppliedOperation::BuildLogReduction).unwrap(),
        r#""build_log_reduction""#
    );
    assert_eq!(
        AppliedOperation::JsonRecordReduction.wire_str(),
        "json_record_reduction"
    );
}

#[test]
fn operation_payloads_are_isolated_and_strict() {
    let mut value: serde_json::Value =
        serde_json::from_str(&requests()[0].to_json().unwrap()).unwrap();
    value["input"]["command_field"] = json!("command");
    assert!(RequestEnvelope::from_json(&value.to_string()).is_err());

    let mut value: serde_json::Value =
        serde_json::from_str(&requests()[0].to_json().unwrap()).unwrap();
    value["input"]["retrieve_tool_name"] = json!("tokenless_retrieve");
    assert!(RequestEnvelope::from_json(&value.to_string()).is_err());

    let mut value: serde_json::Value =
        serde_json::from_str(&requests()[0].to_json().unwrap()).unwrap();
    value["input"]["capabilities"]["publish_retrieve_tool"] = json!(true);
    assert!(RequestEnvelope::from_json(&value.to_string()).is_err());

    let mut value: serde_json::Value =
        serde_json::from_str(&requests()[2].to_json().unwrap()).unwrap();
    value["unexpected"] = json!(true);
    assert!(RequestEnvelope::from_json(&value.to_string()).is_err());

    let mut value: serde_json::Value =
        serde_json::from_str(&requests()[2].to_json().unwrap()).unwrap();
    value["attribution"]["unexpected"] = json!(true);
    assert!(RequestEnvelope::from_json(&value.to_string()).is_err());

    let response = ResponseEnvelope {
        attribution: attribution(),
        response: Response::Retrieve(RetrieveResponse {
            hash: "0123456789abcdef01234567".into(),
            payload: "payload".into(),
        }),
    };
    let mut value: serde_json::Value = serde_json::from_str(&response.to_json().unwrap()).unwrap();
    value["result"]["unexpected"] = json!(true);
    assert!(ResponseEnvelope::from_json(&value.to_string()).is_err());

    let response = ResponseEnvelope {
        attribution: attribution(),
        response: Response::BeforeModel(BeforeModelResponse {
            tools: vec![],
            visible_markers: vec![],
        }),
    };
    let mut value: serde_json::Value = serde_json::from_str(&response.to_json().unwrap()).unwrap();
    value["result"]["retrieve_tool"] = json!(null);
    assert!(ResponseEnvelope::from_json(&value.to_string()).is_err());
}

#[test]
fn protocol_v1_is_rejected_before_shape_validation() {
    let error = RequestEnvelope::from_json(
        r#"{"protocol_version":1,"content":"x","agent_id":"a","seam":"post_tool"}"#,
    )
    .unwrap_err();
    assert!(matches!(
        error,
        ProtocolError::UnsupportedVersion { found: 1 }
    ));
}

#[test]
fn response_operation_must_match_request() {
    let response = ResponseEnvelope {
        attribution: attribution(),
        response: Response::Retrieve(RetrieveResponse {
            hash: "0123456789abcdef01234567".into(),
            payload: "payload".into(),
        }),
    };
    assert!(matches!(
        response.ensure_operation(Operation::PostTool),
        Err(ProtocolError::OperationMismatch { .. })
    ));
}

#[test]
fn attribution_accepts_trajectory_identity_names() {
    // A host that reports trajectories to an agent observability backend
    // names these identities conversation_id / tool_call_id. It must be able
    // to bind tokenless to the exact identity it already reports.
    let attribution: Attribution = serde_json::from_str(
        r#"{"agent_id":"agentcore","conversation_id":"conv-1","tool_call_id":"call-1"}"#,
    )
    .unwrap();

    assert_eq!(
        attribution,
        Attribution {
            agent_id: "agentcore".into(),
            session_id: Some("conv-1".into()),
            tool_use_id: Some("call-1".into()),
        }
    );
}

#[test]
fn attribution_serializes_its_own_identity_names() {
    // The alias is input-only: the emitted wire contract stays session_id /
    // tool_use_id so existing adapters and stored requests are unaffected.
    let json = serde_json::to_value(attribution()).unwrap();

    assert_eq!(json["session_id"], "session-1");
    assert_eq!(json["tool_use_id"], "call-1");
    assert!(json.get("conversation_id").is_none());
    assert!(json.get("tool_call_id").is_none());
}

#[test]
fn attribution_rejects_both_spellings_of_one_identity() {
    // Ambiguous input must fail loudly rather than silently pick a side.
    let error = serde_json::from_str::<Attribution>(
        r#"{"agent_id":"agentcore","session_id":"s-1","conversation_id":"c-1"}"#,
    )
    .unwrap_err();

    assert!(
        error.to_string().contains("duplicate field"),
        "unexpected error: {error}"
    );
}

#[test]
fn attribution_still_rejects_unknown_fields() {
    let error =
        serde_json::from_str::<Attribution>(r#"{"agent_id":"agentcore","trajectory_id":"t-1"}"#)
            .unwrap_err();

    assert!(
        error.to_string().contains("unknown field"),
        "unexpected error: {error}"
    );
}

#[test]
fn request_envelope_accepts_trajectory_spelled_attribution() {
    // End-to-end shape a host that reports trajectories to an agent
    // observability backend sends: the envelope carries conversation_id /
    // tool_call_id instead of session_id / tool_use_id. The operation payload
    // is taken from a real round-trip so this case stays focused on
    // attribution naming.
    let native = RequestEnvelope {
        attribution: Attribution {
            agent_id: "agentcore".into(),
            session_id: Some("conv-1".into()),
            tool_use_id: Some("call-1".into()),
        },
        request: Request::PreTool(PreToolRequest {
            tool_name: "Bash".into(),
            arguments: json!({"command": "git status"}),
            command_field: "command".into(),
            capabilities: PreToolCapabilities {
                replace_arguments: true,
                block_and_suggest: false,
            },
        }),
    };
    let mut value: serde_json::Value = serde_json::from_str(&native.to_json().unwrap()).unwrap();
    value["attribution"] = json!({
        "agent_id": "agentcore",
        "conversation_id": "conv-1",
        "tool_call_id": "call-1",
    });

    let envelope = RequestEnvelope::from_json(&value.to_string()).unwrap();

    assert_eq!(envelope.attribution.session_id.as_deref(), Some("conv-1"));
    assert_eq!(envelope.attribution.tool_use_id.as_deref(), Some("call-1"));
    assert_eq!(envelope.attribution.agent_id, "agentcore");
    // The operation payload is untouched by the attribution spelling.
    assert_eq!(envelope.request, native.request);
}
