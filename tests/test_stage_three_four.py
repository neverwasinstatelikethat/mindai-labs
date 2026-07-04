from uuid import UUID

import pytest

from scientific_tangle.domain.intelligence import (
    AuditEvent,
    ComparableValue,
    DataClass,
    PipelineStage,
    Principal,
    ProtectedResource,
    ResearchClaim,
    ResearchSpace,
    ScopeDimension,
)
from scientific_tangle.services.governance import AccessPolicyEngine, InMemoryAuditLog
from scientific_tangle.services.kg_pipeline import KnowledgeGraphPipeline
from scientific_tangle.services.research_intelligence import ResearchIntelligenceService


def claim(identifier: str, minimum: float, maximum: float, geography: str) -> ResearchClaim:
    return ResearchClaim(
        id=identifier,
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
    principal = Principal(
        id="researcher-1",
        roles={"researcher"},
        permissions={"knowledge:read"},
        allowed_projects={"public-demo"},
    )
    resources = [
        ProtectedResource(id="public", data_class=DataClass.PUBLIC, project_id="public-demo"),
        ProtectedResource(id="secret", data_class=DataClass.RESTRICTED, project_id="secret-rnd"),
    ]

    visible = AccessPolicyEngine().filter_before_retrieval(principal, resources)

    assert [item.id for item in visible] == ["public"]


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


@pytest.mark.asyncio
async def test_kg_pipeline_is_idempotent_and_preserves_partial_stage_status() -> None:
    calls: list[tuple[PipelineStage, UUID]] = []

    def handler(stage: PipelineStage, failed: int = 0):
        async def execute(document_id: UUID) -> tuple[int, int]:
            calls.append((stage, document_id))
            return (3, failed)

        return execute

    handlers = {
        stage: handler(stage, 1 if stage == PipelineStage.EXTRACT else 0) for stage in PipelineStage
    }
    pipeline = KnowledgeGraphPipeline(handlers)

    first = await pipeline.run(b"document", "trace-1")
    second = await pipeline.run(b"document", "trace-2")

    assert first.status == "partial"
    assert first.stages[2].status == "partial"
    assert len(calls) == len(PipelineStage) * 2
    assert second.document_id == first.document_id
