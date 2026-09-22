from scientific_tangle.domain.intelligence import (
    AuditEvent,
    ComparableValue,
    DataClass,
    ProtectedResource,
    ResearchClaim,
    ResearchSpace,
    ScopeDimension,
)
from scientific_tangle.services.governance import AccessPolicyEngine, InMemoryAuditLog
from scientific_tangle.services.research_intelligence import ResearchIntelligenceService


def claim(identifier: str, minimum: float, maximum: float, geography: str) -> ResearchClaim:
    return ResearchClaim(
        id=identifier,
        finding_id=f"finding-{identifier}",
        subject_id="reverse-osmosis",
        predicate="HAS_SALT_REJECTION",
        value=ComparableValue(
            property_name="salt_rejection",
            min_value=minimum,
            max_value=maximum,
            unit="%",
        ),
        scope=[
            ScopeDimension(name="geography", value=geography),
            ScopeDimension(name="water_type", value="mine_water"),
        ],
        evidence_ids=[f"evidence-{identifier}"],
    )


def test_conflicts_require_compatible_conditions_and_disjoint_ranges() -> None:
    service = ResearchIntelligenceService()
    claims = [
        claim("a", 92, 98, "RU"),
        claim("b", 40, 70, "RU"),
        claim("c", 30, 50, "KZ"),
    ]

    conflicts = service.detect_conflicts(claims)

    assert [(item.left_claim_id, item.right_claim_id) for item in conflicts] == [("a", "b")]
    assert conflicts[0].status == "candidate"


def test_gap_analysis_uses_explicit_research_space() -> None:
    report = ResearchIntelligenceService().analyze(
        [claim("ru", 90, 98, "RU")],
        ResearchSpace(dimensions={"geography": ["RU", "KZ"], "water_type": ["mine_water"]}),
    )

    assert len(report.gaps) == 1
    assert {item.value for item in report.gaps[0].dimensions} == {"KZ", "mine_water"}


def test_acl_filters_restricted_documents_before_retrieval() -> None:
    """Двухуровневый доступ: restricted виден только экспертному уровню.

    Субъект строится движком политик (единственный источник истины по правам):
    в ``Principal`` нет ни ролей, ни списка разрешений — только признак
    экспертного доступа из серверной учётной записи.
    """
    engine = AccessPolicyEngine()
    base = engine.principal("researcher-1", review_enabled=False)
    expert = engine.principal("expert-1", review_enabled=True)
    resources = [
        ProtectedResource(id="public", data_class=DataClass.PUBLIC),
        ProtectedResource(id="secret", data_class=DataClass.RESTRICTED),
    ]

    assert [item.id for item in engine.filter_before_retrieval(base, resources)] == ["public"]
    assert [item.id for item in engine.filter_before_retrieval(expert, resources)] == [
        "public",
        "secret",
    ]
    assert engine.decide(base, resources[1]).reason_code == "restricted_review_required"
    assert engine.decide(expert, resources[1]).reason_code == "allowed"


def test_two_tier_policy_table_is_stable() -> None:
    """Таблица политик — контракт для UI: её же отдаёт ``/api/v1/auth/me``."""
    engine = AccessPolicyEngine()

    assert engine.capabilities(False) == [
        "evaluation:view",
        "export:run",
        "feedback:give",
        "knowledge:read",
        "query:ask",
    ]
    assert engine.data_class_names(False) == ["internal", "public"]
    assert sorted(set(engine.capabilities(True)) - set(engine.capabilities(False))) == [
        "audit:read",
        "proposal:review",
        "restricted:read",
    ]
    assert engine.data_class_names(True) == ["internal", "public", "restricted"]
    assert engine.allowed_data_classes(engine.principal("u-1", True)) == frozenset(DataClass)


def test_project_scope_denies_without_membership() -> None:
    """Вне списка проектов аккаунта ресурс не виден никому, кроме владельца."""
    engine = AccessPolicyEngine()
    principal = engine.principal("analyst-1", review_enabled=False)
    resource = ProtectedResource(id="other", data_class=DataClass.INTERNAL, project_id="rnd-x")

    decision = engine.decide(principal, resource)

    assert decision.allowed is False
    assert decision.reason_code == "project_scope_denied"


def test_audit_log_returns_copies_and_filters_by_correlation() -> None:
    log = InMemoryAuditLog()
    log.append(
        AuditEvent(
            actor_id="expert-1",
            action="claim.review",
            object_id="claim-1",
            outcome="success",
            correlation_id="trace-1",
        )
    )

    events = log.list(correlation_id="trace-1")
    events[0].metadata["tampered"] = True

    assert len(log.list(correlation_id="trace-1")) == 1
    assert log.list()[0].metadata == {}


def test_audit_log_bounds_its_own_memory() -> None:
    """Журнал в памяти не должен расти быстрее ёмкости — иначе worker съест RAM."""
    log = InMemoryAuditLog(capacity=3)
    for index in range(5):
        log.append(
            AuditEvent(
                actor_id="expert-1",
                action="claim.review",
                object_id=f"claim-{index}",
                outcome="success",
                correlation_id="trace-2",
            )
        )

    events = log.list()
    assert len(events) == 3
    assert [event.object_id for event in events] == ["claim-2", "claim-3", "claim-4"]
