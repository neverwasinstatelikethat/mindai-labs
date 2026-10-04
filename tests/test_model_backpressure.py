"""Занятость модели — повторимый отказ: 429 с ``Retry-After``, а не 503.

Проверки держат границу, на которой сервис различает «модела нет» и «модель
вся занята»: во втором случае серверное ожидание уже потрачено, и следующий
шаг — сказать аналитику, когда спросить снова, а не жаловаться на 503.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.responses import JSONResponse

from scientific_tangle.api.app import model_busy, model_unavailable
from scientific_tangle.config import Settings
from scientific_tangle.services.provider import (
    GigaChatProvider,
    ModelBusyError,
    ModelUnavailableError,
)


def _provider(*, wait_seconds: float = 1.0) -> GigaChatProvider:
    return GigaChatProvider(
        Settings(
            gigachat_api_key="test-key",
            gigachat_max_concurrent=1,
            gigachat_queue_wait_seconds=wait_seconds,
        )
    )


def test_busy_model_is_a_distinct_kind_of_unavailability():
    # Наследование сохраняет честную деградацию внутри рабочего процесса:
    # узел, поймавший занятость модели, отдаёт неполный ответ, а не теряет находки.
    assert issubclass(ModelBusyError, ModelUnavailableError)


def test_exhausted_slot_wait_raises_busy_with_a_retry_hint():
    provider = _provider()
    # Счётчик ожидания ведёт `_request`: он знает, что слот на момент обращения
    # занят. `_acquire_slot` только передаёт это число в отказ.
    provider._waiting = 2

    async def scenario() -> ModelBusyError:
        await provider._semaphore.acquire()
        try:
            with pytest.raises(ModelBusyError) as captured:
                await provider._acquire_slot()
            return captured.value
        finally:
            provider._semaphore.release()

    error = asyncio.run(scenario())

    assert error.retry_after == 1
    assert error.limit == 1
    assert error.waiting == 2
    assert "сервис занят" in str(error)


def test_free_slot_is_acquired_without_an_error():
    provider = _provider()

    async def scenario() -> None:
        await provider._acquire_slot()
        provider._semaphore.release()

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_busy_refusal_answers_429_with_the_admission_shape():
    error = ModelBusyError(
        "GigaChat: сервис занят — все слоты модели заняты",
        retry_after=60,
        active=1,
        waiting=2,
        limit=1,
    )

    response = await model_busy(None, error)  # type: ignore[arg-type]

    assert isinstance(response, JSONResponse)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    body = json.loads(response.body)
    assert body["active"] == 1
    assert body["limit"] == 1
    assert body["retry_after"] == 60


@pytest.mark.asyncio
async def test_real_unavailability_still_answers_503():
    response = await model_unavailable(None, ModelUnavailableError("модель не настроена"))

    assert response.status_code == 503
