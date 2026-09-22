"""Базовый HTTP-контур: здоровье, валидация плана и поведение без модели.

Клиент — модульная фикстура с lifespan: ``app.state.dependencies`` присваивается
только внутри контекста, а без него тесты зависали бы на состоянии, оставленном
предыдущим модулем (тот же ``TestClient(app)`` без входа в контекст молча
наследовал чужие зависимости и давал 200 там, где ждёшь 503).
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
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_query_plan_validation_requires_session(client: TestClient) -> None:
    """План запроса — часть рабочего контура: аноним его не вызывает."""
    anonymous = client.post(
        "/api/v1/queries/validate",
        json={"question": "Найти режимы выщелачивания", "language": "ru", "max_hops": 2},
    )
    assert anonymous.status_code == 401

    token = signup(client, "plan@mindai.tech")
    bounded = client.post(
        "/api/v1/queries/validate",
        json={"question": "Найти режимы выщелачивания", "language": "ru", "max_hops": 2},
        headers=cookie_header(token),
    )
    assert bounded.status_code == 200


def test_query_plan_rejects_unbounded_graph_depth(client: TestClient) -> None:
    token = signup(client, "plan-limits@mindai.tech")
    response = client.post(
        "/api/v1/queries/validate",
        json={"question": "Найти режимы выщелачивания", "language": "ru", "max_hops": 10},
        headers=cookie_header(token),
    )

    assert response.status_code == 422


def test_live_query_requires_configured_model(client: TestClient) -> None:
    """Гарантия деградации: без настроенной модели — 503, а не пустой ответ.

    Проверка идёт поверх действующей сессии, иначе 503 не отличить от 401.
    """
    token = signup(client, "model-mode@mindai.tech")
    response = client.get("/api/v1/demo", headers=cookie_header(token))

    assert response.status_code == 503
