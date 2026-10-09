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
import socket
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import unquote
from uuid import UUID

import fitz
import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable

from scientific_tangle.agents.workflow import ResearchWorkflow
from scientific_tangle.api.app import (
    CANCELLED_RUN_AGENT,
    CANCELLED_STREAM_AGENT,
    DEMO_QUESTION,
    AppDependencies,
    app,
)
from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    AgentControlDecision,
    AnswerPayload,
    CritiqueResult,
    DocumentReceipt,
    DocumentRequest,
    EntityMergeProposal,
    EntityResolutionProposal,
    EvaluationMetrics,
    EvaluationRun,
    EvidenceLocator,
    EvolutionDraft,
    EvolutionExperiment,
    EvolutionProposal,
    ExpertDecision,
    Finding,
    GraphSnapshot,
    Notification,
    PipelineVariantMetrics,
    ReasoningResult,
)
from scientific_tangle.domain.hypotheses import HypothesisSignal, HypothesisWindow
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
from scientific_tangle.services.knowledge import FindingWindow, InMemoryKnowledgeBase
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

QUESTION = "Как сократить время обработки запросов?"
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
    # Транспорт проверяется на согласованной цитате: seed-тезис содержит «31»,
    # которого нет в его доказательстве, и теперь непроверенный черновик скрывается.
    pilot = dependencies.knowledge.claim_history("finding-ro-pilot")[-1]
    pilot.statement = pilot.evidence[0].quote
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


def test_answer_history_is_private_and_saved_answers_can_be_reopened(
    client: TestClient, base: dict[str, str]
) -> None:
    query_id = _run_query(client, base)
    stranger = cookie_header(signup(client, "http-history-stranger@mindai.tech"))

    history = client.get("/api/v1/answers?limit=10&offset=0", headers=base)
    saved = client.get(f"/api/v1/answers/{query_id}", headers=base)
    stranger_history = client.get("/api/v1/answers?limit=10&offset=0", headers=stranger)
    stranger_saved = client.get(f"/api/v1/answers/{query_id}", headers=stranger)

    assert history.status_code == 200
    assert history.json()["items"][0]["query_id"] == query_id
    assert history.json()["has_more"] is False
    assert saved.status_code == 200
    assert saved.json()["query_id"] == query_id
    assert stranger_history.status_code == 200
    assert stranger_history.json()["items"] == []
    assert stranger_saved.status_code == 404


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
    """Честный контракт деградации: память = история не переживает перезапуск.

    Код — 503: провайдер в этом контуре scripted, а готовым считается только
    GigaChat-контур. Тело остаётся полным SystemStatus, чтобы причину можно было
    прочитать, а не только поймать статусом.
    """
    deps.state = InMemoryDurableState()

    ready = client.get("/health/ready")

    assert ready.status_code == 503
    body = ready.json()
    assert body["status"] == "degraded"
    assert body["state_backend"] == "in-memory"
    assert body["services"]["server_state"] == "fallback"
    assert body["agent_runs_limit"] >= 1
    assert body["agent_runs_active"] == 0


def test_health_ready_is_200_only_for_the_gigachat_contour(
    client: TestClient, deps: AppDependencies
) -> None:
    """Единственное условие readiness — настроенная модель (``model_mode``).

    Проверяется без живого GigaChat: маршруту важен режим, зафиксированный
    провайдером. Без этого pins 503 въедался бы в любой контур, где модель есть.
    """
    deps.provider = _ConfiguredModeProvider()

    ready = client.get("/health/ready")

    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert body["model_mode"] == "gigachat"
    assert body["services"]["model_provider"] == "configured"
    assert body["degradation_reasons"] == []


class _ConfiguredModeProvider:
    """Провайдер «как будто GigaChat»: для readiness важен только режим."""

    mode = "gigachat"

    async def complete_model(self, system: str, user: str, schema: type) -> object:
        raise AssertionError("в этом тесте модель не вызывается")


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
        def ingest(
            self,
            document: object,
            extraction: ExtractionResult,
            **kwargs: object,
        ) -> DocumentReceipt:
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


# ── Отмена прогона: закрытая вкладка оставляет след ────────────────────────


class _CancelledWorkflow:
    """Рабочий процесс, снятый с очереди на середине прогона.

    Так выглядит отмена: uvicorn снимает задачу ответа ``CancelledError``, и
    ``finally`` с ``_finalize_answer`` за циклом событий не выполняется. Точка
    выброса здесь — ``run`` и середина ``stream``; механизм тот же самый.
    """

    async def run(self, query: object, *, allowed_data_classes: object) -> AnswerPayload:
        raise asyncio.CancelledError

    async def stream(
        self, query: object, allowed: object
    ) -> AsyncIterator[tuple[str, dict[str, object]]]:
        yield "planner", {"trace": []}
        raise asyncio.CancelledError


@pytest.fixture
def quiet_client(deps: AppDependencies) -> Iterator[TestClient]:
    """Клиент, который не превращает отмену в падение теста.

    ``CancelledError`` обязан уйти в ASGI-слой (так и есть), и с
    ``raise_server_exceptions=False`` TestClient отдаёт заготовленный 500 вместо
    выброса: в продакшене клиента уже нет, и важен не статус, а след прогона.
    """
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.app.state.dependencies = deps
        yield test_client


def _agent_failures(deps: AppDependencies, agent: str) -> int:
    """Число провалов конкретного прогона в агентных метриках (реестр общий)."""
    return next(
        (item.failures for item in deps.metrics.snapshot().agents if item.agent == agent),
        0,
    )


def test_cancelled_json_run_leaves_audit_and_metric_trace(
    quiet_client: TestClient, deps: AppDependencies
) -> None:
    """Отменённый JSON-прогон: акт с ``correlation_id``, провал в метриках, слот свободен.

    Раньше такой прогон исчезал молча: ни акта, ни единицы в
    ``mindai_agent_runs_total``, и ``success_rate`` считался только по долетевшим.
    Серверную копию ответа при этом заводить нельзя — половины ответа в продукте
    нет, поэтому проверяется и отсутствие ``query.run``/оценок.
    """
    deps.workflow = _CancelledWorkflow()  # type: ignore[assignment]
    trace = "corr-cancel-json"
    headers = {
        **cookie_header(signup(quiet_client, "http-cancel-json@mindai.tech")),
        "X-Correlation-Id": trace,
    }
    before = _agent_failures(deps, CANCELLED_RUN_AGENT)

    quiet_client.post("/api/v1/query", json={"question": QUESTION}, headers=headers)

    events = asyncio.run(deps.state.recent_audit(limit=50))
    actions = {event.action for event in events}
    cancelled = [event for event in events if event.action == "query.cancelled"]
    assert [(event.outcome, event.correlation_id) for event in cancelled] == [
        ("failure", trace)
    ]
    # Финала не было: ни ``query.run``, ни оценки, ни серверной копии.
    assert "query.run" not in actions
    assert asyncio.run(deps.state.recent_evaluations(limit=10)) == []
    assert _agent_failures(deps, CANCELLED_RUN_AGENT) == before + 1
    assert deps.admission.active == 0


def test_total_count_header_is_exposed_to_the_browser() -> None:
    """``X-Total-Count`` обязан быть в ``expose_headers``: без этого браузер его не читает.

    Замер в настоящем браузере: запрос с ``localhost:5173`` до ``127.0.0.1:47815``
    возвращал заголовок, но ``response.headers.get('X-Total-Count')`` давал
    ``null`` — CORSMiddleware публикует только простые заголовки ответа. Экран
    «показаны первые N из M» на этом молча превращается в «показаний нет».
    """
    from fastapi.middleware.cors import CORSMiddleware

    options = next(
        item.kwargs for item in app.user_middleware if item.cls is CORSMiddleware
    )
    exposed = {name.lower() for name in options["expose_headers"]}
    assert "x-total-count" in exposed
    assert "x-correlation-id" in exposed
    # Оговорка окна — тот же класс: без публикации браузер её не прочитает, и
    # «хранилище не ответило, окно из каталога процесса» останется внутри сервиса.
    assert "x-window-note" in exposed


def test_access_layer_is_not_base_http_middleware() -> None:
    """Слой доступа обязан оставаться чистым ASGI, иначе отмена в роуте сломается.

    ``BaseHTTPMiddleware`` запускает обработчик в своей задаче и подменяет канал
    ``receive``, из-за чего ``request.is_disconnected()`` внутри маршрута обрыв
    соединения не видит. Проверено замером на реальном сокете: с этим слоем
    медленный JSON-прогон доходил до конца, без него — снимался через 2–3 с после
    ухода клиента. Правка доступа на чистом ASGI-классе поэтому не стилистическая.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    from scientific_tangle.api.app import AccessMiddleware

    classes = [item.cls for item in app.user_middleware]
    assert BaseHTTPMiddleware not in classes
    assert AccessMiddleware in classes


def test_storage_reads_run_on_the_named_bounded_pool() -> None:
    """Блокирующие чтения идут в именованном пуле заданного размера.

    До правки ``asyncio.to_thread`` уходил в default executor цикла, размер
    которого считается от ядер хоста: медленный обход графа занимал потоки, и
    список находок ждал не хранилище, а освободившийся поток. Проверяется и то,
    что после бури счётчики не остаются занятыми.
    """
    import threading

    from prometheus_client import REGISTRY

    from scientific_tangle.api.app import _configure_storage_pool

    def probe() -> str:
        time.sleep(0.02)
        return threading.current_thread().name

    async def scenario() -> list[str]:
        pool = _configure_storage_pool(Settings(storage_thread_pool_size=3))
        try:
            return list(await asyncio.gather(*(asyncio.to_thread(probe) for _ in range(9))))
        finally:
            pool.shutdown(wait=False)

    names = asyncio.run(scenario())

    assert all(name.startswith("storage") for name in names), names
    assert len(set(names)) <= 3
    sample = REGISTRY.get_sample_value
    assert sample("mindai_storage_pool_threads", {"state": "in_flight"}) == 0
    assert sample("mindai_storage_pool_threads", {"state": "queued"}) == 0
    assert sample("mindai_storage_pool_threads", {"state": "limit"}) == 3


def test_storage_pool_forwards_arguments_to_submitted_work() -> None:
    """Обёртка метрик не съедает аргументы: пул — default executor всего цикла.

    ``set_default_executor`` ставит пул исполнителем не только для
    ``asyncio.to_thread``. ``asyncio.to_thread`` передаёт работу уже свёрнутой в
    ``partial`` и для обёртки с нулевым вызовом оставался рабочим путём, а вот
    ``loop.run_in_executor`` с аргументами — нет: через него идёт сетевое
    разрешение имён стандартной библиотеки (``getaddrinfo`` получает шесть
    аргументов), и нулевой обёрнутый вызов ронял ``TypeError`` в любом соединении
    с Neo4j, Elasticsearch или GigaChat. Тест держит оба пути — прямой вызов с
    позиционными и именованными аргументами и разрешение имени.
    """
    from prometheus_client import REGISTRY

    from scientific_tangle.api.app import _configure_storage_pool

    def work(first: int, second: int, third: str) -> str:
        return f"{first}/{second}/{third}"

    async def scenario() -> tuple[str, str, list[Any]]:
        pool = _configure_storage_pool(Settings(storage_thread_pool_size=2))
        try:
            loop = asyncio.get_running_loop()
            via_executor = await loop.run_in_executor(None, work, 1, 2, "из потока")
            via_thread = await asyncio.to_thread(work, 3, 4, third="to_thread")
            addresses = await loop.getaddrinfo("localhost", 46617, type=socket.SOCK_STREAM)
            return via_executor, via_thread, addresses
        finally:
            pool.shutdown(wait=False)

    via_executor, via_thread, addresses = asyncio.run(scenario())

    assert via_executor == "1/2/из потока"
    assert via_thread == "3/4/to_thread"
    assert addresses
    sample = REGISTRY.get_sample_value
    assert sample("mindai_storage_pool_threads", {"state": "in_flight"}) == 0
    assert sample("mindai_storage_pool_threads", {"state": "queued"}) == 0


def test_undersized_storage_pool_is_named_at_startup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Маленький пул при широком приёме — предупреждение на старте, а не тайна."""

    from scientific_tangle.api.app import _configure_storage_pool

    async def scenario() -> None:
        with caplog.at_level(logging.WARNING):
            _configure_storage_pool(
                Settings(storage_thread_pool_size=2, agent_max_concurrent_runs=8)
            )

    asyncio.run(scenario())
    assert "STORAGE_THREAD_POOL_SIZE=2" in caplog.text


def test_startup_warms_the_knowledge_store_and_survives_its_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Старт прогревает хранилище, а его отказ и таймаут не мешают подняться.

    Раньше схема Neo4j, индексы Elasticsearch, подъём каталога и доводка старых
    записей были ленивыми: первый запрос аналитика платил за них секундами при
    полностью исправном контуре. Прогретое хранилище не повод не запускать сервис:
    готовность измеряет ``/health/ready``, а зависший ответ обрезается потолком.

    Проверяется на настоящих зависимостях приложения: тестовый ``deps`` подменяется
    уже после входа в lifespan и для старта не показателен.
    """
    from scientific_tangle.api.app import get_dependencies

    store = get_dependencies().knowledge
    calls: list[int] = []
    monkeypatch.setattr(store, "warmup", lambda: calls.append(len(calls)))
    with TestClient(app) as warmed:
        assert warmed.get("/health/live").status_code == 200
    assert calls == [0]

    def broken() -> None:
        raise ServiceUnavailable("Elasticsearch не отвечает")

    monkeypatch.setattr(store, "warmup", broken)
    with TestClient(app) as degraded:
        assert degraded.get("/health/live").status_code == 200

    def hang() -> None:
        time.sleep(0.5)

    monkeypatch.setattr("scientific_tangle.api.app.STORAGE_WARMUP_SECONDS", 0.01)
    monkeypatch.setattr(store, "warmup", hang)
    with TestClient(app) as patient:
        assert patient.get("/health/live").status_code == 200


def test_cancelled_stream_leaves_trace_without_a_partial_answer(
    quiet_client: TestClient, deps: AppDependencies
) -> None:
    """Обрыв SSE на середине: ответа нет, а след прогона обязан быть.

    Здесь проверяются серверные обязательства (акт ``query.stream.cancelled``,
    отсутствие финала и оценки, освобождённый слот). Что именно дошло клиенту,
    через этот транспорт не проверяется: если приложение подняло исключение
    посреди стрима, ASGI-транспорт TestClient не отдаёт уже отправленные чанки.
    На реальном сокете uvicorn событие ``start`` до клиента доходит — это
    замерялось отдельно (см. docs/architecture-review/review.md).
    """
    deps.workflow = _CancelledWorkflow()  # type: ignore[assignment]
    trace = "corr-cancel-stream"
    headers = {
        **cookie_header(signup(quiet_client, "http-cancel-stream@mindai.tech")),
        "X-Correlation-Id": trace,
    }
    before = _agent_failures(deps, CANCELLED_STREAM_AGENT)
    events: list[dict[str, Any]] = []

    with quiet_client.stream(
        "POST", "/api/v1/query/stream", json={"question": QUESTION}, headers=headers
    ) as stream:
        assert stream.status_code == 200
        for line in stream.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line.removeprefix("data:").strip()))

    # Ответ сформирован не был: ни события answer, ни done (см. docstring —
    # какие именно чанки видны транспорту, здесь не предмет проверки).
    assert [event["type"] for event in events] in ([], ["start"])
    stored = asyncio.run(deps.state.recent_audit(limit=50))
    cancelled = [event for event in stored if event.action == "query.stream.cancelled"]
    assert [(event.outcome, event.correlation_id) for event in cancelled] == [
        ("failure", trace)
    ]
    assert "query.stream" not in {event.action for event in stored}
    assert asyncio.run(deps.state.recent_evaluations(limit=10)) == []
    assert _agent_failures(deps, CANCELLED_STREAM_AGENT) == before + 1
    # Слот освобождён и в finally генератора, и в background-задаче: отмена не
    # должна оставляать занятый приём на весь дедлайн.
    assert deps.admission.active == 0


# ── Недоступная модель: отказ до открытия потока ───────────────────────────


def test_stream_fails_fast_with_503_when_the_model_is_unavailable(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """Режим модели известен до ``stream`` → настоящий 503, а не 200 с ``error``.

    Форма отказа — та же, что у JSON-пути (обработчик ``ModelUnavailableError``),
    и слот приёма не остаётся занятым: ``/query`` и ``/query/stream`` не должны
    различаться тем, что один честно отказывает, а второй притворяется стримом.
    """
    deps.provider = UnavailableProvider()
    deps.workflow = ResearchWorkflow(
        knowledge=deps.knowledge,
        provider=deps.provider,
        metrics=deps.metrics,
        settings=deps.settings,
    )

    refused = client.post("/api/v1/query/stream", json={"question": QUESTION}, headers=base)

    assert refused.status_code == 503
    assert refused.headers["content-type"].startswith("application/json")
    assert "event-stream" not in refused.headers["content-type"]
    assert refused.json()["detail"]
    assert deps.admission.active == 0


def test_json_and_stream_refusals_have_the_same_shape(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """Один и тот же отказ на обоих путях: ``{"detail": ...}`` + 503.

    Разные формы отказа означают, что клиенту нужны две ветки обработки; здесь
    они намеренно совпадают, поэтому сверяются напрямую.
    """
    deps.provider = UnavailableProvider()
    deps.workflow = ResearchWorkflow(
        knowledge=deps.knowledge,
        provider=deps.provider,
        metrics=deps.metrics,
        settings=deps.settings,
    )

    json_route = client.post("/api/v1/query", json={"question": QUESTION}, headers=base)
    stream_route = client.post("/api/v1/query/stream", json={"question": QUESTION}, headers=base)

    assert json_route.status_code == stream_route.status_code == 503
    assert set(json_route.json()) == set(stream_route.json()) == {"detail"}


def test_stream_keeps_the_in_stream_error_event_for_midrun_failures(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """Событие ``error`` остаётся для сбоев посреди прогона: отказ заранее
    неизвестный не может быть отдан статусом."""

    class _MidrunFailure:
        async def run(self, query: object, *, allowed_data_classes: object) -> AnswerPayload:
            raise RuntimeError("здесь нечего ловить")

        async def stream(
            self, query: object, allowed: object
        ) -> AsyncIterator[tuple[str, dict[str, object]]]:
            yield "reasoner", {"trace": []}
            raise RuntimeError("узел развалился после start")

    deps.workflow = _MidrunFailure()  # type: ignore[assignment]
    events: list[dict[str, Any]] = []
    with client.stream(
        "POST", "/api/v1/query/stream", json={"question": QUESTION}, headers=base
    ) as stream:
        assert stream.status_code == 200
        for line in stream.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line.removeprefix("data:").strip()))

    kinds = [event["type"] for event in events]
    assert kinds == ["start", "error"]
    assert events[-1]["code"] == "workflow_failed"


# ── Сбой хранилища: 503 вместо 500 ─────────────────────────────────────────


def _break_graph_reads(monkeypatch: pytest.MonkeyPatch, deps: AppDependencies) -> None:
    """Все чтения графа и очередей падают так, как падает недоступное хранилище."""

    def unavailable(*args: object, **kwargs: object) -> object:
        raise ServiceUnavailable("Не удалось установить соединение с neo4j:7687")

    for name in (
        "full_graph",
        "all_findings",
        "findings_window",
        "facts_window",
        "corpus_stats",
        "claim_history",
    ):
        monkeypatch.setattr(deps.knowledge, name, unavailable)
    monkeypatch.setattr(deps.resolution, "list_proposals", unavailable)

    async def unavailable_state(*args: object, **kwargs: object) -> object:
        raise psycopg.OperationalError("сервер закрыл соединение")

    monkeypatch.setattr(deps.state, "recent_proposals", unavailable_state)


def test_storage_outage_is_service_unavailable_not_an_application_crash(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    expert: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Маршруты чтения графа обязаны различать «базы нет» и «приложение упало».

    Без отображения недоступный Neo4j/Elasticsearch/Postgres давал 500 с пустым
    объяснением: мониторинг звал разработчика, а аналитик видел красный экран
    вместо «хранилище не отвечает».
    """
    _break_graph_reads(monkeypatch, deps)
    routes = (
        ("/api/v1/graph", base),
        ("/api/v1/findings", base),
        ("/api/v1/conflicts", base),
        ("/api/v1/corpus/stats", base),
        ("/api/v1/claims/finding-ro/history", base),
        ("/api/v1/dashboard", base),
        ("/api/v1/proposals", expert),
        ("/api/v1/entity-resolution/proposals", expert),
    )

    for path, headers in routes:
        response = client.get(path, headers=headers)
        assert response.status_code == 503, f"{path}: {response.text}"
        assert "Хранилище знаний недоступно" in response.json()["detail"], path
        assert response.headers["X-Correlation-Id"], path


@pytest.mark.parametrize(
    "error",
    (
        ServiceUnavailable("нет bolt-соединения"),
        httpx.ConnectError("connection refused"),
        psycopg.OperationalError("пул закрыт"),
    ),
    ids=("neo4j", "http", "postgres"),
)
def test_every_storage_transport_family_maps_to_503(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """Driver Neo4j, HTTP-транспорт и psycopg — одна и та же судьба: 503.

    Список классов ошибок собран по реальным импортам ``services/infrastructure``
    (neo4j, elasticsearch) и транспорта эмбеддингов/состояния (httpx, psycopg),
    а не по догадкам: ложный класс в списке означал бы, что часть сбоев
    по-прежнему уходит как 500.
    """

    def unavailable() -> object:
        raise error

    monkeypatch.setattr(deps.knowledge, "corpus_stats", unavailable)

    response = client.get("/api/v1/corpus/stats", headers=base)

    assert response.status_code == 503
    assert "Хранилище знаний недоступно" in response.json()["detail"]


def test_application_bug_is_not_disguised_as_a_storage_outage(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ValueError`` из чтения остаётся ошибкой кода, а не «хранилище не отвечает».

    Широкий ``except Exception`` превратил бы каждый баг в 503 и выключил бы
    тревогу там, где она нужнее всего.
    """

    def broken(*args: object, **kwargs: object) -> object:
        raise ValueError("граф вернул структуру вне схемы")

    monkeypatch.setattr(deps.knowledge, "all_findings", broken)
    monkeypatch.setattr(deps.knowledge, "facts_window", broken)

    with pytest.raises(ValueError):
        client.get("/api/v1/findings", headers=base)


# ── Пагинация списков: тело остаётся массивом ──────────────────────────────


def _finding_window(count: int, disputed: int = 0) -> list[Finding]:
    return [
        Finding(
            id=f"finding-{index}",
            subject="Обессоливание",
            statement=f"Тезис {index} для проверки окна выдачи.",
            confidence=0.7,
            evidence=[
                EvidenceLocator(
                    document_id=UUID(int=index + 1),
                    source_title=f"Источник {index}",
                    page=1,
                    quote=f"Тезис {index} для проверки окна выдачи.",
                )
            ],
            predicate="HAS_PROPERTY",
            object=f"значение {index}",
            status="disputed" if index < disputed else "consensus",
        )
        for index in range(count)
    ]


def _window_reader(items: list[Finding]):
    """Чтение окна с теми же предикатами, что у хранилища: статус и субъект."""

    def read(
        *,
        limit: int,
        offset: int,
        allowed_data_classes: object = None,
        status: str | None = None,
        subject: str | None = None,
    ) -> FindingWindow:
        selected = [item for item in items if status is None or item.status == status]
        if subject:
            selected = [
                item for item in selected if subject.lower() in (item.subject or "").lower()
            ]
        return FindingWindow(
            findings=selected[offset : offset + limit],
            total=len(selected),
            offset=offset,
            limit=limit,
            note=None,
        )

    return read


def test_findings_and_conflicts_are_paginated_with_a_total_header(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``limit``/``offset`` при открытом потоке находок, ``X-Total-Count`` — потолок.

    Тело остаётся массивом: фронтенд (``frontend/src/lib/api.ts``) читает эти
    маршруты как список, и превращать ответ в объект ради одного числа нельзя.
    Считается полное число подходящих записей, а не длина окна.
    """
    monkeypatch.setattr(deps.knowledge, "all_findings", lambda allowed=None: _finding_window(5, 2))
    monkeypatch.setattr(
        deps.knowledge,
        "facts_window",
        _window_reader(_finding_window(5, 2)),
    )
    monkeypatch.setattr(
        deps.knowledge,
        "findings_window",
        _window_reader(_finding_window(5, 2)),
    )

    window = client.get("/api/v1/findings", headers=base, params={"limit": 2, "offset": 1})
    default = client.get("/api/v1/findings", headers=base)
    conflicts = client.get("/api/v1/conflicts", headers=base, params={"limit": 1})
    beyond = client.get("/api/v1/findings", headers=base, params={"offset": 9})

    assert window.status_code == 200
    assert isinstance(window.json(), list)
    assert [item["id"] for item in window.json()] == ["finding-1", "finding-2"]
    assert window.headers["X-Total-Count"] == "5"
    assert len(default.json()) == 5
    # В конфликтах считается только disputed — экран «N расхождений» сходится с
    # выгрузкой, а не с размером всего корпуса.
    assert conflicts.headers["X-Total-Count"] == "2"
    assert [item["status"] for item in conflicts.json()] == ["disputed"]
    assert beyond.json() == []
    assert beyond.headers["X-Total-Count"] == "5"


def test_hypotheses_list_is_paginated(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = [
        HypothesisSignal(
            id=UUID(int=index + 1),
            kind="improvement_opportunity",
            statement=f"Короткая гипотеза {index}",
            proposal=None,
            evidence=[
                {
                    "document_id": UUID(int=100 + index),
                    "source_title": f"Источник {index}",
                    "page": 1,
                    "quote": f"Проверяемое свидетельство {index}",
                }
            ],
            confidence=0.7,
            data_class=DataClass.PUBLIC,
        )
        for index in range(3)
    ]
    calls: list[dict[str, object]] = []

    def read_window(*, limit: int, offset: int, allowed_data_classes: set[DataClass]):
        calls.append(
            {"limit": limit, "offset": offset, "allowed_data_classes": allowed_data_classes}
        )
        return HypothesisWindow(
            signals=signals[offset : offset + limit],
            total=len(signals),
            limit=limit,
            offset=offset,
        )

    monkeypatch.setattr(deps.knowledge, "hypotheses_window", read_window, raising=False)

    response = client.get(
        "/api/v1/hypotheses", headers=base, params={"limit": 1, "offset": 1}
    )

    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert response.json()[0]["statement"] == "Короткая гипотеза 1"
    assert response.headers["X-Total-Count"] == "3"
    assert calls == [
        {"limit": 1, "offset": 1, "allowed_data_classes": {DataClass.PUBLIC, DataClass.INTERNAL}}
    ]


def test_hypotheses_list_respects_data_class_acl(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[set[DataClass]] = []

    def read_window(*, limit: int, offset: int, allowed_data_classes: set[DataClass]):
        calls.append(allowed_data_classes)
        signals = [
            HypothesisSignal(
                id=UUID(int=201),
                kind="bottleneck",
                statement="Закрытый сигнал",
                evidence=[
                    {
                        "document_id": UUID(int=202),
                        "source_title": "Внутренний отчёт",
                        "page": 1,
                        "quote": "Ограничение требует проверки",
                    }
                ],
                confidence=0.8,
                data_class=DataClass.RESTRICTED,
            )
        ]
        visible = [signal for signal in signals if signal.data_class in allowed_data_classes]
        return HypothesisWindow(
            signals=visible[offset : offset + limit],
            total=len(visible),
            limit=limit,
            offset=offset,
        )

    monkeypatch.setattr(deps.knowledge, "hypotheses_window", read_window, raising=False)

    response = client.get("/api/v1/hypotheses", headers=base)

    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"
    assert calls == [{DataClass.PUBLIC, DataClass.INTERNAL}]


def test_hypotheses_list_requires_knowledge_read(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scientific_tangle.api import app as app_module

    monkeypatch.setattr(
        app_module.access_engine,
        "has_permission",
        lambda principal, permission: False,
    )
    monkeypatch.setattr(
        deps.knowledge,
        "hypotheses_window",
        lambda **kwargs: pytest.fail("запрос к хранилищу выполнен до проверки права"),
        raising=False,
    )

    response = client.get("/api/v1/hypotheses", headers=base)

    assert response.status_code == 403
    assert response.json()["detail"] == "Требуется разрешение: knowledge:read"


def test_hypothesis_detail_hides_signals_outside_acl(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_id = UUID(int=303)
    calls: list[set[DataClass]] = []

    def find_signal(requested_id: UUID, *, allowed_data_classes: set[DataClass]):
        calls.append(allowed_data_classes)
        assert requested_id == signal_id
        return None

    monkeypatch.setattr(deps.knowledge, "hypothesis_by_id", find_signal, raising=False)

    response = client.get(f"/api/v1/hypotheses/{signal_id}", headers=base)

    assert response.status_code == 404
    assert response.json()["detail"] == "Гипотеза не найдена"
    assert calls == [{DataClass.PUBLIC, DataClass.INTERNAL}]


def test_findings_list_reads_the_window_from_the_store(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Список без фильтров читает окно хранилища, а не весь каталог процесса.

    Маршрут резал в процессе уже скопированный корпус: на боевом контуре это
    глубокая копия всех находок на каждый экран, а ``X-Total-Count`` считался по
    RAM-каталогу с его потолком ``CATALOG_RESTORE_LIMIT``. Окно хранилища копирует
    ровно страницу и приносит полную численность из индекса.
    """
    calls: list[dict[str, object]] = []

    def window_read(
        *,
        limit: int,
        offset: int,
        allowed_data_classes: object = None,
        status: str | None = None,
        subject: str | None = None,
    ) -> FindingWindow:
        calls.append({"limit": limit, "offset": offset, "status": status, "subject": subject})
        return FindingWindow(
            findings=_finding_window(5, 2)[offset : offset + limit],
            total=5,
            offset=offset,
            limit=limit,
            note="Elasticsearch не ответил, окно каталога взято из RAM-каталога процесса.",
        )

    def no_catalog_read(allowed: object = None) -> list[Finding]:
        raise AssertionError("список без фильтров не обязан читать весь каталог")

    monkeypatch.setattr(deps.knowledge, "facts_window", window_read)
    monkeypatch.setattr(deps.knowledge, "all_findings", no_catalog_read)

    response = client.get("/api/v1/findings", headers=base, params={"limit": 2, "offset": 1})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == ["finding-1", "finding-2"]
    assert response.headers["X-Total-Count"] == "5"
    assert calls == [{"limit": 2, "offset": 1, "status": None, "subject": None}]
    # Оговорка окна доходит до клиента: заголовок уходит как latin-1, поэтому
    # русский текст закодирован и читается через decode.
    assert unquote(response.headers["X-Window-Note"]).startswith("Elasticsearch не ответил")

    filtered = client.get(
        "/api/v1/findings", headers=base, params={"subject": "Обесс", "status": "disputed"}
    )
    assert filtered.status_code == 200
    # Фильтры уходят в хранилище предикатами, а не вырезаются из каталога: только
    # так страница не зависит от того, сколько корпуса поднялось в RAM.
    assert calls[-1] == {"limit": 200, "offset": 0, "status": "disputed", "subject": "Обесс"}


def test_durable_lists_expose_offset_and_full_total(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    expert: dict[str, str],
) -> None:
    """Окно durable-лент читается хранилищем, а полное число уходит в ``X-Total-Count``.

    Без ``offset`` и точного total аналитик видел 50 строк и не мог отличить
    «больше не было» от «не дочитали» — молчаливая обрезка, которую продукт
    запрещает. Здесь: страницы не пересекаются, вторая короче, выход за границу
    даёт пустой массив с тем же total, ``/me/activity`` и ``/decisions`` считают
    только свои записи, а ``/audit`` с фильтром — total по фильтру, а не по
    всему журналу.

    Total считается от базового числа (в журнале уже есть акт ``auth.register``
    самого аккаунта), чтобы проверка не зависела от того, сколько записей
    оставил контур до сидинга.
    """
    own_id = client.get("/api/v1/auth/me", headers=base).json()["id"]

    def total_of(path: str, headers: dict[str, str]) -> int:
        return int(client.get(path, headers=headers).headers["X-Total-Count"])

    base_evaluations = total_of("/api/v1/evaluations", base)
    base_experiments = total_of("/api/v1/experiments", expert)
    base_activity = total_of("/api/v1/me/activity", base)
    base_audit = total_of("/api/v1/audit", expert)
    base_decisions = total_of("/api/v1/decisions", base)

    async def seed() -> None:
        # Три своих акта (одна correlation-группа) и два чужих: total на
        # ``/me/activity`` обязан считать только свои, а ``/audit`` — по фильтру.
        for index in range(3):
            await deps.state.append_audit(
                AuditEvent(
                    id=UUID(int=0x100 + index),
                    actor_id=own_id,
                    action="query.run",
                    object_id=f"own-{index}",
                    outcome="success",
                    correlation_id="corr-own",
                )
            )
        for index in range(2):
            await deps.state.append_audit(
                AuditEvent(
                    id=UUID(int=0x200 + index),
                    actor_id="someone-else",
                    action="query.run",
                    object_id=f"other-{index}",
                    outcome="success",
                    correlation_id="corr-other",
                )
            )
        for index in range(3):
            await deps.state.record_evaluation(
                EvaluationRun(
                    id=UUID(int=0x300 + index),
                    query_id=UUID(int=index),
                    metrics=_metrics(),
                    passed=True,
                )
            )
            await deps.state.record_experiment(
                _tiny_experiment().model_copy(update={"id": UUID(int=0x400 + index)})
            )
        # Решения: три своих и два чужих. На ``/decisions`` чужие не должны
        # появляться ни на странице, ни в полной численности.
        for index in range(3):
            await deps.state.record_decision(
                ExpertDecision(
                    id=UUID(int=0x500 + index),
                    actor_id=own_id,
                    action="answer.exported",
                    object_id=f"own-answer-{index}",
                )
            )
        for index in range(2):
            await deps.state.record_decision(
                ExpertDecision(
                    id=UUID(int=0x510 + index),
                    actor_id="someone-else",
                    action="proposal.reviewed",
                    object_id=f"other-proposal-{index}",
                )
            )
        # Лента уведомлений смещения не имеет (фид последних N по контракту), но
        # полный счётчик обязан отличаться от длины страницы.
        for index in range(2):
            await deps.state.append_notification(
                Notification(topic="claim.superseded", message=f"n{index}")
            )

    asyncio.run(seed())

    def check_window(
        path: str,
        headers: dict[str, str],
        key: str,
        expected_total: int,
        *,
        params: dict[str, object] | None = None,
    ) -> None:
        query = {"limit": 2, **(params or {})}
        first = client.get(path, headers=headers, params={**query, "offset": 0})
        second = client.get(path, headers=headers, params={**query, "offset": 2})
        beyond = client.get(path, headers=headers, params={**query, "offset": 9})
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert beyond.status_code == 200, beyond.text
        # Тело остаётся массивом, полное число — в заголовке, а не в новом поле.
        assert isinstance(first.json(), list)
        # Один и тот же полный счётчик на всех страницах, включая выход за границу.
        assert first.headers["X-Total-Count"] == str(expected_total), path
        assert second.headers["X-Total-Count"] == str(expected_total), path
        assert beyond.headers["X-Total-Count"] == str(expected_total), path
        ids_first = [item[key] for item in first.json()]
        ids_second = [item[key] for item in second.json()]
        # Окно читается хранилищем: страницы не пересекаются, размеры честные.
        assert len(ids_first) == min(2, expected_total), path
        assert len(ids_second) == max(0, min(2, expected_total - 2)), path
        assert set(ids_first).isdisjoint(ids_second), path
        assert beyond.json() == [], path

    # Ровно 3 подходящих акта (corr-own): вторая страница короче первой (2 → 1).
    check_window("/api/v1/audit", expert, "id", 3, params={"correlation_id": "corr-own"})
    own_page = client.get(
        "/api/v1/audit", headers=expert, params={"correlation_id": "corr-own", "limit": 2}
    )
    assert len(own_page.json()) == 2

    # Журнал собственного аккаунта: базовый акт + три своих, чужие не учтены.
    check_window("/api/v1/me/activity", base, "object_id", base_activity + 3)
    # Оценка и эксперименты: базовое число + три сидингованных.
    check_window("/api/v1/evaluations", base, "id", base_evaluations + 3)
    check_window("/api/v1/experiments", expert, "id", base_experiments + 3)
    # Решения собственного аккаунта: чужие в total не входят.
    check_window("/api/v1/decisions", base, "id", base_decisions + 3)

    # Полный журнал без фильтра — весь журнал (база + 3 своих + 2 чужих), т.е.
    # total по фильтру (3) меньше полного: фильтр меняет численность, а не страницу.
    whole_audit = total_of("/api/v1/audit", expert)
    assert whole_audit == base_audit + 5
    assert 3 < whole_audit

    # ``/me/activity`` считает только свои: чужой акт (corr-other) не виден ни на
    # одной странице, и собственный total меньше полного журнала ровно на чужие.
    foreign_ids = {f"other-{index}" for index in range(2)}
    seen_object_ids: set[str] = set()
    for page_offset in (0, 2, 4, 6):
        rows = client.get(
            "/api/v1/me/activity", headers=base, params={"limit": 2, "offset": page_offset}
        ).json()
        seen_object_ids.update(item["object_id"] for item in rows)
    assert seen_object_ids.isdisjoint(foreign_ids)
    assert all(f"own-{index}" in seen_object_ids for index in range(3))

    # То же для ленты решений: чужое экспертное действие не выходит ни на одной
    # странице, даже когда своё окно уже дочитано до конца.
    decisions: list[dict[str, object]] = []
    for page_offset in (0, 2, 4):
        decisions += client.get(
            "/api/v1/decisions", headers=base, params={"limit": 2, "offset": page_offset}
        ).json()
    assert {item["object_id"] for item in decisions} == {
        f"own-answer-{index}" for index in range(3)
    }
    assert all(item["actor_id"] == own_id for item in decisions)

    # ``/notifications`` — фид последних N без смещения (так решено контрактом),
    # но полный счётчик всё равно обязан отличаться от длины страницы: читатель
    # должен видеть, что часть событий вытеснена потолком ленты.
    feed = client.get("/api/v1/notifications", headers=base, params={"limit": 1})
    assert len(feed.json()) == 1
    assert int(feed.headers["X-Total-Count"]) > 1




def test_findings_page_ceiling_is_reported_when_request_is_larger(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если хранилище отдало меньше запрошенного лимита — клиент это видит."""

    def capped(
        *,
        limit: int,
        offset: int,
        allowed_data_classes: object = None,
        status: str | None = None,
        subject: str | None = None,
    ) -> FindingWindow:
        return FindingWindow(
            findings=_finding_window(3)[:500],
            total=3,
            offset=offset,
            limit=500,
            note=None,
        )

    monkeypatch.setattr(deps.knowledge, "facts_window", capped)
    monkeypatch.setattr(deps.knowledge, "all_findings", lambda allowed=None: _finding_window(3))

    response = client.get("/api/v1/findings", headers=base, params={"limit": 1000})

    assert "X-Window-Note" in response.headers
    note = unquote(response.headers["X-Window-Note"])
    assert "500" in note and "1000" in note


def test_pagination_bounds_are_validated(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Неотрицательный смещение и потолок лимита — как у ``/audit``."""
    monkeypatch.setattr(deps.knowledge, "all_findings", lambda allowed=None: _finding_window(3))

    assert client.get("/api/v1/findings", headers=base, params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/findings", headers=base, params={"limit": 5000}).status_code == 422
    assert client.get("/api/v1/findings", headers=base, params={"offset": -1}).status_code == 422


def test_proposal_lists_are_paginated(
    client: TestClient,
    deps: AppDependencies,
    expert: dict[str, str],
) -> None:
    """``/proposals`` и ``/entity-resolution/proposals`` — окно плюс полный счётчик."""
    for index in range(3):
        asyncio.run(
            deps.state.record_proposal(
                EvolutionProposal(
                    source_query_id=UUID(int=index + 1),
                    kind="gold_case",
                    title=f"Кейс {index}",
                    change="Зафиксировать исправленный вывод как ожидаемый ответ.",
                    impact=["critic"],
                )
            )
        )
    deps.resolution.register(
        [
            EntityResolutionProposal(
                mention="Обратный осмос",
                canonical_name="Мембранное обессоливание",
                action="link",
                confidence=0.9,
                rationale="Синонимичные технологии в обзоре и в отчёте.",
            ),
            EntityResolutionProposal(
                mention="Электродиализ",
                canonical_name="Мембранное обессоливание",
                action="link",
                confidence=0.8,
                rationale="Соседняя схема в том же обзоре.",
            ),
        ]
    )

    proposals = client.get("/api/v1/proposals", headers=expert, params={"limit": 2, "offset": 1})
    merges = client.get(
        "/api/v1/entity-resolution/proposals", headers=expert, params={"limit": 1}
    )

    assert isinstance(proposals.json(), list)
    assert [item["title"] for item in proposals.json()] == ["Кейс 1", "Кейс 0"]
    assert proposals.headers["X-Total-Count"] == "3"
    assert len(merges.json()) == 1
    # Элемент окна по-прежнему читается объявленной моделью: тело не изменилось.
    assert EntityMergeProposal.model_validate(merges.json()[0]).status == "proposed"
    assert merges.headers["X-Total-Count"] == "2"


# ── Порог чтения /metrics ──────────────────────────────────────────────────


def test_metrics_are_public_only_while_no_token_is_configured(
    client: TestClient, deps: AppDependencies, base: dict[str, str]
) -> None:
    """Без ``METRICS_TOKEN`` эндпоинт открыт (локальный контур), с токеном — Bearer.

    Проверка живёт в ``access_middleware``, а не в зависимости маршрута:
    ``/metrics`` отдаёт сторонний Instrumentator. Сессия браузера метрики не
    открывает — иначе вход превращался бы в право читать поведение корпуса.
    """
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(deps.settings, "metrics_token", None)
        assert client.get("/metrics").status_code == 200

        monkeypatch.setattr(deps.settings, "metrics_token", "tok-metrics-2026")
        assert client.get("/metrics").status_code == 401
        assert "Authorization: Bearer" in client.get("/metrics").json()["detail"]
        assert client.get("/metrics", headers=base).status_code == 401
        assert (
            client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
        )
        allowed = client.get("/metrics", headers={"Authorization": "Bearer tok-metrics-2026"})
        assert allowed.status_code == 200
        assert "mindai_agent_run_slots" in allowed.text
    finally:
        monkeypatch.undo()


# ── Кэш витрины переживает только собственную сборку ───────────────────────


class _CountingWorkflow:
    """Рабочий процесс, который считает прогоны: попадание в кэш видно по числу."""

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, query: Any, *, allowed_data_classes: object) -> AnswerPayload:
        self.runs += 1
        return _tiny_answer(f"{query.question} — прогон {self.runs}")


def test_demo_cache_is_reset_when_the_corpus_changes(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Импорт документа сбрасывает витрину: старый ответ нового корпуса не даёт.

    ``demo_answer`` кэшируется на процесс, и без сброса витрина продолжала
    отвечать по корпусу до импорта — обещание «ответ настоящий, из того же
    корпуса» переставало выполняться ровно в момент импорта.
    """
    workflow = _CountingWorkflow()
    deps.workflow = workflow  # type: ignore[assignment]

    async def fake_ingest(document: DocumentRequest) -> DocumentReceipt:
        return DocumentReceipt(
            document_id=UUID(int=9), checksum="sum", status="created", extracted_claims=1
        )

    monkeypatch.setattr(deps.ingestion, "ingest", fake_ingest)

    first = client.get("/api/v1/demo", headers=base)
    second = client.get("/api/v1/demo", headers=base)
    assert first.status_code == 200
    assert workflow.runs == 1
    assert first.json()["answer"]["query_id"] == second.json()["answer"]["query_id"]

    imported = client.post(
        "/api/v1/documents",
        json={"title": "Отчёт по пилоту", "text": "шахта " * 12},
        headers=base,
    )
    assert imported.status_code == 200
    third = client.get("/api/v1/demo", headers=base)

    assert third.status_code == 200
    assert workflow.runs == 2
    assert third.json()["answer"]["query_id"] != first.json()["answer"]["query_id"]


def test_upload_and_supersede_reset_the_demo_cache(
    client: TestClient,
    deps: AppDependencies,
    expert: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Загрузка файла и замена утверждения — те же пути, что меняют корпус."""
    workflow = _CountingWorkflow()
    deps.workflow = workflow  # type: ignore[assignment]

    async def fake_ingest(document: DocumentRequest) -> DocumentReceipt:
        return DocumentReceipt(
            document_id=UUID(int=10), checksum="sum", status="created", extracted_claims=1
        )

    def fake_parse(filename: str, content: bytes, **kwargs: object) -> DocumentRequest:
        return DocumentRequest(title="Отчёт", text="шахта " * 12)

    monkeypatch.setattr(deps.ingestion, "ingest", fake_ingest)
    monkeypatch.setattr("scientific_tangle.api.app.parse_document", fake_parse)

    assert client.get("/api/v1/demo", headers=expert).status_code == 200
    assert workflow.runs == 1
    uploaded = client.post(
        "/api/v1/documents/upload",
        files={"file": ("otchet.txt", b"shutdown text " * 10)},
        data={"data_class": "public"},
        headers=expert,
    )
    assert uploaded.status_code == 200
    assert client.get("/api/v1/demo", headers=expert).status_code == 200
    assert workflow.runs == 2

    finding_id = _restricted_finding_id(deps)
    corrected = client.post(
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
    assert corrected.status_code == 200
    assert client.get("/api/v1/demo", headers=expert).status_code == 200
    assert workflow.runs == 3


# ── Зона B: единый гейт закрытого класса, форма ошибок, честный отказ ───────


def _receipt(document_id: int) -> DocumentReceipt:
    return DocumentReceipt(
        document_id=UUID(int=document_id),
        checksum="sum",
        status="created",
        extracted_claims=1,
    )


def test_restricted_write_is_refused_on_both_import_paths_before_the_work(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    expert: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``data_class`` проверяется одним гейтом, по значению до тяжёлой работы.

    Прежний JSON-путь не смотрел на класс доступа вовсе (``knowledge:read``
    пропускал запись в restricted), а multipart проверял уже после разбора:
    отказ стоил минут OCR, неразбираемый закрытый файл давал 422 вместо 403, а
    закрытый класс, который назначил парсер, обходился ``data_class=public`` в
    форме.
    """
    calls: list[str] = []

    def fake_parse(filename: str, content: bytes, **kwargs: object) -> DocumentRequest:
        calls.append("parse")
        return DocumentRequest(title="Отчёт", text="шахта " * 12)

    async def fake_ingest(document: DocumentRequest) -> DocumentReceipt:
        calls.append("ingest")
        return _receipt(7)

    monkeypatch.setattr("scientific_tangle.api.app.parse_document", fake_parse)
    monkeypatch.setattr(deps.ingestion, "ingest", fake_ingest)
    body = {"title": "Отчёт", "text": "шахта " * 12, "data_class": "restricted"}
    files = {"file": ("zakrytyj.txt", b"shahta " * 12)}

    denied_json = client.post("/api/v1/documents", json=body, headers=base)
    denied_upload = client.post(
        "/api/v1/documents/upload", files=files, data={"data_class": "restricted"}, headers=base
    )

    assert denied_json.status_code == 403, denied_json.text
    assert denied_upload.status_code == 403, denied_upload.text
    assert "restricted" in denied_json.json()["detail"]
    # Ни извлечения, ни разбора: право проверяется по значению, известному заранее.
    assert calls == []

    # Базовый уровень вправе писать в internal: этот класс входит в его набор
    # данных (PRODUCT.md), под гейтом только restricted.
    internal = client.post(
        "/api/v1/documents",
        json={"title": "Отчёт", "text": "шахта " * 12, "data_class": "internal"},
        headers=base,
    )
    assert internal.status_code == 200, internal.text

    allowed_upload = client.post(
        "/api/v1/documents/upload", files=files, data={"data_class": "restricted"}, headers=expert
    )
    assert allowed_upload.status_code == 200, allowed_upload.text

    # Класс, который назначил парсер, тоже под гейтом: форма была бы public.
    def sneaky_parse(filename: str, content: bytes, **kwargs: object) -> DocumentRequest:
        calls.append("parse")
        return DocumentRequest(
            title="Отчёт", text="шахта " * 12, data_class=DataClass.RESTRICTED
        )

    monkeypatch.setattr("scientific_tangle.api.app.parse_document", sneaky_parse)
    before = list(calls)

    smuggled = client.post(
        "/api/v1/documents/upload", files=files, data={"data_class": "public"}, headers=base
    )

    assert smuggled.status_code == 403, smuggled.text
    assert "ingest" not in calls[len(before) :]


def test_validation_error_is_russian_and_never_echoes_the_body(
    client: TestClient, base: dict[str, str]
) -> None:
    """422 не возвращает присланные данные: текст закрытого документа — не detail.

    pydantic клал в ``detail[].input`` всё тело запроса, и restricted-фрагмент
    уезжал обратно клиенту, в логи прокси и в историю браузера.
    """
    secret = "Скрытый фрагмент restricted-отчёта о задержании солей"

    response = client.post(
        "/api/v1/documents", json={"title": "", "text": secret}, headers=base
    )

    assert response.status_code == 422
    assert secret not in response.text
    assert "input" not in response.text
    detail = response.json()["detail"]
    assert isinstance(detail, str), detail
    assert "title" in detail and "тело запроса" in detail
    assert response.headers["X-Correlation-Id"]

    # Параметр строки запроса описывается теми же русскими формулировками.
    bounded = client.get("/api/v1/findings", params={"limit": 0}, headers=base)

    assert bounded.status_code == 422
    assert "limit" in bounded.json()["detail"]
    assert "параметр запроса" in bounded.json()["detail"]


async def _state_unavailable(*args: object, **kwargs: object) -> object:
    raise psycopg.OperationalError("сервер закрыл соединение")


def test_durable_reads_and_write_paths_report_storage_outage_as_503(
    client: TestClient,
    deps: AppDependencies,
    base: dict[str, str],
    expert: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отказ хранилища честен и на durable-чтениях, и на путях записи.

    ``_storage_or_unavailable`` стоял только на девяти read-путях графа:
    выгрузка, журнал, оценки, расход и запись импорта/замены на боевом
    Postgres/Neo4j давали 500. При этом 403 и 422 обязаны остаться своими
    кодами — иначе «базы нет» вытеснило бы настоящие причины отказа.
    """
    for name in (
        "get_answer",
        "recent_audit",
        "recent_evaluations",
        "recent_decisions",
        "recent_notifications",
        "recent_experiments",
        "recent_proposals",
        "usage_by_account",
    ):
        monkeypatch.setattr(deps.state, name, _state_unavailable)

    routes = (
        ("/api/v1/audit", expert),
        ("/api/v1/evaluations", base),
        ("/api/v1/decisions", base),
        ("/api/v1/notifications", base),
        ("/api/v1/experiments", expert),
        ("/api/v1/me/usage", base),
        ("/api/v1/me/activity", base),
        ("/api/v1/proposals", expert),
    )
    for path, headers in routes:
        response = client.get(path, headers=headers)
        assert response.status_code == 503, f"{path}: {response.text}"
        assert "Хранилище знаний недоступно" in response.json()["detail"], path
        assert response.headers["X-Correlation-Id"], path

    exported = client.post(
        "/api/v1/export",
        json={"query_id": str(UUID(int=1)), "format": "markdown"},
        headers=base,
    )
    assert exported.status_code == 503
    assert "Хранилище знаний недоступно" in exported.json()["detail"]

    # Право и форма не превращаются в «хранилище не отвечает».
    assert client.get("/api/v1/audit", headers=base).status_code == 403
    assert client.get("/api/v1/findings", params={"limit": 0}, headers=base).status_code == 422

    def unavailable_graph(*args: object, **kwargs: object) -> object:
        raise ServiceUnavailable("нет bolt-соединения")

    async def unavailable_ingest(document: DocumentRequest) -> DocumentReceipt:
        raise ServiceUnavailable("нет bolt-соединения")

    monkeypatch.setattr(deps.ingestion, "ingest", unavailable_ingest)
    monkeypatch.setattr(deps.knowledge, "supersede_finding", unavailable_graph)

    imported = client.post(
        "/api/v1/documents", json={"title": "Отчёт", "text": "шахта " * 12}, headers=base
    )
    assert imported.status_code == 503, imported.text

    superseded = client.post(
        "/api/v1/feedback",
        json={
            "query_id": str(UUID(int=2)),
            "finding_id": _restricted_finding_id(deps),
            "verdict": "correct",
            "comment": "Проказ отказа хранилища на пути записи",
            "correction": "Вывод применим только после предварительной очистки.",
        },
        headers=expert,
    )
    assert superseded.status_code == 503, superseded.text
    assert "Хранилище знаний недоступно" in superseded.json()["detail"]


def _proposal(
    index: int,
    *,
    kind: str = "gold_case",
    change: str = "Фиксировать исправленный вывод как ожидаемый ответ.",
    title: str = "Кейс",
) -> EvolutionProposal:
    return EvolutionProposal(
        source_query_id=UUID(int=index + 1),
        kind=kind,  # type: ignore[arg-type]
        title=title,
        change=change,
        impact=["critic"],
    )


def _pass_the_ab_gate(deps: AppDependencies, proposal: EvolutionProposal) -> None:
    """Пройденный A/B gate: принятие prompt/rule-предложения проверяется по нему."""
    asyncio.run(
        deps.state.record_experiment(
            _tiny_experiment().model_copy(
                update={"proposal_id": proposal.id, "decision": "promote", "regressions": []}
            )
        )
    )


def test_rejecting_an_accepted_proposal_rebuilds_the_agent(
    client: TestClient, deps: AppDependencies, expert: dict[str, str]
) -> None:
    """Отклонение ранее принятого предложения снимает правку с агента.

    Пересборка рабочего процесса стояла только под ``accepted``, поэтому
    отклонённая ``CANDIDATE POLICY`` продолжала исполняться: активная политика
    пересобирается по статусам, а статус изменился — значит, промпт обязан
    пересобраться тоже.
    """
    policy = "CANDIDATE POLICY: считать сухим остаток только по протоколу пилота."
    proposal = _proposal(11, kind="prompt", change=policy, title="Единицы и протокол")
    asyncio.run(deps.state.record_proposal(proposal))
    _pass_the_ab_gate(deps, proposal)

    accepted = client.post(
        f"/api/v1/proposals/{proposal.id}/review", json={"accepted": True}, headers=expert
    )
    assert accepted.status_code == 200, accepted.text
    assert policy in deps.workflow.extra_policy

    rejected = client.post(
        f"/api/v1/proposals/{proposal.id}/review", json={"accepted": False}, headers=expert
    )

    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert deps.workflow.extra_policy == ""


def test_second_decision_on_a_proposal_keeps_the_first_one(
    client: TestClient, deps: AppDependencies, expert: dict[str, str]
) -> None:
    """Два «accept» по одному предложению больше не теряют правку: версий нет.

    Решение принимается по серверному статусу: разрешены первый обзор и явный
    откат принятого, всё остальное — 409, а не молчаливая перезапись.
    """
    proposal = _proposal(
        12, kind="prompt", change="Требовать единицу у каждого числа.", title="Единицы"
    )
    asyncio.run(deps.state.record_proposal(proposal))
    _pass_the_ab_gate(deps, proposal)

    first = client.post(
        f"/api/v1/proposals/{proposal.id}/review", json={"accepted": True}, headers=expert
    )
    assert first.status_code == 200, first.text

    again = client.post(
        f"/api/v1/proposals/{proposal.id}/review", json={"accepted": True}, headers=expert
    )
    assert again.status_code == 409, again.text
    assert "уже рассмотрено" in again.json()["detail"]
    assert [
        item["status"] for item in client.get("/api/v1/proposals", headers=expert).json()
        if item["id"] == str(proposal.id)
    ] == ["accepted"]

    reverted = client.post(
        f"/api/v1/proposals/{proposal.id}/review", json={"accepted": False}, headers=expert
    )
    assert reverted.status_code == 200, reverted.text
    assert reverted.json()["status"] == "rejected"

    assert client.post(
        f"/api/v1/proposals/{proposal.id}/review", json={"accepted": False}, headers=expert
    ).status_code == 409


def test_proposals_past_the_old_default_are_visible_and_decidable(
    client: TestClient, deps: AppDependencies, expert: dict[str, str]
) -> None:
    """Очередь читается до одного потолка всеми путями, а не только списком.

    ``/proposals`` звал ``PROPOSAL_FEED_READ_LIMIT``, а гейт «известно ли
    предложение» и hydration зеркала — ``recent_proposals()`` с дефолтом 100:
    предложения 101+ были видны в очереди, но review и experiment на них давали
    404.
    """

    async def fill(count: int) -> list[EvolutionProposal]:
        made: list[EvolutionProposal] = []
        for index in range(count):
            item = _proposal(index, title=f"Кейс {index}")
            await deps.state.record_proposal(item)
            made.append(item)
        return made

    made = asyncio.run(fill(105))
    oldest = made[0]

    feed = client.get("/api/v1/proposals", headers=expert, params={"limit": 200})

    assert feed.status_code == 200
    assert feed.headers["X-Total-Count"] == "105"
    assert str(oldest.id) in [item["id"] for item in feed.json()]

    decided = client.post(
        f"/api/v1/proposals/{oldest.id}/review", json={"accepted": False}, headers=expert
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "rejected"


def test_demo_get_refuses_a_foreign_origin(client: TestClient, deps: AppDependencies) -> None:
    """Витрина — GET, который мутирует состояние: источник проверяется и здесь.

    ``STATE_CHANGING_METHODS`` в middleware покрывает POST/PATCH/PUT/DELETE, а
    ``/demo`` пишет серверную копию ответа, акт в журнал и на первом обращении
    сжигает бюджет прогона. Перевод роута в POST сломал бы интерфейс, поэтому
    проверка источника живёт внутри обработчика.
    """
    deps.demo_answer = _tiny_answer(DEMO_QUESTION).model_copy(
        update={"query_id": UUID(int=13)}
    )
    deps.demo_evaluation = EvaluationRun(
        query_id=UUID(int=13), metrics=_metrics(), passed=True
    )
    deps.demo_access = {DataClass.PUBLIC}
    token = signup(client, "http-demo-origin@mindai.tech")

    foreign = client.get(
        "/api/v1/demo", headers=cookie_header(token, Origin="https://chuzhij-sajt.example")
    )
    cross_site = client.get(
        "/api/v1/demo", headers=cookie_header(token, **{"Sec-Fetch-Site": "cross-site"})
    )

    assert foreign.status_code == 403, foreign.text
    assert "Источник запроса" in foreign.json()["detail"]
    assert cross_site.status_code == 403, cross_site.text

    # Доверенный источник работает как раньше: витрина отдаётся без прогона.
    trusted = client.get("/api/v1/demo", headers=cookie_header(token))
    assert trusted.status_code == 200, trusted.text
    assert trusted.json()["answer"]["query_id"] == str(UUID(int=13))


class _DeadDriver:
    """Драйвер Neo4j, который не подтверждает соединение: так выглядит упавший граф."""

    def __init__(self) -> None:
        self.calls = 0

    def verify_connectivity(self, timeout: float | None = None) -> bool:
        self.calls += 1
        raise ServiceUnavailable("Не удалось установить соединение с neo4j:7687")


class _DeadCluster:
    def health(self) -> dict[str, str]:
        raise ServiceUnavailable("кластер не отвечает")


class _DeadSearch:
    cluster = _DeadCluster()


class _DeadPool:
    """Пулу psycopg не отдать соединение: ``SELECT 1`` не проходит."""

    def connection(self, timeout: float | None = None) -> None:
        raise psycopg.OperationalError("пул закрыт")


def test_readiness_probes_the_stores_and_caches_the_result(
    client: TestClient,
    deps: AppDependencies,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Готовность обязана проверять соединения, а не только читать флаги старта.

    Без проб healthcheck оставался зелёным при мёртвых Neo4j, Elasticsearch и
    Postgres, а платить за ленивое соединение приходилось первому аналитику.
    Пробы кэшируются на несколько секунд: мониторинг опрашивает роут часто, и он
    не должен каждый раз гонять ping по всему контуру.
    """
    monkeypatch.setattr("scientific_tangle.api.app._readiness_probe_cache", None)
    driver = _DeadDriver()
    monkeypatch.setattr(deps.knowledge, "_driver", driver, raising=False)
    monkeypatch.setattr(deps.knowledge, "_search", _DeadSearch(), raising=False)
    monkeypatch.setattr(deps.state, "_pool", _DeadPool(), raising=False)
    monkeypatch.setattr(deps.accounts, "_pool", _DeadPool(), raising=False)

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    # Независимо от того, каким окажется значение в контракте, «настроен» уже нет.
    assert body["services"]["knowledge_graph"] not in {"configured", "fallback"}
    assert body["services"]["hybrid_search"] not in {"configured", "fallback"}
    assert body["services"]["server_state"] not in {"configured", "fallback"}
    assert any("Neo4j" in reason for reason in body["degradation_reasons"])
    assert any("Elasticsearch" in reason for reason in body["degradation_reasons"])
    assert any("Postgres" in reason for reason in body["degradation_reasons"])

    client.get("/health/ready")

    assert driver.calls == 1


class _LiveConnection:
    """Живой Postgres: ``SELECT 1`` проходит, соединение не брошено."""

    async def execute(self, query: str) -> None:
        assert query == "SELECT 1"


class _LiveConnectionContext:
    async def __aenter__(self) -> _LiveConnection:
        return _LiveConnection()

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _LivePool:
    def connection(self, timeout: float | None = None) -> _LiveConnectionContext:
        return _LiveConnectionContext()


class _LiveDriver:
    """Драйвер Neo4j 6.x: подтверждает соединение и возвращает ``None``.

    Ловушка пробы: решение об отказе даёт исключение, а не булево значение,
    иначе здоровый граф выглядел бы упавшим.
    """

    def verify_connectivity(self) -> None:
        return None


class _LiveCluster:
    def health(self) -> dict[str, str]:
        return {"status": "green"}


class _LiveSearch:
    cluster = _LiveCluster()


def test_readiness_does_not_invent_an_outage(
    client: TestClient, deps: AppDependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Живые пробы не должны выглядеть деградацией.

    Тот же контур, что и в тесте с упавшими хранилищами, но BaseService отвечают:
    причин «не отвечает» в теле не появляется, и кодом остаётся прежний 503 без
    настроенной модели.
    """
    monkeypatch.setattr("scientific_tangle.api.app._readiness_probe_cache", None)
    monkeypatch.setattr(deps.knowledge, "_driver", _LiveDriver(), raising=False)
    monkeypatch.setattr(deps.knowledge, "_search", _LiveSearch(), raising=False)
    monkeypatch.setattr(deps.state, "_pool", _LivePool(), raising=False)
    monkeypatch.setattr(deps.accounts, "_pool", _LivePool(), raising=False)

    response = client.get("/health/ready")

    body = response.json()
    assert not any("не отвечает" in reason for reason in body["degradation_reasons"])
    assert body["services"]["knowledge_graph"] == "fallback"
    assert body["services"]["server_state"] == "fallback"


def test_readiness_keeps_the_memory_contour_verdict(
    client: TestClient, deps: AppDependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пробы не выдумывают отказ там, где хранилища нет: память = ``fallback``.

    Поведение 503 без модели остаётся прежним, и ни одна лишняя строка деградации
    в ``degradation_reasons`` не появляется: контур на памяти честно говорит
    «не рабочий», а не «упавший».
    """
    monkeypatch.setattr("scientific_tangle.api.app._readiness_probe_cache", None)

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["services"]["knowledge_graph"] == "fallback"
    assert body["services"]["server_state"] == "fallback"
    assert body["degradation_reasons"] == [
        "Модель не настроена: агентные ответы недоступны до указания GIGACHAT_API_KEY."
    ]


@pytest.mark.asyncio
async def test_json_run_is_cancelled_when_client_gone() -> None:
    """Закрытое соединение освобождает слот сразу, а не по истечении дедлайна.

    uvicorn не гарантирует отмену обработчика на длинном POST, поэтому прогон
    проверяется на «клиент ушёл» периодическим опросом: без него один закрытый
    тест занимал бы слот приёма и слот модели до ``AGENT_DEADLINE_SECONDS``.
    """
    from scientific_tangle.api.app import _await_or_client_gone

    class _Request:
        def __init__(self, *, gone_after: int) -> None:
            self.calls = 0
            self.gone_after = gone_after

        async def is_disconnected(self) -> bool:
            self.calls += 1
            return self.calls >= self.gone_after

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _slow_work() -> str:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "не должно дойти"

    request = _Request(gone_after=1)
    with pytest.raises(asyncio.CancelledError):
        await _await_or_client_gone(request, _slow_work)
    assert started.is_set() and cancelled.is_set()

    async def _fast_work() -> str:
        return "готово"

    assert await _await_or_client_gone(_Request(gone_after=99), _fast_work) == "готово"
