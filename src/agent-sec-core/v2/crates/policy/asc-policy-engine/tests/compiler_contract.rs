use asc_foundation_types::Revision;
use asc_pap::PolicyCompiler;
use asc_policy_engine::PolicyTemplateCompiler;
use asc_policy_types::authoring::{PolicyTemplate, TemplateEnvelope};
use asc_policy_types::identifiers::PolicyId;

#[test]
fn golden_freezes_the_compiler_input_and_output() {
    let contract: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/compiler-contract.json")).unwrap();
    let input: TemplateEnvelope = serde_json::from_value(contract["input"].clone()).unwrap();

    let output = PolicyTemplateCompiler.lower(&input).unwrap();

    assert_eq!(serde_json::to_value(output).unwrap(), contract["output"]);
}

#[test]
fn rejects_empty_files_and_unimplemented_template_kinds() {
    let policy_id = PolicyId::new("unsupported").unwrap();
    let revision = Revision::new(1).unwrap();
    let empty = TemplateEnvelope {
        policy_id: policy_id.clone(),
        revision,
        template: PolicyTemplate::PreventFileDeletion { files: Vec::new() },
    };
    assert_eq!(
        PolicyTemplateCompiler.lower(&empty).unwrap_err().path,
        "template.files"
    );

    let unsupported = TemplateEnvelope {
        policy_id,
        revision,
        template: PolicyTemplate::HighSensitivityReadDeny {
            files: vec!["/secret".to_owned()],
        },
    };
    assert_eq!(
        PolicyTemplateCompiler.lower(&unsupported).unwrap_err().path,
        "template.kind"
    );
}
