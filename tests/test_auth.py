"""Контур серверной аутентификации: регистрация, сессии, троттлинг и CSRF.

Это контракт, на который опирается фронтенд: ``AccountInfo`` с ``capabilities``
и ``data_classes``, cookie-сессия ``nk_session`` вместо заголовков
``X-User-Role``/``X-User-Id`` и отказ в доступе по умолчанию.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from scientific_tangle.api.app import AppDependencies, app
from scientific_tangle.config import Settings
from scientific_tangle.services.accounts import EmailAlreadyRegisteredError, PostgresAccounts
from tests.auth_support import (
    BROWSER_HEADERS,
    OTHER_PASSWORD,
    PASSWORD,
    RESTRICTED_STATEMENT,
    cookie_header,
    make_expert,
    register,
    restricted_knowledge,
    signup,
    token_of,
)

BASE_CAPABILITIES = [
    "evaluation:view",
    "export:run",
    "feedback:give",
    "knowledge:read",
    "query:ask",
]
EXPERT_CAPABILITIES = sorted(
    [*BASE_CAPABILITIES, "audit:read", "proposal:review", "restricted:read"]
)
QUESTION = "Какие методы обессоливания подходят для шахтной воды?"


@pytest.fixture
def deps() -> AppDependencies:
    """Свои учётные записи и своя база на тест: состояние не перетекает между ними."""
    dependencies = AppDependencies()
    dependencies.knowledge = restricted_knowledge()
    return dependencies


@pytest.fixture
def client(deps: AppDependencies) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        test_client.app.state.dependencies = deps
        yield test_client


def test_register_me_logout_round_trip(client: TestClient) -> None:
    """Полный жизненный цикл сессии через те же вызовы, что делает браузер."""
    created = register(client, "round-trip@mindai.tech", display_name="Аналитик")
    assert created.status_code == 201
    profile = created.json()
    assert profile["email"] == "round-trip@mindai.tech"
    assert profile["display_name"] == "Аналитик"
    assert profile["review_enabled"] is False
    token = token_of(created)

    me = client.get("/api/v1/auth/me", headers=cookie_header(token))
    assert me.status_code == 200
    assert me.json() == profile

    logout = client.post("/api/v1/auth/logout", headers=cookie_header(token))
    assert logout.status_code == 200
    assert logout.json() == {"status": "logged_out"}
    assert "nk_session=" in logout.headers["set-cookie"]

    # Сессия снята на сервере, а не только в браузере.
    assert client.get("/api/v1/auth/me", headers=cookie_header(token)).status_code == 401


def test_duplicate_email_conflicts(client: TestClient) -> None:
    first = register(client, "Duplicate@MindAI.tech")
    duplicate = register(client, "duplicate@mindai.tech")

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Email уже зарегистрирован"}


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("nobody@mindai.tech", "sovsem-ne-tot-parol"),
        ("known@mindai.tech", "sovsem-ne-tot-parol"),
    ],
)
def test_login_failure_is_indistinguishable(
    client: TestClient, email: str, password: str
) -> None:
    """Нет перечисления аккаунтов: ответ одинаковый для неизвестного и известного."""
    register(client, "known@mindai.tech")
    client.cookies.clear()

    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Неверный email или пароль"}


def test_password_policy_and_email_format(client: TestClient) -> None:
    short = register(client, "short@mindai.tech", password="123456789")
    malformed = client.post(
        "/api/v1/auth/register",
        json={"email": "ne-email", "display_name": "X", "password": PASSWORD},
        headers=BROWSER_HEADERS,
    )

    assert short.status_code == 422
    assert "минимальных 10 символов" in short.json()["detail"]
    assert malformed.status_code == 422


def test_protected_read_requires_session(client: TestClient) -> None:
    """Без cookie protected-чтение невозможно: 401 до обращения к базе знаний."""
    anonymous = client.get("/api/v1/findings")
    stale = client.get("/api/v1/findings", headers=cookie_header("net-takogo-tokena"))
    stats = client.get("/api/v1/corpus/stats")

    assert anonymous.status_code == 401
    assert anonymous.json() == {"detail": "Требуется вход в аккаунт"}
    assert stale.status_code == 401
    # Агрегированная статистика корпуса — единственное открытое чтение в API.
    assert stats.status_code == 200


def test_stream_is_authorized_before_opening_sse(client: TestClient) -> None:
    """SSE проверяется до потока: 401 приходит JSON, а не «начавшимся» стримом."""
    with client.stream("POST", "/api/v1/query/stream", json={"question": QUESTION}) as stream:
        assert stream.status_code == 401
        assert stream.headers["content-type"].startswith("application/json")


def test_restricted_findings_follow_access_level(
    client: TestClient, deps: AppDependencies
) -> None:
    """Базовый уровень не видит restricted-находки, экспертный — видит."""
    base = cookie_header(signup(client, "base-observer@mindai.tech"))
    expert_email = "expert-observer@mindai.tech"
    expert = cookie_header(signup(client, expert_email))
    make_expert(deps.accounts, expert_email)

    hidden = client.get("/api/v1/findings", headers=base)
    visible = client.get("/api/v1/findings", headers=expert)

    assert hidden.status_code == 200
    assert RESTRICTED_STATEMENT not in hidden.text
    assert RESTRICTED_STATEMENT in visible.text


def test_me_reports_capabilities_and_data_classes(
    client: TestClient, deps: AppDependencies
) -> None:
    """Дословный контракт ``AccountInfo`` для фронтенда."""
    email = "contract@mindai.tech"
    token = signup(client, email)

    base = client.get("/api/v1/auth/me", headers=cookie_header(token)).json()
    make_expert(deps.accounts, email)
    expert = client.get("/api/v1/auth/me", headers=cookie_header(token)).json()

    assert set(base) == {
        "id",
        "email",
        "display_name",
        "review_enabled",
        "created_at",
        "capabilities",
        "data_classes",
    }
    assert base["capabilities"] == BASE_CAPABILITIES
    assert base["data_classes"] == ["internal", "public"]
    assert base["review_enabled"] is False and expert["review_enabled"] is True
    assert expert["capabilities"] == EXPERT_CAPABILITIES
    assert expert["data_classes"] == ["internal", "public", "restricted"]


def test_role_endpoints_are_removed(client: TestClient) -> None:
    """Ролевой лестницы больше нет: `/auth/me` — единственный источник прав."""
    token = signup(client, "ex-role@mindai.tech")

    assert client.get("/api/v1/roles", headers=cookie_header(token)).status_code == 404
    assert client.get("/api/v1/principal", headers=cookie_header(token)).status_code == 404


def test_password_rotation_kills_other_sessions(client: TestClient) -> None:
    """Смена пароля ротирует токен и гасит прочие сессии этого аккаунта."""
    email = "rotate@mindai.tech"
    first = cookie_header(signup(client, email))
    second_response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
        headers=BROWSER_HEADERS,
    )
    second = cookie_header(token_of(second_response))

    rotated = client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": OTHER_PASSWORD},
        headers=second,
    )

    assert rotated.status_code == 200
    current = cookie_header(token_of(rotated))
    assert client.get("/api/v1/auth/me", headers=current).status_code == 200
    assert client.get("/api/v1/auth/me", headers=first).status_code == 401
    assert client.get("/api/v1/auth/me", headers=second).status_code == 401
    # Старый пароль больше не принимает ни одна сессия.
    stale = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
        headers=BROWSER_HEADERS,
    )
    assert stale.status_code == 401


def test_password_change_requires_current_password(
    client: TestClient, deps: AppDependencies
) -> None:
    token = signup(client, "current-pw@mindai.tech")
    headers = cookie_header(token)

    denied = client.post(
        "/api/v1/auth/password",
        json={"current_password": "sovsem-ne-tot-parol", "new_password": OTHER_PASSWORD},
        headers=headers,
    )
    too_short = client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "12345"},
        headers=headers,
    )

    assert denied.status_code == 403
    assert denied.json() == {"detail": "Текущий пароль указан неверно"}
    assert too_short.status_code == 422

    # Действующая сессия не должна давать бесконечный оракул для подбора пароля.
    # Первый отказ выше уже учтён счётчиком, поэтому порог — 3 попытки.
    deps.login_limiter.max_attempts = 3
    for _ in range(2):
        assert (
            client.post(
                "/api/v1/auth/password",
                json={"current_password": "sovsem-ne-tot-parol", "new_password": OTHER_PASSWORD},
                headers=headers,
            )
        ).status_code == 403
    blocked = client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": OTHER_PASSWORD},
        headers=headers,
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


def test_profile_patch_updates_display_name(client: TestClient) -> None:
    token = signup(client, "profile@mindai.tech", display_name="Прежнее имя")

    patched = client.patch(
        "/api/v1/auth/profile",
        json={"display_name": "  Новое имя  "},
        headers=cookie_header(token),
    )

    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Новое имя"
    blank = client.patch(
        "/api/v1/auth/profile",
        json={"display_name": "   "},
        headers=cookie_header(token),
    )
    assert blank.status_code == 422


def test_login_is_throttled(client: TestClient, deps: AppDependencies) -> None:
    """После порога неудач — 429 с Retry-After; успешный вход сбрасывает счётчик."""
    deps.login_limiter.max_attempts = 3
    email = "throttled@mindai.tech"
    register(client, email)
    client.cookies.clear()

    attempts = [
        client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "sovsem-ne-tot-parol"},
            headers=BROWSER_HEADERS,
        )
        for _ in range(3)
    ]
    assert [response.status_code for response in attempts] == [401, 401, 401]

    blocked = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
        headers=BROWSER_HEADERS,
    )

    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Слишком много неудачных попыток входа. Повторите позже."}
    assert int(blocked.headers["retry-after"]) > 0
    # Порог считается на пару (email, host): другой аккаунт не задет.
    other = client.post(
        "/api/v1/auth/login",
        json={"email": "another@mindai.tech", "password": "sovsem-ne-tot-parol"},
        headers=BROWSER_HEADERS,
    )
    assert other.status_code == 401


def test_state_changing_request_needs_trusted_origin(client: TestClient) -> None:
    """CSRF: запрос с cookie из чужого источника отклоняется, без cookie — нет."""
    token = signup(client, "csrf@mindai.tech")

    evil = client.patch(
        "/api/v1/auth/profile",
        json={"display_name": "Подмена"},
        headers={**cookie_header(token), "Origin": "https://evil.example"},
    )
    # Referer смотрим только когда Origin не прислан: иначе Origin главнее.
    referer_spoof = client.patch(
        "/api/v1/auth/profile",
        json={"display_name": "Подмена"},
        headers={"Cookie": f"nk_session={token}", "Referer": "https://evil.example/path"},
    )
    # Источник не прислан вообще: доверять такому запросу с cookie нельзя.
    no_source = client.patch(
        "/api/v1/auth/profile",
        json={"display_name": "Подмена"},
        headers={"Cookie": f"nk_session={token}"},
    )
    trusted = client.patch(
        "/api/v1/auth/profile",
        json={"display_name": "Доверенный"},
        headers=cookie_header(token),
    )

    assert evil.status_code == 403
    assert evil.json() == {"detail": "Источник запроса не входит в доверенный список"}
    assert referer_spoof.status_code == 403
    assert no_source.status_code == 403
    assert trusted.status_code == 200
    assert trusted.json()["display_name"] == "Доверенный"


def test_anonymous_state_changing_requests_skip_origin_rule(client: TestClient) -> None:
    """Регистрация и вход доступны и без Origin: подделывать там нечего."""
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "no-origin@mindai.tech",
            "display_name": "Без источника",
            "password": PASSWORD,
        },
    )

    assert response.status_code == 201


def test_session_cookie_flags(
    client: TestClient, deps: AppDependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """httpOnly + SameSite=lax всегда; Secure — только в production.

    Иначе локальный контур на http://localhost:13000 не сохранит сессию, и вход
    выглядел бы сломанным без видимой причины.
    """
    dev = register(client, "cookie-dev@mindai.tech").headers["set-cookie"].lower()

    assert "httponly" in dev
    assert "samesite=lax" in dev
    assert "path=/" in dev
    assert "secure" not in dev
    assert "max-age=1209600" in dev  # session_ttl_days=14

    # Settings кешируется на процесс, поэтому значение возвращают monkeypatch.
    monkeypatch.setattr(deps.settings, "app_env", "production")
    prod = register(client, "cookie-prod@mindai.tech").headers["set-cookie"].lower()
    assert "secure" in prod


def test_expired_session_is_rejected(
    client: TestClient, deps: AppDependencies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Просроченный токен не даёт доступа, даже если подпись когда-то выдавалась."""
    monkeypatch.setattr(deps.settings, "session_ttl_days", 0)
    token = signup(client, "expired@mindai.tech")

    assert client.get("/api/v1/auth/me", headers=cookie_header(token)).status_code == 401


def test_health_ready_reports_accounts_backend(client: TestClient) -> None:
    """Видно, где живут сессии: на памяти переживание перезапуска невозможно."""
    ready = client.get("/health/ready").json()

    assert ready["accounts"] == "in-memory"
    assert ready["services"]["accounts"] == "fallback"


_POSTGRES_DSN = os.environ.get("POSTGRES_TEST_DSN", "")


@pytest.mark.skipif(
    not _POSTGRES_DSN,
    reason="нужен реальный Postgres: запустите контур и задайте POSTGRES_TEST_DSN",
)
def test_postgres_accounts_round_trip() -> None:
    """Опциональная проверка боевого адаптера: SQL живёт только в нём.

    Запуск: ``POSTGRES_TEST_DSN=postgresql://…/mindai python -m pytest``.
    Без DSN тест пропускается (юнит-контур сознательно на in-memory), поэтому
    перед merge в рабочий контур его стоит прогнать вручную.
    """
    asyncio.run(_postgres_round_trip())


async def _postgres_round_trip() -> None:
    settings = Settings(
        app_env="test",
        accounts_backend="postgres",
        database_url=_POSTGRES_DSN,
    )
    store = PostgresAccounts(settings)
    email = f"pg-check-{uuid4().hex[:8]}@mindai.tech"
    try:
        await store.setup()
        account = await store.create_user(
            email=email.upper(), display_name="  PG  ", password=PASSWORD
        )
        assert account.email == email and account.display_name == "PG"

        with pytest.raises(EmailAlreadyRegisteredError):
            await store.create_user(email=email, display_name="PG", password=PASSWORD)

        assert await store.authenticate(email, "sovsem-ne-tot-parol") is None
        assert await store.authenticate("net-takogo@mindai.tech", PASSWORD) is None
        assert (await store.authenticate(email, PASSWORD)) is not None

        token = await store.create_session(account.id)
        resolved = await store.resolve_session(token)
        assert resolved is not None and resolved.id == account.id
        assert await store.resolve_session("tokena-net-v-baze") is None

        rotated = await store.create_session(account.id)
        await store.drop_other_sessions(account.id, rotated)
        assert await store.resolve_session(token) is None
        assert await store.resolve_session(rotated) is not None

        renamed = await store.rename(account.id, "PG переименован")
        assert renamed.display_name == "PG переименован"

        updated = await store.set_password(account.id, OTHER_PASSWORD)
        assert updated.password_hash != account.password_hash
        assert await store.authenticate(email, PASSWORD) is None

        await store.drop_session(rotated)
        assert await store.resolve_session(rotated) is None
    finally:
        # Он же каскадно снимает nk_sessions (FK ON DELETE CASCADE).
        await store._execute("DELETE FROM nk_users WHERE email = %s", (email,))
        await store.close()
