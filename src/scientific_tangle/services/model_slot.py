from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from psycopg import AsyncConnection, Connection

from scientific_tangle.config import Settings

_GIGACHAT_LOCK_KEY = 0x4D494E444149


@asynccontextmanager
async def async_model_slot(settings: Settings) -> AsyncIterator[None]:
    """Один общий слот для chat и embeddings во всех серверных процессах.

    PostgreSQL освобождает session lock при закрытии соединения, в том числе
    при отмене запроса или завершении процесса. Memory-контур общей БД не имеет.
    """
    if settings.knowledge_backend != "neo4j":
        yield
        return
    async with await AsyncConnection.connect(
        settings.database_url, autocommit=True, connect_timeout=5
    ) as connection:
        await connection.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (f"{int(settings.gigachat_queue_wait_seconds * 1000)}ms",),
        )
        await connection.execute("SELECT pg_advisory_lock(%s)", (_GIGACHAT_LOCK_KEY,))
        yield


@contextmanager
def model_slot(settings: Settings) -> Iterator[None]:
    """Синхронная ветка того же слота для эмбеддингов в worker-потоках."""
    if settings.knowledge_backend != "neo4j":
        yield
        return
    with Connection.connect(
        settings.database_url, autocommit=True, connect_timeout=5
    ) as connection:
        connection.execute(
            "SELECT set_config('statement_timeout', %s, false)",
            (f"{int(settings.gigachat_queue_wait_seconds * 1000)}ms",),
        )
        connection.execute("SELECT pg_advisory_lock(%s)", (_GIGACHAT_LOCK_KEY,))
        yield
