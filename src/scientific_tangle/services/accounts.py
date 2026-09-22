"""Учётные записи, сессии и троттлинг входа — серверная аутентификация.

Контур повторяет переключение хранилища знаний (``services/knowledge.py``):
протокол ``AccountsStore`` и два адаптера, выбор — по настройке
``accounts_backend``. Postgres — рабочий контур аналитиков, in-memory — тесты и
запуск без базы. При недоступном Postgres приложение деградирует до памяти
вместо падения (тот же принцип, что и с GigaChat), а ``/health/ready`` честно
показывает фактическое хранилище.

Схему создаёт сам адаптер на первом подключении: alembic в проекте нет, и
чекпоинтер LangGraph управляет своими таблицами так же.

Экспертный доступ выдаётся только SQL — намеренно без UI и без самовыдачи
(полный контракт прав и процедур — ``docs/security-auth.md``)::

    UPDATE nk_users SET review_enabled = true WHERE email = 'expert@mindai.tech';
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import secrets
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import AccountsMode

logger = logging.getLogger(__name__)

__all__ = [
    "Account",
    "AccountsStore",
    "EmailAlreadyRegisteredError",
    "InMemoryAccounts",
    "LoginRateLimiter",
    "PostgresAccounts",
    "build_accounts",
    "hash_password",
    "new_session_token",
    "token_digest",
    "verify_password",
]

# scrypt: n=2**14, r=8, p=1 — примерно 16 MiB и ~0,1 с на проверку. Дешевле —
# подбор по дампу базы становится массовым, дороже — троттлинг сам превращается
# в вектор отказа в обслуживании.
SCRYPT_LOG_N = 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_DKLEN = 64
_HASH_PREFIX = "scrypt"

SESSION_TOKEN_BYTES = 32
# last_seen_at обновляется не на каждый запрос: write-усиление на горячих
# эндпоинтах (dashboard, graph) иначе превышает полезный эффект.
LAST_SEEN_SLACK = timedelta(seconds=60)


class EmailAlreadyRegisteredError(Exception):
    """Email занят другой учётной записью (обработчик отдаёт 409)."""


@dataclass(frozen=True, slots=True)
class Account:
    """Запись учётного хранилища; ``password_hash`` наружу не отдаётся."""

    id: str
    email: str
    display_name: str
    password_hash: str
    review_enabled: bool
    created_at: datetime


def _scrypt_maxmem(log_n: int, r: int) -> int:
    """Лимит памяти scrypt с запасом: по умолчанию OpenSSL берёт 32 MiB."""
    return 128 * (1 << log_n) * r + (1 << 20)


def hash_password(password: str, *, min_length: int) -> str:
    """Хэширует scrypt'ом: ``scrypt$14$8$1$<salt hex>$<hash hex>``.

    Параметры зашиты в строку, чтобы будущая смена cost-фактора не сделала
    существующие хэши нечитаемыми.
    """
    if len(password) < min_length:
        raise ValueError(f"Пароль короче минимальных {min_length} символов")
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**SCRYPT_LOG_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=_scrypt_maxmem(SCRYPT_LOG_N, SCRYPT_R),
        dklen=SCRYPT_DKLEN,
    )
    return "$".join(
        (_HASH_PREFIX, str(SCRYPT_LOG_N), str(SCRYPT_R), str(SCRYPT_P), salt.hex(), digest.hex())
    )


def verify_password(password: str, stored: str) -> bool:
    """Постоянное по времени сравнение; битый или чужой формат — просто ``False``."""
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != _HASH_PREFIX:
        return False
    try:
        log_n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=2**log_n,
            r=r,
            p=p,
            maxmem=_scrypt_maxmem(log_n, r),
            dklen=len(expected),
        )
    except (ValueError, TypeError, OverflowError):
        return False
    return hmac.compare_digest(digest, expected)


def new_session_token() -> str:
    """Opaque-токен сессии: Клиенту показывается целиком, в базе — только хэш."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def token_digest(token: str) -> str:
    """sha256 токена в hex. Дамп ``nk_sessions`` не должен давать готовые сессии."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AccountsStore(Protocol):
    """Контур хранения учётных записей и сессий.

    Все методы асинхронные: вызов идёт из HTTP-обработчика, а блокирующий
    scrypt или сетевой запрос к Postgres не должны останавливать event loop.
    """

    @property
    def kind(self) -> AccountsMode: ...

    async def setup(self) -> None: ...

    async def create_user(self, *, email: str, display_name: str, password: str) -> Account: ...

    async def authenticate(self, email: str, password: str) -> Account | None: ...

    async def get_user(self, user_id: str) -> Account | None: ...

    async def get_user_by_email(self, email: str) -> Account | None: ...

    async def rename(self, user_id: str, display_name: str) -> Account: ...

    async def set_password(self, user_id: str, password: str) -> Account: ...

    async def create_session(self, user_id: str) -> str: ...

    async def resolve_session(self, token: str) -> Account | None: ...

    async def drop_session(self, token: str) -> None: ...

    async def drop_other_sessions(self, user_id: str, keep_token: str) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _MemorySession:
    token_hash: str
    user_id: str
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime


class InMemoryAccounts:
    """Адаптер для тестов и запуска без базы.

    Состояние живёт только в процессе: после перезапуска аккаунты и сессии
    теряются, поэтому на этом бэкенде не проверяют поведение рабочих данных.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._users: dict[str, Account] = {}
        self._email_index: dict[str, str] = {}
        self._sessions: dict[str, _MemorySession] = {}

    @property
    def kind(self) -> AccountsMode:
        return "in-memory"

    async def setup(self) -> None:
        return None

    async def create_user(self, *, email: str, display_name: str, password: str) -> Account:
        normalized = normalize_email(email)
        digest = await asyncio.to_thread(
            hash_password, password, min_length=self._settings.password_min_length
        )
        account = Account(
            id=str(uuid4()),
            email=normalized,
            display_name=display_name.strip(),
            password_hash=digest,
            review_enabled=False,
            created_at=datetime.now(UTC),
        )
        async with self._lock:
            if normalized in self._email_index:
                raise EmailAlreadyRegisteredError(normalized)
            self._users[account.id] = account
            self._email_index[normalized] = account.id
        return account

    async def authenticate(self, email: str, password: str) -> Account | None:
        account = await self.get_user_by_email(email)
        if account is None:
            # Хэш «в холостую»: ответ на неизвестный email и на неверный пароль
            # стоит одинакового времени, иначе тайминг выдаёт существующие адреса.
            await asyncio.to_thread(hash_password, password, min_length=0)
            return None
        ok = await asyncio.to_thread(verify_password, password, account.password_hash)
        return account if ok else None

    async def get_user(self, user_id: str) -> Account | None:
        async with self._lock:
            return self._users.get(user_id)

    async def get_user_by_email(self, email: str) -> Account | None:
        async with self._lock:
            user_id = self._email_index.get(normalize_email(email))
            return self._users.get(user_id) if user_id else None

    async def rename(self, user_id: str, display_name: str) -> Account:
        async with self._lock:
            account = self._users.get(user_id)
            if account is None:
                raise KeyError(user_id)
            updated = replace(account, display_name=display_name.strip())
            self._users[user_id] = updated
            return updated

    async def set_password(self, user_id: str, password: str) -> Account:
        digest = await asyncio.to_thread(
            hash_password, password, min_length=self._settings.password_min_length
        )
        async with self._lock:
            account = self._users.get(user_id)
            if account is None:
                raise KeyError(user_id)
            updated = replace(account, password_hash=digest)
            self._users[user_id] = updated
            return updated

    async def create_session(self, user_id: str) -> str:
        token = new_session_token()
        now = datetime.now(UTC)
        digest = token_digest(token)
        async with self._lock:
            if user_id not in self._users:
                raise KeyError(user_id)
            self._purge(now)
            self._sessions[digest] = _MemorySession(
                token_hash=digest,
                user_id=user_id,
                created_at=now,
                expires_at=now + timedelta(days=self._settings.session_ttl_days),
                last_seen_at=now,
            )
        return token

    async def resolve_session(self, token: str) -> Account | None:
        digest = token_digest(token)
        now = datetime.now(UTC)
        async with self._lock:
            session = self._sessions.get(digest)
            if session is None:
                return None
            account = self._users.get(session.user_id)
            if account is None or session.expires_at <= now:
                del self._sessions[digest]
                return None
            if now - session.last_seen_at >= LAST_SEEN_SLACK:
                session.last_seen_at = now
            return account

    async def drop_session(self, token: str) -> None:
        async with self._lock:
            self._sessions.pop(token_digest(token), None)

    async def drop_other_sessions(self, user_id: str, keep_token: str) -> None:
        keep = token_digest(keep_token)
        async with self._lock:
            for digest, session in list(self._sessions.items()):
                if session.user_id == user_id and digest != keep:
                    del self._sessions[digest]

    async def close(self) -> None:
        """Внешних ресурсов нет, поэтому и закрывать нечего: сессии живут до
        выхода процесса (в отличие от postgres-адаптера, где гасится пул)."""

    def grant_review_enabled(self, email: str, enabled: bool = True) -> str:
        """Зеркало операторского ``UPDATE nk_users SET review_enabled = …``.

        Метода нет в протоколе ``AccountsStore`` и в API — намеренно: экспертный
        доступ боевого контура выдаётся только SQL (docs/security-auth.md), и
        эндпоинта повышения прав не существует. Здесь он нужен тестам и
        seed-скриптам, которые поднимаются без Postgres.
        """
        user_id = self._email_index.get(normalize_email(email))
        if user_id is None:
            raise KeyError(email)
        account = self._users[user_id]
        self._users[user_id] = replace(account, review_enabled=enabled)
        return user_id

    def _purge(self, now: datetime) -> None:
        for digest, session in list(self._sessions.items()):
            if session.expires_at <= now:
                del self._sessions[digest]


_USERS_DDL = """
CREATE TABLE IF NOT EXISTS nk_users (
    id text PRIMARY KEY,
    email text NOT NULL,
    display_name text NOT NULL,
    password_hash text NOT NULL,
    review_enabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL
)
"""

# Уникальность по lower(email): «Ivan@…» и «ivan@…» — одна учётная запись.
_USERS_EMAIL_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS nk_users_email_lower_uidx ON nk_users (lower(email))"
)

_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS nk_sessions (
    token_hash text PRIMARY KEY,
    user_id text NOT NULL REFERENCES nk_users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL
)
"""

_SESSIONS_USER_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS nk_sessions_user_idx ON nk_sessions (user_id)"
)

_SELECT_USER = (
    "SELECT id, email, display_name, password_hash, review_enabled, created_at "
    "FROM nk_users WHERE id = %s"
)
_SELECT_USER_BY_EMAIL = (
    "SELECT id, email, display_name, password_hash, review_enabled, created_at "
    "FROM nk_users WHERE lower(email) = %s"
)
_SELECT_SESSION_USER = (
    "SELECT u.id, u.email, u.display_name, u.password_hash, u.review_enabled, u.created_at, "
    "s.expires_at, s.last_seen_at "
    "FROM nk_sessions s JOIN nk_users u ON u.id = s.user_id "
    "WHERE s.token_hash = %s AND s.expires_at > %s"
)


def _account_from_row(row: dict[str, Any]) -> Account:
    return Account(
        id=str(row["id"]),
        email=str(row["email"]),
        display_name=str(row["display_name"]),
        password_hash=str(row["password_hash"]),
        review_enabled=bool(row["review_enabled"]),
        created_at=cast(datetime, row["created_at"]),
    )


class PostgresAccounts:
    """Рабочий адаптер: ``psycopg_pool.AsyncConnectionPool`` + само-схема.

    ``review_enabled`` здесь только читается. Выдаёт его оператор прямым SQL
    (``UPDATE nk_users SET review_enabled = true WHERE email = '…'``): эндпоинта
    для повышения прав в API сознательно нет — см. docs/security-auth.md.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: AsyncConnectionPool[AsyncConnection] | None = None

    @property
    def kind(self) -> AccountsMode:
        return "postgres"

    @property
    def _ready(self) -> AsyncConnectionPool[AsyncConnection]:
        if self._pool is None:
            raise RuntimeError("PostgresAccounts.setup() не вызван")
        return self._pool

    async def setup(self) -> None:
        """Открывает пул и создаёт схемы. Ошибку не глотает: её решит lifespan."""
        pool: AsyncConnectionPool[AsyncConnection] = AsyncConnectionPool(
            conninfo=self._settings.database_url,
            min_size=1,
            max_size=4,
            open=False,
            timeout=5.0,
            name="nk-accounts",
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        try:
            await pool.open(wait=True, timeout=5.0)
            async with pool.connection(timeout=5.0) as conn:
                for ddl in (
                    _USERS_DDL,
                    _USERS_EMAIL_INDEX_DDL,
                    _SESSIONS_DDL,
                    _SESSIONS_USER_INDEX_DDL,
                ):
                    await conn.execute(ddl)
        except Exception:
            await pool.close()
            raise
        self._pool = pool
        logger.info("Схемы учётных записей готовы (postgres)")

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def _execute(self, query: str, params: Sequence[Any] = ()) -> int:
        async with self._ready.connection(timeout=5.0) as conn:
            cursor = await conn.execute(query, params or None)
            return int(cursor.rowcount)

    async def _fetchone(self, query: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        async with self._ready.connection(timeout=5.0) as conn:
            cursor = await conn.execute(query, params or None)
            return cast("dict[str, Any] | None", await cursor.fetchone())

    async def create_user(self, *, email: str, display_name: str, password: str) -> Account:
        normalized = normalize_email(email)
        digest = await asyncio.to_thread(
            hash_password, password, min_length=self._settings.password_min_length
        )
        account = Account(
            id=str(uuid4()),
            email=normalized,
            display_name=display_name.strip(),
            password_hash=digest,
            review_enabled=False,
            created_at=datetime.now(UTC),
        )
        try:
            await self._execute(
                "INSERT INTO nk_users (id, email, display_name, password_hash, review_enabled,"
                " created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    account.id,
                    account.email,
                    account.display_name,
                    account.password_hash,
                    account.review_enabled,
                    account.created_at,
                ),
            )
        except pg_errors.UniqueViolation as error:
            raise EmailAlreadyRegisteredError(normalized) from error
        return account

    async def authenticate(self, email: str, password: str) -> Account | None:
        account = await self.get_user_by_email(email)
        if account is None:
            await asyncio.to_thread(hash_password, password, min_length=0)
            return None
        ok = await asyncio.to_thread(verify_password, password, account.password_hash)
        return account if ok else None

    async def get_user(self, user_id: str) -> Account | None:
        row = await self._fetchone(_SELECT_USER, (user_id,))
        return _account_from_row(row) if row else None

    async def get_user_by_email(self, email: str) -> Account | None:
        row = await self._fetchone(_SELECT_USER_BY_EMAIL, (normalize_email(email),))
        return _account_from_row(row) if row else None

    async def rename(self, user_id: str, display_name: str) -> Account:
        await self._execute(
            "UPDATE nk_users SET display_name = %s WHERE id = %s",
            (
                display_name.strip(),
                user_id,
            ),
        )
        account = await self.get_user(user_id)
        if account is None:
            raise KeyError(user_id)
        return account

    async def set_password(self, user_id: str, password: str) -> Account:
        digest = await asyncio.to_thread(
            hash_password, password, min_length=self._settings.password_min_length
        )
        await self._execute(
            "UPDATE nk_users SET password_hash = %s WHERE id = %s", (digest, user_id)
        )
        account = await self.get_user(user_id)
        if account is None:
            raise KeyError(user_id)
        return account

    async def create_session(self, user_id: str) -> str:
        token = new_session_token()
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=self._settings.session_ttl_days)
        # Уборка просроченных строк на входе: дёшево, и таблица не разрастается.
        await self._execute("DELETE FROM nk_sessions WHERE expires_at <= %s", (now,))
        await self._execute(
            "INSERT INTO nk_sessions (token_hash, user_id, created_at, expires_at, last_seen_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (token_digest(token), user_id, now, expires_at, now),
        )
        return token

    async def resolve_session(self, token: str) -> Account | None:
        now = datetime.now(UTC)
        row = await self._fetchone(_SELECT_SESSION_USER, (token_digest(token), now))
        if row is None:
            return None
        last_seen = cast(datetime, row["last_seen_at"])
        if now - last_seen >= LAST_SEEN_SLACK:
            await self._execute(
                "UPDATE nk_sessions SET last_seen_at = %s WHERE token_hash = %s",
                (now, token_digest(token)),
            )
        return _account_from_row(row)

    async def drop_session(self, token: str) -> None:
        await self._execute("DELETE FROM nk_sessions WHERE token_hash = %s", (token_digest(token),))

    async def drop_other_sessions(self, user_id: str, keep_token: str) -> None:
        await self._execute(
            "DELETE FROM nk_sessions WHERE user_id = %s AND token_hash <> %s",
            (user_id, token_digest(keep_token)),
        )


def build_accounts(settings: Settings) -> AccountsStore:
    """Выбор адаптера по ``accounts_backend`` — как ``_build_knowledge`` в приложении.

    Postgres-адаптер здесь только конструируется: пул открывается в lifespan,
    где отказ предсказуемо заменяют памятью, а не падением сервиса.
    """
    if settings.accounts_backend == "postgres":
        return PostgresAccounts(settings)
    return InMemoryAccounts(settings)


@dataclass(slots=True)
class _FailureWindow:
    hits: deque[float]


class LoginRateLimiter:
    """Неудачные входы: ``(email, client host)`` → попытки в скользящем окне.

    Счётчик живёт в процессе. Он честен в одном воркере; за балансировщиком с
    несколькими копиями сервиса порог формально выше во столько раз, сколько
    копий держат свой счётчик. Отдельный ограничитель на Redis осознанно не
    вводим: новая зависимость дороже текущего уровня защиты, а настоящий
    контур всё равно за WAF.
    """

    def __init__(self, *, max_attempts: int, window_seconds: int, max_keys: int = 10_000) -> None:
        # Порог открыт для записи: тесты и seed-скрипты снижают его локально,
        # в сервисе значение приходит из settings.login_max_attempts.
        self.max_attempts = max_attempts
        self._window = float(window_seconds)
        self._max_keys = max_keys
        self._windows: dict[str, _FailureWindow] = {}

    def retry_after(self, key: str) -> int | None:
        """Секунды до разблокировки либо ``None``, если вход ещё разрешён."""
        now = time.monotonic()
        window = self._windows.get(key)
        if window is None:
            return None
        hits = self._trim(window, now)
        if len(hits) < self.max_attempts:
            return None
        return max(1, math.ceil(hits[0] + self._window - now))

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        window = self._windows.setdefault(key, _FailureWindow(hits=deque()))
        window.hits.append(now)
        while len(window.hits) > self.max_attempts:
            window.hits.popleft()
        if len(self._windows) > self._max_keys:
            self._prune(now)

    def reset(self, key: str) -> None:
        self._windows.pop(key, None)

    def _trim(self, window: _FailureWindow, now: float) -> deque[float]:
        while window.hits and now - window.hits[0] > self._window:
            window.hits.popleft()
        return window.hits

    def _prune(self, now: float) -> None:
        """Не держим бесконечный словарь ключей: очищаем протухшие окна."""
        stale = [key for key, window in self._windows.items() if not self._trim(window, now)]
        for key in stale:
            del self._windows[key]
        if len(self._windows) > self._max_keys:
            for key in list(self._windows)[: len(self._windows) - self._max_keys]:
                del self._windows[key]
