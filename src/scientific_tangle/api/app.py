import base64
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from prometheus_fastapi_instrumentator import Instrumentator

from scientific_tangle import __version__
from scientific_tangle.agents.tools import ResearchToolExecutor
from scientific_tangle.agents.workflow import ResearchWorkflow
from scientific_tangle.config import get_settings
from scientific_tangle.domain.contracts import (
    ActivityEntry,
    AgentMetricsResponse,
    ClaimHistory,
    ClaimHistoryEntry,
    ComparisonRequest,
    ComparisonTable,
    CorpusStats,
    DashboardResponse,
    DocumentReceipt,
    DocumentRequest,
    EntityMergeProposal,
    EvaluationRun,
    EvolutionExperiment,
    EvolutionProposal,
    ExportRequest,
    FeedbackRequest,
    Finding,
    GoldCase,
    GraphSnapshot,
    MergeReviewRequest,
    Notification,
    PipelineBenchmark,
    PrincipalInfo,
    ProposalReviewRequest,
    QueryRequest,
    QueryResponse,
    RetrievalBenchmark,
    RoleInfo,
    SubscriptionRequest,
    SystemStatus,
)
from scientific_tangle.domain.intelligence import AuditEvent, Principal, ResearchSpace
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.evaluation.harness import EvaluationHarness
from scientific_tangle.services.agent_metrics import agent_metrics
from scientific_tangle.services.comparison import ComparisonService
from scientific_tangle.services.document_parser import UnsupportedDocumentError, parse_document
from scientific_tangle.services.evolution import EvolutionService
from scientific_tangle.services.exporter import ExportService
from scientific_tangle.services.governance import AccessPolicyEngine, InMemoryAuditLog
from scientific_tangle.services.infrastructure import build_knowledge_base
from scientific_tangle.services.ingestion import IngestionService
from scientific_tangle.services.provider import ModelUnavailableError, build_provider
from scientific_tangle.services.research_intelligence import ResearchIntelligenceService
from scientific_tangle.services.resolution import EntityResolutionWorkbench

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

settings = get_settings()
knowledge = build_knowledge_base(settings)
provider = build_provider(settings)
workflow = ResearchWorkflow(knowledge=knowledge, provider=provider)
harness = EvaluationHarness()
evolution = EvolutionService(provider)
resolution = EntityResolutionWorkbench(settings)
ingestion = IngestionService(knowledge, provider, resolution)

audit_log = InMemoryAuditLog()
access_engine = AccessPolicyEngine()
comparison_service = ComparisonService()
export_service = ExportService()
_notifications: list[Notification] = []
_subscriptions: dict[str, set[str]] = {}

# Ролевая модель доступа (FT-20/21)
ROLE_PERMISSIONS: dict[str, dict[str, set[str]]] = {
    "researcher": {
        "permissions": {"knowledge:read", "query:ask", "feedback:give"},
        "data_classes": {"public", "internal"},
    },
    "analyst": {
        "permissions": {
            "knowledge:read", "query:ask", "feedback:give",
            "export:run", "evaluation:view",
        },
        "data_classes": {"public", "internal"},
    },
    "project_manager": {
        "permissions": {
            "knowledge:read", "query:ask", "feedback:give",
            "export:run", "evaluation:view", "proposal:review",
            "restricted:read",
        },
        "data_classes": {"public", "internal", "restricted"},
    },
    "administrator": {
        "permissions": {
            "knowledge:read", "query:ask", "feedback:give",
            "export:run", "evaluation:view", "proposal:review",
            "restricted:read", "audit:read", "user:manage",
        },
        "data_classes": {"public", "internal", "restricted"},
    },
    "external_partner": {
        "permissions": {"knowledge:read", "query:ask"},
        "data_classes": {"public"},
    },
}

# Маппинг статуса finding на класс данных для ACL-фильтрации
STATUS_TO_DATA_CLASS: dict[str, str] = {
    "consensus": "public",
    "hypothesis": "internal",
    "disputed": "restricted",
}

# Маппинг mode провайдера на валидное значение SystemStatus.model_mode.
# UnavailableProvider («unavailable») не имеет реального LLM — отображаем в «scripted».


_PROVIDER_MODE_TO_STATUS: dict[
    str, Literal["yandex", "gigachat", "fallback", "scripted"]
] = {
    "yandex": "yandex",
    "gigachat": "gigachat",
    "fallback": "fallback",
    "unavailable": "scripted",
}


def _build_principal(request: Request) -> Principal:
    role = request.headers.get("X-User-Role", "researcher")
    if role not in ROLE_PERMISSIONS:
        role = "researcher"
    user_id = request.headers.get("X-User-Id", "anonymous")
    perms = ROLE_PERMISSIONS[role]
    return Principal(
        id=user_id,
        roles={role},  # type: ignore[arg-type]
        permissions=perms["permissions"],
    )


def _log_audit(
    request: Request, action: str, object_id: str, outcome: str = "success"
) -> None:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return
    audit_log.append(
        AuditEvent(
            actor_id=principal.id,
            action=action,
            object_id=object_id,
            outcome=outcome,  # type: ignore[arg-type]
            correlation_id=getattr(request.state, "correlation_id", str(uuid4())),
        )
    )


def require_permission(
    permission: str,
) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency: проверяет наличие permission у principal."""

    async def _check(request: Request) -> None:
        principal = getattr(request.state, "principal", None)
        if principal is None or permission not in principal.permissions:
            raise HTTPException(
                status_code=403, detail=f"Требуется разрешение: {permission}"
            )

    return _check


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.workflow = workflow
    application.state.checkpointer = None
    if settings.knowledge_backend != "neo4j":
        yield
        return
    async with AsyncPostgresSaver.from_conn_string(settings.database_url) as checkpointer:
        await checkpointer.setup()
        application.state.workflow = ResearchWorkflow(
            knowledge=knowledge,
            provider=provider,
            metrics=agent_metrics,
            checkpointer=checkpointer,
        )
        application.state.checkpointer = checkpointer
        yield


app = FastAPI(
    title="Научный Клубок API",
    version=__version__,
    description="Evidence-centric Agentic GraphRAG API by MindAI",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
Instrumentator(excluded_handlers=["/metrics"]).instrument(app).expose(
    app,
    include_in_schema=False,
)


@app.middleware("http")
async def acl_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    request.state.principal = _build_principal(request)
    request.state.correlation_id = str(uuid4())
    response = await call_next(request)
    return response


@app.exception_handler(ModelUnavailableError)
async def model_unavailable(_: Request, error: ModelUnavailableError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(error)})


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"], response_model=SystemStatus)
async def readiness(request: Request) -> SystemStatus:
    active_workflow = getattr(request.app.state, "workflow", workflow)
    raw_mode = active_workflow.provider.mode
    model_mode: Literal["yandex", "gigachat", "fallback", "scripted"] = (
        _PROVIDER_MODE_TO_STATUS.get(raw_mode, "scripted")
    )
    model_ready = model_mode != "scripted"
    return SystemStatus(
        status="ready" if model_ready else "degraded",
        model_mode=model_mode,
        services={
            "knowledge_graph": (
                "configured" if settings.knowledge_backend == "neo4j" else "fallback"
            ),
            "hybrid_search": (
                "configured" if settings.knowledge_backend == "neo4j" else "fallback"
            ),
            "model_provider": "configured" if model_ready else "fallback",
            "evaluation": "ready",
        },
    )


@app.post("/api/v1/queries/validate", tags=["queries"])
async def validate_query_plan(plan: QueryPlan) -> QueryPlan:
    """Проверяет типизированный план до обращения к retrieval-контуру."""
    return plan


@app.post("/api/v1/query", tags=["queries"], response_model=QueryResponse)
async def research_query(query: QueryRequest, request: Request) -> QueryResponse:
    active_workflow = getattr(request.app.state, "workflow", workflow)
    # Pre-retrieval ACL: передаём allowed_data_classes в workflow
    principal = request.state.principal
    role = next(iter(principal.roles))
    allowed = ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["researcher"])["data_classes"]
    answer = await active_workflow.run(query, allowed_data_classes=allowed)
    # Post-retrieval ACL safety net: фильтруем findings по data_class
    allowed_findings = [
        f for f in answer.findings
        if STATUS_TO_DATA_CLASS.get(f.status, "internal") in allowed
    ]
    removed_finding_ids = {f.id for f in answer.findings} - {f.id for f in allowed_findings}
    # Graph node IDs that correspond to removed findings
    removed_node_ids = set(removed_finding_ids)
    for fid in removed_finding_ids:
        if fid.startswith("finding-"):
            removed_node_ids.add(fid.removeprefix("finding-"))
    # Filter conflicts and gaps that reference removed findings
    filtered_conflicts = [
        c for c in answer.conflicts
        if not any(rid in c for rid in removed_finding_ids)
    ]
    filtered_gaps = [
        g for g in answer.knowledge_gaps
        if not any(rid in g for rid in removed_finding_ids)
    ]
    # Filter tool observations that reference removed findings
    filtered_observations = [
        obs for obs in answer.tool_observations
        if not any(rid in obs.finding_ids for rid in removed_finding_ids)
    ]
    # Filter graph nodes and edges that reference removed findings
    filtered_nodes = [
        n for n in answer.graph.nodes if n.id not in removed_node_ids
    ]
    kept_node_ids = {n.id for n in filtered_nodes}
    filtered_edges = [
        e for e in answer.graph.edges
        if e.source in kept_node_ids and e.target in kept_node_ids
    ]
    answer = answer.model_copy(
        update={
            "findings": allowed_findings,
            "conflicts": filtered_conflicts,
            "knowledge_gaps": filtered_gaps,
            "tool_observations": filtered_observations,
            "graph": GraphSnapshot(
                nodes=filtered_nodes,
                edges=filtered_edges,
                communities=answer.graph.communities,
            ),
        }
    )
    evaluation = harness.evaluate(answer)
    _log_audit(request, "query.run", str(answer.query_id))
    return QueryResponse(answer=answer, evaluation=evaluation)


@app.get("/api/v1/demo", tags=["queries"], response_model=QueryResponse)
async def demo_query(request: Request) -> QueryResponse:
    return await research_query(
        QueryRequest(
            question=(
                "Какие методы обессоливания подходят для шахтной воды с сульфатами и "
                "хлоридами 200–300 мг/л при сухом остатке ≤1000 мг/л?"
            )
        ),
        request,
    )


@app.post("/api/v1/query/stream", tags=["queries"])
async def stream_query(
    query: QueryRequest, request: Request
) -> StreamingResponse:
    """Стриминг выполнения агентного пайплайна через SSE."""
    active_workflow = getattr(request.app.state, "workflow", workflow)
    # Pre-retrieval ACL: передаём allowed_data_classes в workflow
    principal = request.state.principal
    role = next(iter(principal.roles))
    allowed = ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["researcher"])["data_classes"]

    async def event_generator():
        try:
            # Входное состояние для LangGraph StateGraph
            input_state = {
                "question": query.question,
                "language": query.language,
                "requested_mode": query.mode,
                "revision_count": 0,
                "action_round": 0,
                "trace": [],
                "allowed_data_classes": allowed,
            }
            config = {"configurable": {"thread_id": str(query.thread_id)}}

            # Немедленное start-событие — пользователь видит, что запрос принят
            yield f"data: {json.dumps({'type': 'start', 'question': query.question}, ensure_ascii=False)}\n\n"
            logger.info("SSE stream started for question: %s", query.question[:100])

            final_answer = None
            # Стриминг обновлений состояния по мере выполнения узлов графа
            async for chunk in active_workflow.graph.astream(
                input_state, config=config, stream_mode="updates"
            ):
                if not isinstance(chunk, dict):
                    continue
                for node_name, update in chunk.items():
                    if not isinstance(update, dict):
                        continue
                    # Отправляем события шагов из trace
                    trace = update.get("trace")
                    if isinstance(trace, list) and trace:
                        last_event = trace[-1]
                        if hasattr(last_event, "model_dump"):
                            event_data = last_event.model_dump()
                            # step отправляется как объект — фронтенд ожидает
                            # { agent, status, message } для обновления шагов агента
                            data = {
                                "type": "step",
                                "step": {
                                    "agent": event_data.get("agent", node_name),
                                    "status": event_data.get("status", "completed"),
                                    "message": event_data.get("message", ""),
                                    "duration_ms": event_data.get("duration_ms"),
                                },
                            }
                            yield f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
                            logger.info("SSE: sent step event for node '%s'", node_name)
                            # Processing-событие показывает прогресс между шагами
                            yield f"data: {json.dumps({'type': 'processing', 'step': node_name}, ensure_ascii=False)}\n\n"
                    # Сохраняем финальный ответ из узла finalize
                    answer = update.get("answer")
                    if answer is not None:
                        final_answer = answer

            # Отправляем финальный ответ
            logger.info("SSE: sending final answer")
            if final_answer is not None:
                # Post-retrieval ACL: фильтруем findings по data_class
                allowed_findings = [
                    f for f in final_answer.findings
                    if STATUS_TO_DATA_CLASS.get(f.status, "internal") in allowed
                ]
                final_answer = final_answer.model_copy(
                    update={"findings": allowed_findings}
                )
                data = {
                    "type": "answer",
                    "answer": final_answer.model_dump(mode="json"),
                }
                yield f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            else:
                # Fallback: запускаем обычный run, если стриминг не дал ответ
                answer = await active_workflow.run(
                    query, allowed_data_classes=allowed
                )
                allowed_findings = [
                    f for f in answer.findings
                    if STATUS_TO_DATA_CLASS.get(f.status, "internal") in allowed
                ]
                answer = answer.model_copy(update={"findings": allowed_findings})
                data = {
                    "type": "answer",
                    "answer": answer.model_dump(mode="json"),
                }
                yield f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

            # Событие завершения стрима
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            error_data = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(error_data, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/v1/documents", tags=["ingestion"], response_model=DocumentReceipt)
async def ingest_document(document: DocumentRequest) -> DocumentReceipt:
    return await ingestion.ingest(document)


@app.post("/api/v1/documents/upload", tags=["ingestion"], response_model=DocumentReceipt)
async def upload_document(
    file: Annotated[UploadFile, File()],
    language: Annotated[Literal["ru", "en"], Form()] = "ru",
    geography: Annotated[str | None, Form()] = None,
    year: Annotated[int | None, Form()] = None,
) -> DocumentReceipt:
    try:
        document = parse_document(
            file.filename or "document.txt",
            await file.read(),
            language=language,
            geography=geography,
            year=year,
        )
    except (UnsupportedDocumentError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return await ingestion.ingest(document)


@app.get("/api/v1/graph", tags=["knowledge"], response_model=GraphSnapshot)
async def get_graph() -> GraphSnapshot:
    return knowledge.full_graph()


@app.get("/api/v1/findings", tags=["knowledge"])
async def get_findings(
    subject: str | None = None,
    status: str | None = None,
) -> list[dict]:  # type: ignore[type-arg]
    """Возвращает все находки с доказательствами и наблюдениями."""
    findings = knowledge.all_findings() if hasattr(knowledge, "all_findings") else []
    # Фильтрация по subject, если передан
    if subject:
        findings = [
            f for f in findings if subject.lower() in (f.subject or "").lower()
        ]
    # Фильтрация по status, если передан
    if status:
        findings = [f for f in findings if f.status == status]
    return [f.model_dump(mode="json") for f in findings]


@app.get("/api/v1/conflicts", tags=["knowledge"])
async def get_conflicts() -> list[dict]:  # type: ignore[type-arg]
    """Возвращает все конфликтные находки (status=disputed)."""
    findings = knowledge.all_findings() if hasattr(knowledge, "all_findings") else []
    disputed = [f for f in findings if f.status == "disputed"]
    return [f.model_dump(mode="json") for f in disputed]


@app.get("/api/v1/corpus/stats", tags=["knowledge"], response_model=CorpusStats)
async def get_corpus_stats() -> CorpusStats:
    return knowledge.corpus_stats()


@app.get(
    "/api/v1/entity-resolution/proposals",
    tags=["knowledge"],
    response_model=list[EntityMergeProposal],
)
async def get_entity_resolution_proposals() -> list[EntityMergeProposal]:
    return resolution.list_proposals()


@app.post(
    "/api/v1/entity-resolution/proposals/{proposal_id}/review",
    tags=["knowledge"],
    response_model=EntityMergeProposal,
)
async def review_entity_resolution_proposal(
    proposal_id: UUID,
    request: MergeReviewRequest,
    _: None = Depends(require_permission("proposal:review")),
) -> EntityMergeProposal:
    try:
        return resolution.review(proposal_id, request.action)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Merge proposal not found") from error


@app.get("/api/v1/evaluations", tags=["evaluation"], response_model=list[EvaluationRun])
async def get_evaluations(
    _: None = Depends(require_permission("evaluation:view")),
) -> list[EvaluationRun]:
    return harness.list_runs()


@app.get("/api/v1/agents/metrics", tags=["evaluation"], response_model=AgentMetricsResponse)
async def get_agent_metrics() -> AgentMetricsResponse:
    return agent_metrics.snapshot()


@app.get("/api/v1/evaluations/gold", tags=["evaluation"], response_model=list[GoldCase])
async def get_gold_cases() -> list[GoldCase]:
    return harness.gold_cases()


@app.post(
    "/api/v1/evaluations/retrieval-benchmark",
    tags=["evaluation"],
    response_model=RetrievalBenchmark,
)
async def run_retrieval_benchmark(
    _: None = Depends(require_permission("evaluation:view")),
) -> RetrievalBenchmark:
    return harness.benchmark_retrieval(knowledge)


@app.post(
    "/api/v1/evaluations/pipeline-benchmark",
    tags=["evaluation"],
    response_model=PipelineBenchmark,
)
async def run_pipeline_benchmark(
    request: Request,
    max_cases: Annotated[int, Query(ge=1, le=3)] = 3,
    _: None = Depends(require_permission("evaluation:view")),
) -> PipelineBenchmark:
    stats = knowledge.corpus_stats()
    if stats.semantic_documents < max_cases:
        raise HTTPException(
            status_code=409,
            detail="Недостаточно semantic_extracted документов. Сначала выполните preload.",
        )
    active_workflow = getattr(request.app.state, "workflow", workflow)
    return await harness.benchmark_pipeline(active_workflow, knowledge, max_cases)


@app.post(
    "/api/v1/proposals/{proposal_id}/experiment",
    tags=["evolution"],
    response_model=EvolutionExperiment,
)
async def run_evolution_experiment(
    proposal_id: UUID,
    request: Request,
    max_cases: Annotated[int, Query(ge=1, le=3)] = 1,
) -> EvolutionExperiment:
    try:
        proposal = evolution.get(proposal_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Proposal not found") from error
    if proposal.kind not in {"prompt", "rule"}:
        raise HTTPException(
            status_code=422,
            detail="A/B workflow experiment поддерживает prompt и rule proposals.",
        )
    cases = harness.gold_cases()[:max_cases]
    baseline_workflow = getattr(request.app.state, "workflow", workflow)
    baseline, _ = await harness.evaluate_workflow(baseline_workflow, cases)
    candidate_workflow = ResearchWorkflow(
        knowledge=knowledge,
        provider=provider,
        metrics=agent_metrics,
        extra_policy=proposal.change,
    )
    candidate, _ = await harness.evaluate_workflow(candidate_workflow, cases)
    promote = (
        candidate.pass_rate >= baseline.pass_rate
        and candidate.source_recall >= baseline.source_recall
        and candidate.citation_coverage >= baseline.citation_coverage
    )
    return evolution.add_experiment(
        EvolutionExperiment(
            proposal_id=proposal_id,
            cases=len(cases),
            baseline=baseline,
            candidate=candidate,
            delta_pass_rate=round(candidate.pass_rate - baseline.pass_rate, 3),
            decision="promote" if promote else "reject",
        )
    )


@app.get(
    "/api/v1/experiments",
    tags=["evolution"],
    response_model=list[EvolutionExperiment],
)
async def get_evolution_experiments() -> list[EvolutionExperiment]:
    return evolution.list_experiments()


@app.post("/api/v1/feedback", tags=["evolution"], response_model=EvolutionProposal)
async def submit_feedback(
    feedback: FeedbackRequest, request: Request
) -> EvolutionProposal:
    proposal = await evolution.propose(feedback)
    _log_audit(request, "feedback.submit", str(proposal.id))
    # FT-08: при экспертной корректировке создаём новую версию утверждения
    if (
        feedback.verdict == "correct"
        and feedback.finding_id
        and feedback.correction
        and hasattr(knowledge, "supersede_finding")
    ):
        principal = request.state.principal
        knowledge.supersede_finding(
            feedback.finding_id,
            feedback.correction,
            0.95,
            reviewer_id=principal.id,
            review_date=datetime.now(UTC).isoformat(),
            review_reason=feedback.comment,
        )
        _notifications.append(
            Notification(
                topic="claim.superseded",
                message=(
                    f"Утверждение {feedback.finding_id} заменено "
                    f"исправленной версией."
                ),
            )
        )
    return proposal


@app.get("/api/v1/proposals", tags=["evolution"], response_model=list[EvolutionProposal])
async def get_proposals() -> list[EvolutionProposal]:
    return evolution.list_proposals()


@app.post(
    "/api/v1/proposals/{proposal_id}/review",
    tags=["evolution"],
    response_model=EvolutionProposal,
)
async def review_proposal(
    proposal_id: UUID,
    review_request: ProposalReviewRequest,
    request: Request,
    _: None = Depends(require_permission("proposal:review")),
) -> EvolutionProposal:
    try:
        reviewed = evolution.review(proposal_id, review_request.accepted)
        if reviewed.status == "accepted":
            _notifications.append(
                Notification(
                    topic="proposal.accepted",
                    message=f"Предложение {proposal_id} принято и активировано.",
                )
            )
            request.app.state.workflow = ResearchWorkflow(
                knowledge=knowledge,
                provider=provider,
                metrics=agent_metrics,
                checkpointer=getattr(request.app.state, "checkpointer", None),
                extra_policy=evolution.active_policy(),
            )
        _log_audit(
            request,
            "proposal.review",
            str(proposal_id),
            "success" if reviewed.status == "accepted" else "denied",
        )
        return reviewed
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Proposal not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


# ── FT-20/21: ACL endpoints ─────────────────────────────────────────────────


@app.get("/api/v1/roles", tags=["acl"], response_model=list[RoleInfo])
async def get_roles() -> list[RoleInfo]:
    return [
        RoleInfo(
            role=role,
            permissions=sorted(perms["permissions"]),
            data_classes=sorted(perms["data_classes"]),
        )
        for role, perms in ROLE_PERMISSIONS.items()
    ]


@app.get("/api/v1/principal", tags=["acl"], response_model=PrincipalInfo)
async def get_principal(request: Request) -> PrincipalInfo:
    principal = request.state.principal
    role = next(iter(principal.roles))
    perms = ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["researcher"])
    return PrincipalInfo(
        user_id=principal.id,
        role=role,
        permissions=sorted(principal.permissions),
        allowed_data_classes=sorted(perms["data_classes"]),
    )


@app.get("/api/v1/audit", tags=["acl"])
async def get_audit_log(
    _: None = Depends(require_permission("user:manage")),
) -> list[dict]:  # type: ignore[type-arg]
    events = audit_log.list()
    return [event.model_dump(mode="json") for event in events]


# ── FT-12/26: Comparison endpoint ──────────────────────────────────────────


@app.post("/api/v1/compare", tags=["comparison"], response_model=ComparisonTable)
async def compare_entities(
    comparison: ComparisonRequest, request: Request,
    _: None = Depends(require_permission("export:run")),
) -> ComparisonTable:
    findings = knowledge.all_findings() if hasattr(knowledge, "all_findings") else []
    _log_audit(request, "compare.run", comparison.question[:80])
    return comparison_service.compare(findings, comparison)


# ── FT-23: Export endpoint ─────────────────────────────────────────────────


@app.post("/api/v1/export", tags=["export"])
async def export_answer(
    export_req: ExportRequest, request: Request,
    _: None = Depends(require_permission("export:run")),
) -> Response:
    content, content_type, filename = export_service.export(
        export_req.answer, export_req.format
    )
    _log_audit(request, "export.run", filename)
    # PDF возвращается как base64 — декодируем в bytes
    response_content: str | bytes = content
    if content_type == "application/pdf":
        response_content = base64.b64decode(content)
    return Response(
        content=response_content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── FT-08: Claim versioning endpoint ───────────────────────────────────────


@app.get(
    "/api/v1/claims/{claim_id}/history",
    tags=["knowledge"],
    response_model=ClaimHistory,
)
async def get_claim_history(claim_id: str) -> ClaimHistory:
    if not hasattr(knowledge, "claim_history"):
        raise HTTPException(status_code=404, detail="История версий недоступна")
    versions = knowledge.claim_history(claim_id)
    if not versions:
        raise HTTPException(
            status_code=404, detail=f"Утверждение {claim_id} не найдено"
        )
    return ClaimHistory(
        claim_id=claim_id,
        versions=[
            ClaimHistoryEntry(
                finding_id=f.id,
                version=f.version,
                statement=f.statement,
                status=f.status,
                superseded_by=f.superseded_by,
                reviewer_id=f.reviewer_id,
                review_date=f.review_date,
                review_reason=f.review_reason,
            )
            for f in versions
        ],
    )


def _count_knowledge_gaps(findings: list[Finding]) -> int:
    """Считает фактические пробелы в покрытии через ResearchIntelligenceService."""
    intelligence = ResearchIntelligenceService()
    claims = ResearchToolExecutor._to_research_claims(findings)
    if not claims:
        return 0
    dimensions: dict[str, set[str]] = {}
    for claim in claims:
        for dim in claim.scope:
            dimensions.setdefault(dim.name, set()).add(dim.value)
    if not dimensions:
        return 0
    research_space = ResearchSpace(
        dimensions={k: sorted(v) for k, v in dimensions.items()}
    )
    gaps = intelligence.detect_gaps(claims, research_space)
    return len(gaps)


@app.get("/api/v1/dashboard", tags=["dashboard"], response_model=DashboardResponse)
async def get_dashboard() -> DashboardResponse:
    stats = knowledge.corpus_stats()
    findings = knowledge.all_findings() if hasattr(knowledge, "all_findings") else []
    evidence_count = sum(len(f.evidence) for f in findings)
    conflict_count = sum(1 for f in findings if f.status == "disputed")
    # Фактические пробелы через research intelligence, а не findings без observations
    gap_count = _count_knowledge_gaps(findings)
    recent = audit_log.list()[-10:]
    return DashboardResponse(
        documents=stats.documents,
        claims=stats.claims,
        entities=stats.entities,
        evidence=evidence_count,
        conflicts=conflict_count,
        gaps=gap_count,
        recent_activity=[
            ActivityEntry(
                action=e.action,
                actor_id=e.actor_id,
                object_id=e.object_id,
                outcome=e.outcome,
                created_at=e.created_at,
            )
            for e in reversed(recent)
        ],
        agent_metrics=agent_metrics.snapshot(),
    )


# ── FT-24: Notifications stub ─────────────────────────────────────────────


@app.get(
    "/api/v1/notifications",
    tags=["notifications"],
    response_model=list[Notification],
)
async def get_notifications() -> list[Notification]:
    return list(reversed(_notifications))[:20]


@app.post("/api/v1/subscriptions", tags=["notifications"])
async def subscribe(subscription: SubscriptionRequest) -> dict[str, str]:
    _subscriptions.setdefault(subscription.topic, set()).add(
        subscription.subscriber_id
    )
    return {"status": "subscribed", "topic": subscription.topic}
