"""Помощники HTTP-тестов контура доступа.

Раньше тесты подставляли ``X-User-Role``/``X-User-Id`` — ровно тот механизм,
который удалён. Теперь идентификация только через auth-эндпоинты: тесты
регистрации и входа идут тем же путём, что браузер, а токен возвращается
явным ``Cookie``-заголовком (у одного ``TestClient`` один cookie-джар, а в
тестах нужны два субъекта одновременно).
"""

from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient
from httpx import Response

from scientific_tangle.api.app import SESSION_COOKIE
from scientific_tangle.config import get_settings
from scientific_tangle.domain.contracts import (
    DocumentRequest,
    ExtractedClaim,
    ExtractedEntity,
    ExtractionResult,
    NodeType,
)
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.services.accounts import AccountsStore, InMemoryAccounts
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase

# Браузер отправляет Origin на каждом state-changing запросе; без него CSRF-
# проверка отклоняет запрос с cookie как запрос из неизвестного источника.
# Берём первый объявленный доверенный источник, чтобы тесты не привязывались
# к порту локального контура: порты меняются, ожидание — нет.
_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in get_settings().trusted_origins.split(",")
    if origin.strip()
]
BROWSER_HEADERS = {"Origin": _TRUSTED_ORIGINS[0]}
# Длиннее settings.password_min_length (10) — иначе регистрация отклоняется политикой.
PASSWORD = "tajnyj-parol-2026"
OTHER_PASSWORD = "drugoj-parol-2026"


def cookie_header(token: str, **extra: str) -> dict[str, str]:
    """Заголовок действующей сессии плюс заголовки вызывающей стороны."""
    return {"Cookie": f"{SESSION_COOKIE}={token}", **BROWSER_HEADERS, **extra}


def token_of(response: Response) -> str:
    """Значение ``nk_session`` из Set-Cookie (атрибуты отрезаны)."""
    raw = response.headers.get("set-cookie", "")
    prefix = f"{SESSION_COOKIE}="
    for part in raw.split(";"):
        if part.startswith(prefix):
            return part.removeprefix(prefix)
    raise AssertionError(f"Set-Cookie без сессии: {raw!r}")


def register(
    client: TestClient,
    email: str,
    *,
    password: str = PASSWORD,
    display_name: str = "Тестовый пользователь",
    headers: dict[str, str] | None = None,
) -> Response:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "display_name": display_name, "password": password},
        headers=headers if headers is not None else BROWSER_HEADERS,
    )
    client.cookies.clear()
    return response


def signup(
    client: TestClient,
    email: str,
    *,
    password: str = PASSWORD,
    display_name: str = "Тестовый пользователь",
) -> str:
    """Регистрация с авто-входом: возвращает токен новой сессии."""
    response = register(client, email, password=password, display_name=display_name)
    assert response.status_code == 201, response.text
    return token_of(response)


def login(client: TestClient, email: str, *, password: str = PASSWORD) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers=BROWSER_HEADERS,
    )
    client.cookies.clear()
    assert response.status_code == 200, response.text
    return token_of(response)


def make_expert(accounts: AccountsStore, email: str, *, enabled: bool = True) -> str:
    """Выдаёт экспертный доступ на уровне хранилища.

    Так в рабочем контуре делает оператор прямым изменением таблицы (см.
    docs/security-auth.md): эндпоинта повышения прав в API нет намеренно.
    ``enabled=False`` снимает признак — тем же способом проверяется, что права
    пересчитываются на каждый запрос, а не «запоминаются» выданным.
    """
    return cast(InMemoryAccounts, accounts).grant_review_enabled(email, enabled)


RESTRICTED_STATEMENT = "Ограниченный тезис о задержании солей на закрытом пилоте."


def restricted_knowledge() -> InMemoryKnowledgeBase:
    """Корпус с одним restricted-утверждением: на нём проверяют двухуровневый ACL."""
    knowledge = InMemoryKnowledgeBase()
    knowledge.ingest(
        DocumentRequest(
            title="Закрытый пилот",
            text="Закрытый отчёт по испытаниям мембран на шахтной воде предприятия.",
            data_class=DataClass.RESTRICTED,
        ),
        ExtractionResult(
            entities=[
                ExtractedEntity(
                    name="Мембрана", canonical_name="Мембрана", type=NodeType.MATERIAL
                )
            ],
            claims=[
                ExtractedClaim(
                    subject="Мембрана",
                    predicate="HAS_PROPERTY",
                    object="Мембрана",
                    statement=RESTRICTED_STATEMENT,
                    confidence=0.8,
                    evidence_quote="задержание солей на закрытом пилоте",
                )
            ],
        ),
    )
    return knowledge
