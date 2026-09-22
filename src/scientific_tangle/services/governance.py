from __future__ import annotations

from collections.abc import Container, Iterable, Sequence
from dataclasses import dataclass

from scientific_tangle.domain.contracts import AnswerPayload, Finding, GraphSnapshot
from scientific_tangle.domain.intelligence import (
    AccessDecision,
    AuditEvent,
    DataClass,
    Principal,
    ProtectedResource,
)

# Двухуровневая модель доступа (решение от 2026-09-20 вместо пяти ролей):
# базовый уровень — любой подтверждённый аккаунт, экспертный — владелец с
# review_enabled=True. Роль больше не приходит от клиента: признак берётся из
# серверной учётной записи, а выдаётся только SQL (см. docs/security-auth.md).
BASE_PERMISSIONS: frozenset[str] = frozenset(
    {
        "knowledge:read",
        "query:ask",
        "feedback:give",
        "export:run",
        "evaluation:view",
    }
)
EXPERT_PERMISSIONS: frozenset[str] = BASE_PERMISSIONS | frozenset(
    {"proposal:review", "audit:read", "restricted:read"}
)

BASE_DATA_CLASSES: frozenset[DataClass] = frozenset({DataClass.PUBLIC, DataClass.INTERNAL})
EXPERT_DATA_CLASSES: frozenset[DataClass] = BASE_DATA_CLASSES | frozenset({DataClass.RESTRICTED})

RESTRICTED_PERMISSION = "restricted:read"

# Записывающие действия и право, которое их разрешает. Отдельная таблица нужна
# ровно потому, что ``restricted:read`` — право на ЧТЕНИЕ закрытого контура:
# раньше оно же пропускало перезапись утверждения и импорт в restricted, и
# расширение доступа на чтение молча открывало бы запись.
# Права не изобретаются: берётся существующее экспертное ``proposal:review``
# (оно и выдаётся признаком ``review_enabled``), перечень разрешений продукта не меняется.
WRITE_ACTIONS: dict[str, str] = {
    "claim.supersede": "proposal:review",
    "document.write_restricted": "proposal:review",
}


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    permissions: frozenset[str]
    data_classes: frozenset[DataClass]


# Единственный источник истины по разрешениям и классам данных.
# Раньше класс доступа определялся дважды: по Finding.data_class во время retrieval
# и по статусу finding (consensus/disputed/hypothesis) в обработчике API — эти две
# классификации могли расходиться.
BASE_POLICY = AccessPolicy(permissions=BASE_PERMISSIONS, data_classes=BASE_DATA_CLASSES)
EXPERT_POLICY = AccessPolicy(permissions=EXPERT_PERMISSIONS, data_classes=EXPERT_DATA_CLASSES)


def policy_for(review_enabled: bool) -> AccessPolicy:
    return EXPERT_POLICY if review_enabled else BASE_POLICY


class AccessPolicyEngine:
    """Применяет ACL до retrieval. Фильтрация в UI контролем доступа не считается.

    Доступ к данным определяется свойством ``data_class`` у finding и у узла графа;
    ответ модели постфактум фильтровать бессмысленно — restricted-текст уже попал
    бы в промпт и в summary. Поэтому фильтрация выполняется на границе knowledge
    (``filter_findings``/``visible_node_ids``), а ``apply_acl`` оставляет за собой
    функцию защиты в глубину уже собранного ответа.

    Вход — объект ``Principal`` (или признак эксперта), а не имя роли: неизвестный
    или отсутствующий субъект не получает доступа, потому что политикой управляет
    булев признак серверной учётной записи, а не строка из заголовка.
    """

    @staticmethod
    def policy(principal: Principal | bool) -> AccessPolicy:
        review_enabled = principal.review_enabled if isinstance(principal, Principal) else principal
        return policy_for(review_enabled)

    @staticmethod
    def principal(user_id: str, review_enabled: bool) -> Principal:
        """Единственное место, где субъект собирается из учётной записи."""
        return Principal(id=user_id, review_enabled=review_enabled)

    @classmethod
    def capabilities(cls, review_enabled: bool) -> list[str]:
        """Разрешения уровня — стабильно отсортированный список для ``/auth/me``."""
        return sorted(cls.policy(review_enabled).permissions)

    @classmethod
    def data_class_names(cls, review_enabled: bool) -> list[str]:
        return sorted(item.value for item in cls.policy(review_enabled).data_classes)

    @classmethod
    def allowed_data_classes(cls, principal: Principal | bool) -> frozenset[DataClass]:
        return cls.policy(principal).data_classes

    @classmethod
    def has_permission(cls, principal: Principal, permission: str) -> bool:
        return permission in cls.policy(principal).permissions

    @classmethod
    def can_write(cls, principal: Principal | None, action: str) -> bool:
        """Право на записывающее действие, а не на чтение тех же данных.

        Неизвестное действие запрещается: список ``WRITE_ACTIONS`` пополняется
        вместе с эндпоинтом, иначе новая запись окажется открыта по умолчанию.
        """
        if principal is None:
            return False
        permission = WRITE_ACTIONS.get(action)
        if permission is None:
            return False
        return cls.has_permission(principal, permission)

    @classmethod
    def decide(cls, principal: Principal, resource: ProtectedResource) -> AccessDecision:
        if not cls.has_permission(principal, resource.required_permission):
            return AccessDecision(allowed=False, reason_code="missing_permission")
        if resource.project_id and resource.project_id not in principal.allowed_projects:
            return AccessDecision(allowed=False, reason_code="project_scope_denied")
        if resource.data_class == DataClass.RESTRICTED:
            if not cls.has_permission(principal, RESTRICTED_PERMISSION):
                return AccessDecision(allowed=False, reason_code="restricted_review_required")
        return AccessDecision(allowed=True, reason_code="allowed")

    @classmethod
    def filter_before_retrieval(
        cls,
        principal: Principal,
        resources: Iterable[ProtectedResource],
    ) -> list[ProtectedResource]:
        return [resource for resource in resources if cls.decide(principal, resource).allowed]

    @staticmethod
    def filter_findings(
        findings: Sequence[Finding],
        allowed: Container[DataClass],
    ) -> list[Finding]:
        return [finding for finding in findings if finding.data_class in allowed]

    @staticmethod
    def visible_node_ids(
        graph: GraphSnapshot,
        allowed: Container[DataClass],
    ) -> set[str]:
        return {node.id for node in graph.nodes if node.data_class in allowed}

    @staticmethod
    def filter_graph(
        graph: GraphSnapshot,
        allowed: Container[DataClass],
    ) -> GraphSnapshot:
        nodes = [node for node in graph.nodes if node.data_class in allowed]
        visible = {node.id for node in nodes}
        edges = [
            edge
            for edge in graph.edges
            if edge.data_class in allowed
            and edge.source in visible
            and edge.target in visible
        ]
        return GraphSnapshot(nodes=nodes, edges=edges, communities=graph.communities)

    @classmethod
    def apply_acl(cls, answer: AnswerPayload, allowed: Container[DataClass]) -> AnswerPayload:
        """Защита в глубину: повторяет cut по data_class на всём ответе целиком.

        Фильтрация идёт по идентичности (набор id), а не по подстроке в
        человекочитаемом описании конфликта — иначе одно вхождение id в текст
        удаляло несвязанные записи.
        """

        def visible(data_class: DataClass) -> bool:
            return data_class in allowed

        findings = [finding for finding in answer.findings if visible(finding.data_class)]
        kept_ids = {finding.id for finding in findings}
        graph = cls.filter_graph(answer.graph, allowed)
        observations = [
            observation
            for observation in answer.tool_observations
            if all(finding_id in kept_ids for finding_id in observation.finding_ids)
        ]
        # Конфликты и пробелы формируются только по видимым evidence, поэтому при
        # дополнительном ограничении класса мы теряем весь слой, а не отдельные строки.
        hidden = kept_ids != {finding.id for finding in answer.findings}
        return answer.model_copy(
            update={
                "findings": findings,
                "graph": graph,
                "tool_observations": observations,
                "conflicts": [] if hidden else answer.conflicts,
                "knowledge_gaps": [] if hidden else answer.knowledge_gaps,
                "degradation_reasons": [
                    *answer.degradation_reasons,
                    *(
                        ["Часть доказательств скрыта политикой доступа по классу данных."]
                        if hidden
                        else []
                    ),
                ],
            }
        )


class InMemoryAuditLog:
    """Журнал с фиксированным потолком: неограниченный рост списка в процессе — утечка памяти."""

    def __init__(self, capacity: int = 1000) -> None:
        self._events: list[AuditEvent] = []
        self._capacity = capacity

    def append(self, event: AuditEvent) -> None:
        self._events.append(event.model_copy(deep=True))
        if len(self._events) > self._capacity:
            del self._events[: -self._capacity]

    def list(
        self, *, correlation_id: str | None = None, actor_id: str | None = None
    ) -> list[AuditEvent]:
        events = self._events
        if correlation_id is not None:
            events = [event for event in events if event.correlation_id == correlation_id]
        if actor_id is not None:
            events = [event for event in events if event.actor_id == actor_id]
        return [event.model_copy(deep=True) for event in events]
