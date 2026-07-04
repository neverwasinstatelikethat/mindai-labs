from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

from scientific_tangle.domain.models import EvidenceLocator, NumericObservation, QueryPlan


class NodeType(StrEnum):
    MATERIAL = "material"
    PROCESS = "process"
    EQUIPMENT = "equipment"
    CONDITION = "condition"
    CLAIM = "claim"
    EXPERIMENT = "experiment"
    PUBLICATION = "publication"
    EXPERT = "expert"
    CHUNK = "chunk"
    LOCATION = "location"
    ORGANIZATION = "organization"


class GraphNode(BaseModel):
    id: str
    label: str
    type: NodeType
    confidence: float = Field(default=1, ge=0, le=1)
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    relation: str
    confidence: float = Field(default=1, ge=0, le=1)


class GraphSnapshot(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    communities: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    id: str
    statement: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceLocator]
    status: Literal["consensus", "disputed", "hypothesis"] = "consensus"
    observations: list[NumericObservation] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)
    superseded_by: str | None = None
    subject: str | None = None
    predicate: str | None = None
    scope: dict[str, str] = Field(default_factory=dict)
    data_class: str = "public"
    reviewer_id: str | None = None
    review_date: str | None = None
    review_reason: str | None = None


class AgentEvent(BaseModel):
    agent: str
    status: Literal["started", "completed", "revised", "failed"]
    message: str
    duration_ms: int = Field(default=0, ge=0)


class AgentMetricSnapshot(BaseModel):
    agent: str
    calls: int = Field(ge=0)
    successes: int = Field(ge=0)
    failures: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    average_duration_ms: float = Field(ge=0)
    p50_duration_ms: float = Field(ge=0)
    p95_duration_ms: float = Field(ge=0)


class AgentMetricsResponse(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    agents: list[AgentMetricSnapshot]


class AnswerPayload(BaseModel):
    query_id: UUID = Field(default_factory=uuid4)
    question: str
    summary: str
    intent: IntentClassification | None = None
    query_plan: QueryPlan
    tool_observations: list[ToolObservation] = Field(default_factory=list)
    findings: list[Finding]
    conflicts: list[str]
    knowledge_gaps: list[str]
    recommendations: list[str]
    graph: GraphSnapshot
    trace: list[AgentEvent]
    confidence: float = Field(ge=0, le=1)
    model_mode: Literal["yandex", "gigachat", "fallback", "scripted"]


class QueryRequest(BaseModel):
    thread_id: UUID = Field(default_factory=uuid4)
    question: str = Field(min_length=3)
    language: Literal["ru", "en"] = "ru"
    mode: Literal["local", "global", "hybrid"] = "hybrid"


class IntentClassification(BaseModel):
    primary: Literal[
        "fact_search",
        "literature_review",
        "technology_comparison",
        "contradiction_analysis",
        "gap_analysis",
        "expert_discovery",
        "graph_edit",
        "report_generation",
    ]
    secondary: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    requires_external_action: bool = False
    requires_user_confirmation: bool = False
    confirmation_reason: str | None = None


class ToolAction(BaseModel):
    id: str = Field(min_length=1)
    tool: Literal[
        "hybrid_search",
        "graph_traverse",
        "community_search",
        "numeric_filter",
        "conflict_scan",
        "gap_scan",
        "expert_lookup",
    ]
    purpose: str = Field(min_length=3)
    query: str = Field(min_length=3)
    entities: list[str] = Field(default_factory=list)
    relation_types: list[str] = Field(default_factory=list)
    max_hops: int = Field(default=2, ge=1, le=4)


class AgentActionPlan(BaseModel):
    rationale: str
    actions: list[ToolAction] = Field(min_length=1, max_length=8)
    completion_criteria: list[str] = Field(min_length=1)


class PlanningBundle(BaseModel):
    intent: IntentClassification
    query_plan: QueryPlan
    action_plan: AgentActionPlan


class ToolObservation(BaseModel):
    action_id: str
    tool: str
    status: Literal["success", "warning", "error"]
    summary: str
    next_actions: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    graph_node_ids: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    root_cause_hint: str | None = None
    safe_retry: str | None = None
    stop_condition: str | None = None


class AgentControlDecision(BaseModel):
    decision: Literal["continue_tools", "reason"]
    rationale: str = Field(min_length=3)
    missing_evidence: list[str] = Field(default_factory=list)


class RetrievalPlan(BaseModel):
    lexical_query: str = Field(min_length=3)
    semantic_query: str = Field(min_length=3)
    entity_names: list[str]
    relation_types: list[str]
    max_hops: int = Field(ge=1, le=4)
    use_global_context: bool
    community_question: str | None = None


class ExtractedEntity(BaseModel):
    name: str
    canonical_name: str
    type: NodeType
    aliases: list[str] = Field(default_factory=list)

    @field_validator("type", mode="before")
    @classmethod
    def normalize_entity_type(cls, value: object) -> object:
        aliases = {
            "place": NodeType.LOCATION,
            "facility": NodeType.LOCATION,
            "company": NodeType.ORGANIZATION,
            "organisation": NodeType.ORGANIZATION,
        }
        return aliases.get(str(value).lower(), value)


class ExtractedClaim(BaseModel):
    subject: str
    predicate: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    object: str
    statement: str
    confidence: float = Field(ge=0, le=1)
    evidence_quote: str = Field(min_length=1)
    observations: list[NumericObservation] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity]
    claims: list[ExtractedClaim]


class EntityResolutionProposal(BaseModel):
    mention: str
    canonical_name: str
    action: Literal["link", "create"]
    confidence: float = Field(ge=0, le=1)
    rationale: str


class EntityMergeProposal(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source: str
    target: str
    confidence: float = Field(ge=0, le=1)
    rationale: str
    status: Literal["proposed", "accepted", "rejected", "reverted"] = "proposed"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MergeReviewRequest(BaseModel):
    action: Literal["accept", "reject", "revert"]


class IngestionBundle(BaseModel):
    extraction: ExtractionResult
    resolutions: list[EntityResolutionProposal] = Field(default_factory=list)


class ReasoningResult(BaseModel):
    summary: str
    finding_ids: list[str]
    conflicts: list[str]
    knowledge_gaps: list[str]
    recommendations: list[str]


class CritiqueResult(BaseModel):
    approved: bool
    issues: list[str]
    revision_instructions: list[str]


class EvolutionDraft(BaseModel):
    kind: Literal["prompt", "rule", "alias", "gold_case"]
    title: str
    change: str
    impact: list[str]


class DocumentRequest(BaseModel):
    title: str = Field(min_length=1)
    text: str = Field(min_length=20)
    language: Literal["ru", "en"] = "ru"
    geography: str | None = None
    year: int | None = Field(default=None, ge=1800, le=2100)
    fragments: list[DocumentFragment] = Field(default_factory=list)


class DocumentFragment(BaseModel):
    text: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    sheet: str | None = None
    cell_range: str | None = None


class DocumentReceipt(BaseModel):
    document_id: UUID
    checksum: str
    status: Literal["created", "duplicate"]
    extracted_claims: int


class PreloadDocumentResult(BaseModel):
    path: str
    title: str
    status: Literal["created", "duplicate", "failed"]
    extracted_claims: int = 0
    error: str | None = None


class PreloadReport(BaseModel):
    started_at: datetime
    finished_at: datetime
    total: int
    created: int
    duplicates: int
    failed: int
    claims: int
    documents: list[PreloadDocumentResult]


class StructuralDocumentReceipt(BaseModel):
    document_id: UUID
    checksum: str
    status: Literal["created", "duplicate"]
    chunks: int


class CorpusCompileReport(BaseModel):
    discovered: int
    eligible: int
    processed: int
    created: int
    duplicates: int
    failed: int
    chunks: int
    skipped_unsupported: int
    skipped_oversize: int
    ocr_required: int
    coverage: float = Field(ge=0, le=1)
    errors: list[str] = Field(default_factory=list)


class CorpusStats(BaseModel):
    documents: int = Field(ge=0)
    chunks: int = Field(ge=0)
    claims: int = Field(ge=0)
    entities: int = Field(ge=0)
    semantic_documents: int = Field(ge=0)


class EvaluationMetrics(BaseModel):
    citation_coverage: float = Field(ge=0, le=1)
    numeric_support: float = Field(ge=0, le=1)
    evidence_precision: float = Field(ge=0, le=1)
    overall: float = Field(ge=0, le=1)


class EvaluationRun(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    query_id: UUID
    metrics: EvaluationMetrics
    passed: bool
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GoldCase(BaseModel):
    id: str
    language: Literal["ru", "en"]
    question: str
    source_path: str
    expected_source_titles: list[str]


class RankingMetrics(BaseModel):
    recall_at_3: float = Field(ge=0, le=1)
    precision_at_3: float = Field(ge=0, le=1)
    mrr: float = Field(ge=0, le=1)
    ndcg_at_3: float = Field(ge=0, le=1)


class RetrievalCaseResult(BaseModel):
    case_id: str
    expected_sources: list[str]
    retrieved_sources: list[str]
    reciprocal_rank: float = Field(ge=0, le=1)


class RetrievalBenchmark(BaseModel):
    gold_cases: int = Field(ge=0)
    corpus_documents: int = Field(ge=0)
    top_k: int = Field(ge=1)
    hybrid: RankingMetrics
    lexical_baseline: RankingMetrics
    cases: list[RetrievalCaseResult]
    leakage_checks: dict[str, bool]
    passed: bool


class PipelineVariantMetrics(BaseModel):
    source_recall: float = Field(ge=0, le=1)
    citation_coverage: float = Field(ge=0, le=1)
    pass_rate: float = Field(ge=0, le=1)
    average_latency_ms: float = Field(ge=0)


class PipelineCaseResult(BaseModel):
    case_id: str
    question: str
    expected_sources: list[str]
    retrieved_sources: list[str]
    latency_ms: float = Field(ge=0)
    passed: bool


class PipelineBenchmark(BaseModel):
    cases: int = Field(ge=0)
    agentic_graphrag: PipelineVariantMetrics
    lexical_baseline: RankingMetrics
    results: list[PipelineCaseResult]
    passed: bool


class EvolutionExperiment(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    proposal_id: UUID
    cases: int = Field(ge=1)
    baseline: PipelineVariantMetrics
    candidate: PipelineVariantMetrics
    delta_pass_rate: float = Field(ge=-1, le=1)
    decision: Literal["promote", "reject"]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class QueryResponse(BaseModel):
    answer: AnswerPayload
    evaluation: EvaluationRun


class FeedbackRequest(BaseModel):
    query_id: UUID
    finding_id: str | None = None
    verdict: Literal["accept", "reject", "correct"]
    comment: str = Field(min_length=3)
    correction: str | None = None


class EvolutionProposal(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source_query_id: UUID
    kind: Literal["prompt", "rule", "alias", "gold_case"]
    title: str
    change: str
    status: Literal["proposed", "accepted", "rejected"] = "proposed"
    impact: list[str]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProposalReviewRequest(BaseModel):
    accepted: bool


class SystemStatus(BaseModel):
    status: Literal["ready", "degraded"]
    model_mode: Literal["yandex", "gigachat", "fallback", "scripted"]
    services: dict[str, Literal["ready", "configured", "fallback"]]


# ── FT-12/26: Comparison models ─────────────────────────────────────────────


class ComparisonCell(BaseModel):
    value: str | None = None
    unit: str | None = None
    evidence: str | None = None
    confidence: float = Field(default=0, ge=0, le=1)


class ComparisonRow(BaseModel):
    item: str
    cells: dict[str, ComparisonCell]


class ComparisonTable(BaseModel):
    question: str
    headers: list[str]
    rows: list[ComparisonRow]


class ComparisonRequest(BaseModel):
    question: str = Field(min_length=3)
    entities: list[str] = Field(min_length=1)
    dimensions: list[str] = Field(default_factory=list)
    language: Literal["ru", "en"] = "ru"


# ── FT-23: Export models ───────────────────────────────────────────────────


class ExportRequest(BaseModel):
    answer: AnswerPayload
    format: Literal["markdown", "json-ld", "pdf"] = "markdown"


class ExportResponse(BaseModel):
    content: str
    content_type: str
    filename: str


# ── FT-08: Fact versioning ─────────────────────────────────────────────────


class ClaimHistoryEntry(BaseModel):
    finding_id: str
    version: int
    statement: str
    status: str
    superseded_by: str | None = None
    reviewer_id: str | None = None
    review_date: str | None = None
    review_reason: str | None = None


class ClaimHistory(BaseModel):
    claim_id: str
    versions: list[ClaimHistoryEntry]


# ── FT-20/21: ACL models ───────────────────────────────────────────────────


class RoleInfo(BaseModel):
    role: str
    permissions: list[str]
    data_classes: list[str]


class PrincipalInfo(BaseModel):
    user_id: str
    role: str
    permissions: list[str]
    allowed_data_classes: list[str]


# ── FT-25: Dashboard models ────────────────────────────────────────────────


class ActivityEntry(BaseModel):
    action: str
    actor_id: str
    object_id: str
    outcome: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DashboardResponse(BaseModel):
    documents: int = Field(ge=0)
    claims: int = Field(ge=0)
    entities: int = Field(ge=0)
    evidence: int = Field(ge=0)
    conflicts: int = Field(ge=0)
    gaps: int = Field(ge=0)
    recent_activity: list[ActivityEntry]
    agent_metrics: AgentMetricsResponse


# ── FT-24: Notification models ─────────────────────────────────────────────


class Notification(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    topic: str
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SubscriptionRequest(BaseModel):
    topic: str = Field(min_length=1)
    subscriber_id: str = Field(min_length=1)
