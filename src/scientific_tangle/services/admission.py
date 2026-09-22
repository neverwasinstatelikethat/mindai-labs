"""Приём агентных прогонов: слот либо отказ, но не очередь внутри чужого дедлайна.

Один запрос к агенту — это ~8 обращений к модели (planning, controller,
action_planner, controller, reasoner, critic, improver, critic), а семафор
провайдера сериализует их в одном процессе. Без учёта на входе второй
пользователь ждал освобождения модели *внутри* своего
``AGENT_DEADLINE_SECONDS`` и получал неполный ответ с ``degradation_reasons``
вместо честного «сервис занят». Здесь решение принимается до запуска графа:
свободных слотов нет — ``429`` + ``Retry-After``, ожидание в очереди отсутствует,
поэтому дедлайн тратится только на собственное исследование.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from scientific_tangle.config import Settings
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry

logger = logging.getLogger(__name__)

MIN_RETRY_AFTER_SECONDS = 5


class AdmissionRefusedError(RuntimeError):
    """Свободных слотов нет: обработчик обязан отдать 429, а не ждать."""

    def __init__(self, retry_after: int, *, active: int, limit: int) -> None:
        super().__init__(f"Все {limit} слотов агентного контура заняты")
        self.retry_after = retry_after
        self.active = active
        self.limit = limit


@dataclass(slots=True)
class AdmissionHandle:
    """Арендованный слот; освобождается ровно один раз — в ``release``.

    Идемпотентность не роскошь: SSE-поток отдаёт слот в ``finally`` генератора и
    дополнительно в background-задаче ответа, а JSON-путь — в ``finally``
    обработчика. Любой из вызовов обязан быть последним, но не двойным.
    """

    token: int
    _on_release: Callable[[], None] | None = field(default=None, repr=False)
    released: bool = field(default=False, repr=False)

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self._on_release is not None:
            self._on_release()


class AgentRunAdmission:
    """Счётчик активных прогонов с жёстким потолком (без ожидания).

    Работает синхронно в пределах event loop: между проверкой и инкрементом нет
    ``await``, поэтому двух прогонов в один слот не пускает даже при взрывном
    притоке. Для нескольких воркеров ``uvicorn`` потолок применяется к каждому
    процессу отдельно — как и счётчик троттлинга входа.
    """

    def __init__(
        self,
        *,
        limit: int,
        deadline_seconds: float,
        metrics: AgentMetricsRegistry | None = None,
    ) -> None:
        self._limit = max(limit, 1)
        # Оценка «когда освобождаться»: при полном наборе слотов новый прогон
        # получит шанс примерно через дедлайн / число слотов.
        self._retry_after = max(
            MIN_RETRY_AFTER_SECONDS, math.ceil(deadline_seconds / self._limit)
        )
        self._active = 0
        self._refused = 0
        self._metrics = metrics
        if metrics is not None:
            metrics.set_agent_capacity(self._limit)

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    @property
    def refused(self) -> int:
        return self._refused

    def acquire(self) -> AdmissionHandle:
        """Берает слот без ожидания: свободных нет — сразу ``AdmissionRefusedError``.

        Между проверкой потолка и инкрементом нет ``await``, поэтому два прогона
        не занимают один слот даже при взрывном притоке в одном event loop.
        """
        if self._active >= self._limit:
            self._refused += 1
            if self._metrics is not None:
                self._metrics.observe_admission_refusal()
            logger.warning(
                "Агентный контур перегружен: %d/%d слотов занято, отказ",
                self._active,
                self._limit,
            )
            raise AdmissionRefusedError(
                self._retry_after, active=self._active, limit=self._limit
            )
        self._active += 1
        if self._metrics is not None:
            self._metrics.observe_agent_slots(self._active, self._limit)
        return AdmissionHandle(token=self._active, _on_release=self._decrement)

    def _decrement(self) -> None:
        if self._active <= 0:
            return
        self._active -= 1
        if self._metrics is not None:
            self._metrics.observe_agent_slots(self._active, self._limit)


def agent_run_limit(settings: Settings) -> int:
    """Потолок одновременных исследований из ``Settings``.

    Потолок держат ниже пропускной способности модели: 4 параллельных
    исследования при одном LLM-слоте — это ~32 ожидающих обращения, дальше
    растёт только очередь. Значение валидирует ``config.py`` (``ge=1``).
    """
    return settings.agent_max_concurrent_runs
