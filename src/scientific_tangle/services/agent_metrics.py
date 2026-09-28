from __future__ import annotations

import threading
from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass

from prometheus_client import Counter, Gauge, Histogram

from scientific_tangle.domain.contracts import (
    AgentMetricSnapshot,
    AgentMetricsResponse,
    LlmMetricSnapshot,
)

AGENT_RUNS = Counter(
    "mindai_agent_runs_total",
    "Количество успешных финальных прогонов узлов агентного графа",
    ["agent", "status"],
)
AGENT_DURATION = Histogram(
    "mindai_agent_duration_seconds",
    "Длительность финального выполнения узла агентного графа",
    ["agent"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
LLM_CALLS = Counter(
    "mindai_llm_calls_total",
    "Обращение к LLM провайдеру",
    ["schema", "status"],
)
LLM_DURATION = Histogram(
    "mindai_llm_duration_seconds",
    "Полный complete_model: ожидание слота плюс все транспортные и repair-попытки",
    ["schema"],
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 60, 90, 180),
)
LLM_TOKENS = Counter(
    "mindai_llm_tokens_total",
    "Токены, потреблённые LLM",
    ["kind"],
)
LLM_RETRIES = Counter(
    "mindai_llm_retries_total",
    "Schema-repair повторы обращения к модели: ответ не прошёл валидацию с первого раза",
    ["schema"],
)
LLM_CACHE = Counter(
    "mindai_llm_cache_total",
    "Обращения к модели, закрытые кэшем structured output",
    ["result"],
)
LLM_CANCELLED = Counter(
    "mindai_llm_cancelled_total",
    "Обращения к модели, оборванные отменой клиента или агентным дедлайном",
    ["schema"],
)
LLM_TIMEOUTS = Counter(
    "mindai_llm_timeouts_total",
    "Обращения к модели, не уложившиеся в таймаут провайдера",
    ["schema"],
)
RETRIEVAL_FAILURES = Counter(
    "mindai_retrieval_failures_total",
    "Деградации retrieval-контура (эмбеддинги, rerank, community detection)",
    ["component"],
)
GRAPH_CACHE_HITS = Counter("mindai_graph_cache_total", "Попадание в кэш графа", ["result"])
CACHE_AGE = Gauge("mindai_full_graph_cache_age_seconds", "Возраст кэша полного графа")
AGENT_RUN_SLOTS = Gauge(
    "mindai_agent_run_slots",
    "Слоты агентного контура: active — занято, limit — потолок приёма",
    ["state"],
)
AGENT_RUN_REFUSALS = Counter(
    "mindai_agent_run_refusals_total",
    "Отказы приёма агентного запроса (429) из-за исчерпанного потолка слотов",
)
AGENT_DEGRADATIONS = Counter(
    "mindai_agent_degradations_total",
    "Деградации агентного ответа: короткий код причины, не текст для клиента",
    ["reason"],
)
LLM_SLOTS = Gauge(
    "mindai_llm_slots",
    "Обращения к LLM: in_flight — в полёте, waiting — ждут семафор, limit — ёмкость",
    ["state"],
)
LLM_QUEUE_WAITS = Counter(
    "mindai_llm_queue_waits_total",
    "Обращения к LLM, которым пришлось ждать освобождения слота провайдера",
)


@dataclass(slots=True)
class LlmRunUsage:
    """Расход модели в границах одного агентного запроса.

    Глобальных счётчиков процесса хватает на «как живёт сервис», но не на вопрос
    «сколько стоили исследования этого аналитика»: границу открывает вызывающий
    код перед прогоном графа (``begin_llm_usage``) и читает накопленное в финале
    ответа (``current_llm_usage``). Нули вне границы означают «расход не
    измерялся», а не «запрос был бесплатным».
    """

    calls: int = 0
    failures: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0


# Накопитель мутируется по ссылке: провайдер вызывает ``observe_llm`` из той же
# цепочки await, что и прогон графа, а child-корутина видит тот же объект.
_RUN_USAGE: ContextVar[LlmRunUsage | None] = ContextVar("mindai_llm_run_usage", default=None)


def begin_llm_usage() -> LlmRunUsage:
    """Открывает границы расхода модели одного запроса и возвращает накопитель."""
    usage = LlmRunUsage()
    _RUN_USAGE.set(usage)
    return usage


def current_llm_usage() -> LlmRunUsage:
    """Расход, накопленный в текущем запросе; вне границы — нули."""
    return _RUN_USAGE.get() or LlmRunUsage()


class AgentMetricsRegistry:
    """Потокобезопасный реестр агентных и LLM-метрик.

    Различает попытки (retry) и прогоны (run): в метрики агента попадает только
    исход последней попытки, а повторы считаются на уровне обращений к модели —
    иначе success_rate смешивался бы с retry-политикой.

    Каждый наблюдатель сначала обновляет внутреннее состояние (из него собирается
    ``/api/v1/agents/metrics``), а затем зеркалит событие в Prometheus: счётчик,
    существующий только в одном из двух контуров, неотличим от потерянного.
    """

    def __init__(self) -> None:
        self._durations: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=1000))
        self._successes: dict[str, int] = defaultdict(int)
        self._failures: dict[str, int] = defaultdict(int)
        self._llm_calls: dict[str, int] = defaultdict(int)
        self._llm_failures: dict[str, int] = defaultdict(int)
        self._llm_retries: dict[str, int] = defaultdict(int)
        self._llm_cancelled: dict[str, int] = defaultdict(int)
        self._llm_timeouts: dict[str, int] = defaultdict(int)
        self._llm_duration: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=1000))
        self._prompt_tokens = 0
        self._completion_tokens = 0
        # Кэш structured output: попадание — сбережённый вызов модели, поэтому
        # считается на пути обращения к модели, а не на графе.
        self._llm_cache_hits = 0
        self._llm_cache_misses = 0
        self._llm_queue_waits = 0
        self._retrieval_failures: dict[str, int] = defaultdict(int)
        self._degradations: dict[str, int] = defaultdict(int)
        # Ёмкость приёма: слоты агентного контура и LLM-семафор провайдера.
        # Отдельные поля, а не производные от счётчиков — saturation обязана
        # читаться из метрик без пересчёта на стороне Grafana.
        self._agent_limit = 0
        self._agent_active = 0
        self._agent_refused = 0
        self._llm_limit = 0
        self._llm_in_flight = 0
        self._llm_waiting = 0
        self._lock = threading.Lock()

    def set_agent_capacity(self, limit: int) -> None:
        with self._lock:
            self._agent_limit = limit
        AGENT_RUN_SLOTS.labels(state="limit").set(limit)

    def observe_agent_slots(self, active: int, limit: int) -> None:
        with self._lock:
            self._agent_active = active
            self._agent_limit = limit
        AGENT_RUN_SLOTS.labels(state="active").set(active)
        AGENT_RUN_SLOTS.labels(state="limit").set(limit)

    def observe_admission_refusal(self) -> None:
        with self._lock:
            self._agent_refused += 1
        AGENT_RUN_REFUSALS.inc()

    def set_llm_capacity(self, limit: int) -> None:
        with self._lock:
            self._llm_limit = limit
        LLM_SLOTS.labels(state="limit").set(limit)

    def observe_llm_queue(self, *, in_flight: int, waiting: int, limit: int) -> None:
        """Занятость LLM-семафора абсолютными значениями: очередь видна заранее.

        Провайдер вызывается до и после ожидания слота, поэтому дельты здесь
        только расходились бы с реальным числом ждущих.
        """
        with self._lock:
            self._llm_limit = limit
            self._llm_in_flight = in_flight
            self._llm_waiting = waiting
        LLM_SLOTS.labels(state="in_flight").set(in_flight)
        LLM_SLOTS.labels(state="waiting").set(waiting)
        LLM_SLOTS.labels(state="limit").set(limit)

    def observe_llm_queue_wait(self) -> None:
        """Одно ожидание слота — один инкремент: счётчик ждущих обращений,
        а не число обновлений gauge."""
        with self._lock:
            self._llm_queue_waits += 1
        LLM_QUEUE_WAITS.inc()

    def observe(self, agent: str, duration_ms: float, success: bool) -> None:
        """Фиксирует исход узла после всех его retry-попыток."""
        with self._lock:
            self._durations[agent].append(duration_ms)
            if success:
                self._successes[agent] += 1
            else:
                self._failures[agent] += 1
        status = "success" if success else "failure"
        AGENT_RUNS.labels(agent=agent, status=status).inc()
        AGENT_DURATION.labels(agent=agent).observe(duration_ms / 1000)

    def observe_llm_retry(self, schema: str) -> None:
        """Засчитывает schema-repair попытку: ответ не прошёл валидацию с первого раза.

        Повтор относится к вызову модели, а не к агенту: агент делает несколько
        обращений, и приписать повтор одному узлу было бы выдумкой.
        """
        with self._lock:
            self._llm_retries[schema] += 1
        usage = _RUN_USAGE.get()
        if usage is not None:
            usage.retries += 1
        LLM_RETRIES.labels(schema=schema).inc()

    def observe_llm_cache(self, hit: bool) -> None:
        """Одно обращение к модели закрыто кэшем structured output или нет.

        Промах по кэшу равен одному оплаченному вызову GigaChat, поэтому доля
        попаданий читается только из пары hit/miss, а не из «успешности» агента.
        """
        with self._lock:
            if hit:
                self._llm_cache_hits += 1
            else:
                self._llm_cache_misses += 1
        LLM_CACHE.labels(result="hit" if hit else "miss").inc()

    def observe_llm_cancelled(self, schema_name: str) -> None:
        """Вызов оборван отменой клиента (обрыв SSE) или агентным дедлайном.

        Это не отказ провайдера: смешивать его с
        ``mindai_llm_calls_total{status="failure"}`` нельзя — доля отказов
        модели стала бы зависеть от того, закрыл ли пользователь вкладку.
        """
        with self._lock:
            self._llm_cancelled[schema_name] += 1
        LLM_CANCELLED.labels(schema=schema_name).inc()

    def observe_llm_timeout(self, schema: str) -> None:
        """Вызов не уложился в таймаут провайдера (``httpx.TimeoutException``).

        Отказывается от repair-повтора: оборванный по времени ответ чинится
        ``GIGACHAT_TIMEOUT_SECONDS``, а не подсказкой модели.
        """
        with self._lock:
            self._llm_timeouts[schema] += 1
        LLM_TIMEOUTS.labels(schema=schema).inc()

    def observe_degradation(self, reason: str) -> None:
        """Деградация ответа по короткой код-причине (``TimeoutError``, ``no_answer``).

        Свободный текст ``degradation_reasons`` сюда не приходит: он меняется от
        узла к узлу и раздул бы cardinality метки.
        """
        with self._lock:
            self._degradations[reason] += 1
        AGENT_DEGRADATIONS.labels(reason=reason).inc()

    def observe_llm(
        self,
        schema: str,
        duration_ms: float,
        *,
        success: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        with self._lock:
            self._llm_calls[schema] += 1
            self._llm_duration[schema].append(duration_ms)
            if not success:
                self._llm_failures[schema] += 1
            self._prompt_tokens += prompt_tokens
            self._completion_tokens += completion_tokens
            usage = _RUN_USAGE.get()
            if usage is not None:
                usage.calls += 1
                usage.latency_ms += duration_ms
                usage.prompt_tokens += prompt_tokens
                usage.completion_tokens += completion_tokens
                if not success:
                    usage.failures += 1
        LLM_CALLS.labels(schema=schema, status="success" if success else "failure").inc()
        LLM_DURATION.labels(schema=schema).observe(duration_ms / 1000)
        if prompt_tokens:
            LLM_TOKENS.labels(kind="prompt").inc(prompt_tokens)
        if completion_tokens:
            LLM_TOKENS.labels(kind="completion").inc(completion_tokens)

    def observe_retrieval_failure(self, component: str) -> None:
        with self._lock:
            self._retrieval_failures[component] += 1
        RETRIEVAL_FAILURES.labels(component=component).inc()

    def observe_graph_cache(self, hit: bool) -> None:
        GRAPH_CACHE_HITS.labels(result="hit" if hit else "miss").inc()

    def observe_cache_age(self, age_seconds: float) -> None:
        CACHE_AGE.set(age_seconds)

    def snapshot(self) -> AgentMetricsResponse:
        with self._lock:
            agents = sorted(set(self._durations) | set(self._successes) | set(self._failures))
            snapshots = [self._snapshot_agent(agent) for agent in agents]
            # Схема попадает в срез, даже если по ней не было засчитано ни одного
            # вызова: отмена и таймаут обрывают обращение до финала ``observe_llm``.
            schemas = sorted(
                set(self._llm_calls)
                | set(self._llm_retries)
                | set(self._llm_cancelled)
                | set(self._llm_timeouts)
            )
            llm_snapshots = [self._snapshot_llm(schema) for schema in schemas]
            prompt_tokens = self._prompt_tokens
            completion_tokens = self._completion_tokens
            retries = dict(self._llm_retries)
            cancelled = dict(self._llm_cancelled)
            timeouts = dict(self._llm_timeouts)
            cache_hits = self._llm_cache_hits
            cache_misses = self._llm_cache_misses
            queue_waits = self._llm_queue_waits
            retrieval_failures = dict(self._retrieval_failures)
            degradations = dict(self._degradations)
            admission = {
                "active": self._agent_active,
                "limit": self._agent_limit,
                "refused": self._agent_refused,
                "llm_in_flight": self._llm_in_flight,
                "llm_waiting": self._llm_waiting,
                "llm_limit": self._llm_limit,
            }
        return AgentMetricsResponse(
            agents=snapshots,
            llm=llm_snapshots,
            total_prompt_tokens=prompt_tokens,
            total_completion_tokens=completion_tokens,
            agent_runs_active=admission["active"],
            agent_runs_limit=admission["limit"],
            agent_runs_refused=admission["refused"],
            llm_calls_in_flight=admission["llm_in_flight"],
            llm_calls_waiting=admission["llm_waiting"],
            llm_slots=admission["llm_limit"],
            llm_retries_total=sum(retries.values()),
            llm_retries_by_schema=retries,
            llm_cancelled_total=sum(cancelled.values()),
            llm_cancelled_by_schema=cancelled,
            llm_timeouts_total=sum(timeouts.values()),
            llm_timeouts_by_schema=timeouts,
            llm_cache_hits=cache_hits,
            llm_cache_misses=cache_misses,
            # Доля попаданий считается здесь, а не на стороне Grafana: ноль
            # обращений к кэшу — это ноль, а не деление на ноль.
            llm_cache_hit_rate=round(cache_hits / (cache_hits + cache_misses), 3)
            if cache_hits or cache_misses
            else 0,
            llm_queue_waits=queue_waits,
            retrieval_failures_by_component=retrieval_failures,
            degradations_by_reason=degradations,
        )

    def _snapshot_agent(self, agent: str) -> AgentMetricSnapshot:
        durations = sorted(self._durations[agent])
        successes = self._successes[agent]
        failures = self._failures[agent]
        calls = successes + failures
        return AgentMetricSnapshot(
            agent=agent,
            calls=calls,
            successes=successes,
            failures=failures,
            success_rate=round(successes / calls, 3) if calls else 0,
            average_duration_ms=round(self._average(durations), 1),
            p50_duration_ms=round(self._percentile(durations, 0.5), 1),
            p95_duration_ms=round(self._percentile(durations, 0.95), 1),
            p99_duration_ms=round(self._percentile(durations, 0.99), 1),
        )

    def _snapshot_llm(self, schema: str) -> LlmMetricSnapshot:
        durations = sorted(self._llm_duration[schema])
        return LlmMetricSnapshot(
            schema_name=schema,
            calls=self._llm_calls[schema],
            failures=self._llm_failures[schema],
            # Schema-repair попытки: повтор был у обращения к модели, а не у агента.
            retries=self._llm_retries[schema],
            cancelled=self._llm_cancelled[schema],
            timeouts=self._llm_timeouts[schema],
            average_duration_ms=round(self._average(durations), 1),
            p50_duration_ms=round(self._percentile(durations, 0.5), 1),
            p95_duration_ms=round(self._percentile(durations, 0.95), 1),
            p99_duration_ms=round(self._percentile(durations, 0.99), 1),
        )

    @staticmethod
    def _average(values: deque[float] | list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0
        index = min(round((len(values) - 1) * percentile), len(values) - 1)
        return values[index]


agent_metrics = AgentMetricsRegistry()
