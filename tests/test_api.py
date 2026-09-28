"""Базовый HTTP-контур: здоровье, порог метрик и поведение без модели.

Клиент — модульная фикстура с lifespan: ``app.state.dependencies`` присваивается
только внутри контекста, а без него тесты зависали бы на состоянии, оставленном
предыдущим модулем (тот же ``TestClient(app)`` без входа в контекст молча
наследовал чужие зависимости и давал 200 там, где ждёшь 503).

Здесь проверяются две гарантии, которые раньше были словами в docstring:
``/health/ready`` на контуре без модели — это HTTP 503 (а не 200 с честным
телом), и ``POST /api/v1/queries/validate`` удалён, а не притворяется проверкой.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from scientific_tangle.api.app import app
from tests.auth_support import cookie_header, signup


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def test_liveness(client: TestClient) -> None:
    """Liveness не зависит от модели: «процесс жив» ≠ «контур готов отвечать»."""
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_is_http_503_while_the_model_is_unavailable(client: TestClient) -> None:
    """Деградация — кодом ответа, а только телом: оркестратор читает код.

    Раньше ``/health/ready`` на контуре без GigaChat давал 200 с
    ``status: "degraded"`` — балансировщик и ``depends_on: service_healthy``
    считали такой сервис готовым и вели на него трафик, который мог ответить
    только 503. Тело при этом обязано остаться прежним: ``model_mode``,
    ``services`` и причина словами — это то, что читает человек.
    """
    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["model_mode"] == "unavailable"
    assert body["services"]["model_provider"] == "disabled"
    assert any("GIGACHAT_API_KEY" in reason for reason in body["degradation_reasons"])


def test_plan_validation_endpoint_is_removed(client: TestClient) -> None:
    """``/queries/validate`` удалён: маршрут возвращал план как есть.

    Docstring обещал «проверяет типизированный план до обращения к retrieval-контуру»,
    тело — ``return plan``. Вызовов из фронтенда у эндпоинта не было, а фасад
    проверки в продукте, где каждый тезис трассируется до первоисточника, быть не
    должен: он превращал бы отсутствие проверки в видимость проверки.
    """
    anonymous = client.post(
        "/api/v1/queries/validate",
        json={"question": "Найти режимы выщелачивания", "language": "ru", "max_hops": 2},
    )
    assert anonymous.status_code == 401

    token = signup(client, "plan@mindai.tech")
    removed = client.post(
        "/api/v1/queries/validate",
        json={"question": "Найти режимы выщелачивания", "language": "ru", "max_hops": 2},
        headers=cookie_header(token),
    )
    assert removed.status_code == 404


def test_live_query_requires_configured_model(client: TestClient) -> None:
    """Гарантия деградации: без настроенной модели — 503, а не пустой ответ.

    Проверка идёт поверх действующей сессии, иначе 503 не отличить от 401.
    """
    token = signup(client, "model-mode@mindai.tech")
    response = client.get("/api/v1/demo", headers=cookie_header(token))

    assert response.status_code == 503
