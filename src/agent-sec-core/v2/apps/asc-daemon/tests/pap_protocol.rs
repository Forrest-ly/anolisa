use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use asc_daemon::{BootstrapConfig, serve};
use asc_daemon_core::{
    PeerCredentials, PolicyAdministration, PolicyAdministrationError, Principal, PrincipalPolicy,
    PrincipalRole, ResourcePage,
};
use asc_daemon_handler::{DaemonDispatcher, JsonRejectionEncoder};
use asc_daemon_protocol::{DaemonRequest, DaemonResponse, RequestId, error_code};
use asc_foundation_types::{ResourceId, Revision};
use asc_pap::PapService;
use asc_pap_repository_memory::ProcessLocalPapRepository;
use asc_policy_engine::PolicyTemplateCompiler;
use asc_policy_types::authoring::PolicyTemplate;
use asc_policy_types::binding::{BindingStatus, BindingView, PreparedBinding};
use asc_policy_types::policy::PreparedPolicy;
use asc_policy_types::scope::{PreparedScope, ScopeSelector};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt as _, AsyncWriteExt as _, BufReader};
use tokio::net::UnixStream;

mod support;

static DIRECTORY_ID: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy)]
struct FixedRolePolicy(PrincipalRole);

impl PrincipalPolicy for FixedRolePolicy {
    fn role_for(&self, _peer: PeerCredentials) -> PrincipalRole {
        self.0
    }
}

#[derive(Clone)]
struct RecordingAdministration {
    binding: BindingView,
    calls: Arc<Mutex<Vec<&'static str>>>,
}

impl RecordingAdministration {
    fn new() -> Self {
        let spec: PreparedBinding = serde_json::from_str(include_str!(
            "../../../crates/policy/asc-policy-types/tests/fixtures/prepared-binding.json"
        ))
        .unwrap();
        Self {
            binding: BindingView {
                spec,
                status: BindingStatus::PendingApply,
            },
            calls: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn record(
        &self,
        principal: &Principal,
        method: &'static str,
    ) -> Result<(), PolicyAdministrationError> {
        if principal.role() != PrincipalRole::PolicyAdministrator {
            return Err(PolicyAdministrationError::Forbidden);
        }
        self.calls.lock().unwrap().push(method);
        Ok(())
    }

    fn policy(&self) -> PreparedPolicy {
        self.binding.spec.policy.clone()
    }

    fn scope(&self) -> PreparedScope {
        self.binding.spec.scope.clone()
    }
}

impl PolicyAdministration for RecordingAdministration {
    fn create_policy(
        &self,
        principal: &Principal,
        _policy_name: &str,
        _template: &PolicyTemplate,
    ) -> Result<PreparedPolicy, PolicyAdministrationError> {
        self.record(principal, "policy.templates.create")?;
        Ok(self.policy())
    }

    fn update_policy(
        &self,
        principal: &Principal,
        _policy_id: &ResourceId,
        _policy_name: &str,
        _template: &PolicyTemplate,
    ) -> Result<PreparedPolicy, PolicyAdministrationError> {
        self.record(principal, "policy.templates.update")?;
        Ok(self.policy())
    }

    fn get_policy(
        &self,
        principal: &Principal,
        _id: &ResourceId,
        _revision: Revision,
    ) -> Result<PreparedPolicy, PolicyAdministrationError> {
        self.record(principal, "policy.templates.get")?;
        Ok(self.policy())
    }

    fn list_policies(
        &self,
        principal: &Principal,
        _limit: u32,
        _offset: u32,
    ) -> Result<ResourcePage<PreparedPolicy>, PolicyAdministrationError> {
        self.record(principal, "policy.templates.list")?;
        Ok(ResourcePage {
            items: vec![self.policy()],
            total: 1,
        })
    }

    fn delete_policy_revision(
        &self,
        principal: &Principal,
        _id: &ResourceId,
        _revision: Revision,
    ) -> Result<PreparedPolicy, PolicyAdministrationError> {
        self.record(principal, "policy.templates.delete")?;
        Ok(self.policy())
    }

    fn create_scope(
        &self,
        principal: &Principal,
        _selector: &ScopeSelector,
    ) -> Result<PreparedScope, PolicyAdministrationError> {
        self.record(principal, "policy.scopes.create")?;
        Ok(self.scope())
    }

    fn update_scope(
        &self,
        principal: &Principal,
        _scope_id: &ResourceId,
        _selector: &ScopeSelector,
    ) -> Result<PreparedScope, PolicyAdministrationError> {
        self.record(principal, "policy.scopes.update")?;
        Ok(self.scope())
    }

    fn get_scope(
        &self,
        principal: &Principal,
        _id: &ResourceId,
        _revision: Revision,
    ) -> Result<PreparedScope, PolicyAdministrationError> {
        self.record(principal, "policy.scopes.get")?;
        Ok(self.scope())
    }

    fn list_scopes(
        &self,
        principal: &Principal,
        _limit: u32,
        _offset: u32,
    ) -> Result<ResourcePage<PreparedScope>, PolicyAdministrationError> {
        self.record(principal, "policy.scopes.list")?;
        Ok(ResourcePage {
            items: vec![self.scope()],
            total: 1,
        })
    }

    fn delete_scope_revision(
        &self,
        principal: &Principal,
        _id: &ResourceId,
        _revision: Revision,
    ) -> Result<PreparedScope, PolicyAdministrationError> {
        self.record(principal, "policy.scopes.delete")?;
        Ok(self.scope())
    }

    fn create_binding(
        &self,
        principal: &Principal,
        _policy_id: &ResourceId,
        _policy_revision: Revision,
        _scope_id: &ResourceId,
        _scope_revision: Revision,
    ) -> Result<BindingView, PolicyAdministrationError> {
        self.record(principal, "policy.bindings.create")?;
        Ok(self.binding.clone())
    }

    fn update_binding(
        &self,
        principal: &Principal,
        _binding_id: &ResourceId,
        _policy_id: &ResourceId,
        _policy_revision: Revision,
        _scope_id: &ResourceId,
        _scope_revision: Revision,
    ) -> Result<BindingView, PolicyAdministrationError> {
        self.record(principal, "policy.bindings.update")?;
        Ok(self.binding.clone())
    }

    fn get_binding(
        &self,
        principal: &Principal,
        _id: &ResourceId,
    ) -> Result<BindingView, PolicyAdministrationError> {
        self.record(principal, "policy.bindings.get")?;
        Ok(self.binding.clone())
    }

    fn list_bindings(
        &self,
        principal: &Principal,
        _limit: u32,
        _offset: u32,
    ) -> Result<ResourcePage<BindingView>, PolicyAdministrationError> {
        self.record(principal, "policy.bindings.list")?;
        Ok(ResourcePage {
            items: vec![self.binding.clone()],
            total: 1,
        })
    }

    fn delete_binding(
        &self,
        principal: &Principal,
        _id: &ResourceId,
    ) -> Result<BindingView, PolicyAdministrationError> {
        self.record(principal, "policy.bindings.delete")?;
        Ok(self.binding.clone())
    }
}

#[test]
fn all_frozen_methods_route_once_and_return_domain_values_directly() {
    let application = RecordingAdministration::new();
    let calls = Arc::clone(&application.calls);
    let expected_policy = serde_json::to_value(application.policy()).unwrap();
    let expected_scope = serde_json::to_value(application.scope()).unwrap();
    let expected_binding = serde_json::to_value(&application.binding).unwrap();
    let handler = DaemonDispatcher::new(
        application,
        Arc::new(FixedRolePolicy(PrincipalRole::PolicyAdministrator)),
    );
    let fixtures: Vec<Value> = serde_json::from_str(include_str!(
        "../../../crates/daemon/asc-daemon-protocol/tests/fixtures/pap-methods.json"
    ))
    .unwrap();

    for (index, fixture) in fixtures.iter().enumerate() {
        let method = fixture["method"].as_str().unwrap();
        let response = handler.handle(
            RequestId::new(format!("request-{index}")).unwrap(),
            PeerCredentials::new(1000, 100, 4242),
            DaemonRequest {
                method: method.to_owned(),
                params: fixture["params"].clone(),
            },
        );
        let DaemonResponse::Success(success) = response else {
            panic!("{method} should succeed");
        };
        let expected = match fixture["resultType"].as_str().unwrap() {
            "PreparedPolicy" => expected_policy.clone(),
            "PreparedScope" => expected_scope.clone(),
            "BindingView" => expected_binding.clone(),
            "ListResult<PreparedPolicy>" => {
                json!({"items": [expected_policy.clone()], "total": 1})
            }
            "ListResult<PreparedScope>" => {
                json!({"items": [expected_scope.clone()], "total": 1})
            }
            "ListResult<BindingView>" => {
                json!({"items": [expected_binding.clone()], "total": 1})
            }
            unexpected => panic!("unexpected result type {unexpected}"),
        };
        assert_eq!(success.result, expected, "wrong direct result for {method}");
    }

    assert_eq!(
        *calls.lock().unwrap(),
        fixtures
            .iter()
            .map(|fixture| fixture["method"].as_str().unwrap())
            .collect::<Vec<_>>()
    );
}

#[test]
fn server_assigned_non_admin_role_is_not_overridden_by_request_data() {
    let application = RecordingAdministration::new();
    let calls = Arc::clone(&application.calls);
    let handler = DaemonDispatcher::new(
        application,
        Arc::new(FixedRolePolicy(PrincipalRole::LocalUser)),
    );
    let response = handler.handle(
        RequestId::new("request-denied").unwrap(),
        PeerCredentials::new(0, 0, 1),
        serde_json::from_value(json!({
            "method": "policy.templates.get",
            "params": {"id": "policy-1", "revision": 1}
        }))
        .unwrap(),
    );

    let DaemonResponse::Error(error) = response else {
        panic!("a local user must not administer Policy");
    };
    assert_eq!(error.error.code.as_str(), error_code::PERMISSION_DENIED);
    assert!(calls.lock().unwrap().is_empty());
}

#[test]
fn unknown_methods_and_invalid_method_params_use_distinct_errors() {
    let handler = DaemonDispatcher::new(
        RecordingAdministration::new(),
        Arc::new(FixedRolePolicy(PrincipalRole::PolicyAdministrator)),
    );
    for (method, params, expected_code, expected_message) in [
        (
            "policy.unknown",
            json!({}),
            error_code::UNKNOWN_METHOD,
            "daemon method is not implemented",
        ),
        (
            "policy.templates.get",
            json!({"id": "policy-1"}),
            error_code::INVALID_REQUEST,
            "missing field `revision`",
        ),
    ] {
        let response = handler.handle(
            RequestId::new(format!("request-{method}")).unwrap(),
            PeerCredentials::new(1000, 100, 4242),
            DaemonRequest {
                method: method.to_owned(),
                params,
            },
        );
        let DaemonResponse::Error(error) = response else {
            panic!("{method} should fail");
        };
        assert_eq!(error.error.code.as_str(), expected_code);
        assert_eq!(error.error.message, expected_message);
    }
}

fn unique_directory() -> PathBuf {
    std::env::temp_dir().join(format!(
        "asc-daemon-pap-protocol-{}-{}",
        std::process::id(),
        DIRECTORY_ID.fetch_add(1, Ordering::Relaxed)
    ))
}

async fn wait_for_socket(path: &Path) {
    tokio::time::timeout(Duration::from_secs(2), async {
        while !path.exists() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("daemon should bind its socket");
}

async fn uds_request(path: &Path, payload: &[u8]) -> Value {
    let mut stream = UnixStream::connect(path).await.unwrap();
    stream.write_all(payload).await.unwrap();
    let mut response = Vec::new();
    BufReader::new(stream)
        .read_until(b'\n', &mut response)
        .await
        .unwrap();
    assert_eq!(response.pop(), Some(b'\n'));
    serde_json::from_slice(&response).unwrap()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn real_uds_executes_the_complete_pap_crud_fixture() {
    let directory = unique_directory();
    std::fs::create_dir(&directory).unwrap();
    let socket_path = directory.join("daemon.sock");
    let application = PapService::new(
        Arc::new(ProcessLocalPapRepository::default()),
        Arc::new(PolicyTemplateCompiler),
    );
    let dispatcher = Arc::new(DaemonDispatcher::new(
        application,
        Arc::new(FixedRolePolicy(PrincipalRole::PolicyAdministrator)),
    ));
    let shutdown = asc_daemon_service::ShutdownToken::new();
    let service_shutdown = shutdown.clone();
    let mut config = BootstrapConfig::new(&socket_path);
    config.service.request_read_timeout = Duration::from_millis(50);
    let task = tokio::spawn(async move {
        serve(
            config,
            dispatcher,
            Arc::new(JsonRejectionEncoder),
            service_shutdown,
        )
        .await
    });

    wait_for_socket(&socket_path).await;
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../crates/daemon/asc-daemon-protocol/tests/fixtures/pap-crud-e2e.json"
    ))
    .unwrap();
    support::run_frozen_pap_crud_scenario(&socket_path, &fixture).await;

    let invalid_params: Value = serde_json::from_str(include_str!(
        "../../../crates/daemon/asc-daemon-protocol/tests/fixtures/pap-invalid-requests.json"
    ))
    .unwrap();
    for case in invalid_params["cases"].as_array().unwrap() {
        let mut payload = serde_json::to_vec(&case["request"]).unwrap();
        payload.push(b'\n');
        let response = uds_request(&socket_path, &payload).await;
        assert!(response["requestId"].as_str().is_some());
        assert_eq!(response["error"], case["expectedError"], "{}", case["name"]);
    }

    let invalid = uds_request(&socket_path, b"not-json\n").await;
    assert!(invalid["requestId"].as_str().is_some());
    assert_eq!(invalid["error"]["code"], error_code::INVALID_REQUEST);

    let idle = UnixStream::connect(&socket_path).await.unwrap();
    let mut rejection = Vec::new();
    tokio::time::timeout(
        Duration::from_secs(1),
        BufReader::new(idle).read_until(b'\n', &mut rejection),
    )
    .await
    .expect("idle connection should receive a bounded rejection")
    .unwrap();
    assert_eq!(rejection.pop(), Some(b'\n'));
    let rejection: Value = serde_json::from_slice(&rejection).unwrap();
    assert!(rejection["requestId"].as_str().is_some());
    assert_eq!(rejection["error"]["code"], error_code::DEADLINE_EXCEEDED);

    shutdown.request();
    task.await.unwrap().unwrap();
    assert!(!socket_path.exists());
    std::fs::remove_dir(directory).unwrap();
}
