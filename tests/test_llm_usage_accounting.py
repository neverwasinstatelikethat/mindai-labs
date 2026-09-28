"""Учёт расхода модели по учётным записям: глобального счётчика недостаточно.

Глобальный счётчик токенов отвечает на вопрос «сколько истратил сервис» и не
отвечает на вопрос «сколько стоили вопросы конкретного аналитика». Здесь
проверяется граница: накопитель открывается на прогон, провайдер складывает в
него usage (включая repair-повторы и отказ), а обработчик отдаёт строку в
серверное состояние.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from scientific_tangle.agents.workflow import ResearchWorkflow
from scientific_tangle.api.app import AppDependencies, _record_llm_usage, app
from scientific_tangle.services.accounts import Account, InMemoryAccounts
from scientific_tangle.services.agent_metrics import begin_llm_usage
from tests.auth_support import cookie_header, restricted_knowledge, signup
from tests.fakes import ScriptedProvider
from tests.test_api_http import BASE_EMAIL, _provider

USAGE_EMAIL = "usage@mindai.tech"
WINDOW = timedelta(minutes=5)


class _AccountingProvider(ScriptedProvider):
    """Scripted-провайдер, воспроизводящий часть учёта настоящего провайдера.

    ``GigaChatProvider`` вызывает ``observe_llm`` на каждое обращение — это и есть
    источник данных границы расхода. Подмена без этого вызова проверяла бы пустоту.
    """

    def __init__(self, *outputs: BaseModel, metrics: Any) -> None:
        super().__init__(*outputs)
        self._metrics = metrics

    async def complete_model(self, system: str, user: str, schema: type[BaseModel]) -> Any:
        result = await super().complete_model(system, user, schema)
        self._metrics.observe_llm(
            schema.__name__, 12.5, success=True, prompt_tokens=100, completion_tokens=20
        )
        return result


@pytest.fixture
def deps() -> AppDependencies:
    dependencies = AppDependencies()
    scripted = _provider()
    outputs = [item for queue in scripted._outputs.values() for item in queue]  # noqa: SLF001
    # Два прогона на фикстуру: сценарий одноразовый, а второй запрос проверяет
    # накопление, а не исчерпание очереди ответов.
    provider = _AccountingProvider(*outputs, *outputs, metrics=dependencies.metrics)
    dependencies.knowledge = restricted_knowledge()
    dependencies.provider = provider
    dependencies.workflow = ResearchWorkflow(
        knowledge=dependencies.knowledge,
        provider=provider,
        metrics=dependencies.metrics,
        settings=dependencies.settings,
    )
    return dependencies


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _account(deps: AppDependencies, email: str) -> Account:
    assert isinstance(deps.accounts, InMemoryAccounts)
    found = _run(deps.accounts.get_user_by_email(email))
    assert found is not None, f"аккаунт {email} не создан"
    return found


def _usage_of(deps: AppDependencies, account_id: str) -> dict[str, int]:
    rows = _run(deps.state.usage_by_account(since=datetime.now(UTC) - WINDOW))
    row = next((item for item in rows if item.account_id == account_id), None)
    if row is None:
        return {}
    return {
        "calls": row.calls,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "failed_calls": row.failed_calls,
    }


@pytest.fixture
def client(deps: AppDependencies) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        test_client.app.state.dependencies = deps
        yield test_client


def test_completed_query_is_attributed_to_the_account(
    client: TestClient, deps: AppDependencies
) -> None:
    cookies = cookie_header(signup(client, USAGE_EMAIL, display_name="Аналитик учёта"))

    response = client.post(
        "/api/v1/query",
        headers=cookies,
        json={"question": "Какие методы обессоливания подходят для шахтной воды?"},
    )

    assert response.status_code == 200, response.text
    usage = _usage_of(deps, _account(deps, USAGE_EMAIL).id)
    # Одна строка на прогон: ``calls`` сводки — записанные прогоны, а токены внутри
    # строки = сумма всех обращений модели этого прогона. Число обращений — внутренность
    # рабочего процесса, поэтому проверяем соотношение и кратность, а не конкретную сумму.
    assert usage["calls"] == 1 and usage["failed_calls"] == 0
    assert usage["prompt_tokens"] >= 300 and usage["prompt_tokens"] % 100 == 0
    assert usage["completion_tokens"] * 5 == usage["prompt_tokens"]


def test_repeated_query_accumulates_rather_than_overwrites(
    client: TestClient, deps: AppDependencies
) -> None:
    cookies = cookie_header(signup(client, BASE_EMAIL, display_name="Повторный"))

    account_id = _account(deps, BASE_EMAIL).id
    seen: list[dict[str, int]] = []
    for _ in range(2):
        assert (
            client.post(
                "/api/v1/query",
                headers=cookies,
                json={"question": "Сравните реагенты по стоимости."},
            ).status_code
            == 200
        )
        seen.append(_usage_of(deps, account_id))

    first, second = seen
    # Два завершённых прогона = две строки и удвоенный расход, а не перезапись первой.
    assert first["calls"] == 1 and second["calls"] == 2
    assert second["prompt_tokens"] == 2 * first["prompt_tokens"]
    assert second["completion_tokens"] == 2 * first["completion_tokens"]


def test_failed_call_is_recorded_as_failure_not_dropped(
    client: TestClient, deps: AppDependencies
) -> None:
    """Оплаченный отказ числится: иначе сводка показывает меньше, чем списал провайдер."""
    signup(client, "failed@mindai.tech", display_name="Отказ")
    account = _account(deps, "failed@mindai.tech")
    begin_llm_usage()
    deps.metrics.observe_llm(
        "ReasoningResult", 40.0, success=False, prompt_tokens=30, completion_tokens=3
    )

    _run(_record_llm_usage(deps, account, "query-not-stored"))

    assert _usage_of(deps, account.id) == {
        "calls": 1,
        "prompt_tokens": 30,
        "completion_tokens": 3,
        "failed_calls": 1,
    }


def test_run_without_model_calls_stores_nothing(
    client: TestClient, deps: AppDependencies
) -> None:
    """Отказ до обращения в модель не порождает строк учёта: нули исказили бы сводку."""
    signup(client, "empty@mindai.tech", display_name="Пустой")
    account = _account(deps, "empty@mindai.tech")
    begin_llm_usage()

    _run(_record_llm_usage(deps, account, "nothing-spent"))

    assert _usage_of(deps, account.id) == {}


def test_usage_route_reports_only_the_own_account(
    client: TestClient, deps: AppDependencies
) -> None:
    """``/me/usage`` — витрина собственного расхода; чужие строки там не появляются."""
    spender = cookie_header(signup(client, USAGE_EMAIL, display_name="Аналитик учёта"))
    observer = cookie_header(signup(client, "observer@mindai.tech", display_name="Наблюдатель"))
    assert (
        client.post(
            "/api/v1/query",
            headers=spender,
            json={"question": "Какие реагенты снижают пенообразование?"},
        ).status_code
        == 200
    )

    mine = client.get("/api/v1/me/usage", headers=spender)
    theirs = client.get("/api/v1/me/usage", headers=observer)

    assert mine.status_code == 200, mine.text
    assert mine.json()["runs"] == 1 and mine.json()["prompt_tokens"] >= 300
    assert theirs.json() == {
        "window_days": 7,
        "runs": 0,
        "failed_runs": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
    assert client.get("/api/v1/me/usage").status_code == 401
