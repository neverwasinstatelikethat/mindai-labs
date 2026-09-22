from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.background import BackgroundTask

from scientific_tangle import __version__
from scientific_tangle.agents.workflow import ResearchWorkflow, WorkflowNodeError
from scientific_tangle.config import Settings, get_settings
from scientific_tangle.domain.contracts import (
    AccountInfo,
    AccountLoginRequest,
    AccountRegisterRequest,
    ActivityEntry,
    AgentMetricsResponse,
    AnswerPayload,
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
    ExpertDecision,
    ExportRequest,
    FeedbackRequest,
    FeedbackResult,
    Finding,
    GoldCase,
    GraphSnapshot,
    MergeReviewRequest,
    Notification,
    NotificationTopic,
    PasswordChangeRequest,
    PipelineBenchmark,
    ProfileUpdateRequest,
    ProposalReviewRequest,
    QueryRequest,
    QueryResponse,
    RetrievalBenchmark,
    ServiceState,
    SystemStatus,
)
from scientific_tangle.domain.intelligence import AuditEvent, DataClass, Principal
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.evaluation.harness import EvaluationHarness
from scientific_tangle.services.accounts import (
    Account,
    AccountsStore,
    EmailAlreadyRegisteredError,
    InMemoryAccounts,
    LoginRateLimiter,
    build_accounts,
    normalize_email,
)
from scientific_tangle.services.admission import (
    AdmissionRefusedError,
    AgentRunAdmission,
    agent_run_limit,
)
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry, agent_metrics
from scientific_tangle.services.comparison import ComparisonService
from scientific_tangle.services.document_parser import UnsupportedDocumentError, parse_document
from scientific_tangle.services.durable_state import (
    DurableState,
    InMemoryDurableState,
    StoredAnswer,
    build_durable_state,
)
from scientific_tangle.services.evolution import EvolutionService
from scientific_tangle.services.exporter import ExportError, ExportService
from scientific_tangle.services.governance import AccessPolicyEngine
from scientific_tangle.services.infrastructure import Neo4jElasticsearchKnowledgeBase
from scientific_tangle.services.ingestion import IngestionService
from scientific_tangle.services.knowledge import KnowledgeBase
from scientific_tangle.services.ontology import OntologyValidationError
from scientific_tangle.services.provider import ModelUnavailableError, build_provider
from scientific_tangle.services.research_intelligence import (
    ResearchIntelligenceService,
    build_research_space,
    to_research_claims,
)
from scientific_tangle.services.resolution import EntityResolutionWorkbench

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Лента уведомлений: столько записей отдаёт ``/notifications`` (буфер серверного
# состояния шире — см. services/durable_state.MAX_NOTIFICATIONS).
NOTIFICATION_FEED_LIMIT = 20
AUDIT_LIMIT_DEFAULT = 200
AUDIT_LIMIT_MAX = 1000
_WORKFLOW_LOCK = threading.Lock()
# Только против параллельного холодного старта витрины (см. /demo).
_DEMO_LOCK = asyncio.Lock()

# ── Доступ: контракт «сессия → субъект» ──────────────────────────────────────
SESSION_COOKIE = "nk_session"
UNAUTHENTICATED_DETAIL = "Требуется вход в аккаунт"
CSRF_DETAIL = "Источник запроса не входит в доверенный список"
STATE_CHANGING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
# Без сессии работают только регистрация/вход и агрегированная статистика
# корпуса (числа без признаков класса доступа).
PUBLIC_API_PATHS = frozenset(
    {"/api/v1/corpus/stats", "/api/v1/auth/register", "/api/v1/auth/login"}
)

access_engine = AccessPolicyEngine()
intelligence = ResearchIntelligenceService()


class AppDependencies:
    """Зависимости приложения.

    Строятся лениво, а не на import модуля: раньше драйвер Neo4j, клиент
    Elasticsearch и LLM-провайдер создавались при импорте, и любое падение
    конфигурации ломало даже импорт приложения.
    """

    def __init__(self) -> None:
        self.settings: Settings = get_settings()
        self.knowledge: KnowledgeBase = _build_knowledge(self.settings)
        self.provider = build_provider(self.settings)
        self.metrics: AgentMetricsRegistry = agent_metrics
        self.harness: EvaluationHarness = EvaluationHarness()
        self.evolution: EvolutionService = EvolutionService(self.provider)
        self.resolution: EntityResolutionWorkbench = EntityResolutionWorkbench(self.settings)
        self.ingestion: IngestionService = IngestionService(
            self.knowledge, self.provider, self.resolution
        )
        self.comparison: ComparisonService = ComparisonService()
        self.exporter: ExportService = ExportService()
        # Граф компилируется один раз на ленивом обращении (см. ``workflow``):
        # раньше StateGraph собирался в конструкторе и тут же пересобирался в
        # lifespan, то есть двойная компиляция на каждый старт процесса.
        self._workflow: ResearchWorkflow | None = None
        self.checkpointer: object | None = None
        # Учётные записи: пул Postgres здесь только конструируется, открытие и
        # создание схемы — в lifespan (см. _prepare_accounts).
        self.accounts: AccountsStore = build_accounts(self.settings)
        # Причина деградации доступа: Postgres недоступен → сессии в памяти.
        self.accounts_error: str | None = None
        # Серверное состояние: копии ответов, аудит, экспертные решения, A/B- прогоны,
        # оценки и лента уведомлений. Выбор контура — вместе с учётными записями.
        self.state: DurableState = build_durable_state(self.settings)
        self.state_error: str | None = None
        self.login_limiter: LoginRateLimiter = LoginRateLimiter(
            max_attempts=self.settings.login_max_attempts,
            window_seconds=self.settings.login_window_seconds,
        )
        # Приём агентных прогонов: слот выдаётся сразу или запрос получает 429.
        # Очереди нет, поэтому дедлайн запроса не тратится на ожидание чужого
        # исследования (см. services/admission.py).
        self.admission: AgentRunAdmission = AgentRunAdmission(
            limit=agent_run_limit(self.settings),
            deadline_seconds=self.settings.agent_deadline_seconds,
            metrics=self.metrics,
        )
        # Демо-ответ кэшируется: эндпоинт не должен сжигать LLM-бюджет при каждом
        # обращении. Кэш — реальный ответ, а не заготовленная выдумка.
        self.demo_answer: AnswerPayload | None = None
        self.demo_evaluation: EvaluationRun | None = None
        # Контур доступа, в котором витрина собрана: без него второй зритель с
        # более широкими правами не понял бы, почему restricted-доказательств нет.
        self.demo_access: set[DataClass] | None = None

    @property
    def workflow(self) -> ResearchWorkflow:
        """Единственный скомпилированный граф приложения."""
        if self._workflow is None:
            self._workflow = self.build_workflow()
        return self._workflow

    @workflow.setter
    def workflow(self, workflow: ResearchWorkflow) -> None:
        self._workflow = workflow

    def build_workflow(
        self,
        *,
        extra_policy: str = "",
        checkpointer: Any | None = None,
    ) -> ResearchWorkflow:
        """Сборка рабочего процесса: набор зависимостей живёт в одном месте.

        ``checkpointer`` по умолчанию берётся из контейнера — так вызов из
        lifespan, из принятия предложения и из A/B-эксперимента не расходятся в
        том, с каким чекпоинтером граф работает.
        """
        return ResearchWorkflow(
            knowledge=self.knowledge,
            provider=self.provider,
            metrics=self.metrics,
            checkpointer=self.checkpointer if checkpointer is None else checkpointer,
            extra_policy=extra_policy,
            settings=self.settings,
        )

    def close(self) -> None:
        """Закрывает внешние соединения процесса (драйверы, пулы)."""
        self.knowledge.close()
        self.resolution.close()


def _build_knowledge(settings: Settings) -> KnowledgeBase:
    if settings.knowledge_backend == "neo4j":
        return Neo4jElasticsearchKnowledgeBase(settings)
    from scientific_tangle.services.knowledge import InMemoryKnowledgeBase

    return InMemoryKnowledgeBase()


@lru_cache(maxsize=1)
def get_dependencies() -> AppDependencies:
    return AppDependencies()


def dependencies(request: Request) -> AppDependencies:
    state = getattr(request.app.state, "dependencies", None)
    return state if isinstance(state, AppDependencies) else get_dependencies()


def workflow_for(request: Request) -> ResearchWorkflow:
    return dependencies(request).workflow


async def _prepare_accounts(deps: AppDependencies) -> None:
    """Поднимает хранилище учётных записей; недоступный Postgres — деградация.

    Повторяет судьбу GigaChat: сервис поднимается в любом случае, а причина
    видна в логах и в ``/health/ready`` (поле ``accounts``). Иначе падение
    postgres или запуск без базы роняли бы весь API вместе с чтением корпуса.
    """
    if deps.settings.accounts_backend != "postgres":
        await deps.accounts.setup()
        return
    try:
        await deps.accounts.setup()
    except Exception as error:  # noqa: BLE001 — деградация вместо падения сервиса
        logger.error("Хранилище учётных записей недоступно, уходим в память: %s", error)
        await deps.accounts.close()
        deps.accounts = InMemoryAccounts(deps.settings)
        deps.accounts_error = f"Postgres недоступен, сессии хранятся в памяти: {error}"


async def _prepare_state(deps: AppDependencies) -> None:
    """Поднимает серверное состояние по той же схеме, что и учётные записи.

    Один и тот же Postgres держит аккаунты и ответы с решениями, поэтому и
    судьба у них общая: база недоступна — контур честно уезжает в память с
    причиной в ``/health/ready``, а не делает вид, что история сохранилась.
    """
    if deps.settings.accounts_backend != "postgres":
        await deps.state.setup()
        return
    try:
        await deps.state.setup()
    except Exception as error:  # noqa: BLE001 — деградация вместо падения сервиса
        logger.error("Серверное состояние недоступно, уходим в память: %s", error)
        await deps.state.close()
        deps.state = InMemoryDurableState()
        deps.state_error = (
            f"Postgres недоступен, ответы, аудит и решения хранятся в памяти: {error}"
        )
    # Очередь предложений эволюции и активная политика промпта поднимаются из
    # серверного состояния: без этого рестарт процесса молча менял поведение
    # агента (принятые правки исчезали) и обнулял экспертный обзор.
    restored = await deps.state.recent_proposals()
    for proposal in restored:
        deps.evolution.restore(proposal)
    if any(proposal.status == "accepted" for proposal in restored):
        with _WORKFLOW_LOCK:
            deps.workflow = deps.build_workflow(extra_policy=deps.evolution.active_policy())


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    deps = get_dependencies()
    application.state.dependencies = deps
    await _prepare_accounts(deps)
    await _prepare_state(deps)
    try:
        if deps.settings.knowledge_backend == "neo4j":
            try:
                async with AsyncPostgresSaver.from_conn_string(
                    deps.settings.database_url
                ) as checkpointer:
                    await checkpointer.setup()
                    deps.checkpointer = checkpointer
                    # Пересборка с чекпоинтером — единственная: ленивый граф из
                    # конструктора здесь ещё не скомпилирован.
                    deps.workflow = deps.build_workflow(checkpointer=checkpointer)
                    logger.info("Checkpointer PostgreSQL подключён к агентному графу")
                    yield
                    return
            except Exception as error:  # noqa: BLE001 — деградация без чекпоинтов допустима
                logger.error("Checkpointer недоступен, работаем без него: %s", error)
        yield
    finally:
        await deps.accounts.close()
        await deps.state.close()
        # Драйверы Neo4j закрываются синхронно — уводим из event loop.
        await asyncio.to_thread(deps.close)


def _correlation_id(request: Request) -> str:
    """Идентификатор запроса для разбора инцидента (middleware гарантирует значение)."""
    return str(getattr(request.state, "correlation_id", ""))


def _reference_id(value: str) -> str:
    """Короткий необратимый ориентир вместо пользовательского текста в журнале.

    ``object_id`` в аудите обязан быть сопоставимым, но не должен становиться
    каналом утечки формулировок вопроса (и их фрагментов) в журнал и ``/audit``.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


async def _log_audit(
    request: Request,
    action: str,
    object_id: str,
    outcome: str = "success",
    *,
    actor_id: str | None = None,
) -> None:
    """Акт пишется на реальный id аккаунта из сессии, а не на client-supplied id.

    ``actor_id`` нужен событиям аутентификации: в момент регистрации/входа
    субъект в запросе ещё отсутствует, но кто именно вошёл — знать обязано.

    Журнал — серверное состояние, поэтому запись асинхронная. Сбой базы не
    отменяет уже выполненное действие пользователя: акт уходит в лог процесса,
    а не превращает успешный запрос в 500.
    """
    principal = getattr(request.state, "principal", None)
    actor = actor_id or (principal.id if principal is not None else None)
    if actor is None:
        return
    deps = dependencies(request)
    try:
        await deps.state.append_audit(
            AuditEvent(
                actor_id=actor,
                action=action,
                object_id=object_id,
                outcome=outcome,  # type: ignore[arg-type]
                correlation_id=_correlation_id(request),
            )
        )
    except Exception:  # noqa: BLE001 — журнал не должен ронять рабочий запрос
        logger.exception("Акт %s не записан в журнал аудита", action)


async def _record_decision(
    request: Request,
    action: str,
    object_id: str,
    outcome: str = "success",
    *,
    metadata: dict[str, str | int | float | bool] | None = None,
) -> None:
    """Экспертное решение — durable-запись: перезапуск процесса её не стирает.

    Отдельно от аудита: ``/api/v1/audit`` читают по праву ``audit:read``, а
    решения эксперта продукт показывает как историю его собственных действий.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return
    deps = dependencies(request)
    try:
        await deps.state.record_decision(
            ExpertDecision(
                actor_id=principal.id,
                action=action,  # type: ignore[arg-type]
                object_id=object_id,
                outcome=outcome,  # type: ignore[arg-type]
                metadata=metadata or {},
            )
        )
    except Exception:  # noqa: BLE001 — решение уже принято и сохранено в своём контуре
        logger.exception("Решение %s не записано в серверное состояние", action)


async def _notify(deps: AppDependencies, topic: NotificationTopic, message: str) -> None:
    """Уведомление в серверное состояние: лента переживает перезапуск в postgres-контуре."""
    try:
        await deps.state.append_notification(Notification(topic=topic, message=message))
    except Exception:  # noqa: BLE001 — лента не критична для основного действия
        logger.exception("Уведомление %s не записано", topic)


def require_permission(
    permission: str,
) -> Callable[[Request], Awaitable[None]]:
    """FastAPI dependency: проверяет разрешение через единый движок политики."""

    async def _check(request: Request) -> None:
        principal = getattr(request.state, "principal", None)
        if principal is None or not access_engine.has_permission(principal, permission):
            raise HTTPException(status_code=403, detail=f"Требуется разрешение: {permission}")

    return _check


def current_account(request: Request) -> Account:
    """Dependency: учётная запись владельца действующей сессии (иначе 401).

    Middleware уже не пускает защищённый путь без сессии, но auth-эндпоинты
    проверяют аккаунт явно: контракт 401 живёт в одном месте и не зависит от
    белого списка путей.
    """
    account = getattr(request.state, "account", None)
    if account is None:
        raise HTTPException(status_code=401, detail=UNAUTHENTICATED_DETAIL)
    return cast(Account, account)


CurrentAccount = Annotated[Account, Depends(current_account)]


def _allowed_classes(request: Request) -> set[DataClass]:
    """Классы данных текущего аккаунта. Конвертация из frozenset один раз.

    Отсутствующий субъект даёт пустой набор: доступ по умолчанию запрещён, а не
    приводится к «базовой роли» — именно такая подмена раньше молча выдавала
    неизвестной роли researcher-права.
    """
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        return set()
    return set(access_engine.allowed_data_classes(principal.review_enabled))


def _requires_session(path: str) -> bool:
    """Всё под ``/api/v1/`` работает только с сессией, кроме белого списка.

    Снаружи остаются ``/health/*``, ``/metrics`` и OpenAPI-инструменты: их
    читают Prometheus и документация, а данных доступа они не отдают.
    """
    if not path.startswith("/api/v1/"):
        return False
    return path not in PUBLIC_API_PATHS


def _origin_trusted(request: Request, settings: Settings) -> bool:
    """CSRF: хост из ``Origin`` (или ``Referer``, когда Origin не прислали) доверен.

    Компромисс сознательный: это проверка источника запроса, а не CSRF-токен.
    Она закрывает основной вектор — чужой сайт не отправит наш httpOnly-cookie,
    — но не различает два приложения на одном хосте и опирается на то, что
    фронтенд ходит в API через свой же origin (SvelteKit-прокси так и делает).
    Запросы без cookie от правила освобождены: подделывать там нечего
    (регистрация и вход доступны всем, вход дополнительно троттлится).
    """
    header = request.headers.get("origin") or request.headers.get("referer")
    if not header:
        return False
    candidate = header.strip().lower().rstrip("/")
    netloc = urlsplit(candidate).netloc or candidate
    return netloc in settings.trusted_origin_hosts


app = FastAPI(
    title="Научный Клубок API",
    version=__version__,
    description="Evidence-centric Agentic GraphRAG API by MindAI",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
Instrumentator(excluded_handlers=["/metrics"]).instrument(app).expose(
    app,
    include_in_schema=False,
)


@app.middleware("http")
async def access_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Серверная аутентификация: cookie-сессия → субъект, затем порог доступа.

    Заменяет эпоху ``X-User-Role``/``X-User-Id``, где личность приходила от
    клиента и была подделываемой за одну строку в curl. Здесь:

    * ``nk_session`` резолвится в учётную запись на сервере (без токена, с
      просрочкой или после смены пароля — субъекта нет);
    * всё под ``/api/v1/`` без действующей сессии — 401, в том числе SSE
      ``/query/stream``: ответ проверяется до открытия потока;
    * state-changing запрос с cookie проверяется на доверенный источник (CSRF);
    * preflight ``OPTIONS`` пропускается: cookie в нём нет, а ответ отдаёт
      CORSMiddleware, который висит внутренним слоем.

    Тонкие права (``audit:read``, ``proposal:review``, ``restricted:read``)
    по-прежнему проверяет ``require_permission`` — middleware отвечает только
    за «есть ли субъект».
    """
    deps = dependencies(request)
    # correlation_id назначается на сервере, если клиент его не принёс: иначе
    # акту в журнале не с чем сопоставить, и разбор инцидента невозможен.
    correlation_id = request.headers.get("X-Correlation-Id", "").strip() or uuid4().hex
    request.state.correlation_id = correlation_id
    token = request.cookies.get(SESSION_COOKIE)
    account = await deps.accounts.resolve_session(token) if token else None
    request.state.account = account
    request.state.principal = (
        access_engine.principal(account.id, account.review_enabled) if account else None
    )
    if request.method != "OPTIONS":
        if _requires_session(request.url.path) and account is None:
            response = JSONResponse(
                status_code=401,
                content={"detail": UNAUTHENTICATED_DETAIL},
                headers={"X-Correlation-Id": correlation_id},
            )
            if token:
                # Мёртвый токен не должен переживать разлогин в браузере.
                response.delete_cookie(key=SESSION_COOKIE, path="/")
            return response
        if (
            token
            and request.method in STATE_CHANGING_METHODS
            and request.url.path.startswith("/api/v1/")
            and not _origin_trusted(request, deps.settings)
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": CSRF_DETAIL},
                headers={"X-Correlation-Id": correlation_id},
            )
    response = await call_next(request)
    # Идентификатор доезжает до клиента и на обычных ответах, и на потоках:
    # заголовок есть у каждого обращения, включая SSE, где тело может и не
    # дойти до события с идентификатором.
    response.headers["X-Correlation-Id"] = correlation_id
    return response


@app.exception_handler(ModelUnavailableError)
async def model_unavailable(_: Request, error: ModelUnavailableError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(error)})


@app.exception_handler(OntologyValidationError)
async def ontology_rejected(request: Request, error: OntologyValidationError) -> JSONResponse:
    """Извлечение не сошлось со словарём отношений даже после повторной сборки.

    Клиенту — что документ не импортирован и что с этим делать. Перечень
    нарушений остаётся в логе под ``correlation_id``: он адресован модели, а не
    аналитику, и без этого в интерфейсе появилась бы простыня служебных имён.
    До обработчика отказ уходил как 500 без объяснения — молчаливый провал
    пользовательского действия.
    """
    logger.warning(
        "Импорт отклонён проверкой онтологии (correlation_id=%s): %s",
        _correlation_id(request) or "—",
        error,
    )
    return JSONResponse(
        status_code=503,
        content={
            "detail": (
                "Документ не импортирован: модель дважды вернула отношения вне словаря "
                "графа. Повторите импорт, а если повторится — загрузите меньший фрагмент."
            )
        },
    )


@app.exception_handler(ExportError)
async def export_failed(_: Request, error: ExportError) -> JSONResponse:
    """Выгрузка не собралась: файл клиенту не отдаём и молча формата не меняем.

    Причина — на стороне серверного контура (шрифт без кириллицы, битый PDF),
    поэтому 503 вместо «пустой, но успешный» ответа.
    """
    return JSONResponse(status_code=503, content={"detail": str(error)})


@app.exception_handler(WorkflowNodeError)
async def workflow_node_failed(_: Request, error: WorkflowNodeError) -> JSONResponse:
    # Клиенту показывается имя узла и класс ошибки; подробности — в логах.
    return JSONResponse(
        status_code=502,
        content={"detail": f"Узел {error.node} не выполнен ({error.detail})"},
    )


@app.exception_handler(AdmissionRefusedError)
async def agent_run_refused(_: Request, error: AdmissionRefusedError) -> JSONResponse:
    """Сервис занят: слотов нет, и ждать их нечего — 429 с честным Retry-After.

    Второй механизм троттлинга здесь не появляется: вход по паролю ограничивает
    перебор учёток, этот — пропускную способность агентного контура.
    """
    return JSONResponse(
        status_code=429,
        content={
            "detail": str(error),
            "active": error.active,
            "limit": error.limit,
            "retry_after": error.retry_after,
        },
        headers={"Retry-After": str(error.retry_after)},
    )


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"], response_model=SystemStatus)
async def readiness(request: Request) -> SystemStatus:
    deps = dependencies(request)
    model_mode = deps.provider.mode
    neo4j_backend = deps.settings.knowledge_backend == "neo4j"
    postgres_accounts = deps.accounts.kind == "postgres"
    postgres_state = deps.state.kind == "postgres"
    services: dict[str, ServiceState] = {
        "knowledge_graph": "configured" if neo4j_backend else "fallback",
        "hybrid_search": "configured" if neo4j_backend else "fallback",
        "model_provider": "configured" if model_mode == "gigachat" else "disabled",
        "evaluation": "ready",
        "checkpointer": "configured" if deps.checkpointer is not None else "disabled",
        # "fallback" = Postgres был недоступен на старте, сессии живут в памяти.
        "accounts": "configured" if postgres_accounts else "fallback",
        # Тот же смысл для серверного состояния: на памяти ответы, аудит,
        # экспертные решения, прогоны и лента уведомлений обнуляются перезапуском.
        "server_state": "configured" if postgres_state else "fallback",
    }
    reasons = [reason for reason in (deps.accounts_error, deps.state_error) if reason]
    return SystemStatus(
        status="ready" if model_mode == "gigachat" else "degraded",
        model_mode=model_mode,
        services=services,
        accounts=deps.accounts.kind,
        state_backend=deps.state.kind,
        degradation_reasons=reasons,
        agent_runs_limit=deps.admission.limit,
        agent_runs_active=deps.admission.active,
    )


# ── Аутентификация и профиль доступа ────────────────────────────────────────


def _ensure_password_length(password: str, settings: Settings) -> None:
    """Политика длины пароля живёт в настройке, а не в схеме запроса."""
    if len(password) < settings.password_min_length:
        raise HTTPException(
            status_code=422,
            detail=f"Пароль короче минимальных {settings.password_min_length} символов",
        )


def _account_info(account: Account) -> AccountInfo:
    """Профиль + фактические возможности: права считает таблица политик."""
    return AccountInfo(
        id=account.id,
        email=account.email,
        display_name=account.display_name,
        review_enabled=account.review_enabled,
        created_at=account.created_at,
        capabilities=access_engine.capabilities(account.review_enabled),
        data_classes=access_engine.data_class_names(account.review_enabled),
    )


def _issue_session(request: Request, response: Response, token: str) -> None:
    """Cookie сессии: httpOnly всегда, secure — только в production.

    Иначе локальный контур на http://localhost:13000 (SvelteKit-прокси) не
    сохранит сессию, и вход станет невозможным «без видимой причины».
    """
    settings = dependencies(request).settings
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=settings.session_ttl_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        path="/",
        secure=settings.cookie_secure,
    )


def _throttle_key(email: str, request: Request) -> str:
    """Ключ троттлинга: аккаунт + клиентский хост.

    Только email — позволял бы одним неверным паролем заблокировать вход
    владельцу; только хост — обходился бы сменой адреса. ``request.client.host``
    за прокси указывает на прокси: для рабочего контура нужен
    ``ProxyHeadersMiddleware`` (или ``uvicorn --proxy-headers``), иначе
    ограничитель фактически работает «по одному email на прокси».
    """
    client_host = request.client.host if request.client else "unknown"
    return f"{normalize_email(email)}|{client_host}"


@app.post(
    "/api/v1/auth/register",
    tags=["auth"],
    status_code=201,
    response_model=AccountInfo,
)
async def register_account(
    payload: AccountRegisterRequest,
    request: Request,
    response: Response,
) -> AccountInfo:
    """Создаёт аккаунт и сразу входит в него (201 + ``Set-Cookie: nk_session``).

    Саморегистрация открытая, поэтому 409 сообщает ровно «email уже
    зарегистрирован»: для продукта с открытой регистрацией сам факт занятости
    адреса не является тайной, а имени, прав и дат мы не раскрываем. Новый
    аккаунт всегда базового уровня — экспертный доступ выдают только SQL.
    """
    deps = dependencies(request)
    _ensure_password_length(payload.password, deps.settings)
    try:
        account = await deps.accounts.create_user(
            email=payload.email,
            display_name=payload.display_name,
            password=payload.password,
        )
    except EmailAlreadyRegisteredError as error:
        raise HTTPException(status_code=409, detail="Email уже зарегистрирован") from error
    token = await deps.accounts.create_session(account.id)
    _issue_session(request, response, token)
    await _log_audit(request, "auth.register", account.id, actor_id=account.id)
    return _account_info(account)


@app.post("/api/v1/auth/login", tags=["auth"], response_model=AccountInfo)
async def login_account(
    payload: AccountLoginRequest,
    request: Request,
    response: Response,
) -> AccountInfo:
    """Вход по email/паролю: 200 + ``Set-Cookie: nk_session``.

    401 с одинаковым текстом и для неизвестного email, и для неверного пароля —
    перечислить аккаунты по ответу нельзя (и время ответа сравнивается
    холостым хэшированием в адаптере). Неудачи считает троттлинг в процессе:
    после ``login_max_attempts`` за ``login_window_seconds`` на пару
    (email, client host) — 429 с ``Retry-After``. Неудачные входы уходят в
    лог процесса, а не в audit-журнал: в нём не должно быть чужих email.
    """
    deps = dependencies(request)
    key = _throttle_key(payload.email, request)
    retry_after = deps.login_limiter.retry_after(key)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Слишком много неудачных попыток входа. Повторите позже.",
            headers={"Retry-After": str(retry_after)},
        )
    account = await deps.accounts.authenticate(payload.email, payload.password)
    if account is None:
        deps.login_limiter.record_failure(key)
        logger.warning("Неудачная попытка входа: %s", key)
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    deps.login_limiter.reset(key)
    token = await deps.accounts.create_session(account.id)
    _issue_session(request, response, token)
    await _log_audit(request, "auth.login", account.id, actor_id=account.id)
    return _account_info(account)


@app.post("/api/v1/auth/logout", tags=["auth"], response_model=dict[str, str])
async def logout_account(
    request: Request,
    response: Response,
    account: CurrentAccount,
) -> dict[str, str]:
    """Гасит сессию на сервере и стирает cookie (иначе токен жил бы до TTL)."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await dependencies(request).accounts.drop_session(token)
    response.delete_cookie(key=SESSION_COOKIE, path="/")
    await _log_audit(request, "auth.logout", account.id, actor_id=account.id)
    return {"status": "logged_out"}


@app.get("/api/v1/auth/me", tags=["auth"], response_model=AccountInfo)
async def read_current_account(account: CurrentAccount) -> AccountInfo:
    """Профиль владельца сессии и его возможности — единственный источник для UI.

    Заменён прежний ``/api/v1/principal`` с role-заголовками: здесь и id, и
    email, и ``capabilities``/``data_classes``, посчитанные политикой.
    """
    return _account_info(account)


@app.post("/api/v1/auth/password", tags=["auth"], response_model=AccountInfo)
async def change_account_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    account: CurrentAccount,
) -> AccountInfo:
    """Меняет пароль, убирает прочие сессии и перевыпускает cookie.

    Текущий сеанс тоже ротируется (новый токен вместо старого), поэтому
    украденный ранее токен перестает работать вместе со всеми остальными.
    Неверный ``current_password`` — 403, а не 401: сессия действующая,
    отклонено конкретное действие. Подбор текущего пароля ограничен тем же
    троттлингом, что и вход (иначе cookie давал бы бесконечный оракул).
    """
    deps = dependencies(request)
    _ensure_password_length(payload.new_password, deps.settings)
    key = _throttle_key(account.email, request)
    retry_after = deps.login_limiter.retry_after(key)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Слишком много неудачных попыток смены пароля. Повторите позже.",
            headers={"Retry-After": str(retry_after)},
        )
    verified = await deps.accounts.authenticate(account.email, payload.current_password)
    if verified is None or verified.id != account.id:
        deps.login_limiter.record_failure(key)
        raise HTTPException(status_code=403, detail="Текущий пароль указан неверно")
    deps.login_limiter.reset(key)
    updated = await deps.accounts.set_password(account.id, payload.new_password)
    token = await deps.accounts.create_session(account.id)
    await deps.accounts.drop_other_sessions(account.id, token)
    _issue_session(request, response, token)
    await _log_audit(request, "auth.password", account.id, actor_id=account.id)
    return _account_info(updated)


@app.patch("/api/v1/auth/profile", tags=["auth"], response_model=AccountInfo)
async def update_account_profile(
    payload: ProfileUpdateRequest,
    request: Request,
    account: CurrentAccount,
) -> AccountInfo:
    """Только отображаемое имя: email, права и id профиля не редактируются."""
    display_name = payload.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="Отображаемое имя не может быть пустым")
    updated = await dependencies(request).accounts.rename(account.id, display_name)
    await _log_audit(request, "auth.profile", account.id, actor_id=account.id)
    return _account_info(updated)


@app.post("/api/v1/queries/validate", tags=["queries"])
async def validate_query_plan(plan: QueryPlan) -> QueryPlan:
    """Проверяет типизированный план до обращения к retrieval-контуру."""
    return plan


async def _finalize_answer(
    request: Request,
    deps: AppDependencies,
    answer: AnswerPayload,
    allowed: set[DataClass],
    account: Account,
    *,
    audit_action: str,
) -> tuple[AnswerPayload, EvaluationRun]:
    """Один финал для JSON- и SSE-пути: ACL, серверная копия, оценка, акт.

    Порядок принципиален: ACL применяется до того, как ответ лёг в
    ``nk_answers`` — экспорт перечитывает именно серверную копию, и закрытый
    текст не должен попадать в неё для аккаунта без права на restricted.
    """
    filtered = access_engine.apply_acl(answer, allowed)
    stored = await deps.state.put_answer(filtered, owner_id=account.id)
    evaluation = deps.harness.evaluate(filtered)
    await deps.state.record_evaluation(evaluation)
    await _log_audit(request, audit_action, str(stored.query_id))
    return filtered, evaluation


@app.post("/api/v1/query", tags=["queries"], response_model=QueryResponse)
async def research_query(
    query: QueryRequest,
    request: Request,
    account: CurrentAccount,
) -> QueryResponse:
    """Полный прогон исследования: слот приёма берётся до запуска графа.

    Отказ (429) возвращается сразу и ничего не ждёт: ожидание чужого
    исследования съело бы ``AGENT_DEADLINE_SECONDS`` этого запроса, и вместо
    «сервис занят» клиент получил бы неполный ответ с ``degradation_reasons``.
    """
    deps = dependencies(request)
    allowed = _allowed_classes(request)
    handle = deps.admission.acquire()
    try:
        answer = await workflow_for(request).run(
            _thread_scoped(account, query), allowed_data_classes=allowed
        )
        filtered, evaluation = await _finalize_answer(
            request, deps, answer, allowed, account, audit_action="query.run"
        )
    finally:
        handle.release()
    return QueryResponse(
        answer=filtered,
        evaluation=evaluation,
        correlation_id=_correlation_id(request),
    )


# Демо-вопрос зафиксирован: витрина не выбирает тему на каждом обращении.
DEMO_QUESTION = (
    "Какие методы обессоливания подходят для шахтной воды с сульфатами и "
    "хлоридами 200–300 мг/л при сухом остатке ≤1000 мг/л?"
)


@app.get("/api/v1/demo", tags=["queries"], response_model=QueryResponse)
async def demo_query(request: Request, account: CurrentAccount) -> QueryResponse:
    """Витрина на зафиксированном вопросе: прогон модели — один, не на обращение.

    Ответ настоящий (тот же рабочий процесс, тот же корпус), но кэшируется в
    процессе после первого успешного прогона: каждый гость не сжигает
    ``AGENT_DEADLINE_SECONDS`` и бюджет LLM. Цена кэша честная и объявлена:
    витрина собирается в контуре доступа первого зрителя, поэтому аккаунт с
    более широкими правами видит отметку в ``degradation_reasons`` — restricted
    доказательств в этом ответе просто нет, задним числом они не подтягиваются.
    """
    deps = dependencies(request)
    allowed = _allowed_classes(request)
    # Витрина собирается один раз на процесс и в том контуре доступа, в котором
    # её впервые запросили: более широкий зритель не получает restricted-текст
    # задним числом (он не был ни в промпте, ни в серверной копии), но обязан
    # видеть, что ответ собран не для его прав.
    access: set[DataClass] = set(deps.demo_access or ())
    if deps.demo_answer is None or deps.demo_evaluation is None:
        # Замок только против одновременного холодного старта: иначе две первые
        # витрины сожгли бы по полному бюджету на один и тот же вопрос.
        async with _DEMO_LOCK:
            if deps.demo_answer is None or deps.demo_evaluation is None:
                handle = deps.admission.acquire()
                try:
                    answer = await workflow_for(request).run(
                        QueryRequest(question=DEMO_QUESTION), allowed_data_classes=allowed
                    )
                finally:
                    handle.release()
                evaluation = deps.harness.evaluate(answer)
                # Прогон оценки — тоже серверное состояние: на памяти процесса
                # он обнулялся перезапуском вместе с остальными журналами.
                await deps.state.record_evaluation(evaluation)
                deps.demo_answer = answer
                deps.demo_evaluation = evaluation
                deps.demo_access = allowed
                access = allowed
    answer, evaluation = deps.demo_answer, deps.demo_evaluation
    # Серверная копия заводится на каждого зрителя: экспорт работает по query_id
    # владельца сессии, а не по чужому кэшу.
    filtered = access_engine.apply_acl(answer, allowed)
    if not allowed <= access:
        filtered = filtered.model_copy(
            update={
                "degradation_reasons": [
                    *filtered.degradation_reasons,
                    "Витрина собрана в более узком контуре доступа, чем права аккаунта.",
                ]
            }
        )
    stored = await deps.state.put_answer(filtered, owner_id=account.id)
    await _log_audit(request, "demo.run", str(stored.query_id))
    return QueryResponse(
        answer=filtered,
        evaluation=evaluation,
        correlation_id=_correlation_id(request),
    )


def _thread_scoped(account: Account, query: QueryRequest) -> QueryRequest:
    """Делает ветку чекпоинтера собственностью аккаунта.

    ``thread_id`` приходит от клиента, а ветка хранит сжатый след прогона, который
    попадает в промпт следующего. Без привязки к субъекту два аккаунта, назвавшие
    один идентификатор, получили бы общую историю — вывод чужого исследования в
    своём ответе. Клиент по-прежнему передаёт свой id: непрерывность follow-up'ов
    сохраняется, наружу уходит только производный ключ.
    """
    scoped = uuid5(NAMESPACE_URL, f"nk-thread:{account.id}:{query.thread_id}")
    return query.model_copy(update={"thread_id": scoped})


def _sse(payload: dict[str, object]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@app.post("/api/v1/query/stream", tags=["queries"])
async def stream_query(
    query: QueryRequest,
    request: Request,
    account: CurrentAccount,
) -> StreamingResponse:
    """SSE поверх того же прогона рабочего процесса, что и JSON-эндпоинт.

    Обновления отдаёт ResearchWorkflow.stream: входное состояние, бюджет и ACL
    больше не дублируются в обработчике (и не расходятся между двумя путями).

    Слот приёма берётся до открытия потока — иначе 429 нельзя было бы отдать
    статусом ответа, и клиент получил бы «успешный» SSE с ошибкой внутри.
    Освобождение идемпотентно и продублировано в ``background``: генератор может
    не стартовать при обрыве соединения, и слот не обязан оставаться занятым.

    ``correlation_id`` едет в каждом событии: поток обрывается чаще, чем JSON,
    и разбирать инцидент приходится именно по тому, что дошло до клиента.
    """
    allowed = _allowed_classes(request)
    deps = dependencies(request)
    handle = deps.admission.acquire()
    correlation_id = _correlation_id(request)

    async def event_generator() -> AsyncIterator[str]:
        try:
            yield _sse(
                {
                    "type": "start",
                    "correlation_id": correlation_id,
                    "question": query.question,
                }
            )
            answer = None
            try:
                async for node_name, update in workflow_for(request).stream(
                    _thread_scoped(account, query), allowed
                ):
                    for event in update.get("trace", []):
                        yield _sse(
                            {
                                "type": "step",
                                "correlation_id": correlation_id,
                                "step": {
                                    "agent": getattr(event, "agent", node_name),
                                    "status": getattr(event, "status", "completed"),
                                    "message": getattr(event, "message", ""),
                                    "duration_ms": getattr(event, "duration_ms", 0),
                                },
                            }
                        )
                    candidate = update.get("answer")
                    if candidate is not None:
                        answer = candidate
            except ModelUnavailableError as error:
                yield _sse(
                    {
                        "type": "error",
                        "correlation_id": correlation_id,
                        "code": "model_unavailable",
                        "message": str(error),
                    }
                )
                return
            except Exception:  # noqa: BLE001 - детали только в логах
                logger.exception("SSE stream failed")
                yield _sse(
                    {
                        "type": "error",
                        "correlation_id": correlation_id,
                        "code": "workflow_failed",
                        "message": "Внутренняя ошибка рабочего процесса",
                    }
                )
                return
            if answer is None:
                yield _sse(
                    {
                        "type": "error",
                        "correlation_id": correlation_id,
                        "code": "no_answer",
                        "message": "Ответ не сформирован",
                    }
                )
                return
            filtered, _ = await _finalize_answer(
                request, deps, answer, allowed, account, audit_action="query.stream"
            )
            yield _sse(
                {
                    "type": "answer",
                    "correlation_id": correlation_id,
                    "answer": filtered.model_dump(mode="json"),
                }
            )
            yield _sse({"type": "done", "correlation_id": correlation_id})
        finally:
            handle.release()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(handle.release),
    )


@app.post("/api/v1/documents", tags=["ingestion"], response_model=DocumentReceipt)
async def ingest_document(
    document: DocumentRequest,
    request: Request,
    _: None = Depends(require_permission("knowledge:read")),
) -> DocumentReceipt:
    receipt = await dependencies(request).ingestion.ingest(document)
    await _log_audit(request, "document.ingest", str(receipt.document_id))
    return receipt


@app.post("/api/v1/documents/upload", tags=["ingestion"], response_model=DocumentReceipt)
async def upload_document(
    request: Request,
    file: Annotated[UploadFile, File()],
    language: Annotated[Literal["ru", "en"], Form()] = "ru",
    geography: Annotated[str | None, Form()] = None,
    year: Annotated[int | None, Form()] = None,
    data_class: Annotated[Literal["public", "internal", "restricted"], Form()] = "public",
    _: None = Depends(require_permission("knowledge:read")),
) -> DocumentReceipt:
    content = await file.read()
    try:
        # Разбор и OCR — блокирующие (PyMuPDF, pytesseract, openpyxl): минуты на
        # скане не должны занимать event loop всего процесса.
        document = await asyncio.to_thread(
            parse_document,
            file.filename or "document.txt",
            content,
            language=language,
            geography=geography,
            year=year,
        )
    except (UnsupportedDocumentError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    restricted_upload = document.data_class != DataClass.PUBLIC or data_class != "public"
    # Запись в закрытый класс требует права на действие, а не права на чтение:
    # ``restricted:read`` отвечает за доступ к данным, и выдавать его аналитику
    # означало бы молча разрешить ему перезапись закрытого контура.
    if restricted_upload and not access_engine.can_write(
        request.state.principal, "document.write_restricted"
    ):
        raise HTTPException(
            status_code=403,
            detail="Загрузка в restricted требует экспертного права на запись",
        )
    document = document.model_copy(update={"data_class": DataClass(data_class)})
    return await ingest_document(document, request)


@app.get("/api/v1/graph", tags=["knowledge"], response_model=GraphSnapshot)
async def get_graph(request: Request) -> GraphSnapshot:
    """Граф отдаётся уже отфильтрованным по классам данных аккаунта."""
    deps = dependencies(request)
    return await asyncio.to_thread(deps.knowledge.full_graph, _allowed_classes(request))


@app.get("/api/v1/findings", tags=["knowledge"])
async def get_findings(
    request: Request,
    subject: str | None = None,
    status: str | None = None,
) -> list[dict[str, object]]:
    """Возвращает находки с доказательствами и наблюдениями."""
    deps = dependencies(request)
    findings = await asyncio.to_thread(deps.knowledge.all_findings, _allowed_classes(request))
    if subject:
        findings = [f for f in findings if subject.lower() in (f.subject or "").lower()]
    if status:
        findings = [f for f in findings if f.status == status]
    return [finding.model_dump(mode="json") for finding in findings]


@app.get("/api/v1/conflicts", tags=["knowledge"])
async def get_conflicts(request: Request) -> list[dict[str, object]]:
    """Конфликтные находки (status=disputed) в пределах прав аккаунта."""
    deps = dependencies(request)
    findings = await asyncio.to_thread(deps.knowledge.all_findings, _allowed_classes(request))
    return [f.model_dump(mode="json") for f in findings if f.status == "disputed"]


@app.get("/api/v1/corpus/stats", tags=["knowledge"], response_model=CorpusStats)
async def get_corpus_stats(request: Request) -> CorpusStats:
    deps = dependencies(request)
    return await asyncio.to_thread(deps.knowledge.corpus_stats)


@app.get(
    "/api/v1/entity-resolution/proposals",
    tags=["knowledge"],
    response_model=list[EntityMergeProposal],
)
async def get_entity_resolution_proposals(
    request: Request,
    _: None = Depends(require_permission("proposal:review")),
) -> list[EntityMergeProposal]:
    """Мерж-предложения — вход в экспертный обзор: в ``rationale`` бывают ссылки на
    закрытые источники, поэтому список отдаётся не всем подряд."""
    deps = dependencies(request)
    return await asyncio.to_thread(deps.resolution.list_proposals)


@app.post(
    "/api/v1/entity-resolution/proposals/{proposal_id}/review",
    tags=["knowledge"],
    response_model=EntityMergeProposal,
)
async def review_entity_resolution_proposal(
    proposal_id: UUID,
    request: Request,
    review: MergeReviewRequest,
    account: CurrentAccount,
    _: None = Depends(require_permission("proposal:review")),
) -> EntityMergeProposal:
    """Принятие склейки: мерж по ``id`` сущностей, решение эксперта — durable.

    Идемпотентность держит сервис склеек: повторный ``accept`` того же
    предложения не плодит рёбра ``ALIAS_OF`` и не меняет состояние дважды.
    """
    deps = dependencies(request)
    try:
        proposal = await asyncio.to_thread(
            deps.resolution.review, proposal_id, review.action, reviewer_id=account.id
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Merge proposal not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await _record_decision(
        request,
        "resolution.reviewed",
        str(proposal_id),
        metadata={"action": review.action, "status": proposal.status},
    )
    await _log_audit(request, "resolution.review", str(proposal_id), review.action)
    return proposal


@app.get("/api/v1/evaluations", tags=["evaluation"], response_model=list[EvaluationRun])
async def get_evaluations(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LIMIT_MAX)] = AUDIT_LIMIT_DEFAULT,
    _: None = Depends(require_permission("evaluation:view")),
) -> list[EvaluationRun]:
    """Прогоны оценки из серверного состояния: история качества не обнуляется
    перезапуском процесса, иначе «самооценка платформы» не подтверждается ничем."""
    deps = dependencies(request)
    return await deps.state.recent_evaluations(limit=limit)


@app.get("/api/v1/agents/metrics", tags=["evaluation"], response_model=AgentMetricsResponse)
async def get_agent_metrics(request: Request) -> AgentMetricsResponse:
    return dependencies(request).metrics.snapshot()


@app.get("/api/v1/evaluations/gold", tags=["evaluation"], response_model=list[GoldCase])
async def get_gold_cases() -> list[GoldCase]:
    return EvaluationHarness.gold_cases()


@app.post(
    "/api/v1/evaluations/retrieval-benchmark",
    tags=["evaluation"],
    response_model=RetrievalBenchmark,
)
async def run_retrieval_benchmark(
    request: Request,
    _: None = Depends(require_permission("evaluation:view")),
) -> RetrievalBenchmark:
    deps = dependencies(request)
    # Бенчмарк прогоняет ранжирование по всему корпусу синхронно.
    return await asyncio.to_thread(deps.harness.benchmark_retrieval, deps.knowledge)


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
    deps = dependencies(request)
    stats = await asyncio.to_thread(deps.knowledge.corpus_stats)
    if stats.semantic_documents < max_cases:
        raise HTTPException(
            status_code=409,
            detail="Недостаточно semantic_extracted документов. Сначала выполните preload.",
        )
    handle = deps.admission.acquire()
    try:
        return await deps.harness.benchmark_pipeline(
            workflow_for(request), deps.knowledge, max_cases
        )
    finally:
        handle.release()


@app.post(
    "/api/v1/proposals/{proposal_id}/experiment",
    tags=["evolution"],
    response_model=EvolutionExperiment,
)
async def run_evolution_experiment(
    proposal_id: UUID,
    request: Request,
    account: CurrentAccount,
    max_cases: Annotated[int, Query(ge=1, le=3)] = 1,
    _: None = Depends(require_permission("proposal:review")),
) -> EvolutionExperiment:
    """A/B-прогон: один эксперимент — одна сборка кандидата, результат в серверном состоянии.

    Право ``proposal:review``, а не ``evaluation:view``: запуск меняет активную
    политику агента после промоушена, и это экспертное действие, а не чтение метрик.
    """
    deps = dependencies(request)
    proposal = next(
        (item for item in await deps.state.recent_proposals() if item.id == proposal_id),
        None,
    )
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found") from None
    if proposal.kind not in {"prompt", "rule"}:
        raise HTTPException(
            status_code=422,
            detail="A/B workflow experiment поддерживает prompt и rule proposals.",
        )
    cases = EvaluationHarness.gold_cases()[:max_cases]
    handle = deps.admission.acquire()
    try:
        baseline, baseline_results = await deps.harness.evaluate_workflow(
            workflow_for(request), cases
        )
        # Кандидат собирается один раз: прежняя сборка совпадала с набором
        # аргументов lifespan и расходилась с ней, если чекпоинтер менялся.
        candidate_workflow = deps.build_workflow(extra_policy=proposal.change)
        candidate, candidate_results = await deps.harness.evaluate_workflow(
            candidate_workflow, cases
        )
    finally:
        handle.release()
    passed_baseline = {result.case_id for result in baseline_results if result.passed}
    passed_candidate = {result.case_id for result in candidate_results if result.passed}
    regressions = sorted(passed_baseline - passed_candidate)
    promote = (
        not regressions
        and candidate.pass_rate >= baseline.pass_rate
        and candidate.source_recall >= baseline.source_recall
        and candidate.citation_coverage >= baseline.citation_coverage
        and candidate.average_latency_ms <= baseline.average_latency_ms * 1.25
    )
    experiment = EvolutionExperiment(
        proposal_id=proposal_id,
        cases=len(cases),
        baseline=baseline,
        candidate=candidate,
        delta_pass_rate=round(candidate.pass_rate - baseline.pass_rate, 3),
        regressions=regressions,
        decision="promote" if promote else "reject",
    )
    await deps.state.record_experiment(experiment)
    await _record_decision(
        request,
        "proposal.reviewed",
        str(proposal_id),
        metadata={"experiment_id": str(experiment.id), "decision": experiment.decision},
    )
    await _log_audit(request, "experiment.run", str(proposal_id), outcome="success")
    return experiment


@app.get(
    "/api/v1/experiments",
    tags=["evolution"],
    response_model=list[EvolutionExperiment],
)
async def get_evolution_experiments(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    _: None = Depends(require_permission("proposal:review")),
) -> list[EvolutionExperiment]:
    """Решения по предложениям и A/B-история — экспертный контур, и они durable."""
    deps = dependencies(request)
    return await deps.state.recent_experiments(limit=limit)


@app.post("/api/v1/feedback", tags=["evolution"], response_model=FeedbackResult)
async def submit_feedback(
    feedback: FeedbackRequest,
    request: Request,
    account: CurrentAccount,
) -> FeedbackResult:
    """Сначала сохраняется решение эксперта, потом — proposal от LLM.

    Порядок важен: замена утверждения — детерминированная запись, а proposal
    требует живую модель. Раньше при недоступном GigaChat эндпоинт падал на
    `503` до записи, и экспертное исправление терялось.
    """
    deps = dependencies(request)
    principal = request.state.principal
    superseded: Finding | None = None
    degradation: list[str] = []
    if feedback.verdict == "correct" and feedback.finding_id and feedback.correction:
        # Право на чтение restricted не разрешает перезаписывать утверждение:
        # запись идёт по экспертному праву на действие (см. governance.WRITE_ACTIONS).
        if not access_engine.can_write(principal, "claim.supersede"):
            raise HTTPException(
                status_code=403,
                detail="Экспертная замена утверждения требует права на экспертные действия",
            )
        try:
            # Перезапись графа — блокирующий вызов драйвера Neo4j.
            superseded = await asyncio.to_thread(
                deps.knowledge.supersede_finding,
                feedback.finding_id,
                feedback.correction,
                0.95,
                reviewer_id=principal.id,
                review_date=datetime.now(UTC).isoformat(),
                review_reason=feedback.comment,
            )
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        await _notify(
            deps,
            "claim.superseded",
            f"Утверждение {superseded.id} заменено исправленной версией.",
        )
        await _record_decision(
            request,
            "claim.superseded",
            superseded.id,
            metadata={"query_id": str(feedback.query_id), "version": superseded.version},
        )
        await _log_audit(request, "feedback.supersede", superseded.id)
    try:
        proposal = await deps.evolution.propose(feedback)
    except ModelUnavailableError as error:
        proposal = None
        degradation.append(f"Self-Evolve proposal не сформирован: {error}")
    if proposal is not None:
        await deps.state.record_proposal(proposal)
        await _record_decision(
            request,
            "proposal.created",
            str(proposal.id),
            metadata={"kind": proposal.kind, "query_id": str(feedback.query_id)},
        )
    await _log_audit(
        request, "feedback.submit", str(proposal.id if proposal else feedback.query_id)
    )
    return FeedbackResult(
        proposal=proposal, superseded=superseded, degradation_reasons=degradation
    )


@app.get(
    "/api/v1/proposals",
    tags=["evolution"],
    response_model=list[EvolutionProposal],
)
async def get_proposals(
    request: Request,
    _: None = Depends(require_permission("proposal:review")),
) -> list[EvolutionProposal]:
    """Список предложений — экспертный контур: в ``change`` живёт текст будущей
    политики агента, и отдавать его всем подряд нельзя (право на обзор —
    ``proposal:review``)."""
    return await dependencies(request).state.recent_proposals()


async def _ab_gate_passed(deps: AppDependencies, proposal_id: UUID) -> bool:
    """Пройдено ли regression A/B для предложения — по серверному состоянию.

    Прежняя проверка читала словарь `EvolutionService`, куда эксперимент больше
    не записывается (результат A/B живёт в `nk_experiments`), и принятие
    prompt/rule-предложения было невозможно в принципе.
    """
    experiments = await deps.state.recent_experiments()
    return any(
        item.proposal_id == proposal_id and item.decision == "promote"
        for item in experiments
    )


@app.post(
    "/api/v1/proposals/{proposal_id}/review",
    tags=["evolution"],
    response_model=EvolutionProposal,
)
async def review_proposal(
    proposal_id: UUID,
    request: Request,
    review_request: ProposalReviewRequest,
    _: None = Depends(require_permission("proposal:review")),
) -> EvolutionProposal:
    deps = dependencies(request)
    # Порядок проверок важен: не найденное предложение обязано остаться 404,
    # а не превращаться в 409 «ворота не пройдены». Источник истины — серверное
    # состояние: зеркало сервиса переживает только текущий процесс.
    known = await deps.state.recent_proposals()
    if not any(item.id == proposal_id for item in known):
        raise HTTPException(status_code=404, detail="Proposal not found")
    if review_request.accepted and not await _ab_gate_passed(deps, proposal_id):
        raise HTTPException(status_code=409, detail="Proposal не прошёл regression A/B gate")
    reviewed = deps.evolution.review(proposal_id, review_request.accepted)
    await deps.state.record_proposal(reviewed)
    if reviewed.status == "accepted":
        await _notify(
            deps,
            "proposal.accepted",
            f"Предложение {proposal_id} принято и активировано.",
        )
        # Переключение политики перенесено в lock приложения: гонка двух
        # одновременных review оставляла рабочий процесс в неопределённом виде.
        with _WORKFLOW_LOCK:
            deps.workflow = deps.build_workflow(extra_policy=deps.evolution.active_policy())
    await _record_decision(
        request,
        "proposal.reviewed",
        str(proposal_id),
        "success" if reviewed.status == "accepted" else "denied",
        metadata={"kind": reviewed.kind, "status": reviewed.status},
    )
    await _log_audit(
        request,
        "proposal.review",
        str(proposal_id),
        "success" if reviewed.status == "accepted" else "denied",
    )
    return reviewed


@app.get("/api/v1/audit", tags=["acl"], response_model=list[AuditEvent])
async def get_audit_log(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LIMIT_MAX)] = AUDIT_LIMIT_DEFAULT,
    correlation_id: str | None = None,
    _: None = Depends(require_permission("audit:read")),
) -> list[AuditEvent]:
    """Журнал из серверного состояния: с фильтром по ``correlation_id`` — тем
    самым, что вернулся клиенту в ответе и в заголовке ``X-Correlation-Id``."""
    deps = dependencies(request)
    return await deps.state.recent_audit(limit=limit, correlation_id=correlation_id)


@app.get(
    "/api/v1/decisions",
    tags=["acl"],
    response_model=list[ExpertDecision],
)
async def get_expert_decisions(
    request: Request,
    account: CurrentAccount,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LIMIT_MAX)] = AUDIT_LIMIT_DEFAULT,
) -> list[ExpertDecision]:
    """История решений собственного аккаунта — из серверного состояния.

    Выдача без фильтра по ``actor_id`` показала бы чужие решения и чужие
    ``query_id``; журнал же всех экспертов закрыт правом ``audit:read`` через
    ``/audit``. Пока read-пути не было, запись решений существовала только как
    обещание «история действий сохранена», которое нечем было проверить.
    """
    deps = dependencies(request)
    return await deps.state.recent_decisions(limit=limit, actor_id=account.id)


@app.post("/api/v1/compare", tags=["comparison"], response_model=ComparisonTable)
async def compare_entities(
    comparison: ComparisonRequest,
    request: Request,
    _: None = Depends(require_permission("export:run")),
) -> ComparisonTable:
    deps = dependencies(request)
    # Чтение корпуса — блокирующий (Neo4j/ES), из event loop убран.
    findings = await asyncio.to_thread(deps.knowledge.all_findings, _allowed_classes(request))
    # В журнал уходит необратимый ориентир, а не фрагмент вопроса: ``/audit``
    # читают не только авторы сравнения.
    await _log_audit(request, "compare.run", _reference_id(comparison.question))
    return deps.comparison.compare(findings, comparison)


@app.post("/api/v1/export", tags=["export"])
async def export_answer(
    export_req: ExportRequest,
    request: Request,
    account: CurrentAccount,
    _: None = Depends(require_permission("export:run")),
) -> Response:
    """Экспортируется серверная копия ответа, а не то, что прислал клиент.

    Пока ответ приходил в теле запроса, ACL фильтровал присланные данные:
    достаточно было переклеить ``data_class: restricted → public`` в JSON, и
    закрытый текст уезжал файлом. Источник истины теперь ``nk_answers``:
    чужой или истёкший ответ — ``404`` (существование чужого запроса не
    раскрываем), нехватка класса данных — ``403``.
    """
    deps = dependencies(request)
    stored: StoredAnswer | None = await deps.state.get_answer(str(export_req.query_id))
    if stored is None or stored.owner_id != account.id:
        raise HTTPException(status_code=404, detail="Ответ не найден или срок хранения истёк")
    allowed = _allowed_classes(request)
    if not stored.data_classes <= allowed:
        raise HTTPException(
            status_code=403,
            detail="В ответе есть данные класса, на который у аккаунта нет права",
        )
    # Пост-генерационная фильтрация остаётся защитой в глубину (см. governance).
    safe_answer = access_engine.apply_acl(stored.answer, allowed)
    # PDF строится в PyMuPDF синхронно: на длинном ответе это секунды, и держать
    # event loop занятым нельзя.
    content, content_type, filename = await asyncio.to_thread(
        deps.exporter.export, safe_answer, export_req.format
    )
    await _record_decision(
        request,
        "answer.exported",
        str(export_req.query_id),
        metadata={"format": export_req.format},
    )
    await _log_audit(request, "export.run", str(export_req.query_id))
    body: bytes | str = content
    if content_type == "application/pdf":
        body = base64.b64decode(content)
    return Response(
        content=body,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/api/v1/claims/{claim_id}/history",
    tags=["knowledge"],
    response_model=ClaimHistory,
)
async def get_claim_history(claim_id: str, request: Request) -> ClaimHistory:
    """История версий — тот же объект доступа, что и само утверждение.

    Адаптер знаний возвращает версии без фильтра по классу данных: без повторной
    проверки здесь любой аккаунт читал бы тезис закрытого утверждения по его id,
    обходя ACL остального контура (``/findings`` такой текст не отдаёт).
    """
    versions = await asyncio.to_thread(
        dependencies(request).knowledge.claim_history, claim_id
    )
    allowed = _allowed_classes(request)
    visible = [version for version in versions if version.data_class in allowed]
    if not visible:
        raise HTTPException(status_code=404, detail=f"Утверждение {claim_id} не найдено")
    return ClaimHistory(
        claim_id=claim_id,
        versions=[
            ClaimHistoryEntry(
                finding_id=version.id,
                version=version.version,
                statement=version.statement,
                status=version.status,
                superseded_by=version.superseded_by,
                reviewer_id=version.reviewer_id,
                review_date=version.review_date,
                review_reason=version.review_reason,
            )
            for version in visible
        ],
    )


def _coverage_gaps(findings: list[Finding]) -> tuple[int, int]:
    """Покрывающие пробелы считаются тем же кодом, что и tool gap_scan."""
    claims = to_research_claims(findings)
    research_space = build_research_space(claims)
    if research_space is None:
        return 0, 0
    summary = intelligence.summarize_gaps(claims, research_space)
    return len(summary.gaps), summary.omitted


@app.get("/api/v1/dashboard", tags=["dashboard"], response_model=DashboardResponse)
async def get_dashboard(request: Request, account: CurrentAccount) -> DashboardResponse:
    """Панель состояния: цифры корпуса без блокирующих вызовов в event loop.

    Один ``to_thread`` на оба чтения — иначе между снимком статистики и списком
    находок граф успевает измениться, и панель показывает несуществующую пару.

    Журнал в панели — не поддельный ``/audit``: общий список актов доступен
    держателю ``audit:read``, остальным — только собственные события. Без этого
    десять строк чужих запросов и правок уходили бы любому вошедшему.
    """
    deps = dependencies(request)
    allowed = _allowed_classes(request)
    principal: Principal | None = getattr(request.state, "principal", None)
    shared_journal = principal is not None and access_engine.has_permission(
        principal, "audit:read"
    )
    stats, findings = await asyncio.to_thread(
        lambda: (deps.knowledge.corpus_stats(), deps.knowledge.all_findings(allowed))
    )
    gaps, omitted = await asyncio.to_thread(_coverage_gaps, findings)
    recent = await deps.state.recent_audit(
        limit=10, actor_id=None if shared_journal else account.id
    )
    return DashboardResponse(
        documents=stats.documents,
        claims=stats.claims,
        entities=stats.entities,
        evidence=sum(len(finding.evidence) for finding in findings),
        conflicts=sum(1 for finding in findings if finding.status == "disputed"),
        gaps=gaps,
        gaps_omitted=omitted,
        recent_activity=[
            ActivityEntry(
                action=event.action,
                actor_id=event.actor_id,
                object_id=event.object_id,
                outcome=event.outcome,
                created_at=event.created_at,
            )
            for event in recent
        ],
        agent_metrics=deps.metrics.snapshot(),
    )


@app.get(
    "/api/v1/notifications",
    tags=["notifications"],
    response_model=list[Notification],
)
async def get_notifications(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=NOTIFICATION_FEED_LIMIT)] = NOTIFICATION_FEED_LIMIT,
) -> list[Notification]:
    """Лента из серверного состояния: в postgres-контуре она переживает перезапуск.

    Доставка — только опросом этого эндпоинта: push-канала в продукте нет, а
    «подписка на тему» обещала бы рассылку, которую выполнять нечем.
    """
    deps = dependencies(request)
    return await deps.state.recent_notifications(limit=limit)
