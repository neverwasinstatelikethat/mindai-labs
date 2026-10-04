from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated, Any, Literal, cast, get_args
from urllib.parse import quote, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from elasticsearch import TransportError as ElasticsearchTransportError
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from httpx import TransportError as HttpxTransportError
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from neo4j.exceptions import DriverError as Neo4jDriverError
from neo4j.exceptions import TransientError as Neo4jTransientError
from prometheus_fastapi_instrumentator import Instrumentator
from psycopg import OperationalError as PsycopgOperationalError
from starlette.background import BackgroundTask
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
    ConflictCandidateView,
    ConflictReview,
    ConflictReviewRequest,
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
    LlmUsageSummary,
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
from scientific_tangle.services.agent_metrics import (
    STORAGE_THREADS,
    AgentMetricsRegistry,
    agent_metrics,
    begin_llm_usage,
    current_llm_usage,
)
from scientific_tangle.services.comparison import ComparisonService
from scientific_tangle.services.conflicts import candidate_views, resolve_for_review
from scientific_tangle.services.document_parser import UnsupportedDocumentError, parse_document
from scientific_tangle.services.durable_state import (
    MAX_CONFLICT_REVIEWS,
    MAX_EXPERIMENTS,
    DurableState,
    InMemoryDurableState,
    StoredAnswer,
    build_durable_state,
)
from scientific_tangle.services.evolution import EvolutionService
from scientific_tangle.services.exporter import ExportError, ExportService
from scientific_tangle.services.governance import BASE_DATA_CLASSES, AccessPolicyEngine
from scientific_tangle.services.infrastructure import Neo4jElasticsearchKnowledgeBase
from scientific_tangle.services.ingestion import IngestionService
from scientific_tangle.services.knowledge import FindingWindow, KnowledgeBase
from scientific_tangle.services.ontology import OntologyValidationError
from scientific_tangle.services.provider import (
    ModelBusyError,
    ModelUnavailableError,
    build_provider,
)
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
# Потолок прогрева хранилища на старте: зависший Neo4j/Elasticsearch не должен
# превращать запуск сервиса в вечное ожидание (см. _warmup_knowledge).
STORAGE_WARMUP_SECONDS = 30.0
# Профиль читает журнал своего аккаунта экраном, а не выгрузкой: сотни строк
# человеку не нужны, а большой лимит только зря гоняет состояние через прокси.
ME_ACTIVITY_LIMIT_DEFAULT = 40
ME_ACTIVITY_LIMIT_MAX = 200
# Потолок чтения ленты предложений: сам журнал обрезается на стороне серверного
# состояния (services/durable_state.MAX_PROPOSALS), поэтому «прочитать всю ленту»
# здесь — это прочитать её до этого потолка, а не неограниченный запрос к базе.
# Значение обязано совпадать с MAX_PROPOSALS: если потолок журнала изменится,
# менять нужно и здесь, иначе X-Total-Count начнёт врать о размере очереди.
# Это единственное число для всех путей, которые что-либо решают по ленте
# (список, гейт «известно ли предложение», A/B-прогон, гидратация зеркала при
# подъёме): дефолт параметра чужой зоны (``durable_state``) эти пути не устраивает —
# предложение за дефолтом оставалось бы видно в очереди, но было бы нерешаемо.
PROPOSAL_FEED_READ_LIMIT = 500
# Запись в закрытый класс — отдельный гейт от чтения, и он проверяется первым на
# обоих путях импорта (JSON и multipart): извлечение и OCR стоят минут, поэтому
# отказ по праву доступа не должен оплачиваться полным разбором файла.
RESTRICTED_WRITE_DETAIL = "Запись в класс «restricted» требует экспертного права на запись"
# Сбой хранилища знаний формулируется человеку, а не стеком: 500 на недоступном
# Neo4j выглядел бы падением приложения, и мониторинг звал бы разработчика туда,
# где нужно только поднять граф или индекс.
STORAGE_UNAVAILABLE_DETAIL = (
    "Хранилище знаний недоступно: граф или поисковый индекс не отвечают. "
    "Данные не потеряны — повторите запрос, когда сервис хранилища поднимется."
)
# Классы ошибок «сервиса нет», а не «код падает»: драйвер Neo4j, транспорт
# Elasticsearch и HTTP-транспорт эмбеддингов/Postgres. Синтаксис Cypher, KeyError
# и ValueError сюда не попадают — они остаются 500, потому что это баг кода.
STORAGE_ERRORS: tuple[type[Exception], ...] = (
    Neo4jDriverError,
    Neo4jTransientError,
    ElasticsearchTransportError,
    HttpxTransportError,
    PsycopgOperationalError,
)
METRICS_PATH = "/metrics"
# Имена отменённых прогонов в агентных метриках: отменён целый HTTP-прогон, а не
# узел графа, поэтому у него своё имя в ``mindai_agent_runs_total`` — иначе
# success_rate узлов начал бы зависеть от того, закрыл ли кто-то вкладку.
CANCELLED_RUN_AGENT = "query.http"
CANCELLED_STREAM_AGENT = "query.stream.http"
# Как часто JSON-прогон смотрит, жив ли клиент: чаще — раньше освободится слот,
# реже — больше лишних обращений к ``is_disconnected`` на длинном прогоне.
DISCONNECT_POLL_SECONDS = 1.0
_WORKFLOW_LOCK = threading.Lock()
# Только против параллельного холодного старта витрины (см. /demo).
_DEMO_LOCK = asyncio.Lock()
# Задачи, дописывающие акты после отмены прогона: event loop хранит задачи по
# слабым ссылкам, без держателя недочитанный акт исчезал бы при сборке мусора.
_PENDING_CLEANUP: set[asyncio.Task[None]] = set()
# Пробы готовности: ``/health/ready`` опрашивают мониторинг и человек, поэтому
# результат держится несколько секунд — иначе каждый опрос гонял бы ping Neo4j,
# cluster health Elasticsearch и SELECT 1 в Postgres. Жёсткий потолок короткий:
# readiness не имеет права сам становиться источником задержки.
READINESS_PROBE_TTL_SECONDS = 5.0
READINESS_PROBE_TIMEOUT_SECONDS = 2.0
# Значение «сервис настроен, но не отвечает» появляется в литерале ``ServiceState``
# только вместе с контрактом (зона domain/contracts.py), поэтому оно выбирается по
# фактическому составу литерала: pydantic отверг бы неизвестное значение прямо в
# теле readiness, и роут падал бы 500 ровно там, где он обязан показать деградацию.
_UNAVAILABLE_SERVICE_STATE = cast(
    ServiceState, "unavailable" if "unavailable" in get_args(ServiceState) else "disabled"
)
_readiness_probe_cache: tuple[float, dict[str, bool]] | None = None
_readiness_probe_lock = asyncio.Lock()

# ── Доступ: контракт «сессия → субъект» ──────────────────────────────────────
SESSION_COOKIE = "nk_session"
UNAUTHENTICATED_DETAIL = "Требуется вход в аккаунт"
CSRF_DETAIL = "Источник запроса не входит в доверенный список"
# Ответ на /metrics без токена: текст объясняет оператору Prometheus, что именно
# не так, и при этом не подтверждает и не опровергает значение токена.
METRICS_UNAUTHENTICATED_DETAIL = (
    "Для чтения метрик нужен заголовок Authorization: Bearer <METRICS_TOKEN>"
)
STATE_CHANGING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
# pydantic описывает отказ схемы типом ошибки и значением, присланным клиентом.
# Значение (``input``, ``ctx``) наружу не уходит: в него попадает весь текст
# restricted-документа, который 422 отдавал бы обратно в браузере и в чужих
# логах. Поэтому сообщения собираются по типу ошибки, а не по присланным данным.
VALIDATION_REASONS: dict[str, str] = {
    "missing": "не заполнено",
    "extra_forbidden": "не разрешено в этом запросе",
    "string_type": "ожидался текст",
    "string_too_short": "значение короче допустимого",
    "string_too_long": "значение длиннее допустимого",
    "int_type": "ожидалось целое число",
    "int_parsing": "не читается как целое число",
    "float_type": "ожидалось число",
    "float_parsing": "не читается как число",
    "bool_type": "ожидается да или нет",
    "bool_parsing": "не читается как да или нет",
    "greater_than": "значение должно быть больше допустимого предела",
    "greater_than_equal": "значение ниже допустимого предела",
    "less_than": "значение должно быть меньше допустимого предела",
    "less_than_equal": "значение выше допустимого предела",
    "too_short": "элементов меньше, чем требуется",
    "too_long": "элементов больше, чем допустимо",
    "literal_error": "значение вне допустимого перечня",
    "enum": "значение вне допустимого перечня",
    "list_type": "ожидался список",
    "dict_type": "ожидался набор полей",
    "model_attributes_type": "передана структура неожиданного вида",
    "uuid_parsing": "ожидался идентификатор в формате UUID",
    "json_invalid": "тело не разобрано как JSON",
    "json_type": "тело не разобрано как JSON",
    "value_error": "значение отклонено проверкой",
}
# Области запроса из ``loc``: без них человек не понял бы, что именно править —
# поле формы, параметр строки запроса или заголовок.
VALIDATION_AREAS: dict[str, str] = {
    "body": "тело запроса",
    "query": "параметр запроса",
    "path": "параметр пути",
    "header": "заголовок",
    "cookie": "cookie",
    "form": "поле формы",
}
# Сколько замечаний показывается человеку: больше — простыня, которую в
# интерфейсе не читают, а полный перечень остаётся в журнале процесса.
VALIDATION_DETAIL_MAX = 3
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
    # Потолок читается тот же, что и у ``/proposals``: дефолт параметра чужой
    # зоны оставлял бы хвост очереди без зеркала, и решение по такому предложению
    # падало бы вместо работы.
    restored = await deps.state.recent_proposals(limit=PROPOSAL_FEED_READ_LIMIT)
    for proposal in restored:
        deps.evolution.restore(proposal)
    if any(proposal.status == "accepted" for proposal in restored):
        with _WORKFLOW_LOCK:
            deps.workflow = deps.build_workflow(extra_policy=deps.evolution.active_policy())


class _StorageThreadPool(ThreadPoolExecutor):
    """Default executor цикла с явным размером и метрикой очереди.

    Все блокирующие обращения к хранилищу уходят через ``asyncio.to_thread``, то
    есть в default executor цикла. Без явного размера это ``min(32, ядер + 4)``,
    посчитанное от хоста, а не от ёмкости контейнера и не от числа прогонов: один
    медленный обход графа занимает потоки, и соседний ``/findings`` ждёт не
    Elasticsearch, а освободившийся поток. Пул именуемый (потоки видно в журнале и
    в снимке трассировки), а его занятость — метрика, а не догадка по p95.
    """

    def submit(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]:
        STORAGE_THREADS.labels(state="queued").inc()
        try:
            future = super().submit(self._instrumented(fn), *args, **kwargs)
        except BaseException:
            STORAGE_THREADS.labels(state="queued").dec()
            raise
        future.add_done_callback(lambda _: STORAGE_THREADS.labels(state="queued").dec())
        return future

    @staticmethod
    def _instrumented(fn: Callable[..., Any]) -> Callable[[], Any]:
        def run() -> Any:
            STORAGE_THREADS.labels(state="in_flight").inc()
            try:
                return fn()
            finally:
                STORAGE_THREADS.labels(state="in_flight").dec()

        return run


def _configure_storage_pool(settings: Settings) -> ThreadPoolExecutor:
    """Ставит именованный пул потоков как default executor текущего цикла."""
    pool = _StorageThreadPool(
        max_workers=settings.storage_thread_pool_size,
        thread_name_prefix="storage",
    )
    asyncio.get_running_loop().set_default_executor(pool)
    STORAGE_THREADS.labels(state="limit").set(settings.storage_thread_pool_size)
    if settings.storage_thread_pool_size < settings.agent_max_concurrent_runs * 2:
        logger.warning(
            "STORAGE_THREAD_POOL_SIZE=%d меньше удвоенного потолка приёма "
            "(%d прогонов): обращения к хранилищу будут ждать поток друг за "
            "другом, и медленный обход графа затормозит списки. Увеличьте пул или "
            "уменьшите AGENT_MAX_CONCURRENT_RUNS.",
            settings.storage_thread_pool_size,
            settings.agent_max_concurrent_runs,
        )
    logger.info(
        "Пул блокирующих чтений хранилища: %d потоков (prefix=storage)",
        settings.storage_thread_pool_size,
    )
    return pool


async def _warmup_knowledge(deps: AppDependencies) -> None:
    """Хранилище прогревается до первого запроса, но старт ради прогрева не встаёт.

    Схема Neo4j, индексы Elasticsearch, подъём каталога и доводка старых записей
    были ленивыми: первый аналитик платил за них секундами при полностью
    исправном контуре. Потолок нужен, потому что зависшее хранилище не должно
    блокировать запуск — healthcheck контейнера посчитал бы его мёртвым. Прогрев
    по таймауту не отменяется (поток в `to_thread` не прерывается): он доходит до
    конца сам, а первый запрос либо застанет каталог готовым, либо прогреет его
    как раньше.
    """
    try:
        await asyncio.wait_for(
            asyncio.to_thread(deps.knowledge.warmup),
            timeout=STORAGE_WARMUP_SECONDS,
        )
        logger.info("Хранилище знаний прогрето до первого запроса")
    except TimeoutError:
        logger.error(
            "Прогрев хранилища не уложился в %.0f с: сервис поднимается, каталог"
            " догреется в фоне или на первом запросе",
            STORAGE_WARMUP_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 — хранилище может лежать, это не причина не стартовать
        logger.error("Прогрев хранилища не удался: %s", error)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    deps = get_dependencies()
    application.state.dependencies = deps
    # Пул блокирующих чтений ставится раньше всего: через него пойдут и прогрев,
    # и первый же список находок.
    storage_pool = _configure_storage_pool(deps.settings)
    await _prepare_accounts(deps)
    await _prepare_state(deps)
    await _warmup_knowledge(deps)
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
        # Пул гасится последним: последний блокирующий вызов выше всё ещё его.
        # Ожидание не ставится: зависший поток хранилища не должен удерживать
        # остановку сервиса, а счётчики обнуляются, чтобы перезапуск в одном
        # процессе не оставил висящее значение.
        storage_pool.shutdown(wait=False, cancel_futures=False)
        STORAGE_THREADS.labels(state="in_flight").set(0)
        STORAGE_THREADS.labels(state="queued").set(0)


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


@asynccontextmanager
async def _storage_or_unavailable(request: Request, what: str) -> AsyncIterator[None]:
    """Обращение к хранилищу под честным 503: сбой базы — не падение приложения.

    Маршруты чтения графа вызывали ``asyncio.to_thread`` прямо, и на недоступном
    Neo4j/Elasticsearch/Postgres это была 500 с пустым объяснением: мониторинг
    поднимал тревогу «приложение упало», а аналитик видел красный экран вместо
    «хранилище не отвечает». Ошибки соединения и таймауты отображаются в 503,
    прикладные (``ValueError``, ``KeyError``, синтаксис Cypher) — нет: они и
    должны оставаться 500, потому что это баг кода, а не отсутствие сервиса.

    Тем же механизмом закрыты durable-чтения (выгрузка, журнал, оценки, расход) и
    пути записи (импорт, перезапись утверждения, решение по предложению): «базы
    нет» одинаково и там, и там, а 4xx (доступ, не найден, конфликт) остаются
    своими кодами — они поднимаются как ``HTTPException`` и через этот фильтр не
    проходят.
    """
    try:
        yield
    except STORAGE_ERRORS as error:
        logger.warning(
            "Обращение «%s» не выполнено: хранилище недоступно (correlation_id=%s): %s",
            what,
            _correlation_id(request) or "—",
            error,
        )
        raise HTTPException(status_code=503, detail=STORAGE_UNAVAILABLE_DETAIL) from error


def _paginated[Item](
    items: Sequence[Item],
    limit: int,
    offset: int,
    response: Response,
) -> list[Item]:
    """Единая пагинация списков: тело остаётся массивом, потолок — в заголовке.

    Интерфейс читает эти маршруты как обычный массив (``frontend/src/lib/api.ts``),
    поэтому контракт тела не меняется: режем окно ``[offset:offset+limit]``, а
    полное число подходящих записей кладём в ``X-Total-Count`` — там ему место, а
    не в новом поле, которого фронтенд не ждёт.
    """
    response.headers["X-Total-Count"] = str(len(items))
    return list(items[offset : offset + limit])


def _report_window(
    response: Response, window: FindingWindow, *, requested_limit: int
) -> None:
    """Окно хранилища: полное число — в ``X-Total-Count``, оговорку — в заголовок.

    ``FindingWindow.note`` жил только в сервисе: клиент получал короткую страницу и
    не мог отличить «больше записей нет» от «Elasticsearch не ответил, окно взято из
    каталога процесса». Тело не трогаем — его читает фронтенд как массив.

    Значение URL-кодировано: заголовок уходит как latin-1, а оговорка по-русски.
    """
    response.headers["X-Total-Count"] = str(window.total)
    notes = [window.note] if window.note else []
    if window.limit < requested_limit:
        notes.append(
            f"Потолок страницы — {window.limit} записей, запрос был на {requested_limit}."
        )
    if notes:
        response.headers["X-Window-Note"] = quote(" ".join(notes))


def _invalidate_demo_cache(deps: AppDependencies) -> None:
    """Витрина сброшена: корпус изменился, а кэш ответа — нет.

    ``demo_answer`` кэшируется на процесс, и раньше после импорта документов
    витрина продолжала отдавать ответ, собранный по старому корпусу: «ответ
    настоящий, прогон один» переставало быть правдой. Сброс бесплатный — цена
    только один повторный прогон рабочего процесса на первом обращении.
    """
    if deps.demo_answer is None and deps.demo_evaluation is None and deps.demo_access is None:
        return
    deps.demo_answer = None
    deps.demo_evaluation = None
    deps.demo_access = None


async def _await_or_client_gone[T](
    request: Request, work: Callable[[], Coroutine[Any, Any, T]]
) -> T:
    """Ждёт работу и отменяет её, как только клиент ушёл.

    Отмена HTTP-запроса при обрыве соединения — поведение сервера, а не приложения:
    uvicorn не отменяет задачу обработчика на POST-запросе с длинным прогоном,
    поэтому без этой проверки закрытая вкладка держала слот приёма и слот модели до
    ``AGENT_DEADLINE_SECONDS``. Поток SSE отменяется сам; этот хелпер закрывает JSON-путь.

    Работает ровно потому, что слой доступа больше не ``BaseHTTPMiddleware``: тот
    подменяет канал ``receive``, и ``is_disconnected()`` из обработчика обрыв не
    видит (замер на реальном сокете: с ним прогон доходил до конца, без него
    отменяется на 0.01 с после ухода клиента).

    ``CancelledError`` поднимается намеренно: вызывающий обработчик ведёт тот же учёт,
    что и при настоящей отмене (след прогона, акт, освобождение слота).
    """
    task: asyncio.Task[T] = asyncio.create_task(work())
    while True:
        done, _ = await asyncio.wait({task}, timeout=DISCONNECT_POLL_SECONDS)
        if done:
            return await task
        if await request.is_disconnected():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise asyncio.CancelledError("клиент отключился до завершения прогона")


async def _record_cancelled_run(
    request: Request,
    deps: AppDependencies,
    *,
    agent: str,
    audit_action: str,
    object_id: str,
    started: float,
    account: Account,
) -> None:
    """Отмена прогона оставляет след: провал в метриках и акт с correlation_id.

    Клиент закрыл вкладку — задачу генератора SSE отменяют, и ``finally`` с
    ``_finalize_answer`` за циклом событий не выполняется: не было ни копии
    ответа в ``nk_answers``, ни акта, ни единицы в агентных метриках. Такой прогон
    исчезал молча, а ``success_rate`` считался только по успешным. Серверную копию
    на отмене НЕ заводим: половины ответа в продукте нет, и «ответ без ответа»
    было бы выдумкой — фиксируется именно отменённый прогон.

    Акт пишется под ``asyncio.shield``: в уже отменённой задаче обычный ``await``
    поймал бы ``CancelledError`` повторно, и очистка упала бы ровно там же, где
    упал прогон. Shield оставляет запись в собственном task'е, который отмена
    вызывающей стороны не трогает.
    """
    deps.metrics.observe(agent, round((time.monotonic() - started) * 1000, 1), False)

    async def _flush_cancelled_run() -> None:
        await _log_audit(request, audit_action, object_id, "failure")
        # Токены отменённого прогона уже оплачены: без этой строки расход
        # «ушёл в пустоту», и accounting по аккаунту показывал бы меньше, чем
        # списал провайдер. query_id не указываем — ответа, к которому его
        # привязать, не существует.
        usage = current_llm_usage()
        if usage.calls:
            try:
                await deps.state.record_llm_usage(
                    account_id=account.id,
                    model=deps.settings.gigachat_model,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    latency_ms=usage.latency_ms,
                    success=False,
                )
            except Exception:  # noqa: BLE001 — след не должен тонуть вместе с отменой
                logger.exception("Расход отменённого прогона не записан")

    cleanup = asyncio.create_task(
        _flush_cancelled_run(),
        name=f"audit:{audit_action}",
    )
    # Цикл событий держит на задачи только слабые ссылки: без явного хранения
    # недочитанный акт мог бы исчезнуть вместе со сборщиком мусора.
    _PENDING_CLEANUP.add(cleanup)
    cleanup.add_done_callback(_PENDING_CLEANUP.discard)
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        logger.info("Акт %s дописывается вне отменённого прогона", audit_action)


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


def _require_restricted_write(request: Request, data_class: DataClass) -> None:
    """Единый гейт на запись в закрытый класс для обоих путей импорта.

    ``restricted:read`` отвечает за доступ к данным, а запись в закрытый контур —
    отдельное действие ``document.write_restricted`` (governance.WRITE_ACTIONS):
    расширение доступа на чтение не должно молча открывать импорт в restricted.
    Класс ``internal`` гейта не требует — он входит в базовый уровень продукта
    (PRODUCT.md), поэтому проверяется принадлежность к ``BASE_DATA_CLASSES``, а не
    «всё, что не public».
    """
    if data_class in BASE_DATA_CLASSES:
        return
    principal: Principal | None = getattr(request.state, "principal", None)
    if access_engine.can_write(principal, "document.write_restricted"):
        return
    raise HTTPException(status_code=403, detail=RESTRICTED_WRITE_DETAIL)


def _requires_session(path: str) -> bool:
    """Всё под ``/api/v1/`` работает только с сессией, кроме белого списка.

    Снаружи остаются ``/health/*``, ``/metrics`` и OpenAPI-инструменты: их
    читают Prometheus и документация, а данных доступа они не отдают. У
    ``/metrics`` свой порог — см. ``_metrics_allowed``: сессия браузера там
    неуместна, а имена серий и корпусные числа без токена наружу не уходят.
    """
    if not path.startswith("/api/v1/"):
        return False
    return path not in PUBLIC_API_PATHS


def _metrics_allowed(request: Request, settings: Settings) -> bool:
    """Порог чтения ``/metrics``: токен из настройки, а не сессия браузера.

    Выбор сознательный: ``metrics_token`` не задан — эндпоинт открыт, это
    локальный контур, где Prometheus ходит по-простому и ломать его нечем. Задан
    — Prometheus обязан прислать ``Authorization: Bearer <токен>``: серии метрик
    называют размеры корпуса, режимы модели и имена агентов, и в публичном
    контуре это разведданные, а не свободный текст.

    Проверка живёт в ``access_middleware``, а не в зависимости маршрута:
    ``/metrics`` отдаёт сторонний Instrumentator, и своя зависимость к его
    обработчику не приклеивается. Сравнение через ``hmac.compare_digest`` — по
    длине токена не должно угадываться ничего.
    """
    expected = settings.metrics_token
    if not expected:
        return True
    scheme, _, provided = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not provided.strip():
        return False
    return hmac.compare_digest(provided.strip(), expected)


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


def _mutating_get_refused(request: Request, settings: Settings) -> bool:
    """Источник GET, который мутирует состояние, назван и чужой — отказ.

    ``STATE_CHANGING_METHODS`` в middleware покрывает POST/PATCH/PUT/DELETE, а
    ``/api/v1/demo`` — GET, который записывает серверную копию ответа, акт в
    журнал и сжигает бюджет витрины на первом обращении. Переводить его в POST
    нельзя: интерфейс обращается к нему как к GET. Поэтому источник проверяется
    внутри обработчика той же функцией, что и в middleware.

    Отказ даёт названный, но не доверенный источник, плюс ``Sec-Fetch-Site`` от
    браузера: запрос вообще без этих заголовков не подделывает чужая страница
    (на навигации браузер источник не сообщает), зато такой запрос legit-клиента
    не должен превращаться в 403.
    """
    declared = request.headers.get("origin") or request.headers.get("referer")
    if declared and not _origin_trusted(request, settings):
        return True
    return request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site"


class AccessMiddleware:
    """Серверная аутентификация: cookie-сессия → субъект, затем порог доступа.

    Заменяет эпоху ``X-User-Role``/``X-User-Id``, где личность приходила от
    клиента и была подделываемой за одну строку в curl. Здесь:

    * ``nk_session`` резолвится в учётную запись на сервере (без токена, с
      просрочкой или после смены пароля — субъекта нет);
    * всё под ``/api/v1/`` без действующей сессии — 401, в том числе SSE
      ``/query/stream``: ответ проверяется до открытия потока;
    * ``/metrics`` при заданном ``METRICS_TOKEN`` требует Bearer-заголовок:
      сессии браузера там нет и быть не может, а серии метрик без токена наружу
      не уходят (см. ``_metrics_allowed``);
    * state-changing запрос с cookie проверяется на доверенный источник (CSRF);
    * preflight ``OPTIONS`` пропускается: cookie в нём нет, а ответ отдаёт
      CORSMiddleware, который висит внутренним слоем.

    Тонкие права (``audit:read``, ``proposal:review``, ``restricted:read``)
    по-прежнему проверяет ``require_permission`` — этот слой отвечает только за
    «есть ли субъект».

    Класс, а не ``@app.middleware("http")``, по причине, измеренной на этом
    контуре: BaseHTTPMiddleware запускает обработчик в отдельной задаче и своим
    каналом ``receive``, из-за чего ``request.is_disconnected()`` внутри маршрута
    обрыв соединения не видит. Замеры на одном и том же медленном JSON-роуте:
    без этого слоя прогон отменяется через 2–3 с после закрытой вкладки, с
    ``@app.middleware("http")`` — доходит до конца (10 с). То есть хелпер
    ``_await_or_client_gone`` был бутафорией ровно до этой замены: закрытая
    вкладка держала слот приёма и слот модели до ``AGENT_DEADLINE_SECONDS``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        # correlation_id назначается на сервере, если клиент его не принёс: иначе
        # акту в журнале не с чем сопоставить, и разбор инцидента невозможен.
        correlation_id = request.headers.get("X-Correlation-Id", "").strip() or uuid4().hex
        request.state.correlation_id = correlation_id
        deps = dependencies(request)
        token = request.cookies.get(SESSION_COOKIE)
        account = await deps.accounts.resolve_session(token) if token else None
        request.state.account = account
        request.state.principal = (
            access_engine.principal(account.id, account.review_enabled) if account else None
        )
        send_with_id = self._send_with_correlation(send, correlation_id)
        if request.method != "OPTIONS":
            refusal = self._refusal(request, deps, account, token, correlation_id)
            if refusal is not None:
                await refusal(scope, receive, send_with_id)
                return
        await self.app(scope, receive, send_with_id)

    @staticmethod
    def _refusal(
        request: Request,
        deps: AppDependencies,
        account: Account | None,
        token: str | None,
        correlation_id: str,
    ) -> JSONResponse | None:
        """Отказ слоя доступа или ``None`` — продолжить обработку."""
        if request.url.path == METRICS_PATH and not _metrics_allowed(request, deps.settings):
            return JSONResponse(
                status_code=401,
                content={"detail": METRICS_UNAUTHENTICATED_DETAIL},
                headers={
                    "X-Correlation-Id": correlation_id,
                    "WWW-Authenticate": "Bearer",
                },
            )
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
        return None

    @staticmethod
    def _send_with_correlation(send: Send, correlation_id: str) -> Send:
        """Отдаёт идентификатор на каждом ответе — и на обычных, и на потоках.

        SSE не обязан доходить до события с идентификатором, поэтому заголовок
        ставится на ``http.response.start``, а не в теле обработчика.
        """

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Correlation-Id"] = correlation_id
            await send(message)

        return wrapped


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
    # Без этого списка браузер читает только простые заголовки ответа: окно
    # выдачи (`limit`/`offset`) теряет полное число записей, и экран «показаны
    # первые N из M» показывает «показаний нет», хотя сервер его отдал.
    expose_headers=["X-Total-Count", "X-Correlation-Id", "X-Window-Note"],
)
Instrumentator(excluded_handlers=["/metrics"]).instrument(app).expose(
    app,
    include_in_schema=False,
)


app.add_middleware(AccessMiddleware)


@app.exception_handler(ModelUnavailableError)
async def model_unavailable(_: Request, error: ModelUnavailableError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(error)})


@app.exception_handler(ModelBusyError)
async def model_busy(_: Request, error: ModelBusyError) -> JSONResponse:
    """Модель занята свободными слотами: это 429, а не 503.

    503 обещает, что с сервисом что-то не так, и интерфейс зовёт администратора.
    Здесь сервис здоров и просто занят до предела, который сервер держал уже
    ``gigachat_queue_wait_seconds``. Тело повторяет форму отказа приёмной
    границы (``active`` / ``limit`` / ``retry_after``), поэтому аналитик видит,
    сколько ответов собирается и через сколько секунд спросить снова, а вопрос
    его не теряется.
    """
    return JSONResponse(
        status_code=429,
        content={
            "detail": str(error),
            "active": error.active,
            "waiting": error.waiting,
            "limit": error.limit,
            "retry_after": error.retry_after,
        },
        headers={"Retry-After": str(error.retry_after)},
    )


def _validation_field(loc: Sequence[object]) -> str:
    """Человеческое имя поля по ``loc`` из ошибки схемы.

    ``loc`` собран из служебных сегментов (``body``, ``query``, имена полей схемы),
    а не из присланных данных, поэтому наружу он безопасен.
    """
    parts = [str(item) for item in loc]
    if not parts:
        return "запрос"
    area = VALIDATION_AREAS.get(parts[0])
    if area is None:
        return f"поле «{' → '.join(parts)}»"
    if len(parts) == 1:
        return area
    return f"поле «{' → '.join(parts[1:])}» ({area})"


def _validation_message(error: dict[str, object]) -> str:
    """Одна причина отказа схемы: поле + что с ним не так, без присланных значений.

    Именно поэтому в текст не попадают ``input`` и ``ctx``: pydantic кладёт в них
    всё тело запроса, включая текст restricted-документа, и такой 422 уезжал бы
    обратно клиенту и в чужие прокси-логи.
    """
    reason = VALIDATION_REASONS.get(str(error.get("type", "")), "заполнено неверно")
    loc = cast(Sequence[object], error.get("loc") or ())
    return f"{_validation_field(loc)}: {reason}"


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
    """Отказ схемы запроса — по-русски, без эха тела и с ``correlation_id``.

    Оболочка остаётся той же, что у остальных ошибок (``{"detail": ...}`` плюс
    заголовок ``X-Correlation-Id``), и код ответа не меняется: 422 как был, так и
    есть. Меняется только содержимое ``detail``: вместо перечня служебных словарей
    — строки, которые читает аналитик. Полная картина (включая ``input``) уходит в
    журнал процесса под ``correlation_id`` — по нему разберёт инцидент тот, кому
    это нужно.
    """
    errors = [dict(item) for item in error.errors()]
    reasons = [_validation_message(item) for item in errors[:VALIDATION_DETAIL_MAX]]
    if not reasons:
        reasons = ["запрос заполнен неверно"]
    remaining = len(errors) - len(reasons)
    detail = "; ".join(reasons)
    if remaining > 0:
        detail += f"; и ещё замечаний: {remaining}"
    logger.warning(
        "Запрос отклонён на границе схемы (correlation_id=%s): %d замечаний",
        _correlation_id(request) or "—",
        len(errors),
    )
    return JSONResponse(
        status_code=422,
        content={"detail": f"Проверьте запрос — {detail}."},
        headers={"X-Correlation-Id": _correlation_id(request)},
    )


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


def _probe_neo4j(knowledge: object) -> bool:
    """Фактическая проверка Neo4j, а не «бэкенд выбран при старте».

    Драйвер ищется по свойству, а не по имени класса: контур на памяти probe не
    имеет, и отсутствие соединения там — норма, а не деградация.

    Вызов без аргументов и без ``timeout``: в драйвере 6.x
    ``verify_connectivity`` возвращает ``None`` (результат полагаться на
    исключение, а не на булево значение), именованные параметры считаются
    preview-фичей. Потолок ожидания держит ``_readiness_probes`` снаружи, плюс
    работают собственные таймауты драйвера из URI.
    """
    driver = getattr(knowledge, "_driver", None)
    verify = getattr(driver, "verify_connectivity", None)
    if not callable(verify):
        return True
    try:
        verify()
    except Exception as error:  # noqa: BLE001 — готовность не вправе падать из-за пробы
        logger.warning("Neo4j не отвечает (readiness-проба): %s", error)
        return False
    return True


def _probe_elasticsearch(knowledge: object) -> bool:
    """Cluster health Elasticsearch: индекс может быть жив, а кластер — нет."""
    client = getattr(knowledge, "_search", None)
    health = getattr(getattr(client, "cluster", None), "health", None)
    if not callable(health):
        return True
    try:
        health()
    except Exception as error:  # noqa: BLE001 — готовность не вправе падать из-за пробы
        logger.warning("Elasticsearch не отвечает (readiness-проба): %s", error)
        return False
    return True


async def _probe_postgres(store: object) -> bool:
    """``SELECT 1`` через собственный пул адаптера: без него «configured» было бы
    обещанием, данным на момент старта процесса.

    Пула нет — контур работает на памяти, и это уже отражено в ``services`` как
    ``fallback``, а не как отказ.
    """
    pool = getattr(store, "_pool", None)
    if pool is None:
        return True
    try:
        async with pool.connection(timeout=READINESS_PROBE_TIMEOUT_SECONDS) as connection:
            await connection.execute("SELECT 1")
    except Exception as error:  # noqa: BLE001 — готовность не вправе падать из-за пробы
        logger.warning("Postgres не отвечает (readiness-проба): %s", error)
        return False
    return True


async def _readiness_probes(deps: AppDependencies) -> dict[str, bool]:
    """Пробы хранилищ с коротким кэшем: роут готовности читают часто.

    Результат живёт ``READINESS_PROBE_TTL_SECONDS`` и выполняется в потоке, потому
    что ping Neo4j и cluster health — блокирующие вызовы драйверов. Без кэша
    каждый опрос мониторинга добавлял бы процессу лишние обращения к базе, а
    без внешнего потолка пробы могли бы держать роут дольше, чем человек готов
    ждать ответа о статусе сервиса.
    """
    global _readiness_probe_cache  # noqa: PLW0603 — кэш проб живёт в модуле намеренно
    now = time.monotonic()
    if (
        _readiness_probe_cache is not None
        and now - _readiness_probe_cache[0] < READINESS_PROBE_TTL_SECONDS
    ):
        return _readiness_probe_cache[1]
    async with _readiness_probe_lock:
        now = time.monotonic()
        if (
            _readiness_probe_cache is not None
            and now - _readiness_probe_cache[0] < READINESS_PROBE_TTL_SECONDS
        ):
            return _readiness_probe_cache[1]
        try:
            graph, search, accounts_store, state = await asyncio.wait_for(
                asyncio.gather(
                    asyncio.to_thread(_probe_neo4j, deps.knowledge),
                    asyncio.to_thread(_probe_elasticsearch, deps.knowledge),
                    _probe_postgres(deps.accounts),
                    _probe_postgres(deps.state),
                ),
                timeout=READINESS_PROBE_TIMEOUT_SECONDS * 2,
            )
        except TimeoutError:
            # CancelledError отправляется дальше: гасить отмену роута означало бы
            # врать вызывающей стороне.
            logger.warning("Readiness-пробы не уложились в потолок, контур деградирован")
            graph = search = accounts_store = state = False
        fresh = {
            "knowledge_graph": graph,
            "hybrid_search": search,
            "accounts": accounts_store,
            "server_state": state,
        }
        _readiness_probe_cache = (time.monotonic(), fresh)
        return fresh


@app.get("/health/ready", tags=["health"], response_model=SystemStatus)
async def readiness(request: Request, response: Response) -> SystemStatus:
    """Готовность сервиса: деградация отдаётся кодом 503, а не «успешным» 200.

    Тело не меняется — тот же ``SystemStatus`` с ``model_mode``, ``services`` и
    ``degradation_reasons``. Менятся только ожидания оркестратора: без GigaChat
    ``/query``, ``/demo`` и ``/query/stream`` отдают 503, и называть такой контур
    «готовым» было бы неправдой — балансировщик заводил бы трафик на сервис,
    который умеет только 503.

    Прошлый вариант сообщал ``configured`` по флагам старта и ни к кому не
    обращался: контейнер считался здоровым при мёртвом Neo4j, Elasticsearch или
    Postgres, а платить за ленивое соединение приходилось первому аналитику.
    Теперь состояние хранилищ проверяется фактическими пробами (с кэшем на
    несколько секунд), и неотвеченный сервис виден в тех же полях ``services`` и
    в ``degradation_reasons`` словами — сам роут при этом не падает.

    ``/health/live`` к этому не относится и остаётся 200: он отвечает за «процесс
    жив», иначе оркестратор убивал бы контейнер, который чинится ключом модели, а
    не перезапуском. Поэтому healthcheck в compose.yaml смотрит на liveness, а
    readiness остаётся делом человека и мониторинга.
    """
    deps = dependencies(request)
    probes = await _readiness_probes(deps)
    model_mode = deps.provider.mode
    neo4j_backend = deps.settings.knowledge_backend == "neo4j"
    postgres_accounts = deps.accounts.kind == "postgres"
    postgres_state = deps.state.kind == "postgres"

    def backend_state(configured: bool, key: str) -> ServiceState:
        """Три состояния вместо двух: «не настроен», «настроен и отвечает»,
        «настроен, но не отвечает».

        Проба важнее флага старта: ``configured`` из конфигурации означал лишь
        «выбрали Neo4j/Postgres при подъёме», из-за чего healthcheck оставался
        зелёным при мёртвом хранилище.
        """
        if not probes[key]:
            return _UNAVAILABLE_SERVICE_STATE
        return "configured" if configured else "fallback"

    services: dict[str, ServiceState] = {
        "knowledge_graph": backend_state(neo4j_backend, "knowledge_graph"),
        "hybrid_search": backend_state(neo4j_backend, "hybrid_search"),
        "model_provider": "configured" if model_mode == "gigachat" else "disabled",
        "evaluation": "ready",
        "checkpointer": "configured" if deps.checkpointer is not None else "disabled",
        # "fallback" = Postgres был недоступен на старте, сессии живут в памяти.
        "accounts": backend_state(postgres_accounts, "accounts"),
        # Тот же смысл для серверного состояния: на памяти состояние обнуляется
        # перезапуском вместе с остальными журналами.
        "server_state": backend_state(postgres_state, "server_state"),
    }
    reasons = [reason for reason in (deps.accounts_error, deps.state_error) if reason]
    if not probes["knowledge_graph"]:
        reasons.append("Граф знаний (Neo4j) не отвечает: проба соединения не прошла.")
    if not probes["hybrid_search"]:
        reasons.append("Поисковый индекс (Elasticsearch) не отвечает: проба не прошла.")
    if not probes["accounts"]:
        reasons.append("Хранилище учётных записей (Postgres) не отвечает: проба не прошла.")
    if not probes["server_state"]:
        reasons.append("Серверное состояние (Postgres) не отвечает: проба не прошла.")
    storage_down = not all(probes.values())
    if model_mode != "gigachat":
        # Причина в теле обязана читаться и на 503: «почему оркестратор считает
        # сервис недоступным» — первый вопрос того, кто смотрит на статус.
        reasons.append(
            "Модель не настроена: агентные ответы недоступны до указания "
            "GIGACHAT_API_KEY."
        )
    if model_mode != "gigachat" or storage_down:
        # Код ставится на внедрённый Response: FastAPI сериализует SystemStatus и
        # берёт status_code из него, поэтому контракт тела не плывёт.
        response.status_code = 503
    return SystemStatus(
        status="ready" if model_mode == "gigachat" and not storage_down else "degraded",
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


async def _record_llm_usage(
    deps: AppDependencies, account: Account, query_id: str
) -> None:
    """Расход модели привязывается к аккаунту и вопросу: «сколько стоил этот запрос».

    Глобальный счётчик токенов отвечает на вопрос «сколько истратил сервис», но не
    на вопрос «кто именно». Накопитель открывает ``begin_llm_usage`` на границе
    прогона, провайдер складывает в него usage по всем обращениям (включая
    schema-repair повторы и отменённый вызов), а здесь строка уходит в
    ``nk_llm_usage``. Денег не считаем: тарифной сетки в конфигурации нет, и
    перевод токенов в рубли был бы выдумкой.
    """
    usage = current_llm_usage()
    if not usage.calls:
        # Демо-кэш и отказ до обращения в модель: строк с нулями быть не должно.
        return
    try:
        await deps.state.record_llm_usage(
            account_id=account.id,
            model=deps.settings.gigachat_model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            latency_ms=usage.latency_ms,
            success=usage.failures == 0,
            query_id=query_id,
        )
    except Exception:  # noqa: BLE001 — учёт не вправе превращать ответ в отказ
        logger.exception("Расход модели не записан (query_id=%s)", query_id)


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
    await _record_llm_usage(deps, account, str(stored.query_id))
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

    Отмена (клиент закрыл вкладку, таймаут прокси) не проходит молча: ``finally``
    освобождает слот, а след прогона пишет ``_record_cancelled_run`` — акт с
    ``correlation_id`` и провал в агентных метриках. ``CancelledError`` при этом
    отправляется дальше: гасить отмену означает врать вызывающей стороне.
    """
    deps = dependencies(request)
    allowed = _allowed_classes(request)
    started = time.monotonic()
    handle = deps.admission.acquire()
    try:
        begin_llm_usage()

        async def _run_research() -> AnswerPayload:
            return await workflow_for(request).run(
                _thread_scoped(account, query), allowed_data_classes=allowed
            )

        answer = await _await_or_client_gone(request, _run_research)
        filtered, evaluation = await _finalize_answer(
            request, deps, answer, allowed, account, audit_action="query.run"
        )
    except asyncio.CancelledError:
        await _record_cancelled_run(
            request,
            deps,
            agent=CANCELLED_RUN_AGENT,
            audit_action="query.cancelled",
            object_id=_reference_id(query.question),
            started=started,
            account=account,
        )
        raise
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

    GET здесь не освобождён от проверки источника: роут мутирует состояние
    (серверная копия ответа и акт), а middleware такие методы не покрывает.
    """
    deps = dependencies(request)
    if _mutating_get_refused(request, deps.settings):
        raise HTTPException(status_code=403, detail=CSRF_DETAIL)
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
                    begin_llm_usage()
                    answer = await workflow_for(request).run(
                        QueryRequest(question=DEMO_QUESTION), allowed_data_classes=allowed
                    )
                finally:
                    handle.release()
                evaluation = deps.harness.evaluate(answer)
                # Прогон оценки — тоже серверное состояние: на памяти процесса
                # он обнулялся перезапуском вместе с остальными журналами.
                await deps.state.record_evaluation(evaluation)
                # Расход витрины числится за тем, кто её собрал: последующие
                # зрители берут готовый ответ и обращений в модель не делают,
                # поэтому у них накопитель пуст и строк в учёте не появляется.
                await _record_llm_usage(deps, account, str(answer.query_id))
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

    Режим модели известен до открытия потока, поэтому недоступная модель даёт
    настоящий 503 (тот же обработчик ``ModelUnavailableError``, что у JSON-пути),
    а не 200 с событием ``error`` внутри: клиент, который не читает служебные
    события, видел бы «успешный» стрим там, где ответа не будет. Событие
    ``error`` остаётся для сбоев, которые обнаруживаются посреди прогона.

    ``correlation_id`` едет в каждом событии: поток обрывается чаще, чем JSON,
    и разбирать инцидент приходится именно по тому, что дошло до клиента.
    """
    allowed = _allowed_classes(request)
    deps = dependencies(request)
    handle = deps.admission.acquire()
    if deps.provider.mode == "unavailable":
        # Слот уже взят — освобожаем до отказа: «сервис занят» и «модели нет»
        # не должны складываться в одном счётчике.
        handle.release()
        raise ModelUnavailableError(
            "Модель недоступна: агентный стрим не запускаем. Укажите GIGACHAT_API_KEY."
        )
    correlation_id = _correlation_id(request)
    started = time.monotonic()

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
                begin_llm_usage()
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
            except asyncio.CancelledError:
                # Закрытая вкладка: до этого места прогон дожил, а ответа в
                # ``nk_answers`` не будет. След обязателен — иначе «пропавший»
                # запрос неотличим от запроса, которого не было.
                await _record_cancelled_run(
                    request,
                    deps,
                    agent=CANCELLED_STREAM_AGENT,
                    audit_action="query.stream.cancelled",
                    object_id=_reference_id(query.question),
                    started=started,
                    account=account,
                )
                raise
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
    """Импорт в корпус: витрина после него сбрасывается, а не живёт старым ответом.

    ``deps.demo_answer`` — кэш прогона на процесс: без сброса витрина продолжала
    показывать ответ, собранный по корпусу до импорта, что противоречит обещанию
    «ответ настоящий, из того же корпуса».
    """
    deps = dependencies(request)
    # Гейт закрытого класса — первым, до извлечения: ``knowledge:read`` даёт
    # читать корпус, но не право записывать в restricted (см. WRITE_ACTIONS).
    _require_restricted_write(request, document.data_class)
    async with _storage_or_unavailable(request, "Импорт документа"):
        receipt = await deps.ingestion.ingest(document)
    _invalidate_demo_cache(deps)
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
    """Загрузка файла в корпус тем же путём импорта, что и JSON-эндпоинт.

    Право на закрытый класс проверяется по значению формы ДО разбора: ``data_class``
    известен серверу сразу, а парсинг и OCR — минуты CPU. Иначе отказ по доступу
    стоил бы полного разбора, а неразбираемый restricted-файл отдавался бы 422
    вместо 403 — то есть сам факт отказа скрывался бы за ошибкой формата.
    """
    _require_restricted_write(request, DataClass(data_class))
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
    # Парсер тоже вправе сам отнести документ к закрытому классу (пометка на
    # скане, гриф на титуле) — тогда гейт повторяется уже по факту разбора.
    _require_restricted_write(request, document.data_class)
    document = document.model_copy(update={"data_class": DataClass(data_class)})
    # Импорт и сброс витрины — в ingest_document: загрузка не минует его.
    return await ingest_document(document, request)


@app.get("/api/v1/graph", tags=["knowledge"], response_model=GraphSnapshot)
async def get_graph(request: Request) -> GraphSnapshot:
    """Граф отдаётся уже отфильтрованным по классам данных аккаунта."""
    deps = dependencies(request)
    allowed = _allowed_classes(request)
    async with _storage_or_unavailable(request, "Граф корпуса"):
        return await asyncio.to_thread(deps.knowledge.full_graph, allowed)


@app.get("/api/v1/findings", tags=["knowledge"])
async def get_findings(
    request: Request,
    response: Response,
    subject: str | None = None,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LIMIT_MAX)] = AUDIT_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, object]]:
    """Возвращает находки с доказательствами и наблюдениями.

    Список режется окном ``[offset:offset+limit]``: находок в корпусе сотни, и
    отдавать их целиком на каждый экран — гонять один и тот же массив через
    прокси. Тело остаётся массивом (так его читает фронтенд), полное число
    подходящих записей — в ``X-Total-Count``, а явная оговорка окна — в
    ``X-Window-Note``.

    Окно читается из хранилища вместе с фильтрами: ``status`` и ``subject`` уходят
    предикатами запроса (поля индексуются обоими индексами), поэтому страница
    перестала быть вырезкой из поднятого в память каталога и не упирается в
    ``CATALOG_RESTORE_LIMIT``.
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Находки"):
        window = await asyncio.to_thread(
            deps.knowledge.findings_window,
            limit=limit,
            offset=offset,
            allowed_data_classes=_allowed_classes(request),
            status=status,
            subject=subject,
        )
    _report_window(response, window, requested_limit=limit)
    return [finding.model_dump(mode="json") for finding in window.findings]


@app.get("/api/v1/conflicts", tags=["knowledge"])
async def get_conflicts(
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LIMIT_MAX)] = AUDIT_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, object]]:
    """Конфликтные находки (status=disputed) в пределах прав аккаунта.

    ``X-Total-Count`` считает именно подходящие конфликты, а не весь корпус, и
    экран «N расхождений» сходится с выгрузкой. Статус отсекается предикатом
    хранилища, как и в ``/findings``: список не строится из поднятого в память
    каталога.
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Конфликты"):
        window = await asyncio.to_thread(
            deps.knowledge.findings_window,
            limit=limit,
            offset=offset,
            allowed_data_classes=_allowed_classes(request),
            status="disputed",
        )
    _report_window(response, window, requested_limit=limit)
    return [finding.model_dump(mode="json") for finding in window.findings]


@app.get(
    "/api/v1/conflicts/candidates",
    tags=["knowledge"],
    response_model=list[ConflictCandidateView],
)
async def get_conflict_candidates(
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LIMIT_MAX)] = AUDIT_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ConflictCandidateView]:
    """Очередь противоречий: пары тезисов, которые считает детектор по корпусу.

    ``/conflicts`` показывает подтверждённое (находки со статусом ``disputed``),
    этот список — то, что ещё ждёт эксперта. Пары пересчитываются на каждое
    обращение и потому не переживают ту пару, которая перестала быть
    противоречием после импорта нового документа или правки тезиса.
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Противоречия"):
        findings = await asyncio.to_thread(deps.knowledge.all_findings, _allowed_classes(request))
        reviews = await deps.state.conflict_reviews(limit=MAX_CONFLICT_REVIEWS)
    views = await asyncio.to_thread(candidate_views, findings, reviews)
    return _paginated(views, limit, offset, response)


@app.post(
    "/api/v1/conflicts/candidates/{candidate_id}/review",
    tags=["knowledge"],
    response_model=ConflictReview,
)
async def review_conflict_candidate(
    candidate_id: str,
    body: ConflictReviewRequest,
    request: Request,
    _: None = Depends(require_permission("proposal:review")),
) -> ConflictReview:
    """Экспертное решение по паре: противоречие подтверждено или отклонено.

    Подтверждение переводит обе находки в ``disputed`` — только так расхождение
    попадает в ``/conflicts``, в сводку дашборда и в фильтр выдачи. Отклонение
    статус не откатывает: консенсус мог быть изменён по другой паре, и откат
    приписал бы эксперту то, чего он не решал.

    Отвечает записанным решением, а не строкой очереди: очередь ограничена
    потолком, и подтверждённая пара могла в неё уже не входить.

    Порядок записей — сначала находки, потом решение: если durable-контур недоступен,
    повтор запроса идемпотентен (статус уже стоит, запись допишется), а не оставляет
    сохранённое решение без последствия для корпуса.
    """
    deps = dependencies(request)
    principal = request.state.principal
    async with _storage_or_unavailable(request, "Противоречия"):
        findings = await asyncio.to_thread(deps.knowledge.all_findings, _allowed_classes(request))
        reviews = await deps.state.conflict_reviews(limit=MAX_CONFLICT_REVIEWS)
    resolution = await asyncio.to_thread(resolve_for_review, candidate_id, findings)
    if resolution is None:
        raise HTTPException(
            status_code=404,
            detail="Пара больше не считается противоречием в текущем корпусе",
        )
    desired: Literal["confirmed", "dismissed"] = "confirmed" if body.confirmed else "dismissed"
    previous = next((item for item in reviews if item.candidate_id == candidate_id), None)
    if previous is not None and previous.status == desired:
        raise HTTPException(status_code=409, detail="Решение по этой паре уже записано")
    if body.confirmed:
        try:
            async with _storage_or_unavailable(request, "Статус находок"):
                for finding_id in (resolution.left_finding_id, resolution.right_finding_id):
                    await asyncio.to_thread(
                        deps.knowledge.set_finding_status, finding_id, "disputed"
                    )
        except KeyError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
    review = ConflictReview(
        candidate_id=candidate_id,
        status=desired,
        actor_id=principal.id,
        property_name=resolution.candidate.property_name,
        left_finding_id=resolution.left_finding_id,
        right_finding_id=resolution.right_finding_id,
    )
    async with _storage_or_unavailable(request, "Решение по противоречию"):
        await deps.state.record_conflict_review(review)
    await _record_decision(
        request,
        "conflict.reviewed",
        candidate_id,
        metadata={
            "status": review.status,
            "left_finding_id": review.left_finding_id,
            "right_finding_id": review.right_finding_id,
        },
    )
    await _log_audit(request, "conflict.review", candidate_id)
    # Корпус изменился (статус находок) — витрина и закэшированные прогоны по
    # старому состоянию больше не ответ на текущие данные.
    _invalidate_demo_cache(deps)
    return review


@app.get("/api/v1/corpus/stats", tags=["knowledge"], response_model=CorpusStats)
async def get_corpus_stats(request: Request) -> CorpusStats:
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Статистика корпуса"):
        return await asyncio.to_thread(deps.knowledge.corpus_stats)


@app.get(
    "/api/v1/entity-resolution/proposals",
    tags=["knowledge"],
    response_model=list[EntityMergeProposal],
)
async def get_entity_resolution_proposals(
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LIMIT_MAX)] = AUDIT_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
    _: None = Depends(require_permission("proposal:review")),
) -> list[EntityMergeProposal]:
    """Мерж-предложения — вход в экспертный обзор: в ``rationale`` бывают ссылки на
    закрытые источники, поэтому список отдаётся не всем подряд.

    Пагинация та же, что у остальных списков: очередь склеек растёт на каждом
    импорте, а окно с ``X-Total-Count`` позволяет эксперту видеть размер очереди,
    не вытаскивая её целиком.
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Предложения склейки"):
        proposals = await asyncio.to_thread(deps.resolution.list_proposals)
    return _paginated(proposals, limit, offset, response)


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
        async with _storage_or_unavailable(request, "Решение по склейке"):
            proposal = await asyncio.to_thread(
                deps.resolution.review, proposal_id, review.action, reviewer_id=account.id
            )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Merge proposal not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    # Склейка меняет граф (узлы и рёбра ALIAS_OF) — витрина обязана пересобраться.
    _invalidate_demo_cache(deps)
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
    response: Response,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LIMIT_MAX)] = AUDIT_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
    _: None = Depends(require_permission("evaluation:view")),
) -> list[EvaluationRun]:
    """Прогоны оценки из серверного состояния: история качества не обнуляется
    перезапуском процесса, иначе «самооценка платформы» не подтверждается ничем.

    Окно читается хранилищем (``LIMIT``/``OFFSET``), а полное число подходящих
    записей уходит в ``X-Total-Count`` — иначе аналитик видит 50 строк и не
    отличает «больше не было» от «не дочитали». Тело остаётся массивом.
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Прогоны оценки"):
        runs = await deps.state.recent_evaluations(limit=limit, offset=offset)
        total = await deps.state.count_evaluations()
    response.headers["X-Total-Count"] = str(total)
    return runs


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
    async with _storage_or_unavailable(request, "Бенчмарк ранжирования"):
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
    async with _storage_or_unavailable(request, "Статистика корпуса для бенчмарка"):
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
    # Тот же потолок ленты, что у ``/proposals``: предложение, видимое в очереди,
    # обязано быть и решаемым, иначе хвост очереди получал бы 404.
    async with _storage_or_unavailable(request, "Лента предложений"):
        feed = await deps.state.recent_proposals(limit=PROPOSAL_FEED_READ_LIMIT)
        proposal = next((item for item in feed if item.id == proposal_id), None)
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
    async with _storage_or_unavailable(request, "Результат A/B"):
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
    response: Response,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    _: None = Depends(require_permission("proposal:review")),
) -> list[EvolutionExperiment]:
    """Решения по предложениям и A/B-история — экспертный контур, и они durable.

    Полное число экспериментов — в ``X-Total-Count``: страница без offset не
    позволяла отличить «решений больше нет» от «окно обрезано».
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "A/B-эксперименты"):
        experiments = await deps.state.recent_experiments(limit=limit, offset=offset)
        total = await deps.state.count_experiments()
    response.headers["X-Total-Count"] = str(total)
    return experiments


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
            async with _storage_or_unavailable(request, "Замена утверждения"):
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
        # Утверждение переписано — витрина по старому тезису больше не ответ на
        # текущий корпус, поэтому кэш прогона сбрасывается вместе с графом.
        _invalidate_demo_cache(deps)
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
    response: Response,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LIMIT_MAX)] = AUDIT_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
    _: None = Depends(require_permission("proposal:review")),
) -> list[EvolutionProposal]:
    """Список предложений — экспертный контур: в ``change`` живёт текст будущей
    политики агента, и отдавать его всем подряд нельзя (право на обзор —
    ``proposal:review``).

    Лента читается до своего потолка целиком (очередь обрезается на стороне
    серверного состояния) и режется окном: ``X-Total-Count`` тогда — фактическое
    число предложений, а не «сколько влезло в ответ».
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Лента предложений"):
        feed = await deps.state.recent_proposals(limit=PROPOSAL_FEED_READ_LIMIT)
    return _paginated(feed, limit, offset, response)


async def _ab_gate_passed(deps: AppDependencies, proposal_id: UUID) -> bool:
    """Пройдено ли regression A/B для предложения — по серверному состоянию.

    Прежняя проверка читала словарь `EvolutionService`, куда эксперимент больше
    не записывается (результат A/B живёт в `nk_experiments`), и принятие
    prompt/rule-предложения было невозможно в принципе.

    Лента читается до своего потолка целиком: с дефолтным окном промоушен,
    сделанный на длинной истории экспериментов, переставал «засчитываться», и
    эксперту показывался ложный 409 «ворота не пройдены».
    """
    experiments = await deps.state.recent_experiments(limit=MAX_EXPERIMENTS)
    return any(
        item.proposal_id == proposal_id and item.decision == "promote"
        for item in experiments
    )


def _review_transition_allowed(status: str, accepted: bool) -> bool:
    """Разрешён ли переход по предложению — детерминированно, по серверному статусу.

    Версий у предложения нет, поэтому второе «accept» молча перезаписывало первое
    решение и правку теряло. Разрешены первый обзор (``proposed`` → любое решение)
    и явный откат принятого (``accepted`` → отказ); отказ повторно и повторный
    отказ после отказа — уже не решение, а конфликт (409).
    """
    if status == "proposed":
        return True
    return status == "accepted" and not accepted


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
    # Потолок чтения — общий со ``/proposals`` (см. PROPOSAL_FEED_READ_LIMIT).
    async with _storage_or_unavailable(request, "Лента предложений"):
        known = await deps.state.recent_proposals(limit=PROPOSAL_FEED_READ_LIMIT)
        current = next((item for item in known if item.id == proposal_id), None)
        if current is None:
            raise HTTPException(status_code=404, detail="Proposal not found")
        if not _review_transition_allowed(current.status, review_request.accepted):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Предложение уже рассмотрено (статус «{current.status}»): "
                    "повторное решение отклонено, чтобы не потерять прежнюю правку."
                ),
            )
        if review_request.accepted and not await _ab_gate_passed(deps, proposal_id):
            raise HTTPException(status_code=409, detail="Proposal не прошёл regression A/B gate")
        # Зеркало сервиса поднимается из серверного состояния, но обязано жить и
        # для записи, сделанной в обход него (другой процесс, обрезанное
        # зеркало): решение принимается по durable-копии, а не по её отсутствию.
        deps.evolution.restore(current)
        reviewed = deps.evolution.review(proposal_id, review_request.accepted)
        await deps.state.record_proposal(reviewed)
    if reviewed.status == "accepted":
        await _notify(
            deps,
            "proposal.accepted",
            f"Предложение {proposal_id} принято и активировано.",
        )
    # Пересборка нужна любому решению, а не только принятию: отклонённое ранее
    # принятое предложение оставалось бы в ``CANDIDATE POLICY`` промпта агента, и
    # отказ эксперта не менял бы его поведение.
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
    response: Response,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LIMIT_MAX)] = AUDIT_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
    correlation_id: str | None = None,
    _: None = Depends(require_permission("audit:read")),
) -> list[AuditEvent]:
    """Журнал из серверного состояния: с фильтром по ``correlation_id`` — тем
    самым, что вернулся клиенту в ответе и в заголовке ``X-Correlation-Id``.

    ``X-Total-Count`` — полное число подходящих актов (по тому же фильтру, что и
    чтение), а не длина страницы: иначе «50 строк» не отличить от «дочитали всё».
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Журнал аудита"):
        events = await deps.state.recent_audit(
            limit=limit, offset=offset, correlation_id=correlation_id
        )
        total = await deps.state.count_audit(correlation_id=correlation_id)
    response.headers["X-Total-Count"] = str(total)
    return events


@app.get(
    "/api/v1/decisions",
    tags=["acl"],
    response_model=list[ExpertDecision],
)
async def get_expert_decisions(
    request: Request,
    response: Response,
    account: CurrentAccount,
    limit: Annotated[int, Query(ge=1, le=AUDIT_LIMIT_MAX)] = AUDIT_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ExpertDecision]:
    """История решений собственного аккаунта — из серверного состояния.

    Выдача без фильтра по ``actor_id`` показала бы чужие решения и чужие
    ``query_id``; журнал же всех экспертов закрыт правом ``audit:read`` через
    ``/audit``. Пока read-пути не было, запись решений существовала только как
    обещание «история действий сохранена», которое нечем было проверить.

    Окно читается хранилищем (``LIMIT``/``OFFSET``), а полное число решений
    того же аккаунта уходит в ``X-Total-Count``: без offset список из 200 строк
    при потолке в 5000 записей не отличал «решений больше нет» от «не дочитали»
    — та же молчаливая обрезка, которую продукт запрещает на прочих лентах.
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Решения экспертов"):
        decisions = await deps.state.recent_decisions(
            limit=limit, offset=offset, actor_id=account.id
        )
        total = await deps.state.count_decisions(actor_id=account.id)
    response.headers["X-Total-Count"] = str(total)
    return decisions


@app.get("/api/v1/me/activity", tags=["acl"], response_model=list[ActivityEntry])
async def get_my_activity(
    account: CurrentAccount,
    request: Request,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=ME_ACTIVITY_LIMIT_MAX)] = ME_ACTIVITY_LIMIT_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ActivityEntry]:
    """Журнал действий собственного аккаунта — он же «что я здесь делал».

    ``/audit`` закрыт правом ``audit:read`` и отдаёт акты всех, поэтому для
    профиля он непригоден; ``actor_id`` берут только из серверной сессии, никогда
    из запрос. Ответ — проекция ``ActivityEntry`` без ``metadata``: в метаданных
    лежат фрагменты вопросов и служебные ссылки, которым не место в списке
    профиля.

    ``X-Total-Count`` считает только свои акты того же фильтра, что и чтение, —
    чужой актор в полную численность не попадает.
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Журнал аккаунта"):
        events = await deps.state.recent_audit(
            limit=limit, offset=offset, actor_id=account.id
        )
        total = await deps.state.count_audit(actor_id=account.id)
    response.headers["X-Total-Count"] = str(total)
    return [
        ActivityEntry(
            action=event.action,
            actor_id=event.actor_id,
            object_id=event.object_id,
            outcome=event.outcome,
            created_at=event.created_at,
        )
        for event in events
    ]


@app.get("/api/v1/me/usage", tags=["acl"], response_model=LlmUsageSummary)
async def get_my_usage(
    account: CurrentAccount,
    request: Request,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> LlmUsageSummary:
    """Расход модели собственным аккаунтом за окно — «сколько стоили мои вопросы».

    Права не требуются: виден только свой аккаунт, ``account_id`` берётся из
    серверной сессии. Сводка по всем аккаунтам остаётся в ``nk_llm_usage``
    (SQL-агрегат ``usage_by_account``); отдельной витрины для администратора
    намеренно нет — под неё пришлось бы выдумывать разрешение, которого в
    продукте нет (``audit:read`` про акты, а не про расход).
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Расход модели"):
        rows = await deps.state.usage_by_account(since=datetime.now(UTC) - timedelta(days=days))
    row = next((item for item in rows if item.account_id == account.id), None)
    return LlmUsageSummary(
        window_days=days,
        runs=row.calls if row else 0,
        failed_runs=row.failed_calls if row else 0,
        prompt_tokens=row.prompt_tokens if row else 0,
        completion_tokens=row.completion_tokens if row else 0,
    )


@app.post("/api/v1/compare", tags=["comparison"], response_model=ComparisonTable)
async def compare_entities(
    comparison: ComparisonRequest,
    request: Request,
    _: None = Depends(require_permission("export:run")),
) -> ComparisonTable:
    deps = dependencies(request)
    # Чтение корпуса — блокирующий (Neo4j/ES), из event loop убран. Сбой
    # хранилища здесь так же честен 503, как на других read-путях: сравнение
    # строится по находкам, и без них это «сервис не отвечает», а не «нет данных».
    async with _storage_or_unavailable(request, "находки для сравнения"):
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
    async with _storage_or_unavailable(request, "Серверная копия ответа"):
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
    async with _storage_or_unavailable(request, "История утверждения"):
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
    async with _storage_or_unavailable(request, "Панель состояния"):
        stats, findings = await asyncio.to_thread(
            lambda: (deps.knowledge.corpus_stats(), deps.knowledge.all_findings(allowed))
        )
        recent = await deps.state.recent_audit(
            limit=10, actor_id=None if shared_journal else account.id
        )
    gaps, omitted = await asyncio.to_thread(_coverage_gaps, findings)
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
    response: Response,
    limit: Annotated[int, Query(ge=1, le=NOTIFICATION_FEED_LIMIT)] = NOTIFICATION_FEED_LIMIT,
) -> list[Notification]:
    """Лента из серверного состояния: в postgres-контуре она переживает перезапуск.

    Доставка — только опросом этого эндпоинта: push-канала в продукте нет, а
    «подписка на тему» обещала бы рассылку, которую выполнять нечем.

    Смещения здесь нет намеренно (это фид последних ``NOTIFICATION_FEED_LIMIT``,
    а не лента), но ``X-Total-Count`` отдаёт полное число записей хранилища:
    читатель обязан отличить «событий больше не было» от «старые вытеснены
    потолком ленты».
    """
    deps = dependencies(request)
    async with _storage_or_unavailable(request, "Лента уведомлений"):
        items = await deps.state.recent_notifications(limit=limit)
        total = await deps.state.count_notifications()
    response.headers["X-Total-Count"] = str(total)
    return items
