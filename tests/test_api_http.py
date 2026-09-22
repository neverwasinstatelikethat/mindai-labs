"""HTTP-контур: серверный доступ, стрим, приём прогонов и серверное состояние.

Регрессии на находки V-1/V-2 (эндпоинты и ACL поверх HTTP не проверялись) и
подтверждение, что ACL считается на сервере до отдачи ответа, а не в интерфейсе.
Идентификация — только через cookie-сессию auth-эндпоинтов: подделываемые
``X-User-Role``/``X-User-Id`` удалены вместе с role-лестницей.
Здесь же проверяются четыре свойства, которые раньше держались на честном слове:
экспорт читает серверную копию ответа, слот агентного контура не ставит запрос в
очередь внутри чужого дедлайна, ``correlation_id`` доезжает до клиента, и
журнал/решения/уведомления живут в серверном состоянии, а не в deque процесса.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import fitz
import pytest
from fastapi.testclient import TestClient

from scientific_tangle.agents.workflow import ResearchWorkflow
from scientific_tangle.api.app import DEMO_QUESTION, AppDependencies, app
from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    AgentControlDecision,
    AnswerPayload,
    CritiqueResult,
    DocumentReceipt,
    DocumentRequest,
    EntityResolutionProposal,
    EvaluationMetrics,
    EvaluationRun,
    EvolutionDraft,
    EvolutionExperiment,
    ExpertDecision,
    GraphSnapshot,
    Notification,
    PipelineVariantMetrics,
    ReasoningResult,
)
from scientific_tangle.domain.intelligence import AuditEvent, DataClass
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.services import durable_state as state_module
from scientific_tangle.services.admission import AgentRunAdmission
from scientific_tangle.services.durable_state import (
    DurableState,
    InMemoryDurableState,
    PostgresDurableState,
    StoredAnswer,
    build_durable_state,
)
from scientific_tangle.services.evolution import EvolutionService
from scientific_tangle.services.exporter import ExportError, ExportService
from scientific_tangle.services.governance import (
    RESTRICTED_PERMISSION,
    WRITE_ACTIONS,
    AccessPolicyEngine,
)
from scientific_tangle.services.ingestion import IngestionService
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from scientific_tangle.services.ontology import OntologyValidationError
from scientific_tangle.services.provider import UnavailableProvider
from scientific_tangle.services.resolution import EntityResolutionWorkbench
from tests.auth_support import (
    RESTRICTED_STATEMENT,
    cookie_header,
    make_expert,
    restricted_knowledge,
    signup,
)
from tests.fakes import ScriptedProvider
from tests.test_workflow import planning_bundle

QUESTION = "Какие методы обессоливания подходят для шахтной воды?"
BASE_EMAIL = "http-researcher@mindai.tech"
EXPERT_EMAIL = "http-expert@mindai.tech"


def _provider() -> ScriptedProvider:
    return ScriptedProvider(
        planning_bundle(),
        AgentControlDecision(decision="reason", rationale="Доказательств достаточно."),
        ReasoningResult(
            summary="Комбинированная схема использует обратный осмос после pretreatment.",
            finding_ids=["finding-ro", "finding-ro-pilot"],
            conflicts=[],
            knowledge_gaps=[],
            recommendations=[],
        ),
        CritiqueResult(approved=True, issues=[], revision_instructions=[]),
        EvolutionDraft(
            kind="gold_case",
            title="Добавить экспертную корректировку в regression set",
            change="Зафиксировать исправленный вывод как ожидаемый ответ.",
            impact=["critic", "evaluation harness"],
        ),
    )


@pytest.fixture
def deps() -> AppDependencies:
    """Изолированный контур: своя база, scripted-провайдер, никаких внешних сервисов."""
    provider = _provider()
    dependencies = AppDependencies()
    dependencies.knowledge = restricted_knowledge()
    dependencies.provider = provider
    dependencies.evolution = EvolutionService(provider)
    dependencies.ingestion = IngestionService(
        dependencies.knowledge, provider, dependencies.resolution
    )
    dependencies.workflow = ResearchWorkflow(
        knowledge=dependencies.knowledge,
        provider=provider,
        metrics=dependencies.metrics,
        settings=dependencies.settings,
    )
    return dependencies


@pytest.fixture
def client(deps: AppDependencies) -> Iterator[TestClient]:
    # lifespan присваивает app.state.dependencies своим значением, поэтому
    # подмена возможна только после входа в контекст.
    with TestClient(app) as test_client:
        test_client.app.state.dependencies = deps
        yield test_client


@pytest.fixture
def base(client: TestClient) -> dict[str, str]:
    """Базовый уровень: любой подтверждённый аккаунт."""
    return cookie_header(signup(client, BASE_EMAIL, display_name="Исследователь"))


@pytest.fixture
def expert(client: TestClient, deps: AppDependencies) -> dict[str, str]:
    """Экспертный уровень: права выдаёт хранилище, как оператор выдаёт их SQL.

    Повышение работает и на уже открытой сессии — субъект пересобирается из
    хранилища на каждый запрос, а не записывается в cookie.
    """
    token = signup(client, EXPERT_EMAIL, display_name="Экспертиза")
    make_expert(deps.accounts, EXPERT_EMAIL)
    return cookie_header(token)


def test_query_over_http_returns_grounded_answer_and_evaluation(
    client: TestClient, base: dict[str, str]
) -> None:
    response = client.post("/api/v1/query", json={"question": QUESTION}, headers=base)

    assert response.status_code == 200
    body = response.json()
    assert body["answer"]["model_mode"] == "scripted"
    assert {item["id"] for item in body["answer"]["findings"]} == {
        "finding-ro",
        "finding-ro-pilot",
    }
    assert all(item["evidence"] for item in body["answer"]["findings"])
    assert body["evaluation"]["metrics"]["citation_coverage"] == 1.0


def test_stream_completes_with_the_same_answer_shape(
    client: TestClient, base: dict[str, str]
) -> None:
    events = []
    with client.stream(
        "POST", "/api/v1/query/stream", json={"question": QUESTION}, headers=base
    ) as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        for line in stream.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line.removeprefix("data:").strip()))

    kinds = [event["type"] for event in events]
    assert kinds[0] == "start"
    assert kinds[-1] == "done"
    assert "answer" in kinds
    answer = next(event["answer"] for event in events if event["type"] == "answer")
    assert answer["summary"].startswith("Комбинированная схема")
    assert {item["id"] for item in answer["findings"]} == {"finding-ro", "finding-ro-pilot"}
    assert answer["model_mode"] == "scripted"


def test_restricted_findings_are_hidden_per_access_level(
    client: TestClient, base: dict[str, str], expert: dict[str, str]
) -> None:
    researcher = client.get("/api/v1/findings", headers=base)
    manager = client.get("/api/v1/findings", headers=expert)
    graph = client.get("/api/v1/graph", headers=base)

    assert researcher.status_code == 200
    assert RESTRICTED_STATEMENT not in json.dumps(researcher.json(), ensure_ascii=False)
    assert RESTRICTED_STATEMENT in json.dumps(manager.json(), ensure_ascii=False)
    assert graph.status_code == 200
    assert all(node["data_class"] != "restricted" for node in graph.json()["nodes"])
    visible = {node["id"] for node in graph.json()["nodes"]}
    assert all(
        edge["source"] in visible and edge["target"] in visible
        for edge in graph.json()["edges"]
    )


def test_expert_supersede_requires_restricted_permission(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    expert: dict[str, str],
) -> None:
    finding_id = _restricted_finding_id(deps)
    payload = {
        "query_id": "8f5f34d0-eac1-4a14-853f-4f3a12ae0eef",
        "finding_id": finding_id,
        "verdict": "correct",
        "comment": "Уточнить область применимости",
        "correction": "Вывод применим только после предварительной очистки.",
    }

    denied = client.post("/api/v1/feedback", json=payload, headers=base)
    accepted = client.post("/api/v1/feedback", json=payload, headers=expert)
    history = client.get(f"/api/v1/claims/{finding_id}/history", headers=expert)

    assert denied.status_code == 403
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["proposal"] is not None and body["proposal"]["kind"] == "gold_case"
    assert body["degradation_reasons"] == []
    assert body["superseded"]["statement"].startswith("Вывод применим")
    versions = history.json()["versions"]
    assert len(versions) == 2
    assert versions[0]["superseded_by"] == versions[1]["finding_id"]
    # Ограниченный тезис остаётся невидимым для аккаунта без права на restricted.
    assert RESTRICTED_STATEMENT not in json.dumps(
        client.get("/api/v1/findings", headers=base).json(), ensure_ascii=False
    )


def test_expert_correction_survives_unavailable_model(
    client: TestClient, deps: AppDependencies, expert: dict[str, str]
) -> None:
    """Без живого LLM proposal не формируется, но решение эксперта сохраняется."""
    finding_id = _restricted_finding_id(deps)
    deps.evolution = EvolutionService(UnavailableProvider())
    payload = {
        "query_id": "8f5f34d0-eac1-4a14-853f-4f3a12ae0eef",
        "finding_id": finding_id,
        "verdict": "correct",
        "comment": "Модель недоступна, правка важнее",
        "correction": "Правка записана без обращения к LLM.",
    }

    response = client.post("/api/v1/feedback", json=payload, headers=expert)

    assert response.status_code == 200
    body = response.json()
    assert body["proposal"] is None
    assert body["superseded"]["statement"].startswith("Правка записана")
    assert any("не сформирован" in item for item in body["degradation_reasons"])
    history = client.get(f"/api/v1/claims/{finding_id}/history", headers=expert).json()
    assert len(history["versions"]) == 2


def _restricted_finding_id(deps: AppDependencies) -> str:
    return next(
        item.id
        for item in deps.knowledge.all_findings({DataClass.RESTRICTED})
        if item.statement == RESTRICTED_STATEMENT
    )


def test_audit_trail_records_real_user_id(client: TestClient, deps: AppDependencies) -> None:
    """Акт пишется на id аккаунта из сессии, а не на client-supplied X-User-Id."""
    auditor = cookie_header(signup(client, "http-auditor@mindai.tech", display_name="Аналитик"))
    account_id = client.get("/api/v1/auth/me", headers=auditor).json()["id"]

    analyst = client.post("/api/v1/query", json={"question": QUESTION}, headers=auditor)
    events = client.get("/api/v1/audit", headers=_audit_reader(client, deps)).json()

    assert analyst.status_code == 200
    assert any(
        event["action"] == "query.run" and event["actor_id"] == account_id for event in events
    )


def test_dashboard_journal_is_scoped_to_the_account(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """Панель выдаёт журнал своих актов, общий журнал — привилегия ``audit:read``.

    ``/api/v1/audit`` защищён проверкой права, а ``recent_activity`` панели
    фильтровался только количеством: десять строк чужих запросов и правок
    доезжали до любого вошедшего мимо защищённого эндпоинта.
    """
    outsider_email = "http-journal-outsider@mindai.tech"
    outsider = cookie_header(signup(client, outsider_email, display_name="Сосед"))
    own_id = client.get("/api/v1/auth/me", headers=outsider).json()["id"]
    for headers in (outsider, base):
        posted = client.post("/api/v1/query", json={"question": QUESTION}, headers=headers)
        assert posted.status_code == 200, posted.text

    own_rows = client.get("/api/v1/dashboard", headers=outsider).json()["recent_activity"]
    assert own_rows and all(row["actor_id"] == own_id for row in own_rows)

    shared_rows = client.get(
        "/api/v1/dashboard", headers=_audit_reader(client, deps)
    ).json()["recent_activity"]
    actors = {row["actor_id"] for row in shared_rows}
    assert own_id in actors and len(actors) > 1


def test_client_supplied_identity_headers_are_ignored(client: TestClient) -> None:
    """Заголовки былой эпохи больше не дают никакой идентичности."""
    spoofed = client.get(
        "/api/v1/findings",
        headers={"X-User-Role": "administrator", "X-User-Id": "pm-1"},
    )

    assert spoofed.status_code == 401
    assert spoofed.json() == {"detail": "Требуется вход в аккаунт"}


def test_session_is_required_and_dead_cookie_never_grants_access(
    client: TestClient, base: dict[str, str], expert: dict[str, str]
) -> None:
    """Без сессии — 401; с неизвестным или снятым токеном — тоже 401.

    Прежний тест «неизвестная роль не получает админских прав» решается самой
    конструкцией: неизвестному субъекту нечего выдавать, а права не берутся из
    заголовка.
    """
    assert client.get("/api/v1/findings").status_code == 401
    dead = client.get("/api/v1/findings", headers=cookie_header("tokena-net-v-hranilishe"))
    assert dead.status_code == 401
    assert client.post("/api/v1/query", json={"question": QUESTION}).status_code == 401

    # Тонкие права по-прежнему проверяются на сервере, а не в интерфейсе.
    assert client.get("/api/v1/audit", headers=base).status_code == 403
    assert client.get("/api/v1/audit", headers=expert).status_code == 200

    assert client.post("/api/v1/auth/logout", headers=base).status_code == 200
    assert client.get("/api/v1/findings", headers=base).status_code == 401


def _audit_reader(client: TestClient, deps: AppDependencies) -> dict[str, str]:
    """Токен эксперта для тестов, где нужен читатель audit-журнала."""
    email = "http-audit-reader@mindai.tech"
    token = signup(client, email, display_name="Хранитель журнала")
    make_expert(deps.accounts, email)
    return cookie_header(token)


# ── Экспорт: только серверная копия ответа ──────────────────────────────────


def _run_query(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/v1/query", json={"question": QUESTION}, headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["answer"]["query_id"])


def test_export_rejects_client_supplied_answer_and_uses_server_copy(
    client: TestClient, base: dict[str, str]
) -> None:
    """Тело ``answer`` больше не часть контракта: ACL не может фильтровать присланное.

    Подмена класса данных в теле запроса раньше превращала закрытый текст в
    файл: проверка шла по тому, что прислал клиент.
    """
    query_id = _run_query(client, base)

    forged = client.post(
        "/api/v1/export",
        json={"query_id": query_id, "answer": {"findings": []}, "format": "markdown"},
        headers=base,
    )
    unknown = client.post(
        "/api/v1/export",
        json={"query_id": "0f0f0f0f-0f0f-4f0f-8f0f-0f0f0f0f0f0f", "format": "markdown"},
        headers=base,
    )
    exported = client.post(
        "/api/v1/export", json={"query_id": query_id, "format": "markdown"}, headers=base
    )

    assert forged.status_code == 422
    assert unknown.status_code == 404
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/markdown")
    assert "Утверждения" in exported.text
    assert RESTRICTED_STATEMENT not in exported.text


def test_export_of_another_account_answer_is_not_found(
    client: TestClient, base: dict[str, str]
) -> None:
    """Чужой ответ — 404, а не 403: существование чужого запроса не раскрываем."""
    query_id = _run_query(client, base)
    stranger = cookie_header(signup(client, "http-stranger@mindai.tech"))

    response = client.post(
        "/api/v1/export", json={"query_id": query_id, "format": "markdown"}, headers=stranger
    )

    assert response.status_code == 404


def test_export_forbids_when_rights_shrank_after_the_answer(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """Права считаются в момент экспорта, а не в момент прогона.

    Серверная копия с закрытым классом данных могла сохраниться, пока право ещё
    действовало (признак ``review_enabled`` отзывают SQL'ем). Ответ обязан
    перечитать политику текущей сессии, а не отдать то, что лежит в таблице.
    """
    answer = AnswerPayload.model_validate(
        client.post("/api/v1/query", json={"question": QUESTION}, headers=base).json()["answer"]
    )
    owner = client.get("/api/v1/auth/me", headers=base).json()["id"]
    closed_finding = answer.findings[0].model_copy(update={"data_class": DataClass.RESTRICTED})
    now = datetime.now(UTC)
    stored = StoredAnswer(
        query_id=str(answer.query_id),
        owner_id=owner,
        created_at=now,
        expires_at=now + timedelta(hours=12),
        answer=answer.model_copy(update={"findings": [closed_finding]}),
    )
    deps.state = _StoredCopyState(stored)
    payload = {"query_id": str(answer.query_id), "format": "markdown"}

    denied = client.post("/api/v1/export", json=payload, headers=base)
    stranger = cookie_header(signup(client, "http-not-owner@mindai.tech"))
    missing = client.post("/api/v1/export", json=payload, headers=stranger)

    assert denied.status_code == 403
    assert RESTRICTED_STATEMENT not in denied.text
    # Чужой серверный ответ — 404: существование чужого запроса не раскрываем.
    assert missing.status_code == 404


class _StoredCopyState(InMemoryDurableState):
    """Отдаёт заранее собранную серверную копию.

    Так выглядит ответ, сохранённый до отзыва права: без живого Postgres в тестах
    эту комбинацию состояний можно получить только подменой хранилища.
    """

    def __init__(self, stored: StoredAnswer) -> None:
        super().__init__()
        self._stored = stored

    async def get_answer(self, query_id: str) -> StoredAnswer | None:
        return self._stored if query_id == self._stored.query_id else None


def test_export_jsonld_and_pdf_come_from_the_same_server_copy(
    client: TestClient, base: dict[str, str]
) -> None:
    """Генерация PDF не должна быть отдельным источником данных (и блокировать цикл)."""
    query_id = _run_query(client, base)

    jsonld = client.post(
        "/api/v1/export", json={"query_id": query_id, "format": "json-ld"}, headers=base
    )
    pdf = client.post(
        "/api/v1/export", json={"query_id": query_id, "format": "pdf"}, headers=base
    )

    assert jsonld.status_code == 200
    assert jsonld.headers["content-type"].startswith("application/ld+json")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content[:4] == b"%PDF"


# ── Приём агентных прогонов ────────────────────────────────────────────────


def test_admission_refuses_without_waiting_and_releases_the_slot(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """Все слоты заняты — 429 с ``Retry-After`` сразу, без очереди внутри дедлайна.

    Ожидание слота съело бы ``AGENT_DEADLINE_SECONDS`` принятого запроса, и
    вместо «сервис занят» клиент получил бы неполный ответ с
    ``degradation_reasons``: отказ обязан быть мгновенным.
    """
    deps.admission = AgentRunAdmission(limit=1, deadline_seconds=300, metrics=deps.metrics)
    held = deps.admission.acquire()
    started = time.monotonic()

    refused = client.post("/api/v1/query", json={"question": QUESTION}, headers=base)
    waited = time.monotonic() - started

    assert refused.status_code == 429
    assert refused.headers["Retry-After"].isdigit()
    assert waited < 1.0
    body = refused.json()
    assert body["limit"] == 1 and body["active"] == 1

    held.release()
    # Идемпотентность освобождения: повторный вызов не «освобождает» чужой слот.
    held.release()
    assert deps.admission.active == 0

    accepted = client.post("/api/v1/query", json={"question": QUESTION}, headers=base)
    assert accepted.status_code == 200
    assert deps.admission.active == 0


def test_stream_refusal_is_a_real_http_status_not_an_sse_error(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """429 до открытия потока: иначе клиент видел бы «успешный» SSE с ошибкой внутри."""
    deps.admission = AgentRunAdmission(limit=1, deadline_seconds=300, metrics=deps.metrics)
    held = deps.admission.acquire()
    try:
        refused = client.post("/api/v1/query/stream", json={"question": QUESTION}, headers=base)
        assert refused.status_code == 429
        assert refused.headers["Retry-After"].isdigit()
    finally:
        held.release()
    assert deps.admission.active == 0


def test_admission_refusals_are_observable(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """Отказы и занятость слотов — в ``/agents/metrics``: 429 не должны выглядеть случайными."""
    deps.admission = AgentRunAdmission(limit=1, deadline_seconds=300, metrics=deps.metrics)
    held = deps.admission.acquire()
    client.post("/api/v1/query", json={"question": QUESTION}, headers=base)
    held.release()

    metrics = client.get("/api/v1/agents/metrics", headers=base).json()

    assert metrics["agent_runs_limit"] == 1
    assert metrics["agent_runs_refused"] >= 1
    assert metrics["agent_runs_active"] == 0


def test_health_ready_reports_state_backend_and_capacity(
    client: TestClient, deps: AppDependencies
) -> None:
    """Честный контракт деградации: память = история не переживает перезапуск."""
    deps.state = InMemoryDurableState()

    ready = client.get("/health/ready").json()

    assert ready["state_backend"] == "in-memory"
    assert ready["services"]["server_state"] == "fallback"
    assert ready["agent_runs_limit"] >= 1
    assert ready["agent_runs_active"] == 0


# ── correlation_id ────────────────────────────────────────────────────────


def test_correlation_id_reaches_the_client_in_json_and_headers(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """Один и тот же идентификатор — в заголовке, в теле и в журнале аудита.

    Без него разбор инцидента невозможен: акт в журнале пишется с тем же
    ``correlation_id``, и сопоставить их клиент может только если значение вернулось.
    """
    trace = "corr-json-1"
    headers = {**base, "X-Correlation-Id": trace}

    response = client.post("/api/v1/query", json={"question": QUESTION}, headers=headers)
    events = client.get(
        "/api/v1/audit", headers=_audit_reader(client, deps), params={"correlation_id": trace}
    ).json()

    assert response.headers["X-Correlation-Id"] == trace
    assert response.json()["correlation_id"] == trace
    assert [event["correlation_id"] for event in events] == [trace]
    assert events[0]["action"] == "query.run"


def test_correlation_id_is_assigned_on_the_server_and_present_in_stream(
    client: TestClient, base: dict[str, str]
) -> None:
    """Клиент может не прислать идентификатор — поток всё равно обязан его нести."""
    events: list[dict[str, object]] = []
    with client.stream(
        "POST", "/api/v1/query/stream", json={"question": QUESTION}, headers=base
    ) as stream:
        assert stream.status_code == 200
        for line in stream.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line.removeprefix("data:").strip()))

    trace = events[0]["correlation_id"]
    assert isinstance(trace, str) and trace
    assert all(event.get("correlation_id") == trace for event in events)


# ── Серверное состояние вместо deque процесса ─────────────────────────────


def test_audit_and_notifications_are_read_from_server_state(
    client: TestClient, deps: AppDependencies, expert: dict[str, str]
) -> None:
    """Журнал и лента живут в ``deps.state``: память приложения их больше не хранит.

    Подмена состояния на пустое после действий — способ доказать, что читается
    именно хранилище, а не deque процесса.
    """
    _run_query(client, expert)
    finding_id = _restricted_finding_id(deps)
    client.post(
        "/api/v1/feedback",
        json={
            "query_id": "8f5f34d0-eac1-4a14-853f-4f3a12ae0eef",
            "finding_id": finding_id,
            "verdict": "correct",
            "comment": "Уточнить область применимости",
            "correction": "Вывод применим только после предварительной очистки.",
        },
        headers=expert,
    )

    assert client.get("/api/v1/notifications", headers=expert).json() != []
    assert client.get("/api/v1/audit", headers=expert).json() != []

    deps.state = InMemoryDurableState()

    assert client.get("/api/v1/notifications", headers=expert).json() == []
    assert client.get("/api/v1/audit", headers=expert).json() == []


def test_subscription_endpoint_is_removed(client: TestClient, expert: dict[str, str]) -> None:
    """Подписок на темы нет: нечего и хранить.

    Эндпоинт складывал ``subscriber_id`` — идентификатор, который назвал сам
    клиент, — в множество, которое никто не читал. В продукте, где личность
    приходит только с серверной сессии, такой вход недопустим, а «зафиксированный
    интерес» без читателя был бы фасадом доставки.
    """
    response = client.post(
        "/api/v1/subscriptions",
        json={"topic": "claim.superseded", "subscriber_id": "analyst-1"},
        headers=expert,
    )

    assert response.status_code == 404


def test_durable_state_keeps_audit_experiments_and_decisions() -> None:
    """Контур состояния: записи возвращаются «с новых к старым» и фильтруются по corrid.

    Отдельно от HTTP: postgres-адаптер в тестах без базы не проверяем, но
    контракт чтения/записи обязан выполняться на обоих адаптерах.
    """
    state = InMemoryDurableState()

    async def scenario() -> None:
        await state.append_audit(
            AuditEvent(
                actor_id="a",
                action="query.run",
                object_id="q1",
                outcome="success",
                correlation_id="c1",
            )
        )
        await state.append_audit(
            AuditEvent(
                actor_id="a",
                action="export.run",
                object_id="q1",
                outcome="success",
                correlation_id="c2",
            )
        )
        await state.append_audit(
            AuditEvent(
                actor_id="b",
                action="query.run",
                object_id="q2",
                outcome="success",
                correlation_id="c3",
            )
        )
        await state.record_decision(
            ExpertDecision(actor_id="a", action="answer.exported", object_id="q1")
        )
        await state.record_decision(
            ExpertDecision(actor_id="b", action="proposal.reviewed", object_id="p1")
        )
        await state.record_experiment(_tiny_experiment())
        await state.record_evaluation(
            EvaluationRun(query_id=UUID(int=1), metrics=_metrics(), passed=True)
        )
        # Уведомления — пятый род серверного состояния: без них «лента переживает
        # перезапуск» было бы обещанием ровно на четыре журнала из пяти.
        await state.append_notification(Notification(topic="claim.superseded", message="первое"))
        await state.append_notification(Notification(topic="proposal.accepted", message="второе"))

        assert [event.correlation_id for event in await state.recent_audit(limit=5)] == [
            "c3",
            "c2",
            "c1",
        ]
        filtered = await state.recent_audit(limit=5, correlation_id="c1")
        assert [event.correlation_id for event in filtered] == ["c1"]
        # Фильтр по актору — та граница, на которую опирается панель: без неё
        # «последние десять событий» означали бы десять событий чужого аккаунта.
        assert [event.actor_id for event in await state.recent_audit(limit=5, actor_id="a")] == [
            "a",
            "a",
        ]
        assert [item.action for item in await state.recent_decisions()] == [
            "proposal.reviewed",
            "answer.exported",
        ]
        # Решения фильтруются по автору: лента чужих экспертных действий
        # приватному аккаунту не показывается.
        assert [item.actor_id for item in await state.recent_decisions(actor_id="a")] == ["a"]
        assert [item.message for item in await state.recent_notifications(limit=5)] == [
            "второе",
            "первое",
        ]
        assert [item.decision for item in await state.recent_experiments()] == ["reject"]
        assert [item.passed for item in await state.recent_evaluations()] == [True]

    asyncio.run(scenario())


def _metrics() -> EvaluationMetrics:
    return EvaluationMetrics(
        citation_coverage=1.0,
        numeric_support=1.0,
        unsupported_claim_ratio=0.0,
        mean_finding_confidence=0.8,
        overall=0.95,
    )


def _variant() -> PipelineVariantMetrics:
    return PipelineVariantMetrics(
        source_recall=0.5,
        citation_coverage=1.0,
        pass_rate=0.5,
        average_latency_ms=1000,
    )


def _tiny_experiment() -> EvolutionExperiment:
    return EvolutionExperiment(
        proposal_id=UUID(int=2),
        cases=1,
        baseline=_variant(),
        candidate=_variant(),
        delta_pass_rate=0.0,
        regressions=["case-1"],
        decision="reject",
    )


# ── Склейка сущностей: решение по id, повтор не меняет состояние ──────────


def _resolution_workbench() -> tuple[EntityResolutionWorkbench, UUID]:
    workbench = EntityResolutionWorkbench(Settings(knowledge_backend="memory"))
    workbench.register(
        [
            EntityResolutionProposal(
                mention="Обратный осмос",
                canonical_name="Мембранное обессоливание",
                action="link",
                confidence=0.9,
                rationale="Синонимичные технологии в обзоре и в отчёте.",
            )
        ]
    )
    return workbench, workbench.list_proposals()[0].id


def test_merge_proposals_are_keyed_by_identity_and_idempotent_on_accept() -> None:
    """Повторный импорт той же пары — одно предложение; повторный accept — то же состояние.

    ``reviewed_at``/``reviewer_id`` не должны перезаписываться вторым принятием:
    по ним журнал решений эксперта сходится с фактическим моментом решения.
    """
    workbench, proposal_id = _resolution_workbench()

    first = workbench.review(proposal_id, "accept", reviewer_id="expert-1")
    again = workbench.review(proposal_id, "accept", reviewer_id="expert-2")
    workbench.register(
        [
            EntityResolutionProposal(
                mention="Обратный осмос",
                canonical_name="Мембранное обессоливание",
                action="link",
                confidence=0.4,
                rationale="Тот же пару принёс следующий импорт.",
            )
        ]
    )
    kept = workbench.get(proposal_id)

    assert again.status == "accepted"
    assert again.reviewed_at == first.reviewed_at
    assert again.reviewer_id == "expert-1"
    assert len(workbench.list_proposals()) == 1
    assert kept.status == "accepted"
    assert workbench.alias_map() == {"обратный осмос": "Мембранное обессоливание"}


def test_revert_and_reaccept_keep_single_proposal_and_unknown_id_is_key_error() -> None:
    workbench, proposal_id = _resolution_workbench()

    workbench.review(proposal_id, "accept")
    reverted = workbench.review(proposal_id, "revert")
    accepted_again = workbench.review(proposal_id, "accept")

    assert reverted.status == "reverted"
    assert accepted_again.status == "accepted"
    assert workbench.alias_map() == {"обратный осмос": "Мембранное обессоливание"}
    with pytest.raises(KeyError):
        workbench.review(UUID(int=7), "accept")


def test_ingestion_reuses_accepted_merge_instead_of_redeciding() -> None:
    """Принятая экспертом склейка попадает в разбор следующего импорта.

    Иначе каждое новое обновление корпуса заново решает, что такое «обратный
    осмос», и решение эксперта разойдётся с графом.
    """
    workbench, proposal_id = _resolution_workbench()
    workbench.review(proposal_id, "accept")
    service = IngestionService(
        InMemoryKnowledgeBase(), ScriptedProvider(), workbench  # type: ignore[arg-type]
    )

    assert service._approved_aliases() == {"обратный осмос": "Мембранное обессоливание"}


# ── Блокирующий I/O вне event loop ────────────────────────────────────────


def _inside_event_loop(flags: list[bool]) -> None:
    """Отмечает, вызвали ли функцию из цикла событий (True) или из потока (False)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        flags.append(False)
    else:
        flags.append(True)


def test_document_upload_runs_parsing_outside_the_event_loop(
    client: TestClient, deps: AppDependencies, base: dict[str, str], monkeypatch
) -> None:
    """OCR и разбор PDF — минуты синхронной работы: в обработчике их быть не должно."""
    seen: list[bool] = []

    def fake_parse(filename: str, content: bytes, **kwargs: object) -> DocumentRequest:
        _inside_event_loop(seen)
        return DocumentRequest(title="Отчёт", text="шахта " * 12)

    async def fake_ingest(document: DocumentRequest) -> DocumentReceipt:
        return DocumentReceipt(document_id=UUID(int=3), checksum="abc", status="created",
                               extracted_claims=1)

    monkeypatch.setattr("scientific_tangle.api.app.parse_document", fake_parse)
    monkeypatch.setattr(deps.ingestion, "ingest", fake_ingest)

    response = client.post(
        "/api/v1/documents/upload",
        files={"file": ("otchet.txt", b"shutdown text " * 10)},
        data={"data_class": "public"},
        headers=base,
    )

    assert response.status_code == 200
    assert seen == [False]


def test_import_with_out_of_vocabulary_predicate_returns_service_unavailable(
    client: TestClient, deps: AppDependencies, base: dict[str, str], monkeypatch
) -> None:
    """Нарушение словаря отношений — 503 с объяснением, а не 500 без него.

    Живой прогон на GigaChat: модель предложила предикат вне подписи, повторная
    сборка его не исправила, и импорт упал как внутренняя ошибка — действие
    аналитика пропало молча, без «документ не в корпусе».
    """

    async def broken_ingest(document: DocumentRequest) -> DocumentReceipt:
        raise OntologyValidationError("predicate=STARTED_WITH: значения нет в словаре подписи")

    monkeypatch.setattr(deps.ingestion, "ingest", broken_ingest)

    response = client.post(
        "/api/v1/documents",
        json={"title": "Отчёт", "text": "шахта " * 12},
        headers=base,
    )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "не импортирован" in detail
    # Служебные имена предикатов остаются в логе: человеку они ничего не говорят.
    assert "STARTED_WITH" not in detail
    assert response.headers["X-Correlation-Id"]


def test_export_renders_outside_the_event_loop(
    client: TestClient, deps: AppDependencies, base: dict[str, str], monkeypatch
) -> None:
    """Генерация PDF в PyMuPDF синхронная — её тоже уводим в поток."""
    seen: list[bool] = []
    query_id = _run_query(client, base)

    def fake_export(answer: AnswerPayload, fmt: str) -> tuple[str, str, str]:
        _inside_event_loop(seen)
        return "# ответ", "text/markdown; charset=utf-8", "answer.md"

    monkeypatch.setattr(deps.exporter, "export", fake_export)

    response = client.post(
        "/api/v1/export", json={"query_id": query_id, "format": "pdf"}, headers=base
    )

    assert response.status_code == 200
    assert seen == [False]


@pytest.mark.asyncio
async def test_ingestion_offloads_ontology_validation_and_graph_writes() -> None:
    """pyshacl, запись находок и регистрация склеек не занимают цикл событий."""
    from scientific_tangle.domain.contracts import ExtractionResult, IngestionBundle

    seen: list[bool] = []

    class _Provider:
        mode = "scripted"

        async def complete_model(self, system: str, user: str, schema: type) -> object:
            return IngestionBundle(
                extraction=ExtractionResult(entities=[], claims=[]), resolutions=[]
            )

    class _Knowledge:
        def ingest(self, document: object, extraction: ExtractionResult) -> DocumentReceipt:
            _inside_event_loop(seen)
            return DocumentReceipt(
                document_id=UUID(int=4), checksum="sum", status="created", extracted_claims=0
            )

    class _Ontology:
        def validate(self, extraction: ExtractionResult) -> None:
            _inside_event_loop(seen)

    class _Resolution:
        def register(self, resolutions: list[object]) -> None:
            _inside_event_loop(seen)

        def alias_map(self) -> dict[str, str]:
            return {}

    service = IngestionService(_Knowledge(), _Provider(), _Resolution())  # type: ignore[arg-type]
    service._ontology = _Ontology()  # type: ignore[assignment]

    await service.ingest(DocumentRequest(title="Отчёт", text="шахта " * 12))

    assert seen and set(seen) == {False}


# ── Выбор контура серверного состояния ────────────────────────────────────


def test_durable_state_follows_accounts_backend_and_declares_every_table() -> None:
    """Postgres-контур обязан создать все пять журналов, а не только ответы с решениями.

    Иначе «durable» выполняется молча только для части состояния: недостающую
    таблицу адаптер discovers уже на первом запросе, в проде — ошибкой 500.
    """
    from scientific_tangle.services import durable_state as module

    settings = Settings(accounts_backend="postgres", knowledge_backend="memory")
    built = build_durable_state(settings)
    memory = build_durable_state(Settings(accounts_backend="memory", knowledge_backend="memory"))

    assert isinstance(built, PostgresDurableState)
    assert isinstance(memory, InMemoryDurableState)
    ddl = "\n".join(
        [
            module._ANSWERS_DDL,
            module._DECISIONS_DDL,
            module._AUDIT_DDL,
            module._NOTIFICATIONS_DDL,
            module._EXPERIMENTS_DDL,
            module._EVALUATIONS_DDL,
        ]
    )
    for table in (
        "nk_answers",
        "nk_expert_decisions",
        "nk_audit_events",
        "nk_notifications",
        "nk_experiments",
        "nk_evaluation_runs",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    assert "nk_audit_events_correlation_idx" in module._AUDIT_CORRELATION_INDEX_DDL


# ── Списки предложений и права на запись ──────────────────────────────────


def test_proposal_lists_are_filtered_by_rights(
    client: TestClient, base: dict[str, str], expert: dict[str, str]
) -> None:
    """``/proposals``, ``/entity-resolution/proposals`` и ``/experiments`` — экспертный контур.

    В ``change`` предложения живёт текст будущей политики агента, в ``rationale`` —
    ссылки на источники; раньше оба списка уходили любому подтверждённому аккаунту.
    """
    assert client.get("/api/v1/proposals", headers=base).status_code == 403
    assert client.get("/api/v1/entity-resolution/proposals", headers=base).status_code == 403
    assert client.get("/api/v1/experiments", headers=base).status_code == 403
    assert client.get("/api/v1/proposals", headers=expert).status_code == 200
    assert client.get("/api/v1/entity-resolution/proposals", headers=expert).status_code == 200
    assert client.get("/api/v1/experiments", headers=expert).status_code == 200


def test_write_actions_require_an_action_permission_not_restricted_read() -> None:
    """``restricted:read`` — право читать закрытый контур, а не право на запись.

    Расширение доступа на чтение не должно молча открывать перезапись утверждений
    и импорт в restricted: для записей в политике отдельная таблица действий.
    """
    engine = AccessPolicyEngine()
    expert = AccessPolicyEngine.principal("reader-1", review_enabled=True)
    analyst = AccessPolicyEngine.principal("reader-2", review_enabled=False)

    assert engine.can_write(expert, "claim.supersede")
    assert engine.can_write(expert, "document.write_restricted")
    assert not engine.can_write(analyst, "claim.supersede")
    # Неизвестное действие запрещено по умолчанию: новый эндпоинт обязан
    # выписать себе право, а не получить его «по наследству».
    assert not engine.can_write(expert, "yet.unknown.write")
    assert not engine.can_write(None, "claim.supersede")
    # Запись не висит на праве чтения: список действий ссылается только на
    # экспертное право на действие.
    assert set(WRITE_ACTIONS.values()) == {"proposal:review"}
    assert RESTRICTED_PERMISSION not in WRITE_ACTIONS.values()


# ── Демо: модель не вызывается на каждое обращение ───────────────────────


def test_demo_replays_one_run_not_one_per_request(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """Второе обращение к витрине не тратит бюджет: прогон один, кэш честный.

    Scripted-провайдер отдаёт по одному ответу на схему: второй реальный прогон
    упал бы на пустой очереди сценария, поэтому стабильный 200 — доказательство кэша.
    """
    provider = cast(ScriptedProvider, deps.provider)

    first = client.get("/api/v1/demo", headers=base)
    calls_after_first = len(provider.calls)
    second = client.get("/api/v1/demo", headers=base)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(provider.calls) == calls_after_first
    assert first.json()["answer"]["query_id"] == second.json()["answer"]["query_id"]
    assert DEMO_QUESTION in second.json()["answer"]["question"]
    # Серверная копия заведена зрителю: экспорт витрины работает по query_id.
    query_id = second.json()["answer"]["query_id"]
    assert client.post(
        "/api/v1/export", json={"query_id": query_id, "format": "markdown"}, headers=base
    ).status_code == 200


# ── Персистентность: все пять родов серверного состояния ────────────────────


def _tiny_answer(question: str = QUESTION) -> AnswerPayload:
    """Минимальный ответ для тестов хранилища: без обращения к рабочему процессу."""
    return AnswerPayload(
        question=question,
        summary="Комбинированная схема использует обратный осмос после предочистки.",
        query_plan=QueryPlan(question=question, language="ru"),
        findings=[],
        conflicts=[],
        knowledge_gaps=[],
        recommendations=[],
        graph=GraphSnapshot(nodes=[], edges=[]),
        trace=[],
        confidence=0.6,
        model_mode="scripted",
    )


def test_all_five_server_state_feeds_live_in_the_store(
    client: TestClient, deps: AppDependencies, expert: dict[str, str]
) -> None:
    """Ни один из пяти журналов не должен жить в deque процесса.

    Подмена хранилища на пустое — способ это доказать: если бы читался deque,
    записи остались бы, а при честном чтении из состояния пустеют все пять лент.
    """
    finding_id = _restricted_finding_id(deps)
    query_id = _run_query(client, expert)
    assert client.post(
        "/api/v1/export", json={"query_id": query_id, "format": "markdown"}, headers=expert
    ).status_code == 200
    client.post(
        "/api/v1/feedback",
        json={
            "query_id": query_id,
            "finding_id": finding_id,
            "verdict": "correct",
            "comment": "Уточнить область применимости",
            "correction": "Вывод применим только после предварительной очистки.",
        },
        headers=expert,
    )
    asyncio.run(deps.state.record_experiment(_tiny_experiment()))

    paths = (
        ("audit", "/api/v1/audit"),
        ("decisions", "/api/v1/decisions"),
        ("experiments", "/api/v1/experiments"),
        ("evaluations", "/api/v1/evaluations"),
        ("notifications", "/api/v1/notifications"),
    )
    for name, path in paths:
        response = client.get(path, headers=expert)
        assert response.status_code == 200, name
        assert response.json() != [], f"{name}: журнал пуст, действие не записано в состояние"

    deps.state = InMemoryDurableState()

    for name, path in paths:
        assert client.get(path, headers=expert).json() == [], f"{name}: читалось не из состояния"


def test_expert_decisions_endpoint_is_scoped_to_the_caller(
    client: TestClient, base: dict[str, str], expert: dict[str, str]
) -> None:
    """``/decisions`` — история собственных действий: чужие query_id не отдаются.

    Выдача всех записей любому аккаунту раскрыла бы идентификаторы чужих запросов
    и сам факт чужого экспорта — это объектный доступ, а не тонкое право.
    """
    own_query = _run_query(client, base)
    assert client.post(
        "/api/v1/export", json={"query_id": own_query, "format": "markdown"}, headers=base
    ).status_code == 200
    other_query = _run_query(client, expert)
    assert client.post(
        "/api/v1/export", json={"query_id": other_query, "format": "markdown"}, headers=expert
    ).status_code == 200

    mine = client.get("/api/v1/decisions", headers=base)
    theirs = client.get("/api/v1/decisions", headers=expert)
    anonymous = client.get("/api/v1/decisions")

    assert mine.status_code == 200
    assert [item["object_id"] for item in mine.json()] == [own_query]
    account_id = client.get("/api/v1/auth/me", headers=base).json()["id"]
    assert {item["actor_id"] for item in mine.json()} == {account_id}
    assert other_query in [item["object_id"] for item in theirs.json()]
    assert own_query not in [item["object_id"] for item in theirs.json()]
    assert anonymous.status_code == 401


def test_restricted_claim_history_is_not_readable_without_the_class(
    client: TestClient, deps: AppDependencies, base: dict[str, str], expert: dict[str, str]
) -> None:
    """История версий — тот же объект доступа, что и само утверждение.

    ``/findings`` закрытый тезис не отдаёт, а эндпоинт истории читал версии
    напрямую из графа: без проверки класса любой аккаунт вытаскивал бы текст
    restricted-утверждения по его идентификатору.
    """
    finding_id = _restricted_finding_id(deps)

    denied = client.get(f"/api/v1/claims/{finding_id}/history", headers=base)
    allowed = client.get(f"/api/v1/claims/{finding_id}/history", headers=expert)

    assert denied.status_code == 404
    assert RESTRICTED_STATEMENT not in denied.text
    assert allowed.status_code == 200
    assert allowed.json()["versions"][0]["statement"] == RESTRICTED_STATEMENT


def test_proposal_review_and_experiment_routes_require_the_review_permission(
    client: TestClient, base: dict[str, str], expert: dict[str, str]
) -> None:
    """Обзор предложений и запуск A/B — экспертные действия по конкретному id."""
    unknown = "77777777-7777-4777-8777-777777777777"
    routes = (
        (f"/api/v1/proposals/{unknown}/review", {"accepted": True}),
        (f"/api/v1/proposals/{unknown}/experiment", None),
        (f"/api/v1/entity-resolution/proposals/{unknown}/review", {"action": "reject"}),
    )

    for path, body in routes:
        denied = (
            client.post(path, json=body, headers=base)
            if body is not None
            else client.post(path, headers=base)
        )
        assert denied.status_code == 403, path

    for path, body in routes:
        # Право есть, объекта нет: 404, а не 403 — иначе список предложений
        # можно было бы перебирать по коду ответа.
        allowed = (
            client.post(path, json=body, headers=expert)
            if body is not None
            else client.post(path, headers=expert)
        )
        assert allowed.status_code == 404, path


def test_pdf_export_carries_cyrillic_and_unsupported_format_is_refused(
    client: TestClient, base: dict[str, str]
) -> None:
    """Base-14 в PDF кириллицы не содержит: файл обязан проверяться до выдачи.

    Проверка идёт извлечением текста из возвращаемых байтов, а не «статусом 200»:
    именно так раньше проходил экспорт, где вместо ответа были вопросительные
    знаки.
    """
    query_id = _run_query(client, base)

    pdf = client.post(
        "/api/v1/export", json={"query_id": query_id, "format": "pdf"}, headers=base
    )
    forged = client.post(
        "/api/v1/export", json={"query_id": query_id, "format": "docx"}, headers=base
    )

    assert pdf.status_code == 200
    assert pdf.content[:4] == b"%PDF"
    document = fitz.open(stream=pdf.content, filetype="pdf")
    try:
        extracted = "".join(page.get_text() for page in document)
    finally:
        document.close()
    compact = "".join(extracted.split())
    # Проба считается из самого вопроса, а не печатается руками: опечатка в
    # букве превращает проверку кириллицы в ложное падение.
    assert "".join(QUESTION.split())[:24] in compact
    assert "Комбинированнаясхема" in compact
    assert "???" not in extracted
    # Неизвестный формат — ошибка схемы, а не молчаливая подмена Markdown.
    assert forged.status_code == 422
    with pytest.raises(ExportError):
        ExportService().export(_tiny_answer(), "docx")  # type: ignore[arg-type]


def test_pdf_export_paginates_without_losing_text() -> None:
    """Длинный ответ обязан разложиться по страницам, а не обрезаться по ширине."""
    answer = _tiny_answer(QUESTION * 30).model_copy(
        update={"summary": "Комбинированная схема с обратным осмосом. " * 120}
    )

    content, content_type, filename = ExportService().export(answer, "pdf")

    assert content_type == "application/pdf"
    assert filename == "answer.pdf"
    payload = base64.b64decode(content)
    document = fitz.open(stream=payload, filetype="pdf")
    try:
        extracted = "".join(page.get_text() for page in document)
        pages = document.page_count
    finally:
        document.close()
    compact = "".join(extracted.split())
    assert pages >= 2
    assert "".join(answer.question.split())[:64] in compact


def test_prometheus_metrics_and_logs_carry_no_question_text(
    client: TestClient,
    base: dict[str, str],
    expert: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ни метрики, ни логи, ни журнал не наполняются текстом вопроса.

    Метрика с вопросом в лейбле разорвала бы кардинальность Prometheus, а
    фрагмент вопроса в логе или журнале — канал утечки закрытого содержимого
    в обход ACL.
    """
    marker = "ТЕСТМАРКЕРФРАГМЕНТАВОПРОСА"
    with caplog.at_level(logging.INFO):
        _run_query(client, base)
        client.post(
            "/api/v1/compare",
            json={"question": f"Сравнить {marker}", "entities": ["Методы обессоливания"]},
            headers=base,
        )
        # Пути с параметром: series обязана строиться по шаблону маршрута, а не по
        # фактическому id, иначе кардинальность Prometheus растёт вместе с корпусом.
        client.get(f"/api/v1/claims/{marker}/history", headers=base)
        scraped = client.get("/metrics")

    assert scraped.status_code == 200
    assert marker not in scraped.text
    assert marker not in caplog.text
    audit = client.get("/api/v1/audit", headers=expert, params={"limit": 100}).json()
    assert marker not in json.dumps(audit, ensure_ascii=False)
    # Емкость приёма при этом наблюдаема и без запретных лейблов.
    assert "mindai_agent_run_slots" in scraped.text
    assert "{claim_id}" in scraped.text
    for forbidden in ("question=", "query_id=", "finding_id=", "document_id="):
        assert forbidden not in scraped.text


def test_postgres_state_sends_sql_for_every_kind_of_server_state() -> None:
    """Postgres-ветка обязана писать и читать все пять журналов, а не только ответы.

    Живой базы в тестах нет, поэтому проверяется SQL, который адаптер отправляет:
    молча оставшийся в памяти журнал — это случай, когда «durable» выполняется
    для части состояния и обнаруживается только после перезапуска.
    """
    state = PostgresDurableState(
        Settings(accounts_backend="postgres", knowledge_backend="memory")
    )
    statements: list[tuple[str, str]] = []

    async def fake_execute(query: str, params: Sequence[Any] = ()) -> int:
        statements.append(("write", query))
        return 1

    async def fake_fetchall(query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        statements.append(("read", query))
        return []

    async def fake_fetchone(query: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        statements.append(("read", query))
        return None

    state._execute = fake_execute  # type: ignore[assignment]
    state._fetchall = fake_fetchall  # type: ignore[assignment]
    state._fetchone = fake_fetchone  # type: ignore[assignment]

    async def scenario() -> None:
        await state.put_answer(_tiny_answer(), owner_id="owner-1")
        await state.get_answer("q1")
        await state.append_audit(
            AuditEvent(
                actor_id="a",
                action="query.run",
                object_id="q1",
                outcome="success",
                correlation_id="c1",
            )
        )
        await state.recent_audit(correlation_id="c1")
        await state.record_decision(
            ExpertDecision(actor_id="a", action="proposal.created", object_id="p1")
        )
        await state.recent_decisions(actor_id="a")
        await state.append_notification(Notification(topic="claim.superseded", message="m"))
        await state.recent_notifications()
        await state.record_experiment(_tiny_experiment())
        await state.recent_experiments()
        await state.record_evaluation(
            EvaluationRun(query_id=UUID(int=1), metrics=_metrics(), passed=True)
        )
        await state.recent_evaluations()

    asyncio.run(scenario())

    writes = " ".join(query for kind, query in statements if kind == "write")
    reads = " ".join(query for kind, query in statements if kind == "read")
    for table in (
        "nk_answers",
        "nk_audit_events",
        "nk_expert_decisions",
        "nk_notifications",
        "nk_experiments",
        "nk_evaluation_runs",
    ):
        assert f"INSERT INTO {table}" in writes, table
        assert f"FROM {table}" in reads, table
    assert "WHERE correlation_id = %s" in reads
    assert "WHERE actor_id = %s" in reads
    # Потолок длины журнала держится удалением, а не неограниченным ростом таблицы.
    assert writes.count("DELETE FROM nk_audit_events") >= 1


def test_both_state_adapters_implement_the_protocol() -> None:
    """Новый метод протокола обязан появиться в обеих ветках, иначе деградация — 500."""
    memory = InMemoryDurableState()
    postgres = PostgresDurableState(
        Settings(accounts_backend="postgres", knowledge_backend="memory")
    )

    members = [name for name in dir(DurableState) if not name.startswith("_")]
    assert members
    for name in members:
        if name == "kind":
            assert memory.kind == "in-memory"
            assert postgres.kind == "postgres"
            continue
        assert callable(getattr(memory, name)), name
        assert callable(getattr(postgres, name)), name


def test_in_memory_state_keeps_every_feed_under_its_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Память тоже под потолком: неограниченный журнал — утечка, а не демо-режим."""
    for name, value in (
        ("MAX_DECISIONS", 3),
        ("MAX_AUDIT_EVENTS", 3),
        ("MAX_NOTIFICATIONS", 3),
        ("MAX_EXPERIMENTS", 3),
        ("MAX_EVALUATION_RUNS", 3),
        ("MAX_STORED_ANSWERS", 3),
    ):
        monkeypatch.setattr(state_module, name, value)
    state = InMemoryDurableState()
    answer_ids: list[str] = []

    async def scenario() -> None:
        for index in range(5):
            await state.append_audit(
                AuditEvent(
                    actor_id="a",
                    action="query.run",
                    object_id=f"q{index}",
                    outcome="success",
                    correlation_id=f"c{index}",
                )
            )
            await state.record_decision(
                ExpertDecision(actor_id="a", action="proposal.created", object_id=f"p{index}")
            )
            await state.append_notification(
                Notification(topic="claim.superseded", message=f"n{index}")
            )
            await state.record_experiment(_tiny_experiment())
            await state.record_evaluation(
                EvaluationRun(
                    id=UUID(int=index + 1),
                    query_id=UUID(int=index + 1),
                    metrics=_metrics(),
                    passed=True,
                )
            )
            answer = _tiny_answer(f"{QUESTION} вариант {index}")
            answer_ids.append(str(answer.query_id))
            await state.put_answer(answer, owner_id="owner")

        assert [event.correlation_id for event in await state.recent_audit(limit=10)] == [
            "c4",
            "c3",
            "c2",
        ]
        assert len(await state.recent_decisions(limit=10)) == 3
        assert len(await state.recent_notifications(limit=10)) == 3
        assert len(await state.recent_experiments(limit=10)) == 3
        assert len(await state.recent_evaluations(limit=10)) == 3
        # Вытесненные копии ответов больше нельзя экспортировать.
        assert await state.get_answer(answer_ids[0]) is None
        assert await state.get_answer(answer_ids[-1]) is not None

    asyncio.run(scenario())


def test_research_workflow_is_compiled_once_and_reused(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """Один скомпилированный граф на запрос: JSON-прогон и витрина его не пересобирают.

    Прежняя сборка в конструкторе плюс пересборка в lifespan давали две
    компиляции StateGraph на старт и ещё одну на каждый вызов — при
    ``AGENT_MAX_TOOL_ROUNDS`` это заметная и ничем не оправданная цена.
    """
    builds: list[str] = []
    original = deps.build_workflow

    def spy(**kwargs: object) -> ResearchWorkflow:
        builds.append(repr(sorted(kwargs)))
        return original(**kwargs)  # type: ignore[arg-type]

    deps.build_workflow = spy  # type: ignore[method-assign]

    assert deps.workflow is deps.workflow
    long_query = client.post("/api/v1/query", json={"question": QUESTION}, headers=base)
    assert long_query.status_code == 200
    assert client.get("/api/v1/demo", headers=base).status_code == 200

    assert builds == []
    assert deps.workflow is not None


def test_demo_records_one_evaluation_and_flags_narrower_cache(
    client: TestClient, deps: AppDependencies
) -> None:
    """Витрина: прогон оценки — в серверном состоянии, кэш — со своей ценой.

    Кэш собирается в контуре доступа первого зрителя; второй зритель с более
    широкими правами не должен получать молчаливую неполноту — отметка в
    ``degradation_reasons`` и есть честная цена такого кэша.
    """
    email = "http-demo-late@mindai.tech"
    headers = cookie_header(signup(client, email))

    first = client.get("/api/v1/demo", headers=headers)
    query_id = first.json()["answer"]["query_id"]
    make_expert(deps.accounts, email)
    second = client.get("/api/v1/demo", headers=headers)
    runs = client.get("/api/v1/evaluations", headers=headers).json()

    assert first.status_code == 200
    assert second.json()["answer"]["query_id"] == query_id
    assert [item["query_id"] for item in runs].count(query_id) == 1
    # Отметка именно про контур доступа: seeds-долг корпуса есть у обоих ответов
    # и маскировать её нельзя.
    reasons_first = first.json()["answer"]["degradation_reasons"]
    reasons_second = second.json()["answer"]["degradation_reasons"]
    assert not any("контуре доступа" in item for item in reasons_first)
    assert any("контуре доступа" in item for item in reasons_second)
