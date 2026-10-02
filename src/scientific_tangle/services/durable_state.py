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
* ``nk_llm_usage`` — расход модели по учётным записям. Глобальный счётчик токенов
  процесса не отвечает на вопрос «сколько стоили исследования этого аналитика»,
  а тарифная сетка в контуре не задана, поэтому расход выражается токенами.

Контур выбора хранилища повторяет ``services/accounts.py``: Postgres — рабочая
ветка, память — тесты и запуск без базы, при недоступной базе — деградация в
память с причиной в ``/health/ready``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    AnswerPayload,
    ConflictReview,
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
#
# Потолок держится НА УЧЁТНУЮ ЗАПИСЬ, а не на весь сервис: при общем лимите в
# 200 копий экспорт собственного ответа давал 404 ровно тогда, когда импортируют
# и спрашивают другие аналитики («answer not found» — это потерянный результат
# работы, а не пустая лента). ``MAX_STORED_ANSWERS_TOTAL`` — предохранитель от
# линейного роста числа активных аккаунтов, и он на порядок больше личного
# потолка, то есть вытесняет чужой ответ только когда журнал упёрся в потолок
# всего процесса.
MAX_STORED_ANSWERS = 100
MAX_STORED_ANSWERS_TOTAL = 5000
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
# Решения по противоречиям — не поток событий, а состояние пары: первичный ключ
# один на пару, поэтому повторное решение обновляет запись, а не множит её.
# Потолок страховочный: он ограничивает число рассмотренных пар, а не число
# обращений к эндпоинту.
MAX_CONFLICT_REVIEWS = 5000
MAX_EVALUATION_RUNS = 200
# Запись закрывает один прогон (реже — одно обращение к модели): потолок держит
# недельную историю расходов, но не превращает таблицу в бесконечный журнал.
MAX_LLM_USAGE_RECORDS = 20000
# Рабочие треды LangGraph. Успешный прогон снимает свой тред ``adelete_thread``,
# а прерванный (отказ модели, снятый процессом worker, отмена по deadline)
# остаётся навсегда: ``checkpoints``/``checkpoint_blobs``/``checkpoint_writes``
# растут только вверх. Порог консервативный — удаляются треды, у которых и
# ПОСЛЕДНИЙ чекпоинт старше срока: активный прогон затронут быть не может.
CHECKPOINT_THREAD_TTL = timedelta(days=7)
CHECKPOINT_CLEANUP_INTERVAL_SECONDS = 6 * 3600.0
CHECKPOINT_CLEANUP_BATCH_THREADS = 200

__all__ = [
    "CHECKPOINT_CLEANUP_BATCH_THREADS",
    "CHECKPOINT_CLEANUP_INTERVAL_SECONDS",
    "CHECKPOINT_THREAD_TTL",
    "CheckpointPurgeReport",
    "DurableState",
    "InMemoryDurableState",
    "LlmAccountUsage",
    "LlmUsageRecord",
    "PostgresDurableState",
    "StoredAnswer",
    "build_durable_state",
    "run_checkpoint_cleanup",
]


@dataclass(frozen=True, slots=True)
class CheckpointPurgeReport:
    """Итог одной волны очистки рабочих тредов LangGraph.

    ``removed_threads`` и ``removed_rows`` идут в журнал и в ``/health/ready``:
    молча удалять чужое состояние нельзя, оператор обязан видеть, сколько и
    насколько древних тредов контур счёл осиротевшими.
    """

    removed_threads: int = 0
    removed_rows: int = 0
    skipped_reason: str | None = None


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


@dataclass(frozen=True, slots=True)
class LlmUsageRecord:
    """Расход модели, привязанный к учётной записи: «сколько стоил вопрос».

    Цена здесь именно токены: тарифной сетки GigaChat в конфигурации нет, и
    перевод в деньги был бы выдумкой. ``schema_name`` — назначение вызова
    (PlanningBundle, ReasonerOutput…), если вызывающая сторона его знает.
    """

    account_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    success: bool
    query_id: str | None = None
    schema_name: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    id: str = field(default_factory=lambda: str(uuid4()))

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class LlmAccountUsage:
    """Сводка расхода одной учётной записи за окно."""

    account_id: str
    calls: int
    failed_calls: int
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


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
        self, *, limit: int = 100, offset: int = 0, actor_id: str | None = None
    ) -> list[ExpertDecision]: ...

    async def count_decisions(self, *, actor_id: str | None = None) -> int: ...

    async def append_audit(self, event: AuditEvent) -> None: ...

    async def recent_audit(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> list[AuditEvent]: ...

    async def count_audit(
        self, *, correlation_id: str | None = None, actor_id: str | None = None
    ) -> int: ...

    async def append_notification(self, notification: Notification) -> None: ...

    async def recent_notifications(self, *, limit: int = 20) -> list[Notification]: ...

    async def count_notifications(self) -> int: ...

    async def record_experiment(self, experiment: EvolutionExperiment) -> None: ...

    async def recent_experiments(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[EvolutionExperiment]: ...

    async def count_experiments(self) -> int: ...

    async def record_evaluation(self, run: EvaluationRun) -> None: ...

    async def recent_evaluations(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[EvaluationRun]: ...

    async def count_evaluations(self) -> int: ...

    async def record_proposal(self, proposal: EvolutionProposal) -> None: ...

    async def recent_proposals(self, *, limit: int = 100) -> list[EvolutionProposal]: ...

    async def record_conflict_review(self, review: ConflictReview) -> None: ...

    async def conflict_reviews(self, *, limit: int = 1000) -> list[ConflictReview]: ...

    async def record_llm_usage(
        self,
        *,
        account_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        success: bool,
        query_id: str | None = None,
        schema_name: str | None = None,
    ) -> LlmUsageRecord: ...

    async def usage_by_account(self, *, since: datetime) -> list[LlmAccountUsage]: ...

    async def purge_stale_checkpoint_threads(
        self,
        *,
        older_than: timedelta = CHECKPOINT_THREAD_TTL,
        batch_threads: int = CHECKPOINT_CLEANUP_BATCH_THREADS,
    ) -> CheckpointPurgeReport:
        """Снимает рабочие треды LangGraph, у которых последний чекпоинт старше срока.

        Метод в контракте, а не в одной ветке: ``/health/ready`` и оператор читают
        один и тот же контракт, а память обязана честно отвечать «чистить нечего»,
        вместо ``AttributeError`` в планировщике.
        """
        ...


def _new_answer(answer: AnswerPayload, owner_id: str, now: datetime) -> StoredAnswer:
    return StoredAnswer(
        query_id=str(answer.query_id),
        owner_id=owner_id,
        created_at=now,
        expires_at=now + ANSWER_TTL,
        answer=answer.model_copy(deep=True),
    )


def _summarize_usage(records: Iterable[LlmUsageRecord]) -> list[LlmAccountUsage]:
    """Сводка расхода по учётным записям.

    Формула совпадает с SQL-агрегатом ``usage_by_account`` рабочей ветки:
    «сколько стоили вопросы аналитика» обязано читаться одинаково на памяти и на
    Postgres, иначе тесты на in-memory контуре ничего не проверяют.
    """
    totals: dict[str, list[int]] = {}
    for record in records:
        acc = totals.setdefault(record.account_id, [0, 0, 0, 0])
        acc[0] += 1
        acc[1] += 0 if record.success else 1
        acc[2] += record.prompt_tokens
        acc[3] += record.completion_tokens
    return sorted(
        (
            LlmAccountUsage(
                account_id=account_id,
                calls=acc[0],
                failed_calls=acc[1],
                prompt_tokens=acc[2],
                completion_tokens=acc[3],
            )
            for account_id, acc in totals.items()
        ),
        key=lambda item: (-item.total_tokens, item.account_id),
    )


def _new_usage(
    *,
    account_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: float,
    success: bool,
    query_id: str | None,
    schema_name: str | None,
) -> LlmUsageRecord:
    """Один конструктор записи для обеих веток.

    Отрицательные значения отсекаются: usage провайдера меньше нуля не бывает, а
    кривая оценка не должна уменьшать суммарный расход аккаунта.
    """
    return LlmUsageRecord(
        account_id=account_id,
        model=model,
        prompt_tokens=max(prompt_tokens, 0),
        completion_tokens=max(completion_tokens, 0),
        latency_ms=max(latency_ms, 0.0),
        success=success,
        query_id=query_id,
        schema_name=schema_name,
    )


class InMemoryDurableState:
    """Адаптер для тестов и запуска без базы: состояние живёт до выхода процесса.

    Здесь же — честная граница обещаний: на этом бэкенде лента уведомлений, журнал
    аудита и журнал расхода модели НЕ переживают перезапуск, и ``/health/ready``
    обязана это показывать (``state_backend: "in-memory"``), чтобы интерфейс не
    выдавал сохранённое за то, чем оно не является.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # ``OrderedDict`` — LRU по обращению, а не порядок вставки: перечитанный
        # ответ уходит в конец очереди, и вытеснение, идущее с начала, до него не
        # добирается. Прежний deque вставлял ответ в хвост один раз и при trim
        # снимал его же, если аналитик как раз открыл экспорт.
        self._answers: OrderedDict[str, StoredAnswer] = OrderedDict()
        self._decisions: deque[ExpertDecision] = deque(maxlen=MAX_DECISIONS)
        self._audit = InMemoryAuditLog(capacity=MAX_AUDIT_EVENTS)
        self._notifications: deque[Notification] = deque(maxlen=MAX_NOTIFICATIONS)
        self._experiments: deque[EvolutionExperiment] = deque(maxlen=MAX_EXPERIMENTS)
        self._proposals: dict[UUID, EvolutionProposal] = {}
        self._conflict_reviews: dict[str, ConflictReview] = {}
        self._evaluations: deque[EvaluationRun] = deque(maxlen=MAX_EVALUATION_RUNS)
        self._llm_usage: deque[LlmUsageRecord] = deque(maxlen=MAX_LLM_USAGE_RECORDS)

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
            self._answers[record.query_id] = record
            self._answers.move_to_end(record.query_id)
            self._trim_answers(record.owner_id)
        return record

    async def get_answer(self, query_id: str) -> StoredAnswer | None:
        now = datetime.now(UTC)
        async with self._lock:
            self._prune(now)
            record = self._answers.get(query_id)
            if record is None:
                return None
            # Отметка обращения — не косметика: журнал вытесняет по «давности
            # пользования», и перечитанный ответ обязан перестать быть кандидатом.
            self._answers.move_to_end(query_id)
            return replace(record, answer=record.answer.model_copy(deep=True))

    async def record_decision(self, decision: ExpertDecision) -> None:
        async with self._lock:
            self._decisions.append(decision.model_copy(deep=True))

    async def recent_decisions(
        self, *, limit: int = 100, offset: int = 0, actor_id: str | None = None
    ) -> list[ExpertDecision]:
        async with self._lock:
            newest_first = list(
                reversed(
                    [
                        item
                        for item in self._decisions
                        if actor_id is None or item.actor_id == actor_id
                    ]
                )
            )
            page = newest_first[offset : offset + max(limit, 1)]
        return [item.model_copy(deep=True) for item in page]

    async def count_decisions(self, *, actor_id: str | None = None) -> int:
        async with self._lock:
            return len(
                [
                    item
                    for item in self._decisions
                    if actor_id is None or item.actor_id == actor_id
                ]
            )

    async def append_audit(self, event: AuditEvent) -> None:
        async with self._lock:
            self._audit.append(event)

    async def recent_audit(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> list[AuditEvent]:
        async with self._lock:
            events = self._audit.list(correlation_id=correlation_id, actor_id=actor_id)
        # Лента «с новых к старым»: окно берём от конца списка (самые свежие),
        # поэтому offset=0 — первая страница, а offset за длиной — пустой массив.
        newest_first = list(reversed(events))
        page = newest_first[offset : offset + max(limit, 1)]
        return [event.model_copy(deep=True) for event in page]

    async def count_audit(
        self, *, correlation_id: str | None = None, actor_id: str | None = None
    ) -> int:
        # Тот же предикат, что у чтения: total обязан сходиться со страницей,
        # иначе «больше не было» и «не дочитали» снова неразличимы.
        async with self._lock:
            return len(self._audit.list(correlation_id=correlation_id, actor_id=actor_id))

    async def append_notification(self, notification: Notification) -> None:
        async with self._lock:
            self._notifications.append(notification.model_copy(deep=True))

    async def recent_notifications(self, *, limit: int = 20) -> list[Notification]:
        async with self._lock:
            items = list(self._notifications)[-max(limit, 1) :]
        return [item.model_copy(deep=True) for item in reversed(items)]

    async def count_notifications(self) -> int:
        async with self._lock:
            return len(self._notifications)

    async def record_experiment(self, experiment: EvolutionExperiment) -> None:
        async with self._lock:
            self._experiments.append(experiment.model_copy(deep=True))

    async def recent_experiments(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[EvolutionExperiment]:
        async with self._lock:
            newest_first = list(reversed(self._experiments))
            page = newest_first[offset : offset + max(limit, 1)]
        return [item.model_copy(deep=True) for item in page]

    async def count_experiments(self) -> int:
        async with self._lock:
            return len(self._experiments)

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

    async def record_conflict_review(self, review: ConflictReview) -> None:
        async with self._lock:
            self._conflict_reviews[review.candidate_id] = review.model_copy(deep=True)
            while len(self._conflict_reviews) > MAX_CONFLICT_REVIEWS:
                oldest = min(self._conflict_reviews.values(), key=lambda item: item.created_at)
                self._conflict_reviews.pop(oldest.candidate_id, None)

    async def conflict_reviews(self, *, limit: int = 1000) -> list[ConflictReview]:
        async with self._lock:
            items = sorted(self._conflict_reviews.values(), key=lambda item: item.created_at)
        return [item.model_copy(deep=True) for item in reversed(items[-max(limit, 1) :])]

    async def record_evaluation(self, run: EvaluationRun) -> None:
        async with self._lock:
            self._evaluations.append(run.model_copy(deep=True))

    async def recent_evaluations(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[EvaluationRun]:
        async with self._lock:
            newest_first = list(reversed(self._evaluations))
            page = newest_first[offset : offset + max(limit, 1)]
        return [item.model_copy(deep=True) for item in page]

    async def count_evaluations(self) -> int:
        async with self._lock:
            return len(self._evaluations)

    async def record_llm_usage(
        self,
        *,
        account_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        success: bool,
        query_id: str | None = None,
        schema_name: str | None = None,
    ) -> LlmUsageRecord:
        record = _new_usage(
            account_id=account_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            success=success,
            query_id=query_id,
            schema_name=schema_name,
        )
        async with self._lock:
            self._llm_usage.append(record)
        return record

    async def usage_by_account(self, *, since: datetime) -> list[LlmAccountUsage]:
        async with self._lock:
            records = [record for record in self._llm_usage if record.created_at >= since]
        return _summarize_usage(records)

    def _prune(self, now: datetime) -> None:
        for key in [key for key, item in self._answers.items() if item.expires_at <= now]:
            self._answers.pop(key, None)

    def _trim_answers(self, owner_id: str) -> None:
        """Вытеснение: потолок на аккаунт, затем общий предохранитель процесса.

        Обход идёт с начала LRU-очереди (давнее обращение), поэтому ответ,
        запрошенный секунду назад, — последний кандидат на removal: он уже
        переставлен в хвост. Личный потолок считает по своему владельцу, и чужая
        активность не съедает историю аналитика.
        """
        own = [key for key, item in self._answers.items() if item.owner_id == owner_id]
        while len(own) > MAX_STORED_ANSWERS:
            self._answers.pop(own.pop(0), None)
        while len(self._answers) > MAX_STORED_ANSWERS_TOTAL:
            self._answers.popitem(last=False)

    async def purge_stale_checkpoint_threads(
        self,
        *,
        older_than: timedelta = CHECKPOINT_THREAD_TTL,
        batch_threads: int = CHECKPOINT_CLEANUP_BATCH_THREADS,
    ) -> CheckpointPurgeReport:
        """На памяти чекпоинтов нет: агентный граф работает без Postgres-сейвера."""
        del older_than, batch_threads
        return CheckpointPurgeReport(skipped_reason="in-memory: чекпоинты не сохраняются")


_ANSWERS_DDL = """
CREATE TABLE IF NOT EXISTS nk_answers (
    query_id text PRIMARY KEY,
    owner_id text NOT NULL,
    answer_json text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    last_accessed_at timestamptz
)
"""

# Таблица в рабочем контуре уже создана без колонки обращений: ``ALTER ... ADD
# COLUMN IF NOT EXISTS`` догоняет её на старте, иначе вытеснение по «давности
# пользования» было бы доступно только свежим установкам.
_ANSWERS_ACCESS_COLUMN_DDL = (
    "ALTER TABLE nk_answers ADD COLUMN IF NOT EXISTS last_accessed_at timestamptz"
)

_ANSWERS_OWNER_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS nk_answers_owner_idx ON nk_answers (owner_id)"
)

# Личный потолок выбирает «кого вытеснить первым» по этому порядку — без индекса
# это сортировка по всей таблице на каждый ответ.
_ANSWERS_OWNER_ACCESS_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS nk_answers_owner_access_idx ON nk_answers "
    "(owner_id, coalesce(last_accessed_at, created_at) DESC)"
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

# Решение привязано к отпечатку пары тезисов, поэтому первичный ключ —
# ``candidate_id``: повторное решение той же пары обновляет строку. Через
# ``nk_expert_decisions`` эта история не проходит — тот журнал вытесняет
# старейшие записи по общему потолку, а подтверждённое противоречие, молча
# исчезнувшее из очереди, снова выглядело бы нерассмотренным.
_CONFLICT_REVIEWS_DDL = """
CREATE TABLE IF NOT EXISTS nk_conflict_reviews (
    candidate_id text PRIMARY KEY,
    status text NOT NULL,
    actor_id text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL
)
"""

# Расход модели — не jsonb payload: его читают агрегатом по аккаунту и окну, и
# колонки (а не пересборка JSON) дают и группировку, и индекс по диапазону дат.
_LLM_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS nk_llm_usage (
    id text PRIMARY KEY,
    account_id text NOT NULL,
    query_id text,
    model text NOT NULL,
    schema_name text,
    prompt_tokens integer NOT NULL,
    completion_tokens integer NOT NULL,
    latency_ms double precision NOT NULL,
    success boolean NOT NULL,
    created_at timestamptz NOT NULL
)
"""

_LLM_USAGE_ACCOUNT_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS nk_llm_usage_account_idx "
    "ON nk_llm_usage (account_id, created_at DESC)"
)


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
                    _ANSWERS_ACCESS_COLUMN_DDL,
                    _ANSWERS_OWNER_INDEX_DDL,
                    _ANSWERS_OWNER_ACCESS_INDEX_DDL,
                    _DECISIONS_DDL,
                    _AUDIT_DDL,
                    _AUDIT_CORRELATION_INDEX_DDL,
                    _NOTIFICATIONS_DDL,
                    _EXPERIMENTS_DDL,
                    _EVALUATIONS_DDL,
                    _PROPOSALS_DDL,
                    _CONFLICT_REVIEWS_DDL,
                    _LLM_USAGE_DDL,
                    _LLM_USAGE_ACCOUNT_INDEX_DDL,
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
            INSERT INTO nk_answers (query_id, owner_id, answer_json, created_at, expires_at,
                                    last_accessed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (query_id) DO UPDATE
            SET answer_json = EXCLUDED.answer_json,
                expires_at = EXCLUDED.expires_at,
                last_accessed_at = EXCLUDED.last_accessed_at
            """,
            (record.query_id, owner_id, answer.model_dump_json(), now, record.expires_at, now),
        )
        # Личный потолок владельца: прежний глобальный trim снимал старейшие копии
        # всего сервиса, и экспорт своего ответа получал 404 из-за чужой активности.
        await self._execute(
            """
            DELETE FROM nk_answers WHERE query_id IN (
                SELECT query_id FROM (
                    SELECT query_id,
                           row_number() OVER (
                               PARTITION BY owner_id
                               ORDER BY coalesce(last_accessed_at, created_at) DESC
                           ) AS rank
                    FROM nk_answers
                    WHERE owner_id = %s
                ) ranked
                WHERE ranked.rank > %s
            )
            """,
            (owner_id, MAX_STORED_ANSWERS),
        )
        # Общий предохранитель — LRU по всему журналу: он обязан сработать только
        # когда упёрся потолок процесса, а не тогда, когда много аналитиков спрашивают.
        await self._execute(
            "DELETE FROM nk_answers WHERE query_id NOT IN ("
            " SELECT query_id FROM nk_answers"
            " ORDER BY coalesce(last_accessed_at, created_at) DESC LIMIT %s)",
            (MAX_STORED_ANSWERS_TOTAL,),
        )
        return record

    async def get_answer(self, query_id: str) -> StoredAnswer | None:
        now = datetime.now(UTC)
        row = await self._fetchone(
            "SELECT query_id, owner_id, answer_json, created_at, expires_at FROM nk_answers "
            "WHERE query_id = %s AND expires_at > %s",
            (query_id, now),
        )
        if row is None:
            return None
        # Отметка обращения двигает ответ в начало LRU-очереди владельца: запись,
        # перечитанная секунду назад, не имеет права быть вытесненной следующим
        # же ответом того же аналитика. Один сбой UPDATE не отменяет чтение.
        try:
            await self._execute(
                "UPDATE nk_answers SET last_accessed_at = %s WHERE query_id = %s",
                (now, query_id),
            )
        except Exception as error:  # noqa: BLE001 - отметка пользования не критична для ответа
            logger.warning("Отметка обращения к ответу %s не сохранена: %s", query_id, error)
        return _answer_from_row(row)

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
        self, *, limit: int = 100, offset: int = 0, actor_id: str | None = None
    ) -> list[ExpertDecision]:
        columns = (
            "SELECT id, actor_id, action, object_id, outcome, metadata, created_at "
            "FROM nk_expert_decisions "
        )
        params: list[Any] = []
        conditions = ""
        if actor_id is not None:
            conditions = "WHERE actor_id = %s "
            params.append(actor_id)
        params.extend([max(limit, 1), max(offset, 0)])
        rows = await self._fetchall(
            columns + conditions + "ORDER BY created_at DESC LIMIT %s OFFSET %s", params
        )
        return [_decision_from_row(row) for row in rows]

    async def count_decisions(self, *, actor_id: str | None = None) -> int:
        # Тот же предикат, что у чтения, без ORDER BY и без окна: ``X-Total-Count``
        # обязан сходиться с длиной страницы, иначе «прочитаны все» снова
        # неразличимо с «не дочитали».
        if actor_id is None:
            row = await self._fetchone("SELECT COUNT(*) AS total FROM nk_expert_decisions")
        else:
            row = await self._fetchone(
                "SELECT COUNT(*) AS total FROM nk_expert_decisions WHERE actor_id = %s",
                (actor_id,),
            )
        return int(row["total"]) if row else 0

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
        offset: int = 0,
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
        params.append(max(offset, 0))
        rows = await self._fetchall(
            "SELECT id, actor_id, action, object_id, outcome, correlation_id, metadata, "
            f"created_at FROM nk_audit_events {conditions}"
            "ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params,
        )
        return [_audit_from_row(row) for row in rows]

    async def count_audit(
        self, *, correlation_id: str | None = None, actor_id: str | None = None
    ) -> int:
        # Те же WHERE, что у чтения, без JOIN и без LIMIT: X-Total-Count обязан
        # быть полным числом подходящих записей, а не длиной страницы.
        where = []
        params: list[Any] = []
        if correlation_id is not None:
            where.append("correlation_id = %s")
            params.append(correlation_id)
        if actor_id is not None:
            where.append("actor_id = %s")
            params.append(actor_id)
        conditions = f" WHERE {' AND '.join(where)}" if where else ""
        row = await self._fetchone(
            f"SELECT COUNT(*) AS total FROM nk_audit_events{conditions}", params
        )
        return int(row["total"]) if row else 0

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

    async def count_notifications(self) -> int:
        # Полное число записей ленты, а не длина страницы: потолок ленты
        # (`MAX_NOTIFICATIONS`) иначе был бы невидим читателю, и «прочитаны все»
        # отличался бы от «старые вытеснены».
        row = await self._fetchone("SELECT COUNT(*) AS total FROM nk_notifications")
        return int(row["total"]) if row else 0

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

    async def recent_experiments(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[EvolutionExperiment]:
        rows = await self._fetchall(
            "SELECT payload FROM nk_experiments ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (max(limit, 1), max(offset, 0)),
        )
        return [EvolutionExperiment.model_validate(row["payload"]) for row in rows]

    async def count_experiments(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS total FROM nk_experiments")
        return int(row["total"]) if row else 0

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

    async def record_conflict_review(self, review: ConflictReview) -> None:
        await self._execute(
            """
            INSERT INTO nk_conflict_reviews
                (candidate_id, status, actor_id, payload, created_at)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (candidate_id) DO UPDATE
            SET status = EXCLUDED.status,
                actor_id = EXCLUDED.actor_id,
                payload = EXCLUDED.payload,
                created_at = EXCLUDED.created_at
            """,
            (
                review.candidate_id,
                review.status,
                review.actor_id,
                review.model_dump_json(),
                review.created_at,
            ),
        )

    async def conflict_reviews(self, *, limit: int = 1000) -> list[ConflictReview]:
        rows = await self._fetchall(
            "SELECT payload FROM nk_conflict_reviews ORDER BY created_at DESC LIMIT %s",
            (max(limit, 1),),
        )
        return [ConflictReview.model_validate(row["payload"]) for row in rows]

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

    async def recent_evaluations(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[EvaluationRun]:
        rows = await self._fetchall(
            "SELECT payload FROM nk_evaluation_runs "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (max(limit, 1), max(offset, 0)),
        )
        return [EvaluationRun.model_validate(row["payload"]) for row in rows]

    async def count_evaluations(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS total FROM nk_evaluation_runs")
        return int(row["total"]) if row else 0

    async def record_llm_usage(
        self,
        *,
        account_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        success: bool,
        query_id: str | None = None,
        schema_name: str | None = None,
    ) -> LlmUsageRecord:
        record = _new_usage(
            account_id=account_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            success=success,
            query_id=query_id,
            schema_name=schema_name,
        )
        await self._execute(
            """
            INSERT INTO nk_llm_usage (id, account_id, query_id, model, schema_name,
                                      prompt_tokens, completion_tokens, latency_ms, success,
                                      created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                record.id,
                record.account_id,
                record.query_id,
                record.model,
                record.schema_name,
                record.prompt_tokens,
                record.completion_tokens,
                record.latency_ms,
                record.success,
                record.created_at,
            ),
        )
        await self._trim_table("nk_llm_usage", "id", MAX_LLM_USAGE_RECORDS)
        return record

    async def usage_by_account(self, *, since: datetime) -> list[LlmAccountUsage]:
        # Агрегат считается базой: «расход за месяц» по сотням тысяч строк в
        # payload-чтении означал бы вытащить весь журнал в процесс.
        rows = await self._fetchall(
            """
            SELECT account_id,
                   count(*) AS calls,
                   count(*) FILTER (WHERE NOT success) AS failed_calls,
                   sum(prompt_tokens) AS prompt_tokens,
                   sum(completion_tokens) AS completion_tokens
            FROM nk_llm_usage
            WHERE created_at >= %s
            GROUP BY account_id
            ORDER BY sum(prompt_tokens + completion_tokens) DESC, account_id
            """,
            (since,),
        )
        return [
            LlmAccountUsage(
                account_id=str(row["account_id"]),
                calls=int(row["calls"]),
                failed_calls=int(row["failed_calls"]),
                prompt_tokens=int(row["prompt_tokens"] or 0),
                completion_tokens=int(row["completion_tokens"] or 0),
            )
            for row in rows
        ]

    async def _table_present(self, table: str) -> bool:
        """Есть ли таблица в схеме: чекпоинты создаёт LangGraph, а не этот контур."""
        row = await self._fetchone("SELECT to_regclass(%s) AS table_name", (table,))
        return bool(row and row.get("table_name"))

    async def purge_stale_checkpoint_threads(
        self,
        *,
        older_than: timedelta = CHECKPOINT_THREAD_TTL,
        batch_threads: int = CHECKPOINT_CLEANUP_BATCH_THREADS,
    ) -> CheckpointPurgeReport:
        """Снимает треды LangGraph, у которых и последний чекпоинт старше срока.

        Успешный прогон вызывает ``adelete_thread`` сам; этот путь закрывает
        прерванный: отказ модели, отмена по дедлайну, снятый worker — и всё, что
        осталось от прошлой версии схемы. Консервативность задана условием
        ``max(ts) < cutoff`` по треду целиком: нитка, в которую писали на этой
        неделе, не удаляется независимо от числа её чекпоинтов.

        Дочерние таблицы снимаются первыми: тред без ``checkpoint_blobs`` и
        ``checkpoint_writes`` читался бы сейвером как повреждённый, а не как
        удалённый. Таблицы — литералы модуля (идентификатор в SQL не
        параметризуется), имена тредов уходят только параметром.
        """
        if not await self._table_present("checkpoints"):
            return CheckpointPurgeReport(skipped_reason="таблица checkpoints не создана")
        cutoff = datetime.now(UTC) - older_than
        try:
            rows = await self._fetchall(
                """
                SELECT thread_id, max((checkpoint->>'ts')::timestamptz) AS last_seen
                FROM checkpoints
                GROUP BY thread_id
                HAVING max((checkpoint->>'ts')::timestamptz) < %s
                ORDER BY last_seen ASC
                LIMIT %s
                """,
                (cutoff, max(batch_threads, 1)),
            )
        except Exception as error:  # noqa: BLE001 - очистка не имеет права ронять цикл
            logger.warning("Список устаревших тредов чекпоинтов не получен: %s", error)
            return CheckpointPurgeReport(skipped_reason=f"чтение checkpoints: {error}")
        if not rows:
            return CheckpointPurgeReport()
        threads = [str(row["thread_id"]) for row in rows]
        removed_rows = 0
        for table in ("checkpoint_writes", "checkpoint_blobs"):
            if not await self._table_present(table):
                continue
            removed_rows += await self._execute(
                f"DELETE FROM {table} WHERE thread_id = ANY(%s)", (threads,)
            )
        removed_rows += await self._execute(
            "DELETE FROM checkpoints WHERE thread_id = ANY(%s)", (threads,)
        )
        logger.info(
            "Осиротевшие треды чекпоинтов сняты: %d (старше %s), строк: %d",
            len(threads),
            older_than,
            removed_rows,
        )
        return CheckpointPurgeReport(removed_threads=len(threads), removed_rows=removed_rows)


async def run_checkpoint_cleanup(
    state: DurableState,
    *,
    interval_seconds: float = CHECKPOINT_CLEANUP_INTERVAL_SECONDS,
    older_than: timedelta = CHECKPOINT_THREAD_TTL,
    batch_threads: int = CHECKPOINT_CLEANUP_BATCH_THREADS,
) -> None:
    """Цикл очистки чекпоинтов: задача для ``asyncio.create_task`` из lifespan.

    Спит сначала и только потом чистит: старт контейнера не должен начинаться с
    массового DELETE по живой базе, а первая волна приходит, когда сервис уже
    готов обслуживать запросы. Ошибка одной волны не убивает планировщик —
    иначе осиротевшие треды снова остались бы навсегда, и об очистке никто не
    узнал бы. ``CancelledError`` наружу: его ждёт ``lifespan`` при остановке.
    """
    while True:
        await asyncio.sleep(max(interval_seconds, 1.0))
        try:
            report = await state.purge_stale_checkpoint_threads(
                older_than=older_than, batch_threads=batch_threads
            )
            if report.skipped_reason is not None:
                logger.info("Очистка чекпоинтов пропущена: %s", report.skipped_reason)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - планировщик не роняет сервис
            logger.error("Очистка чекпоинтов не удалась, следующая волна по расписанию: %s", error)


def build_durable_state(settings: Settings) -> DurableState:
    """Выбор контура по ``accounts_backend`` — вместе с учётными записями.

    Конструируется без подключения: открытие пула и деградация — дело lifespan,
    там же, где и у ``build_accounts``.
    """
    if settings.accounts_backend == "postgres":
        return PostgresDurableState(settings)
    return InMemoryDurableState()
