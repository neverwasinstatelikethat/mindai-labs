"""Очередь противоречий и экспертное решение по ней.

Пара тезисов с непересекающимися числами существовала только как строка в ответе
модели: ``ConflictCandidate.status`` никто не ставил, эндпоинта с persist не было,
и «разбор противоречий» в продукте сводился к упоминанию. Здесь проверяется
замкнутый переход: детектор → очередь → решение эксперта → статус находок →
журнал решений.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from scientific_tangle.api.app import AppDependencies, app
from scientific_tangle.domain.contracts import (
    ConflictReview,
    DocumentRequest,
    ExtractedClaim,
    ExtractedEntity,
    ExtractionResult,
    NodeType,
)
from scientific_tangle.domain.intelligence import (
    DataClass,
    ScopeDimension,
    conflict_candidate_id,
)
from scientific_tangle.domain.models import NumericObservation
from scientific_tangle.services.conflicts import candidate_views, resolve_for_review
from scientific_tangle.services.durable_state import InMemoryDurableState
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from tests.auth_support import cookie_header, make_expert, signup

BASE_EMAIL = "conflict-researcher@mindai.tech"
EXPERT_EMAIL = "conflict-expert@mindai.tech"

PROPERTY = "задержание солей"


def _observation(low: float, high: float, unit: str = "%") -> NumericObservation:
    return NumericObservation(
        property_name=PROPERTY,
        operator="between",
        min_value=low,
        max_value=high,
        unit=unit,
        normalized_min=low,
        normalized_max=high,
        normalized_unit=unit,
        raw_text=f"{low}-{high} {unit}",
    )


def _claim(statement: str, quote: str, low: float, high: float) -> ExtractedClaim:
    return ExtractedClaim(
        subject="Мембрана",
        predicate="HAS_PROPERTY",
        object="Мембрана",
        statement=statement,
        confidence=0.8,
        evidence_quote=quote,
        observations=[_observation(low, high)],
    )


def _entity() -> ExtractedEntity:
    return ExtractedEntity(name="Мембрана", canonical_name="Мембрана", type=NodeType.MATERIAL)


def corpus_knowledge(*, restricted: bool = False) -> InMemoryKnowledgeBase:
    """Два источника с непересекающимися диапазонами одного показателя — противоречие."""
    knowledge = InMemoryKnowledgeBase()
    knowledge.ingest(
        DocumentRequest(
            title="Пилот 2024",
            text="Отчёт испытания мембраны: задержание солей 95-97 %.",
        ),
        ExtractionResult(
            entities=[_entity()],
            claims=[
                _claim(
                    "Мембрана задерживает соли на 95-97 %.",
                    "задержание солей 95-97 %",
                    95.0,
                    97.0,
                )
            ],
        ),
    )
    knowledge.ingest(
        DocumentRequest(
            title="Отчёт по второму пилоту",
            text="Задержание солей на второй линии: 70-75 %.",
            data_class=DataClass.RESTRICTED if restricted else DataClass.PUBLIC,
        ),
        ExtractionResult(
            entities=[_entity()],
            claims=[
                _claim(
                    "Мембрана задерживает соли на 70-75 %.",
                    "задержание солей 70-75 %",
                    70.0,
                    75.0,
                )
            ],
        ),
    )
    return knowledge


@pytest.fixture
def deps() -> AppDependencies:
    dependencies = AppDependencies()
    dependencies.knowledge = corpus_knowledge()
    dependencies.state = InMemoryDurableState()
    return dependencies


@pytest.fixture
def client(deps: AppDependencies) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        test_client.app.state.dependencies = deps
        yield test_client


@pytest.fixture
def base(client: TestClient) -> dict[str, str]:
    return cookie_header(signup(client, BASE_EMAIL, display_name="Исследователь"))


@pytest.fixture
def expert(client: TestClient, deps: AppDependencies) -> dict[str, str]:
    token = signup(client, EXPERT_EMAIL, display_name="Экспертиза")
    make_expert(deps.accounts, EXPERT_EMAIL)
    return cookie_header(token)


def _queue(client: TestClient, headers: dict[str, str]) -> list[dict[str, object]]:
    response = client.get("/api/v1/conflicts/candidates", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _review(
    client: TestClient, headers: dict[str, str], candidate_id: str, *, confirmed: bool
) -> Response:
    return client.post(
        f"/api/v1/conflicts/candidates/{candidate_id}/review",
        json={"confirmed": confirmed},
        headers=headers,
    )


def test_candidate_pair_enters_the_queue_as_unresolved(
    client: TestClient, base: dict[str, str]
) -> None:
    """Детектор сам находит пару: очередь не ждёт, пока агент упомянет противоречие."""
    queue = _queue(client, base)
    assert len(queue) == 1
    item = queue[0]
    assert item["status"] == "candidate"
    assert item["property_name"] == PROPERTY
    assert item["decided_by"] is None
    # Стороны показывают исходные числа с единицами, а не «конфликт найден».
    assert {item["left"]["value"], item["right"]["value"]} == {"95–97 %", "70–75 %"}
    disputed = client.get("/api/v1/conflicts", headers=base)
    assert disputed.status_code == 200
    assert disputed.json() == []


def test_confirm_marks_both_findings_disputed_and_is_audited(
    client: TestClient, expert: dict[str, str]
) -> None:
    item = _queue(client, expert)[0]
    response = _review(client, expert, item["id"], confirmed=True)
    assert response.status_code == 200, response.text
    review = response.json()
    assert review["status"] == "confirmed"

    disputed = client.get("/api/v1/conflicts", headers=expert).json()
    # Демонстрационный тезис витрины тоже может быть оспорен — проверяем, что
    # обе находки подтверждённой пары оказались в списке, а не весь его состав.
    resolved_ids = {review["left_finding_id"], review["right_finding_id"]}
    assert resolved_ids <= {finding["id"] for finding in disputed}
    # Подтверждённая пара остаётся в очереди, но уже с отметкой о решении.
    confirmed = next(entry for entry in _queue(client, expert) if entry["id"] == item["id"])
    assert confirmed["status"] == "confirmed"
    assert confirmed["decided_by"] == review["actor_id"]

    decisions = client.get("/api/v1/decisions", headers=expert).json()
    assert [entry["action"] for entry in decisions] == ["conflict.reviewed"]
    assert decisions[0]["object_id"] == item["id"]
    audit = client.get("/api/v1/audit", headers=expert).json()
    assert "conflict.review" in {entry["action"] for entry in audit}


def test_repeating_the_same_decision_is_rejected(
    client: TestClient, expert: dict[str, str]
) -> None:
    item = _queue(client, expert)[0]
    assert _review(client, expert, item["id"], confirmed=True).status_code == 200
    assert _review(client, expert, item["id"], confirmed=True).status_code == 409
    # Отклонение после подтверждения — новое решение, а не повтор: право на пересмотр.
    assert _review(client, expert, item["id"], confirmed=False).status_code == 200


def test_dismiss_does_not_roll_back_finding_status(
    client: TestClient, expert: dict[str, str]
) -> None:
    """Откатывать консенсус по одной паре — приписывать эксперту чужое решение.

    Статус находки могла поставить и другая пара; dismiss фиксирует только
    собственное решение.
    """
    item = _queue(client, expert)[0]
    confirmed = _review(client, expert, item["id"], confirmed=True).json()
    dismissed = _review(client, expert, item["id"], confirmed=False)
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "dismissed"
    disputed = {
        finding["id"] for finding in client.get("/api/v1/conflicts", headers=expert).json()
    }
    assert {confirmed["left_finding_id"], confirmed["right_finding_id"]} <= disputed


def test_review_requires_expert_right_and_unknown_pair_is_not_found(
    client: TestClient, base: dict[str, str], expert: dict[str, str]
) -> None:
    item = _queue(client, base)[0]
    assert _review(client, base, item["id"], confirmed=True).status_code == 403
    # Право проверяется до поиска пары: эндпоинт, различающий «нет доступа» и
    # «такой пары нет», сам выдаёт состав очереди чужому аккаунту.
    assert _review(client, expert, str(uuid4()), confirmed=True).status_code == 404


def test_restricted_side_keeps_the_pair_out_of_base_queue() -> None:
    """Пара видна только тому, кто видит обе находки: одна закрытая — и очередь пуста."""
    dependencies = AppDependencies()
    dependencies.knowledge = corpus_knowledge(restricted=True)
    dependencies.state = InMemoryDurableState()
    with TestClient(app) as test_client:
        test_client.app.state.dependencies = dependencies
        token = signup(test_client, "reader@mindai.tech", display_name="Читатель")
        headers = cookie_header(token)
        assert _queue(test_client, headers) == []
        make_expert(dependencies.accounts, "reader@mindai.tech")
        queue = _queue(test_client, headers)
        assert len(queue) == 1
        assert {side["data_class"] for side in (queue[0]["left"], queue[0]["right"])} == {
            "public",
            "restricted",
        }


def test_pair_identity_survives_recount_and_swapped_sides() -> None:
    """Отпечаток пары держит решение: без случайного id очередь «забыла» бы просмотры."""
    findings = corpus_knowledge().all_findings()
    first_id = candidate_views(findings, [])[0].id
    first = resolve_for_review(str(first_id), findings)
    assert first is not None
    reversed_findings = list(reversed(findings))
    again = resolve_for_review(
        str(candidate_views(reversed_findings, [])[0].id), reversed_findings
    )
    assert again is not None
    assert first.candidate.id == again.candidate.id
    assert {first.left_finding_id, first.right_finding_id} == {
        again.left_finding_id,
        again.right_finding_id,
    }


def test_candidate_id_normalizes_claim_order_and_scope() -> None:
    """Левый и правый тезисы, поставленные местами, дают тот же отпечаток пары."""
    dimensions = [ScopeDimension(name="territory", value="Урал")]
    forward = conflict_candidate_id("a#p#0", "b#p#0", "прочность", dimensions)
    backward = conflict_candidate_id("b#p#0", "a#p#0", "прочность", list(reversed(dimensions)))
    assert forward == backward
    assert forward != conflict_candidate_id("a#p#0", "c#p#0", "прочность", dimensions)


def test_review_overlays_status_without_rewriting_detection() -> None:
    """Детектор не знает о решениях: статус очереди даёт наложение durable-записи."""
    findings = corpus_knowledge().all_findings()
    candidate_id = str(candidate_views(findings, [])[0].id)
    resolution = resolve_for_review(candidate_id, findings)
    assert resolution is not None
    review = ConflictReview(
        candidate_id=candidate_id,
        status="dismissed",
        actor_id="expert-1",
        property_name=PROPERTY,
        left_finding_id=resolution.left_finding_id,
        right_finding_id=resolution.right_finding_id,
    )
    views = candidate_views(findings, [review])
    assert [(view.status, view.decided_by) for view in views] == [("dismissed", "expert-1")]


def test_set_finding_status_does_not_create_a_version() -> None:
    """Степень консенсуса — не правка тезиса: версия и цепочка замещений не двигаются."""
    knowledge = corpus_knowledge()
    finding = knowledge.all_findings()[0]
    updated = knowledge.set_finding_status(finding.id, "disputed")
    assert updated.status == "disputed"
    assert updated.version == finding.version
    assert updated.superseded_by is None
    assert len(knowledge.claim_history(finding.id)) == 1
    with pytest.raises(KeyError):
        knowledge.set_finding_status("net-takoy-nahodki", "disputed")
