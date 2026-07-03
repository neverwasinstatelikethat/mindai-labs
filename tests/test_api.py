from fastapi.testclient import TestClient

from scientific_tangle.api.app import app

client = TestClient(app)


def test_liveness() -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_query_plan_rejects_unbounded_graph_depth() -> None:
    response = client.post(
        "/api/v1/queries/validate",
        json={"question": "Найти режимы выщелачивания", "language": "ru", "max_hops": 10},
    )

    assert response.status_code == 422
