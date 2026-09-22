from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


class KnowledgeKind(StrEnum):
    EXTRACTED = "extracted"
    INFERRED = "inferred"
    EXPERT_VALIDATED = "expert_validated"


class DataClass(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class ScopeDimension(BaseModel):
    name: str = Field(min_length=1)
    value: str = Field(min_length=1)


class ComparableValue(BaseModel):
    property_name: str = Field(min_length=1)
    min_value: float
    max_value: float
    unit: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_range(self) -> ComparableValue:
        if self.min_value > self.max_value:
            raise ValueError("min_value не может превышать max_value")
        return self


class ResearchClaim(BaseModel):
    id: str = Field(min_length=1)
    # finding_id отделяет идентичность числового наблюдения (id) от происхождения
    # тезиса: два наблюдения одного finding не должны выглядеть самоконфликтом.
    finding_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    predicate: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    value: ComparableValue
    scope: list[ScopeDimension] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    knowledge_kind: KnowledgeKind = KnowledgeKind.EXTRACTED


class ConflictCandidate(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    left_claim_id: str
    right_claim_id: str
    property_name: str
    shared_scope: list[ScopeDimension]
    reason: str
    status: Literal["candidate", "confirmed", "dismissed"] = "candidate"


class ResearchSpace(BaseModel):
    dimensions: dict[str, list[str]] = Field(min_length=1)


class KnowledgeGap(BaseModel):
    dimensions: list[ScopeDimension]
    reason: str
    supporting_claim_ids: list[str] = Field(default_factory=list)


class IntelligenceReport(BaseModel):
    conflicts: list[ConflictCandidate]
    gaps: list[KnowledgeGap]
    analyzed_claims: int = Field(ge=0)


class Principal(BaseModel):
    """Субъект доступа — владелец подтверждённой серверной сессии.

    Прав здесь намеренно нет: единственным источником истины остаётся таблица
    политик в ``services/governance.py``, а ``review_enabled`` — единственный
    признак, который учётная запись несёт с собой. Разрешения нельзя назначить
    вручную и нельзя разойтись с политикой (раньше ``roles`` + ``permissions``
    давали два независимых основания для доступа).
    """

    id: str
    review_enabled: bool = False
    allowed_projects: set[str] = Field(default_factory=set)


class ProtectedResource(BaseModel):
    id: str
    data_class: DataClass
    project_id: str | None = None
    required_permission: str = "knowledge:read"


class AccessDecision(BaseModel):
    allowed: bool
    reason_code: Literal[
        "allowed",
        "missing_permission",
        "project_scope_denied",
        "restricted_review_required",
    ]


class AuditEvent(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    actor_id: str
    action: str
    object_id: str
    outcome: Literal["allowed", "denied", "success", "failure"]
    correlation_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)
