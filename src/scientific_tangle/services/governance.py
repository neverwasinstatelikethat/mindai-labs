from __future__ import annotations

from collections.abc import Iterable

from scientific_tangle.domain.intelligence import (
    AccessDecision,
    AuditEvent,
    DataClass,
    Principal,
    ProtectedResource,
)


class AccessPolicyEngine:
    """Применяет ACL до retrieval; UI-фильтрация не считается контролем доступа."""

    def decide(self, principal: Principal, resource: ProtectedResource) -> AccessDecision:
        if resource.required_permission not in principal.permissions:
            return AccessDecision(allowed=False, reason_code="missing_permission")
        if resource.project_id and resource.project_id not in principal.allowed_projects:
            if "administrator" not in principal.roles:
                return AccessDecision(allowed=False, reason_code="project_scope_denied")
        if resource.data_class == DataClass.RESTRICTED:
            if not principal.roles.intersection({"administrator", "project_manager"}):
                return AccessDecision(allowed=False, reason_code="restricted_role_required")
        return AccessDecision(allowed=True, reason_code="allowed")

    def filter_before_retrieval(
        self,
        principal: Principal,
        resources: Iterable[ProtectedResource],
    ) -> list[ProtectedResource]:
        return [resource for resource in resources if self.decide(principal, resource).allowed]


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self._events.append(event.model_copy(deep=True))

    def list(self, *, correlation_id: str | None = None) -> list[AuditEvent]:
        events = self._events
        if correlation_id is not None:
            events = [event for event in events if event.correlation_id == correlation_id]
        return [event.model_copy(deep=True) for event in events]
