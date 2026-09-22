from __future__ import annotations

import threading
from collections import defaultdict, deque

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
LLM_SLOTS = Gauge(
    "mindai_llm_slots",
    "Обращения к LLM: in_flight — в полёте, waiting — ждут семафор, limit — ёмкость",
    ["state"],
)
LLM_QUEUE_WAITS = Counter(
    "mindai_llm_queue_waits_total",
    "Обращения к LLM, которым пришлось ждать освобождения слота провайдера",
)


class AgentMetricsRegistry:
    """Потокобезопасный реестр агентных и LLM-метрик.

    Различает попытки (retry) и прогоны (run): в метрики агента попадает только
    исход последней попытки, а повторы считаются на уровне обращений к модели —
    иначе success_rate смешивался бы с retry-политикой.
    """

    def __init__(self) -> None:
        self._durations: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=1000))
        self._successes: dict[str, int] = defaultdict(int)
        self._failures: dict[str, int] = defaultdict(int)
        self._llm_calls: dict[str, int] = defaultdict(int)
        self._llm_failures: dict[str, int] = defaultdict(int)
        self._llm_retries: dict[str, int] = defaultdict(int)
        self._llm_duration: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=1000))
        self._prompt_tokens = 0
        self._completion_tokens = 0
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
        LLM_CALLS.labels(schema=schema, status="success" if success else "failure").inc()
        LLM_DURATION.labels(schema=schema).observe(duration_ms / 1000)
        if prompt_tokens:
            LLM_TOKENS.labels(kind="prompt").inc(prompt_tokens)
        if completion_tokens:
            LLM_TOKENS.labels(kind="completion").inc(completion_tokens)

    def observe_retrieval_failure(self, component: str) -> None:
        RETRIEVAL_FAILURES.labels(component=component).inc()

    def observe_graph_cache(self, hit: bool) -> None:
        GRAPH_CACHE_HITS.labels(result="hit" if hit else "miss").inc()

    def observe_cache_age(self, age_seconds: float) -> None:
        CACHE_AGE.set(age_seconds)

    def snapshot(self) -> AgentMetricsResponse:
        with self._lock:
            agents = sorted(set(self._durations) | set(self._successes) | set(self._failures))
            snapshots = [self._snapshot_agent(agent) for agent in agents]
            llm_schemas = sorted(self._llm_calls)
            llm_snapshots = [
                LlmMetricSnapshot(
                    schema_name=schema,
                    calls=self._llm_calls[schema],
                    failures=self._llm_failures[schema],
                    retries=self._llm_retries[schema],
                    average_duration_ms=round(self._average(self._llm_duration[schema]), 1),
                    p95_duration_ms=round(
                        self._percentile(sorted(self._llm_duration[schema]), 0.95), 1
                    ),
                )
                for schema in llm_schemas
            ]
            totals = {
                "prompt": self._prompt_tokens,
                "completion": self._completion_tokens,
            }
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
            total_prompt_tokens=totals["prompt"],
            total_completion_tokens=totals["completion"],
            agent_runs_active=admission["active"],
            agent_runs_limit=admission["limit"],
            agent_runs_refused=admission["refused"],
            llm_calls_in_flight=admission["llm_in_flight"],
            llm_calls_waiting=admission["llm_waiting"],
            llm_slots=admission["llm_limit"],
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
