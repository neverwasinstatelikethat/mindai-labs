from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import EvidenceLocator, NumericObservation, QueryPlan

# Валидатор email без новой зависимости (pydantic[email] тянет email-validator):
# грубой проверки формата достаточно — настоящий контроль даёт подтверждение
# адреса, а оно появится вместе с почтовым контуром.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}")


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
    data_class: DataClass = DataClass.PUBLIC
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    relation: str
    confidence: float = Field(default=1, ge=0, le=1)
    data_class: DataClass = DataClass.PUBLIC


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
    # Единственный источник истины для классификации доступа. Статус finding
    # (consensus/disputed/hypothesis) — про степень консенсуса, а не про права,
    # и не может служить вторым, независимым основанием для ACL.
    data_class: DataClass = DataClass.PUBLIC
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


class LlmMetricSnapshot(BaseModel):
    schema_name: str = Field(min_length=1)
    calls: int = Field(ge=0)
    failures: int = Field(ge=0)
    # Schema-repair попытки: повтор был у обращения к модели, а не у агента.
    retries: int = Field(default=0, ge=0)
    average_duration_ms: float = Field(ge=0)
    p95_duration_ms: float = Field(ge=0)


class AgentMetricsResponse(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    agents: list[AgentMetricSnapshot]
    llm: list[LlmMetricSnapshot] = Field(default_factory=list)
    total_prompt_tokens: int = Field(default=0, ge=0)
    total_completion_tokens: int = Field(default=0, ge=0)
    # Приём агентных прогонов: saturation обязана быть наблюдаемой, иначе 429
    # выглядят как случайные отказы сервиса.
    agent_runs_active: int = Field(default=0, ge=0)
    agent_runs_limit: int = Field(default=0, ge=0)
    agent_runs_refused: int = Field(default=0, ge=0)
    llm_calls_in_flight: int = Field(default=0, ge=0)
    llm_calls_waiting: int = Field(default=0, ge=0)
    llm_slots: int = Field(default=0, ge=0)


ModelMode = Literal["gigachat", "scripted", "unavailable"]
# Темы ленты, которые продукт действительно порождает: новая тема появляется
# вместе с вызовом _notify, а не как свободная строка клиента.
NotificationTopic = Literal["claim.superseded", "proposal.accepted"]
ServiceState = Literal["ready", "configured", "fallback", "disabled"]
AccountsMode = Literal["postgres", "in-memory"]
# Общий выбор контура хранения для серверного состояния (сессии, ответы,
# экспертные решения): postgres — рабочий контур, in-memory — тесты и запуск
# без базы.
StoreBackend = AccountsMode


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
    model_mode: ModelMode
    # Честная сигнализация деградации: какие ограничения не дали собрать ответ
    # полностью (дедлайн, лимит рекурсии, пустое доказательное покрытие).
    degradation_reasons: list[str] = Field(default_factory=list)


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
    actions: list[ToolAction] = Field(min_length=1, max_length=6)
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
    # Сколько фактов срезано потолком выдачи: полем, а не разбором строки среза,
    # чтобы счётчики не зависели от формулировки текста для человека.
    omitted_count: int = Field(default=0, ge=0)
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
    use_community_context: bool = False
    use_local_graph: bool = True


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
            "device": NodeType.EQUIPMENT,
            "apparatus": NodeType.EQUIPMENT,
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
    """Предложение склейки сущностей.

    ``id`` — детерминированный uuid5 от пары (алиас, канон): регистрация той же
    пары на каждом импорте обязана обновлять запись, а не плодить предложения в
    неограниченной очереди. ``source_id``/``target_id`` — реальные ``id`` узлов
    графа (идентичность сущности в этом коде живёт в ``id``, а ``label``
    описателен); заполняются при принятии и остаются пустыми, пока узлы не
    найдены.
    """

    id: UUID = Field(default_factory=uuid4)
    source: str
    target: str
    confidence: float = Field(ge=0, le=1)
    rationale: str
    status: Literal["proposed", "accepted", "rejected", "reverted"] = "proposed"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reviewed_at: datetime | None = None
    reviewer_id: str | None = None
    source_id: str | None = None
    target_id: str | None = None


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
    data_class: DataClass = DataClass.PUBLIC
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
    # Промпт извлечения ограничен бюджетом: молча не досылать часть документа —
    # приём числа из хвоста не должен выглядеть «в корпусе такого нет».
    prompt_truncated: bool = False
    omitted_characters: int = Field(default=0, ge=0)


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
    vectors_indexed: int = 0


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
    vectors_indexed: int = Field(default=0, ge=0)


class EvaluationMetrics(BaseModel):
    citation_coverage: float = Field(ge=0, le=1)
    numeric_support: float = Field(ge=0, le=1)
    # Средне-заявленная confidence findings заменена честной парой метрик:
    # доля выводов без поддержки и само-оценка модели не одно и то же.
    unsupported_claim_ratio: float = Field(ge=0, le=1)
    mean_finding_confidence: float = Field(ge=0, le=1)
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
    # Кейсы, которые вообще возможно засчитать: ожидаемый источник лежит в
    # текущем корпусе. Остальные дают recall=0 не из-за плохого поиска, а
    # потому что измерять нечего.
    scored_cases: int = Field(ge=0)
    corpus_documents: int = Field(ge=0)
    top_k: int = Field(ge=1)
    hybrid: RankingMetrics
    lexical_baseline: RankingMetrics
    cases: list[RetrievalCaseResult]
    # Инварианты корректности замера (не «утечки»): baseline обязан быть слабее
    # или равен hybrid, ожидаемый источник обязан быть в корпусе.
    validity_checks: dict[str, bool]
    passed: bool


class PipelineVariantMetrics(BaseModel):
    source_recall: float = Field(ge=0, le=1)
    citation_coverage: float = Field(ge=0, le=1)
    pass_rate: float = Field(ge=0, le=1)
    average_latency_ms: float = Field(ge=0)
    p95_latency_ms: float = Field(default=0, ge=0)
    retries_per_case: float = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class PipelineCaseResult(BaseModel):
    case_id: str
    question: str
    expected_sources: list[str]
    retrieved_sources: list[str]
    latency_ms: float = Field(ge=0)
    degradation_reasons: list[str] = Field(default_factory=list)
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
    regressions: list[str] = Field(default_factory=list)
    decision: Literal["promote", "reject"]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class QueryResponse(BaseModel):
    answer: AnswerPayload
    evaluation: EvaluationRun
    # Идентификатор запроса для разбора инцидента: тот же correlation_id, что
    # попадает в журнал аудита и в заголовок ответа. В SSE он едет в каждом
    # событии, поэтому и в JSON-ответе обязан быть — иначе клиент не свяжет
    # свой запрос с записью в журнале.
    correlation_id: str = ""


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


class FeedbackResult(BaseModel):
    """Экспертное решение и предложение по нему — разные по надёжности части.

    Замена утверждения записывается всегда; proposal генерирует LLM, и без
    настроенной модели экспертное исправление не должно теряться.
    """

    proposal: EvolutionProposal | None = None
    superseded: Finding | None = None
    degradation_reasons: list[str] = Field(default_factory=list)


class ProposalReviewRequest(BaseModel):
    accepted: bool


class SystemStatus(BaseModel):
    status: Literal["ready", "degraded"]
    model_mode: ModelMode
    services: dict[str, ServiceState]
    # Фактическое хранилище учётных записей: "in-memory" означает, что Postgres
    # был недоступен на старте и сессии переживают перезапуск только в памяти.
    accounts: AccountsMode = "in-memory"
    # Тот же выбор для серверных копий ответов и экспертных решений: на памяти
    # экспорт возможен только до перезапуска процесса.
    state_backend: StoreBackend = "in-memory"
    # Причина деградации словами: интерфейс показывает её вместо обещаний
    # «история решений сохранена», которых на этом контуре нет.
    degradation_reasons: list[str] = Field(default_factory=list)
    # Пропускная способность агентного контура: сколько прогонов одновременно
    # принимает сервис, а сколько — уже отказ.
    agent_runs_limit: int = Field(default=0, ge=0)
    agent_runs_active: int = Field(default=0, ge=0)


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


# ── FT-23: Export models ────────────────────────────────────────────────────


ExportFormat = Literal["markdown", "json-ld", "pdf"]


class ExportRequest(BaseModel):
    """Экспортируется серверный ответ по ``query_id``, а не присланный клиентом.

    Прежняя форма принимала весь ``AnswerPayload``: клиент мог переклеить
    ``data_class: restricted → public`` в теле и получить закрытый текст
    файлом, потому что ACL фильтровал именно присланные данные. Пост-генерационная
    фильтрация контролем доступа не считается (см. services/governance.py),
    поэтому источник данных — только серверное хранилище ответов.
    ``extra="forbid"``: лишние поля (в том числе подставленный ``answer``) —
    явная ошибка 422, а не молчаливо проигнорированный вход.
    """

    model_config = ConfigDict(extra="forbid")

    query_id: UUID
    format: ExportFormat = "markdown"


class StoredAnswerInfo(BaseModel):
    """Метаданные серверной копии ответа (для диагностики и истории)."""

    query_id: UUID
    owner_id: str
    created_at: datetime
    data_classes: list[str]
    findings: int = Field(ge=0)


class ExpertDecision(BaseModel):
    """Дurable-запись экспертного решения: перезапуск процесса её не стирает.

    Свободного текста из источников здесь нет — только идентификаторы и
    структурированные признаки, чтобы журнал решений не становился каналом
    утечки restricted-содержимого.
    """

    id: UUID = Field(default_factory=uuid4)
    actor_id: str
    action: Literal[
        "proposal.created",
        "proposal.reviewed",
        "resolution.reviewed",
        "claim.superseded",
        "answer.exported",
    ]
    object_id: str
    outcome: Literal["success", "denied", "failure"] = "success"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


# ── FT-08: Claim versioning ─────────────────────────────────────────────────


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


# ── FT-20/21: Учётные записи и доступ ───────────────────────────────────────


class EmailRequest(BaseModel):
    """Общая форма email для запросов входа и регистрации.

    Нормализация (strip + lower) здесь, а не только в хранилище: «Ivan@…» и
    «ivan@…» — один и тот же аккаунт на всём пути запроса.
    """

    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _EMAIL_RE.fullmatch(normalized):
            raise ValueError("Некорректный email")
        return normalized


class AccountRegisterRequest(EmailRequest):
    """Запрос саморегистрации. Минимальную длину пароля задаёт не схема, а
    ``settings.password_min_length`` — обработчик проверяет её после валидации."""

    display_name: str = Field(min_length=1, max_length=120)
    # Потолок нужен, чтобы scrypt не считал бесконечный ввод клиента.
    password: str = Field(min_length=1, max_length=256)


class AccountLoginRequest(EmailRequest):
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ProfileUpdateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class AccountInfo(BaseModel):
    """Ответ ``/api/v1/auth/*``: профиль плюс фактические возможности аккаунта.

    ``capabilities`` и ``data_classes`` всегда приходят из таблицы политик
    (services/governance.py), а не из сохранённых полей, поэтому интерфейс не
    может показать права, которых на самом деле нет.
    """

    id: str
    email: str
    display_name: str
    review_enabled: bool
    created_at: datetime
    capabilities: list[str] = Field(default_factory=list)
    data_classes: list[str] = Field(default_factory=list)


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
    gaps_omitted: int = Field(default=0, ge=0)
    recent_activity: list[ActivityEntry]
    agent_metrics: AgentMetricsResponse


# ── FT-24: Notification models ─────────────────────────────────────────────


class Notification(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    topic: NotificationTopic
    message: str = Field(min_length=1, max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
