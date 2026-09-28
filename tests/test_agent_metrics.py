"""Реестр метрик агентного контура: обещанное обязано быть наблюдаемым.

Живого GigaChat в тестовом контуре нет, а retry, отмена и таймаут рождаются
только в нём. Поэтому проверяется сам реестр: что каждый наблюдатель доходит до
Prometheus (иначе мониторинг молчит при порче structured output), что перцентили
считаются, а не копируются, и что ``/api/v1/agents/metrics`` остаётся
обратимо совместимым. Расход модели на аккаунт — вторая половина теста: он
живёт в серверном состоянии, а не в счётчиках процесса.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    AgentMetricSnapshot,
    AgentMetricsResponse,
    LlmMetricSnapshot,
)
from scientific_tangle.services.agent_metrics import (
    AgentMetricsRegistry,
    LlmRunUsage,
    begin_llm_usage,
    current_llm_usage,
)
from scientific_tangle.services.durable_state import (
    InMemoryDurableState,
    LlmAccountUsage,
    LlmUsageRecord,
    PostgresDurableState,
)

# Схема и агент в каждом тесте свои: Prometheus-реестр один на процесс, и
# инкременты других тестов иначе попали бы в assertions.
SCHEMA = "RetryProbeBundle"
AGENT = "probe_reasoner"


def sample(name: str, labels: dict[str, str]) -> float:
    """Значение метрики Prometheus; отсутствующий счётчик читается как ноль."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def usage() -> Iterator[LlmRunUsage]:
    """Граница расхода вокруг теста: накопитель не перетекает между тестами."""
    yield begin_llm_usage()
    begin_llm_usage()


def test_llm_retry_reaches_prometheus_and_the_api_snapshot() -> None:
    """retry без Prometheus-метрики — невидимый диагноз порчи structured output."""
    registry = AgentMetricsRegistry()
    before = sample("mindai_llm_retries_total", {"schema": SCHEMA})

    registry.observe_llm_retry(SCHEMA)
    registry.observe_llm_retry(SCHEMA)

    assert sample("mindai_llm_retries_total", {"schema": SCHEMA}) - before == 2
    snapshot = registry.snapshot()
    assert snapshot.llm_retries_by_schema == {SCHEMA: 2}
    assert snapshot.llm_retries_total == 2
    # Схема видна в срезе даже без засчитанных вызовов: иначе retry «испаряется».
    assert [item.schema_name for item in snapshot.llm] == [SCHEMA]
    assert snapshot.llm[0].retries == 2


def test_llm_cache_reports_hits_misses_and_rate() -> None:
    registry = AgentMetricsRegistry()
    hits = sample("mindai_llm_cache_total", {"result": "hit"})
    misses = sample("mindai_llm_cache_total", {"result": "miss"})

    for hit in (True, True, True, False):
        registry.observe_llm_cache(hit)

    assert sample("mindai_llm_cache_total", {"result": "hit"}) - hits == 3
    assert sample("mindai_llm_cache_total", {"result": "miss"}) - misses == 1
    snapshot = registry.snapshot()
    assert (snapshot.llm_cache_hits, snapshot.llm_cache_misses) == (3, 1)
    assert snapshot.llm_cache_hit_rate == 0.75


def test_cache_hit_rate_without_traffic_is_zero_not_division_error() -> None:
    snapshot = AgentMetricsRegistry().snapshot()
    assert snapshot.llm_cache_hit_rate == 0
    assert snapshot.llm_cache_hits == 0


def test_cancelled_call_is_counted_separately_from_model_failure() -> None:
    """Отмена клиента — не отказ модели: смешивание портило бы долю отказов."""
    registry = AgentMetricsRegistry()
    failures_before = sample(
        "mindai_llm_calls_total", {"schema": SCHEMA, "status": "failure"}
    )
    cancelled_before = sample("mindai_llm_cancelled_total", {"schema": SCHEMA})

    registry.observe_llm_cancelled(SCHEMA)

    assert sample("mindai_llm_cancelled_total", {"schema": SCHEMA}) - cancelled_before == 1
    assert sample("mindai_llm_calls_total", {"schema": SCHEMA, "status": "failure"}) == (
        failures_before
    )
    snapshot = registry.snapshot()
    assert snapshot.llm_cancelled_by_schema == {SCHEMA: 1}
    assert snapshot.llm_cancelled_total == 1
    assert snapshot.llm[0].cancelled == 1
    assert snapshot.llm[0].failures == 0


def test_timeout_is_observed_as_a_timeout() -> None:
    registry = AgentMetricsRegistry()
    before = sample("mindai_llm_timeouts_total", {"schema": SCHEMA})

    registry.observe_llm_timeout(SCHEMA)

    assert sample("mindai_llm_timeouts_total", {"schema": SCHEMA}) - before == 1
    snapshot = registry.snapshot()
    assert snapshot.llm_timeouts_by_schema == {SCHEMA: 1}
    assert snapshot.llm[0].timeouts == 1


def test_degradation_counts_short_reason_codes_only() -> None:
    registry = AgentMetricsRegistry()
    before = sample("mindai_agent_degradations_total", {"reason": "TimeoutError"})

    registry.observe_degradation("TimeoutError")

    assert sample("mindai_agent_degradations_total", {"reason": "TimeoutError"}) - before == 1
    assert registry.snapshot().degradations_by_reason == {"TimeoutError": 1}


def test_queue_waits_and_retrieval_failures_reach_the_response() -> None:
    registry = AgentMetricsRegistry()
    component = f"probe-embedding-{uuid4().hex[:8]}"
    before = sample("mindai_llm_queue_waits_total", {})

    registry.observe_llm_queue_wait()
    registry.observe_retrieval_failure(component)

    assert sample("mindai_llm_queue_waits_total", {}) - before == 1
    assert sample("mindai_retrieval_failures_total", {"component": component}) == 1
    snapshot = registry.snapshot()
    assert snapshot.llm_queue_waits == 1
    assert snapshot.retrieval_failures_by_component == {component: 1}


def _hundred_durations(registry: AgentMetricsRegistry, *, as_llm: bool) -> None:
    for value in range(1, 101):
        if as_llm:
            registry.observe_llm(SCHEMA, float(value), success=True)
        else:
            registry.observe(AGENT, float(value), True)


def test_agent_p99_is_measured_and_differs_from_p95() -> None:
    registry = AgentMetricsRegistry()
    _hundred_durations(registry, as_llm=False)

    agent = next(item for item in registry.snapshot().agents if item.agent == AGENT)
    assert (agent.p50_duration_ms, agent.p95_duration_ms, agent.p99_duration_ms) == (
        51.0,
        95.0,
        99.0,
    )
    assert agent.p99_duration_ms > agent.p95_duration_ms


def test_llm_p50_and_p99_are_not_copies_of_p95() -> None:
    registry = AgentMetricsRegistry()
    _hundred_durations(registry, as_llm=True)

    llm = registry.snapshot().llm[0]
    assert llm.p50_duration_ms == 51.0
    assert llm.p95_duration_ms == 95.0
    assert llm.p99_duration_ms == 99.0
    assert llm.average_duration_ms == 50.5


def test_percentiles_of_an_empty_registry_are_zero() -> None:
    snapshot = AgentMetricsRegistry().snapshot()
    assert snapshot.agents == []
    assert snapshot.llm == []
    assert snapshot.llm_retries_by_schema == {}


def test_run_usage_attributes_tokens_to_one_request(usage: LlmRunUsage) -> None:
    """Глобальный счётчик токенов процесса не отвечает на «сколько стоил вопрос»."""
    registry = AgentMetricsRegistry()
    registry.observe_llm(SCHEMA, 120, success=True, prompt_tokens=900, completion_tokens=60)
    registry.observe_llm(SCHEMA, 80, success=False, prompt_tokens=400, completion_tokens=10)
    registry.observe_llm_retry(SCHEMA)

    assert usage.calls == 2
    assert usage.failures == 1
    assert usage.retries == 1
    assert usage.prompt_tokens == 1300
    assert usage.completion_tokens == 70
    assert usage.latency_ms == 200
    assert current_llm_usage() is usage


def test_next_boundary_starts_from_zero(usage: LlmRunUsage) -> None:
    registry = AgentMetricsRegistry()
    registry.observe_llm(SCHEMA, 10, success=True, prompt_tokens=5, completion_tokens=5)
    assert (usage.calls, usage.prompt_tokens, usage.completion_tokens) == (1, 5, 5)

    fresh = begin_llm_usage()
    assert (fresh.calls, fresh.prompt_tokens, fresh.completion_tokens) == (0, 0, 0)


def test_usage_outside_any_boundary_is_zero_not_invented() -> None:
    """Вне границы накопителя расхода нет — и придумывать его реестр не вправе."""
    empty = contextvars.Context().run(current_llm_usage)
    assert empty == LlmRunUsage()


def test_response_still_constructs_from_old_style_snapshots() -> None:
    """Старые вызовы конструктора (и старые JSON-тела) обязаны оставаться валидными."""
    response = AgentMetricsResponse(
        agents=[
            AgentMetricSnapshot(
                agent="reasoner",
                calls=3,
                successes=2,
                failures=1,
                success_rate=0.667,
                average_duration_ms=120.5,
                p50_duration_ms=100.0,
                p95_duration_ms=310.0,
            )
        ],
        llm=[
            LlmMetricSnapshot(
                schema_name="PlanningBundle",
                calls=2,
                failures=0,
                average_duration_ms=200.0,
                p95_duration_ms=410.0,
            )
        ],
        total_prompt_tokens=1000,
    )
    assert response.agents[0].p99_duration_ms == 0
    assert response.llm[0].p99_duration_ms == 0
    assert response.llm_cache_hit_rate == 0
    assert response.llm_retries_by_schema == {}
    assert response.llm_cancelled_by_schema == {}
    assert response.degradations_by_reason == {}

    revived = AgentMetricsResponse.model_validate(json.loads(response.model_dump_json()))
    assert revived == response


def test_snapshot_carries_every_new_field_through_json() -> None:
    registry = AgentMetricsRegistry()
    registry.observe_llm(SCHEMA, 300, success=True, prompt_tokens=70, completion_tokens=20)
    registry.observe_llm_retry(SCHEMA)
    registry.observe_llm_cache(True)
    registry.observe_llm_cache(False)
    registry.observe_llm_cancelled(SCHEMA)
    registry.observe_llm_timeout(SCHEMA)
    registry.observe(AGENT, 400, False)
    registry.observe_admission_refusal()

    payload = registry.snapshot().model_dump(mode="json")
    for key in (
        "llm_retries_total",
        "llm_retries_by_schema",
        "llm_cancelled_total",
        "llm_timeouts_total",
        "llm_cache_hits",
        "llm_cache_misses",
        "llm_cache_hit_rate",
        "llm_queue_waits",
        "retrieval_failures_by_component",
        "degradations_by_reason",
    ):
        assert key in payload, key
    assert AgentMetricsResponse.model_validate(payload).llm[0].retries == 1


# ── Расход модели по учётным записям (серверное состояние) ──────────────────


async def _collect(state: InMemoryDurableState) -> list[LlmAccountUsage]:
    await state.record_llm_usage(
        account_id="analyst-1",
        model="GigaChat",
        prompt_tokens=1000,
        completion_tokens=200,
        latency_ms=4200,
        success=True,
        query_id=str(uuid4()),
    )
    await state.record_llm_usage(
        account_id="analyst-1",
        model="GigaChat",
        prompt_tokens=800,
        completion_tokens=150,
        latency_ms=3000,
        success=False,
        schema_name="ReasonerOutput",
    )
    await state.record_llm_usage(
        account_id="expert-2",
        model="GigaChat",
        prompt_tokens=100,
        completion_tokens=20,
        latency_ms=900,
        success=True,
    )
    return await state.usage_by_account(since=datetime.now(UTC) - timedelta(days=1))


def test_in_memory_usage_answers_cost_per_analyst() -> None:
    state = InMemoryDurableState()
    rows = asyncio.run(_collect(state))

    analyst = next(row for row in rows if row.account_id == "analyst-1")
    assert analyst.calls == 2
    assert analyst.failed_calls == 1
    assert analyst.prompt_tokens == 1800
    assert analyst.completion_tokens == 350
    assert analyst.total_tokens == 2150
    # Самый прожорливый аккаунт — первым: список читают люди, отвечающие за бюджет.
    assert [row.account_id for row in rows] == ["analyst-1", "expert-2"]


def test_in_memory_usage_window_excludes_older_records() -> None:
    state = InMemoryDurableState()
    asyncio.run(_collect(state))
    future = datetime.now(UTC) + timedelta(hours=1)
    assert asyncio.run(state.usage_by_account(since=future)) == []


def test_usage_clamps_negative_counters() -> None:
    """Кривая оценка провайдера не уменьшает суммарный расход аккаунта."""
    state = InMemoryDurableState()

    async def scenario() -> tuple[LlmUsageRecord, int]:
        record = await state.record_llm_usage(
            account_id="analyst-1",
            model="GigaChat",
            prompt_tokens=-50,
            completion_tokens=-5,
            latency_ms=-1,
            success=True,
        )
        rows = await state.usage_by_account(since=datetime.now(UTC) - timedelta(days=1))
        return record, rows[0].total_tokens

    record, total = asyncio.run(scenario())
    assert (record.prompt_tokens, record.completion_tokens, record.latency_ms) == (0, 0, 0.0)
    assert total == 0


def test_postgres_branch_writes_and_aggregates_usage_in_sql() -> None:
    """Без живой базы проверяется сам SQL: деградация «остался в памяти» видна здесь."""
    state = PostgresDurableState(
        Settings(accounts_backend="postgres", knowledge_backend="memory")
    )
    statements: list[tuple[str, str, tuple[Any, ...]]] = []

    async def fake_execute(query: str, params: Any = ()) -> int:
        statements.append(("write", query, tuple(params or ())))
        return 1

    async def fake_fetchall(query: str, params: Any = ()) -> list[dict[str, Any]]:
        statements.append(("read", query, tuple(params or ())))
        return []

    state._execute = fake_execute  # type: ignore[assignment]
    state._fetchall = fake_fetchall  # type: ignore[assignment]

    async def scenario() -> None:
        await state.record_llm_usage(
            account_id="analyst-1",
            model="GigaChat",
            prompt_tokens=10,
            completion_tokens=2,
            latency_ms=15,
            success=True,
            query_id="q-1",
            schema_name="PlanningBundle",
        )
        await state.usage_by_account(since=datetime(2026, 1, 1, tzinfo=UTC))

    asyncio.run(scenario())

    writes = [query for kind, query, _ in statements if kind == "write"]
    reads = [query for kind, query, _ in statements if kind == "read"]
    assert any("INSERT INTO nk_llm_usage" in query for query in writes)
    assert any("DELETE FROM nk_llm_usage" in query for query in writes), "потолок журнала"
    aggregate = next(query for query in reads if "FROM nk_llm_usage" in query)
    for fragment in ("GROUP BY account_id", "sum(prompt_tokens)", "WHERE created_at >= %s"):
        assert fragment in aggregate, fragment


_POSTGRES_DSN = os.environ.get("POSTGRES_TEST_DSN", "")


@pytest.mark.skipif(
    not _POSTGRES_DSN,
    reason="нужен реальный Postgres: запустите контур и задайте POSTGRES_TEST_DSN",
)
def test_postgres_usage_round_trip() -> None:
    """Опциональная проверка боевого адаптера: SQL живёт только в нём.

    Запуск: ``POSTGRES_TEST_DSN=postgresql://…/mindai python -m pytest``.
    """
    asyncio.run(_postgres_usage_round_trip())


async def _postgres_usage_round_trip() -> None:
    state = PostgresDurableState(
        Settings(
            accounts_backend="postgres",
            knowledge_backend="memory",
            database_url=_POSTGRES_DSN,
        )
    )
    account = f"usage-check-{uuid4().hex[:8]}"
    await state.setup()
    try:
        await state.record_llm_usage(
            account_id=account,
            model="GigaChat",
            prompt_tokens=1200,
            completion_tokens=240,
            latency_ms=5100,
            success=True,
            query_id=str(uuid4()),
            schema_name="PlanningBundle",
        )
        await state.record_llm_usage(
            account_id=account,
            model="GigaChat",
            prompt_tokens=300,
            completion_tokens=60,
            latency_ms=1200,
            success=False,
        )
        rows = await state.usage_by_account(since=datetime.now(UTC) - timedelta(days=1))
    finally:
        await state.close()

    summary = next(row for row in rows if row.account_id == account)
    assert (summary.calls, summary.failed_calls) == (2, 1)
    assert summary.total_tokens == 1800
