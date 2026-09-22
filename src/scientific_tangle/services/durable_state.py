"""Серверное состояние: копия ответа, экспертные решения и рабочие журналы.

Всё, чему нельзя доверять клиенту и что не имеет права исчезать с перезапуском
процесса:

* ``nk_answers`` — серверная копия ответа по ``query_id``. Экспорт обязан
  перечитывать её и заново прогонять политику текущей сессии: пока ответ
  приходил в теле запроса на экспорт, класс данных наделял клиент, а не сервер.
* ``nk_expert_decisions`` — решения эксперта (обзоры предложений, склейки
  сущностей, замена утверждения, экспорт).
* ``nk_audit_events`` — журнал аудита: без него ``correlation_id`` из ответа
  не с чем сопоставить при разборе инцидента.
* ``nk_experiments`` / ``nk_evaluation_runs`` / ``nk_notifications`` — A/B-прогоны,
  оценки и лента уведомлений. Раньше все три жили в deque процесса и обнулялись
  перезапуском, хотя продукт показывал их как историю решений.

Контур выбора хранилища повторяет ``services/accounts.py``: Postgres — рабочая
ветка, память — тесты и запуск без базы, при недоступной базе — деградация в
память с причиной в ``/health/ready``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    AnswerPayload,
    EvaluationRun,
    EvolutionExperiment,
    EvolutionProposal,
    ExpertDecision,
    Notification,
    StoreBackend,
    StoredAnswerInfo,
)
from scientific_tangle.domain.intelligence import AuditEvent, DataClass
from scientific_tangle.services.governance import InMemoryAuditLog

logger = logging.getLogger(__name__)

# Буферизация обязательна: прежние словари росли вместе с числом запросов и
# никогда не очищались, а ответ — это полный граф с evidence-цитатами.
MAX_STORED_ANSWERS = 200
ANSWER_TTL = timedelta(hours=12)
MAX_DECISIONS = 5000
# Журнал аудита вытесняет старейшие события безвозвратно: потолок держится
# достаточно большим, чтобы разбор инцидента не терял начало цепочки
# correlation_id, но не бесконечным, чтобы таблица не росла неограниченно.
MAX_AUDIT_EVENTS = 50000
MAX_NOTIFICATIONS = 200
MAX_EXPERIMENTS = 500
# Предложения эволюции — очередь экспертного обзора: потерянная при перезапуске
# очередь означает потерянные решения, а активная политика промпта выводится именно
# из принятых предложений и обязана переживать рестарт.
MAX_PROPOSALS = 500
MAX_EVALUATION_RUNS = 200

__all__ = [
    "DurableState",
    "InMemoryDurableState",
    "PostgresDurableState",
    "StoredAnswer",
    "build_durable_state",
]


@dataclass(frozen=True, slots=True)
class StoredAnswer:
    """Серверная копия ответа вместе с владельцем сессии и сроком хранения."""

    query_id: str
    owner_id: str
    created_at: datetime
    expires_at: datetime
    answer: AnswerPayload

    @property
    def data_classes(self) -> frozenset[DataClass]:
        return frozenset(finding.data_class for finding in self.answer.findings)

    def info(self) -> StoredAnswerInfo:
        return StoredAnswerInfo(
            query_id=UUID(self.query_id),
            owner_id=self.owner_id,
            created_at=self.created_at,
            data_classes=sorted(value.value for value in self.data_classes),
            findings=len(self.answer.findings),
        )


class DurableState(Protocol):
    """Хранилище серверного состояния.

    Методы асинхронные: вызов идёт из HTTP-обработчика, и блокирующий сетевой
    запрос к Postgres не должен останавливать event loop.

    Списки отдаются «с новых к старым» — так читают журнал аудита и ленту
    уведомлений и в ``/health``, и в интерфейсе.
    """

    @property
    def kind(self) -> StoreBackend: ...

    async def setup(self) -> None: ...

    async def close(self) -> None: ...

    async def put_answer(self, answer: AnswerPayload, *, owner_id: str) -> StoredAnswer: ...

    async def get_answer(self, query_id: str) -> StoredAnswer | None: ...

    async def record_decision(self, decision: ExpertDecision) -> None: ...

    async def recent_decisions(
        self, *, limit: int = 100, actor_id: str | None = None
    ) -> list[ExpertDecision]: ...

    async def append_audit(self, event: AuditEvent) -> None: ...

    async def recent_audit(
        self,
        *,
        limit: int = 100,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> list[AuditEvent]: ...

    async def append_notification(self, notification: Notification) -> None: ...

    async def recent_notifications(self, *, limit: int = 20) -> list[Notification]: ...

    async def record_experiment(self, experiment: EvolutionExperiment) -> None: ...

    async def recent_experiments(self, *, limit: int = 100) -> list[EvolutionExperiment]: ...

    async def record_evaluation(self, run: EvaluationRun) -> None: ...

    async def recent_evaluations(self, *, limit: int = 100) -> list[EvaluationRun]: ...

    async def record_proposal(self, proposal: EvolutionProposal) -> None: ...

    async def recent_proposals(self, *, limit: int = 100) -> list[EvolutionProposal]: ...


def _new_answer(answer: AnswerPayload, owner_id: str, now: datetime) -> StoredAnswer:
    return StoredAnswer(
        query_id=str(answer.query_id),
        owner_id=owner_id,
        created_at=now,
        expires_at=now + ANSWER_TTL,
        answer=answer.model_copy(deep=True),
    )


class InMemoryDurableState:
    """Адаптер для тестов и запуска без базы: состояние живёт до выхода процесса.

    Здесь же — честная граница обещаний: на этом бэкенде лента уведомлений и
    журнал аудита НЕ переживают перезапуск, и ``/health/ready`` обязана это
    показывать (``state_backend: "in-memory"``), чтобы интерфейс не выдавал
    сохранённое за то, чем оно не является.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._answers: dict[str, StoredAnswer] = {}
        self._order: deque[str] = deque()
        self._decisions: deque[ExpertDecision] = deque(maxlen=MAX_DECISIONS)
        self._audit = InMemoryAuditLog(capacity=MAX_AUDIT_EVENTS)
        self._notifications: deque[Notification] = deque(maxlen=MAX_NOTIFICATIONS)
        self._experiments: deque[EvolutionExperiment] = deque(maxlen=MAX_EXPERIMENTS)
        self._proposals: dict[UUID, EvolutionProposal] = {}
        self._evaluations: deque[EvaluationRun] = deque(maxlen=MAX_EVALUATION_RUNS)

    @property
    def kind(self) -> StoreBackend:
        return "in-memory"

    async def setup(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def put_answer(self, answer: AnswerPayload, *, owner_id: str) -> StoredAnswer:
        now = datetime.now(UTC)
        record = _new_answer(answer, owner_id, now)
        async with self._lock:
            self._prune(now)
            if record.query_id not in self._answers:
                self._order.append(record.query_id)
            self._answers[record.query_id] = record
            while len(self._order) > MAX_STORED_ANSWERS:
                self._answers.pop(self._order.popleft(), None)
        return record

    async def get_answer(self, query_id: str) -> StoredAnswer | None:
        now = datetime.now(UTC)
        async with self._lock:
            self._prune(now)
            record = self._answers.get(query_id)
            if record is None:
                return None
            return replace(record, answer=record.answer.model_copy(deep=True))

    async def record_decision(self, decision: ExpertDecision) -> None:
        async with self._lock:
            self._decisions.append(decision.model_copy(deep=True))

    async def recent_decisions(
        self, *, limit: int = 100, actor_id: str | None = None
    ) -> list[ExpertDecision]:
        async with self._lock:
            items = [
                item
                for item in self._decisions
                if actor_id is None or item.actor_id == actor_id
            ][-max(limit, 1) :]
        return [item.model_copy(deep=True) for item in reversed(items)]

    async def append_audit(self, event: AuditEvent) -> None:
        async with self._lock:
            self._audit.append(event)

    async def recent_audit(
        self,
        *,
        limit: int = 100,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> list[AuditEvent]:
        async with self._lock:
            events = self._audit.list(correlation_id=correlation_id, actor_id=actor_id)
        return [event.model_copy(deep=True) for event in reversed(events[-max(limit, 1) :])]

    async def append_notification(self, notification: Notification) -> None:
        async with self._lock:
            self._notifications.append(notification.model_copy(deep=True))

    async def recent_notifications(self, *, limit: int = 20) -> list[Notification]:
        async with self._lock:
            items = list(self._notifications)[-max(limit, 1) :]
        return [item.model_copy(deep=True) for item in reversed(items)]

    async def record_experiment(self, experiment: EvolutionExperiment) -> None:
        async with self._lock:
            self._experiments.append(experiment.model_copy(deep=True))

    async def recent_experiments(self, *, limit: int = 100) -> list[EvolutionExperiment]:
        async with self._lock:
            items = list(self._experiments)[-max(limit, 1) :]
        return [item.model_copy(deep=True) for item in reversed(items)]

    async def record_proposal(self, proposal: EvolutionProposal) -> None:
        async with self._lock:
            self._proposals[proposal.id] = proposal.model_copy(deep=True)
            while len(self._proposals) > MAX_PROPOSALS:
                # Первыми уходят отклонённые и самые старые: принятые предложения
                # формируют активную политику промпта после гидратации.
                drop = min(
                    self._proposals.values(),
                    key=lambda item: (item.status == "accepted", item.created_at),
                )
                self._proposals.pop(drop.id, None)

    async def recent_proposals(self, *, limit: int = 100) -> list[EvolutionProposal]:
        async with self._lock:
            items = sorted(self._proposals.values(), key=lambda item: item.created_at)
        return [item.model_copy(deep=True) for item in reversed(items[-max(limit, 1) :])]

    async def record_evaluation(self, run: EvaluationRun) -> None:
        async with self._lock:
            self._evaluations.append(run.model_copy(deep=True))

    async def recent_evaluations(self, *, limit: int = 100) -> list[EvaluationRun]:
        async with self._lock:
            items = list(self._evaluations)[-max(limit, 1) :]
        return [item.model_copy(deep=True) for item in reversed(items)]

    def _prune(self, now: datetime) -> None:
        for key in [key for key, item in self._answers.items() if item.expires_at <= now]:
            self._answers.pop(key, None)
        alive = set(self._answers)
        self._order = deque(key for key in self._order if key in alive)


_ANSWERS_DDL = """
CREATE TABLE IF NOT EXISTS nk_answers (
    query_id text PRIMARY KEY,
    owner_id text NOT NULL,
    answer_json text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL
)
"""

_ANSWERS_OWNER_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS nk_answers_owner_idx ON nk_answers (owner_id)"
)

_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS nk_expert_decisions (
    id text PRIMARY KEY,
    actor_id text NOT NULL,
    action text NOT NULL,
    object_id text NOT NULL,
    outcome text NOT NULL,
    metadata jsonb NOT NULL,
    created_at timestamptz NOT NULL
)
"""

# correlation_id — отдельной колонкой и с индексом: разбор инцидента начинается
# с него, и «пересобрать журнал фильтром по metadata» означает полное сканирование.
_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS nk_audit_events (
    id text PRIMARY KEY,
    actor_id text NOT NULL,
    action text NOT NULL,
    object_id text NOT NULL,
    outcome text NOT NULL,
    correlation_id text NOT NULL,
    metadata jsonb NOT NULL,
    created_at timestamptz NOT NULL
)
"""

_AUDIT_CORRELATION_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS nk_audit_events_correlation_idx "
    "ON nk_audit_events (correlation_id, created_at DESC)"
)

_NOTIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS nk_notifications (
    id text PRIMARY KEY,
    topic text NOT NULL,
    message text NOT NULL,
    created_at timestamptz NOT NULL
)
"""

# A/B-эксперименты и прогоны оценки перечитываются целиком (список короткий,
# потолок выражен константой), поэтому храним payload как есть: новые поля метрик
# не требуют ALTER и не ломают чтение старых записей.
_EXPERIMENTS_DDL = """
CREATE TABLE IF NOT EXISTS nk_experiments (
    id text PRIMARY KEY,
    proposal_id text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL
)
"""

_EVALUATIONS_DDL = """
CREATE TABLE IF NOT EXISTS nk_evaluation_runs (
    id text PRIMARY KEY,
    query_id text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL
)
"""

_PROPOSALS_DDL = """
CREATE TABLE IF NOT EXISTS nk_evolution_proposals (
    id text PRIMARY KEY,
    status text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL
)
"""


def _answer_from_row(row: dict[str, Any]) -> StoredAnswer:
    payload = AnswerPayload.model_validate_json(str(row["answer_json"]))
    return StoredAnswer(
        query_id=str(row["query_id"]),
        owner_id=str(row["owner_id"]),
        created_at=cast(datetime, row["created_at"]),
        expires_at=cast(datetime, row["expires_at"]),
        answer=payload,
    )


def _decision_from_row(row: dict[str, Any]) -> ExpertDecision:
    return ExpertDecision(
        id=UUID(str(row["id"])),
        actor_id=str(row["actor_id"]),
        action=cast(Any, row["action"]),
        object_id=str(row["object_id"]),
        outcome=cast(Any, row["outcome"]),
        created_at=cast(datetime, row["created_at"]),
        metadata=dict(row["metadata"] or {}),
    )


def _audit_from_row(row: dict[str, Any]) -> AuditEvent:
    return AuditEvent(
        id=UUID(str(row["id"])),
        actor_id=str(row["actor_id"]),
        action=str(row["action"]),
        object_id=str(row["object_id"]),
        outcome=cast(Any, row["outcome"]),
        correlation_id=str(row["correlation_id"]),
        created_at=cast(datetime, row["created_at"]),
        metadata=dict(row["metadata"] or {}),
    )


def _notification_from_row(row: dict[str, Any]) -> Notification:
    return Notification(
        id=UUID(str(row["id"])),
        topic=cast(Any, row["topic"]),
        message=str(row["message"]),
        created_at=cast(datetime, row["created_at"]),
    )


class PostgresDurableState:
    """Рабочий адаптер: те же таблицы, что и у учётных записей, одним пулом."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: AsyncConnectionPool[AsyncConnection] | None = None

    @property
    def kind(self) -> StoreBackend:
        return "postgres"

    @property
    def _ready(self) -> AsyncConnectionPool[AsyncConnection]:
        if self._pool is None:
            raise RuntimeError("PostgresDurableState.setup() не вызван")
        return self._pool

    async def setup(self) -> None:
        """Открывает пул и создаёт схемы. Ошибку не глотает: её решит lifespan."""
        pool: AsyncConnectionPool[AsyncConnection] = AsyncConnectionPool(
            conninfo=self._settings.database_url,
            min_size=1,
            max_size=4,
            open=False,
            timeout=5.0,
            name="nk-state",
            kwargs={"autocommit": True, "row_factory": dict_row},
        )
        try:
            await pool.open(wait=True, timeout=5.0)
            async with pool.connection(timeout=5.0) as conn:
                for ddl in (
                    _ANSWERS_DDL,
                    _ANSWERS_OWNER_INDEX_DDL,
                    _DECISIONS_DDL,
                    _AUDIT_DDL,
                    _AUDIT_CORRELATION_INDEX_DDL,
                    _NOTIFICATIONS_DDL,
                    _EXPERIMENTS_DDL,
                    _EVALUATIONS_DDL,
                    _PROPOSALS_DDL,
                ):
                    await conn.execute(ddl)
        except Exception:
            await pool.close()
            raise
        self._pool = pool
        logger.info("Серверное состояние готово (postgres)")

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def _execute(self, query: str, params: Sequence[Any] = ()) -> int:
        async with self._ready.connection(timeout=5.0) as conn:
            cursor = await conn.execute(query, params or None)
            return int(cursor.rowcount)

    async def _fetchall(self, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        async with self._ready.connection(timeout=5.0) as conn:
            cursor = await conn.execute(query, params or None)
            return cast("list[dict[str, Any]]", await cursor.fetchall())

    async def _fetchone(self, query: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        async with self._ready.connection(timeout=5.0) as conn:
            cursor = await conn.execute(query, params or None)
            return cast("dict[str, Any] | None", await cursor.fetchone())

    async def _trim_table(self, table: str, primary_key: str, limit: int) -> None:
        """Держит таблицу под потолком длины.

        Имена таблицы и ключа — литералы этого модуля: идентификатор в SQL
        параметризовать нельзя, поэтому принимаемых от вызывающей стороны строк
        здесь не бывает.
        """
        await self._execute(
            f"DELETE FROM {table} WHERE {primary_key} NOT IN ("
            f" SELECT {primary_key} FROM {table} ORDER BY created_at DESC LIMIT %s)",
            (limit,),
        )

    async def put_answer(self, answer: AnswerPayload, *, owner_id: str) -> StoredAnswer:
        now = datetime.now(UTC)
        record = _new_answer(answer, owner_id, now)
        # Просрочку убираем на записи: таблица не должна разрастаться молча.
        await self._execute("DELETE FROM nk_answers WHERE expires_at <= %s", (now,))
        await self._execute(
            """
            INSERT INTO nk_answers (query_id, owner_id, answer_json, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (query_id) DO UPDATE
            SET answer_json = EXCLUDED.answer_json, expires_at = EXCLUDED.expires_at
            """,
            (record.query_id, owner_id, answer.model_dump_json(), now, record.expires_at),
        )
        await self._trim_table("nk_answers", "query_id", MAX_STORED_ANSWERS)
        return record

    async def get_answer(self, query_id: str) -> StoredAnswer | None:
        row = await self._fetchone(
            "SELECT query_id, owner_id, answer_json, created_at, expires_at FROM nk_answers "
            "WHERE query_id = %s AND expires_at > %s",
            (query_id, datetime.now(UTC)),
        )
        return _answer_from_row(row) if row else None

    async def record_decision(self, decision: ExpertDecision) -> None:
        await self._execute(
            """
            INSERT INTO nk_expert_decisions (id, actor_id, action, object_id, outcome, metadata,
                                             created_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                str(decision.id),
                decision.actor_id,
                decision.action,
                decision.object_id,
                decision.outcome,
                json.dumps(decision.metadata, ensure_ascii=False),
                decision.created_at,
            ),
        )
        await self._trim_table("nk_expert_decisions", "id", MAX_DECISIONS)

    async def recent_decisions(
        self, *, limit: int = 100, actor_id: str | None = None
    ) -> list[ExpertDecision]:
        columns = (
            "SELECT id, actor_id, action, object_id, outcome, metadata, created_at "
            "FROM nk_expert_decisions "
        )
        if actor_id is not None:
            rows = await self._fetchall(
                columns + "WHERE actor_id = %s ORDER BY created_at DESC LIMIT %s",
                (actor_id, max(limit, 1)),
            )
        else:
            rows = await self._fetchall(
                columns + "ORDER BY created_at DESC LIMIT %s", (max(limit, 1),)
            )
        return [_decision_from_row(row) for row in rows]

    async def append_audit(self, event: AuditEvent) -> None:
        await self._execute(
            """
            INSERT INTO nk_audit_events (id, actor_id, action, object_id, outcome,
                                         correlation_id, metadata, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                str(event.id),
                event.actor_id,
                event.action,
                event.object_id,
                event.outcome,
                event.correlation_id,
                json.dumps(event.metadata, ensure_ascii=False),
                event.created_at,
            ),
        )
        await self._trim_table("nk_audit_events", "id", MAX_AUDIT_EVENTS)

    async def recent_audit(
        self,
        *,
        limit: int = 100,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> list[AuditEvent]:
        # Значения только параметрами: ``/audit`` и панель — читательские пути,
        # а ``actor_id`` приходит из серверной сессии, не из SQL-литерала.
        where = []
        params: list[Any] = []
        if correlation_id is not None:
            where.append("correlation_id = %s")
            params.append(correlation_id)
        if actor_id is not None:
            where.append("actor_id = %s")
            params.append(actor_id)
        conditions = f"WHERE {' AND '.join(where)} " if where else ""
        params.append(max(limit, 1))
        rows = await self._fetchall(
            "SELECT id, actor_id, action, object_id, outcome, correlation_id, metadata, "
            f"created_at FROM nk_audit_events {conditions}"
            "ORDER BY created_at DESC LIMIT %s",
            params,
        )
        return [_audit_from_row(row) for row in rows]

    async def append_notification(self, notification: Notification) -> None:
        await self._execute(
            """
            INSERT INTO nk_notifications (id, topic, message, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                str(notification.id),
                notification.topic,
                notification.message,
                notification.created_at,
            ),
        )
        await self._trim_table("nk_notifications", "id", MAX_NOTIFICATIONS)

    async def recent_notifications(self, *, limit: int = 20) -> list[Notification]:
        rows = await self._fetchall(
            "SELECT id, topic, message, created_at FROM nk_notifications "
            "ORDER BY created_at DESC LIMIT %s",
            (max(limit, 1),),
        )
        return [_notification_from_row(row) for row in rows]

    async def record_experiment(self, experiment: EvolutionExperiment) -> None:
        await self._execute(
            """
            INSERT INTO nk_experiments (id, proposal_id, payload, created_at)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload
            """,
            (
                str(experiment.id),
                str(experiment.proposal_id),
                experiment.model_dump_json(),
                experiment.created_at,
            ),
        )
        await self._trim_table("nk_experiments", "id", MAX_EXPERIMENTS)

    async def recent_experiments(self, *, limit: int = 100) -> list[EvolutionExperiment]:
        rows = await self._fetchall(
            "SELECT payload FROM nk_experiments ORDER BY created_at DESC LIMIT %s",
            (max(limit, 1),),
        )
        return [EvolutionExperiment.model_validate(row["payload"]) for row in rows]

    async def record_proposal(self, proposal: EvolutionProposal) -> None:
        await self._execute(
            """
            INSERT INTO nk_evolution_proposals (id, status, payload, created_at)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (id) DO UPDATE
            SET status = EXCLUDED.status, payload = EXCLUDED.payload
            """,
            (
                str(proposal.id),
                proposal.status,
                proposal.model_dump_json(),
                proposal.created_at,
            ),
        )
        await self._trim_table("nk_evolution_proposals", "id", MAX_PROPOSALS)

    async def recent_proposals(self, *, limit: int = 100) -> list[EvolutionProposal]:
        rows = await self._fetchall(
            "SELECT payload FROM nk_evolution_proposals ORDER BY created_at DESC LIMIT %s",
            (max(limit, 1),),
        )
        return [EvolutionProposal.model_validate(row["payload"]) for row in rows]

    async def record_evaluation(self, run: EvaluationRun) -> None:
        await self._execute(
            """
            INSERT INTO nk_evaluation_runs (id, query_id, payload, created_at)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload
            """,
            (str(run.id), str(run.query_id), run.model_dump_json(), run.created_at),
        )
        await self._trim_table("nk_evaluation_runs", "id", MAX_EVALUATION_RUNS)

    async def recent_evaluations(self, *, limit: int = 100) -> list[EvaluationRun]:
        rows = await self._fetchall(
            "SELECT payload FROM nk_evaluation_runs ORDER BY created_at DESC LIMIT %s",
            (max(limit, 1),),
        )
        return [EvaluationRun.model_validate(row["payload"]) for row in rows]


def build_durable_state(settings: Settings) -> DurableState:
    """Выбор контура по ``accounts_backend`` — вместе с учётными записями.

    Конструируется без подключения: открытие пула и деградация — дело lifespan,
    там же, где и у ``build_accounts``.
    """
    if settings.accounts_backend == "postgres":
        return PostgresDurableState(settings)
    return InMemoryDurableState()
