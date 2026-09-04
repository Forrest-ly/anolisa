use asc_foundation_types::{ResourceId, Revision};
use asc_pap::{Page, PapError, PapRepository, PapService, PolicyCompiler};
use asc_policy_types::authoring::PolicyTemplate;
use asc_policy_types::binding::BindingView;
use asc_policy_types::policy::PreparedPolicy;
use asc_policy_types::scope::{PreparedScope, ScopeSelector};

use crate::{Principal, PrincipalRole};

/// Stable PAP application failures safe for a daemon adapter to project.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum PolicyAdministrationError {
    /// The server-assigned principal lacks Policy administration authority.
    #[error("principal is not authorized to administer policy")]
    Forbidden,
    /// Authored input failed domain validation.
    #[error("policy input failed domain validation")]
    InvalidArgument,
    /// Current revision or lifecycle state conflicts with the request.
    #[error("policy request conflicts with current state")]
    Conflict,
    /// The requested current resource does not exist.
    #[error("requested policy resource was not found")]
    NotFound,
    /// No further positive revision can be allocated.
    #[error("revision space is exhausted")]
    ResourceExhausted,
    /// Serialization or persistence failed with details withheld.
    #[error("policy state could not be processed")]
    Internal,
}

impl From<PapError> for PolicyAdministrationError {
    fn from(error: PapError) -> Self {
        match error {
            PapError::InvalidIdentifier(_)
            | PapError::InvalidPolicyName(_)
            | PapError::InvalidPolicy(_)
            | PapError::InvalidScope(_)
            | PapError::InvalidBinding(_)
            | PapError::InvalidPagination => Self::InvalidArgument,
            PapError::Conflict | PapError::OperationInProgress => Self::Conflict,
            PapError::NotFound => Self::NotFound,
            PapError::RevisionExhausted => Self::ResourceExhausted,
            PapError::Serialization | PapError::Persistence => Self::Internal,
        }
    }
}

/// Bounded application query result with its total before pagination.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResourcePage<T> {
    /// Selected current records.
    pub items: Vec<T>,
    /// Total matching records before pagination.
    pub total: u64,
}

impl<T> From<Page<T>> for ResourcePage<T> {
    fn from(page: Page<T>) -> Self {
        Self {
            items: page.items,
            total: page.total,
        }
    }
}

/// Principal-aware, type-erased PAP application boundary used by daemon adapters.
///
/// The method set deliberately mirrors the authoritative [`PapService`] use
/// cases only to stop its repository/compiler generics at this boundary and to
/// apply server-owned authorization. Implementations must delegate domain
/// semantics to PAP rather than reimplementing them.
pub trait PolicyAdministration: Send + Sync {
    /// Creates one Policy with a server-generated identity.
    ///
    /// # Errors
    /// Returns authorization, validation, conflict, capacity, or internal failures.
    fn create_policy(
        &self,
        principal: &Principal,
        policy_name: &str,
        template: &PolicyTemplate,
    ) -> Result<PreparedPolicy, PolicyAdministrationError>;

    /// Updates one existing Policy identity.
    ///
    /// # Errors
    /// Returns authorization, validation, conflict, not-found, capacity, or internal failures.
    fn update_policy(
        &self,
        principal: &Principal,
        policy_id: &ResourceId,
        policy_name: &str,
        template: &PolicyTemplate,
    ) -> Result<PreparedPolicy, PolicyAdministrationError>;

    /// Reads one exact current Policy revision.
    ///
    /// # Errors
    /// Returns authorization, not-found, or internal failures.
    fn get_policy(
        &self,
        principal: &Principal,
        id: &ResourceId,
        revision: Revision,
    ) -> Result<PreparedPolicy, PolicyAdministrationError>;

    /// Lists current Policies.
    ///
    /// # Errors
    /// Returns authorization, pagination-validation, or internal failures.
    fn list_policies(
        &self,
        principal: &Principal,
        limit: u32,
        offset: u32,
    ) -> Result<ResourcePage<PreparedPolicy>, PolicyAdministrationError>;

    /// Deletes one exact current Policy revision.
    ///
    /// # Errors
    /// Returns authorization, conflict, not-found, or internal failures.
    fn delete_policy_revision(
        &self,
        principal: &Principal,
        id: &ResourceId,
        revision: Revision,
    ) -> Result<PreparedPolicy, PolicyAdministrationError>;

    /// Creates one Scope with a server-generated identity.
    ///
    /// # Errors
    /// Returns authorization, validation, conflict, capacity, or internal failures.
    fn create_scope(
        &self,
        principal: &Principal,
        selector: &ScopeSelector,
    ) -> Result<PreparedScope, PolicyAdministrationError>;

    /// Updates one existing Scope identity.
    ///
    /// # Errors
    /// Returns authorization, validation, conflict, not-found, capacity, or internal failures.
    fn update_scope(
        &self,
        principal: &Principal,
        scope_id: &ResourceId,
        selector: &ScopeSelector,
    ) -> Result<PreparedScope, PolicyAdministrationError>;

    /// Reads one exact current Scope revision.
    ///
    /// # Errors
    /// Returns authorization, not-found, or internal failures.
    fn get_scope(
        &self,
        principal: &Principal,
        id: &ResourceId,
        revision: Revision,
    ) -> Result<PreparedScope, PolicyAdministrationError>;

    /// Lists current Scopes.
    ///
    /// # Errors
    /// Returns authorization, pagination-validation, or internal failures.
    fn list_scopes(
        &self,
        principal: &Principal,
        limit: u32,
        offset: u32,
    ) -> Result<ResourcePage<PreparedScope>, PolicyAdministrationError>;

    /// Deletes one exact current Scope revision.
    ///
    /// # Errors
    /// Returns authorization, conflict, not-found, or internal failures.
    fn delete_scope_revision(
        &self,
        principal: &Principal,
        id: &ResourceId,
        revision: Revision,
    ) -> Result<PreparedScope, PolicyAdministrationError>;

    /// Creates one Binding Apply intent with a server-generated identity.
    ///
    /// # Errors
    /// Returns authorization, validation, conflict, not-found, capacity, or internal failures.
    fn create_binding(
        &self,
        principal: &Principal,
        policy_id: &ResourceId,
        policy_revision: Revision,
        scope_id: &ResourceId,
        scope_revision: Revision,
    ) -> Result<BindingView, PolicyAdministrationError>;

    /// Updates one existing Binding and requests Apply.
    ///
    /// # Errors
    /// Returns authorization, validation, conflict, not-found, capacity, or internal failures.
    fn update_binding(
        &self,
        principal: &Principal,
        binding_id: &ResourceId,
        policy_id: &ResourceId,
        policy_revision: Revision,
        scope_id: &ResourceId,
        scope_revision: Revision,
    ) -> Result<BindingView, PolicyAdministrationError>;

    /// Reads one current Binding spec and lifecycle status.
    ///
    /// # Errors
    /// Returns authorization, not-found, or internal failures.
    fn get_binding(
        &self,
        principal: &Principal,
        id: &ResourceId,
    ) -> Result<BindingView, PolicyAdministrationError>;

    /// Lists current Binding specs and lifecycle statuses.
    ///
    /// # Errors
    /// Returns authorization, pagination-validation, or internal failures.
    fn list_bindings(
        &self,
        principal: &Principal,
        limit: u32,
        offset: u32,
    ) -> Result<ResourcePage<BindingView>, PolicyAdministrationError>;

    /// Accepts one Binding Delete intent without discarding its current spec.
    ///
    /// # Errors
    /// Returns authorization, conflict, not-found, capacity, or internal failures.
    fn delete_binding(
        &self,
        principal: &Principal,
        id: &ResourceId,
    ) -> Result<BindingView, PolicyAdministrationError>;
}

impl<R, C> PolicyAdministration for PapService<R, C>
where
    R: PapRepository,
    C: PolicyCompiler,
{
    fn create_policy(
        &self,
        principal: &Principal,
        policy_name: &str,
        template: &PolicyTemplate,
    ) -> Result<PreparedPolicy, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::create_policy(self, policy_name, template)?)
    }

    fn update_policy(
        &self,
        principal: &Principal,
        policy_id: &ResourceId,
        policy_name: &str,
        template: &PolicyTemplate,
    ) -> Result<PreparedPolicy, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::update_policy(
            self,
            policy_id,
            policy_name,
            template,
        )?)
    }

    fn get_policy(
        &self,
        principal: &Principal,
        id: &ResourceId,
        revision: Revision,
    ) -> Result<PreparedPolicy, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::get_policy(self, id, revision)?)
    }

    fn list_policies(
        &self,
        principal: &Principal,
        limit: u32,
        offset: u32,
    ) -> Result<ResourcePage<PreparedPolicy>, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::list_policies(self, limit, offset)?.into())
    }

    fn delete_policy_revision(
        &self,
        principal: &Principal,
        id: &ResourceId,
        revision: Revision,
    ) -> Result<PreparedPolicy, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::delete_policy_revision(self, id, revision)?)
    }

    fn create_scope(
        &self,
        principal: &Principal,
        selector: &ScopeSelector,
    ) -> Result<PreparedScope, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::create_scope(self, selector)?)
    }

    fn update_scope(
        &self,
        principal: &Principal,
        scope_id: &ResourceId,
        selector: &ScopeSelector,
    ) -> Result<PreparedScope, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::update_scope(self, scope_id, selector)?)
    }

    fn get_scope(
        &self,
        principal: &Principal,
        id: &ResourceId,
        revision: Revision,
    ) -> Result<PreparedScope, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::get_scope(self, id, revision)?)
    }

    fn list_scopes(
        &self,
        principal: &Principal,
        limit: u32,
        offset: u32,
    ) -> Result<ResourcePage<PreparedScope>, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::list_scopes(self, limit, offset)?.into())
    }

    fn delete_scope_revision(
        &self,
        principal: &Principal,
        id: &ResourceId,
        revision: Revision,
    ) -> Result<PreparedScope, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::delete_scope_revision(self, id, revision)?)
    }

    fn create_binding(
        &self,
        principal: &Principal,
        policy_id: &ResourceId,
        policy_revision: Revision,
        scope_id: &ResourceId,
        scope_revision: Revision,
    ) -> Result<BindingView, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::create_binding(
            self,
            policy_id,
            policy_revision,
            scope_id,
            scope_revision,
        )?)
    }

    fn update_binding(
        &self,
        principal: &Principal,
        binding_id: &ResourceId,
        policy_id: &ResourceId,
        policy_revision: Revision,
        scope_id: &ResourceId,
        scope_revision: Revision,
    ) -> Result<BindingView, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::update_binding(
            self,
            binding_id,
            policy_id,
            policy_revision,
            scope_id,
            scope_revision,
        )?)
    }

    fn get_binding(
        &self,
        principal: &Principal,
        id: &ResourceId,
    ) -> Result<BindingView, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::get_binding(self, id)?)
    }

    fn list_bindings(
        &self,
        principal: &Principal,
        limit: u32,
        offset: u32,
    ) -> Result<ResourcePage<BindingView>, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::list_bindings(self, limit, offset)?.into())
    }

    fn delete_binding(
        &self,
        principal: &Principal,
        id: &ResourceId,
    ) -> Result<BindingView, PolicyAdministrationError> {
        require_policy_administrator(principal)?;
        Ok(PapService::delete_binding(self, id)?)
    }
}

fn require_policy_administrator(principal: &Principal) -> Result<(), PolicyAdministrationError> {
    if principal.role() == PrincipalRole::PolicyAdministrator {
        Ok(())
    } else {
        Err(PolicyAdministrationError::Forbidden)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::PeerCredentials;

    #[test]
    fn authorization_uses_only_the_server_assigned_role() {
        let peer = PeerCredentials::new(1000, 100, 4242);
        let user = Principal::from_authenticated_peer(peer, PrincipalRole::LocalUser);
        let administrator =
            Principal::from_authenticated_peer(peer, PrincipalRole::PolicyAdministrator);

        assert_eq!(
            require_policy_administrator(&user),
            Err(PolicyAdministrationError::Forbidden)
        );
        assert_eq!(require_policy_administrator(&administrator), Ok(()));
    }

    #[test]
    fn pap_errors_are_projected_once_at_the_application_boundary() {
        assert_eq!(
            PolicyAdministrationError::from(PapError::OperationInProgress),
            PolicyAdministrationError::Conflict
        );
        assert_eq!(
            PolicyAdministrationError::from(PapError::Persistence),
            PolicyAdministrationError::Internal
        );
        assert_eq!(
            PolicyAdministrationError::from(PapError::InvalidPagination),
            PolicyAdministrationError::InvalidArgument
        );
    }
}
