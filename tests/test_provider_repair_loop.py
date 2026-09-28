"""Цикл schema-repair провайдера: битый вывод чинится, а не превращается в тихую подмену.

До этих тестов ``GigaChatProvider.complete_model`` не исполнялся ни в одном тесте —
проверялись только хелперы разбора ответа, то есть путь «модель вернула не-JSON →
repair-промпт → потолок попыток → явный отказ» оставался непроверенным.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field

from scientific_tangle.config import Settings
from scientific_tangle.services import provider as provider_module
from scientific_tangle.services.provider import GigaChatProvider, ModelUnavailableError


class Answer(BaseModel):
    summary: str = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)


class RecordingRegistry:
    """Заглушка реестра метрик: фиксирует учёт обращений к модели и repair-повторы."""

    def __init__(self) -> None:
        self.llm_calls: list[dict[str, Any]] = []
        self.retries: list[str] = []
        self.timeouts: list[str] = []

    def observe_llm(self, schema: str, duration_ms: float, **usage: Any) -> None:
        self.llm_calls.append({"schema": schema, "duration_ms": duration_ms, **usage})

    def observe_llm_retry(self, schema: str) -> None:
        self.retries.append(schema)

    def set_llm_capacity(self, slots: int) -> None:
        return None

    def observe_llm_queue_wait(self) -> None:
        return None

    def observe_llm_timeout(self, schema: str) -> None:
        self.timeouts.append(schema)


def _completion(
    content: str, *, finish: str = "stop", tokens: tuple[int, int] = (10, 5)
) -> SimpleNamespace:
    """Ответ формы SDK: ``usage`` суммируется по попыткам, ``choices`` — источник текста."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=tokens[0], completion_tokens=tokens[1]),
    )


def _provider(
    responses: list[Any], *, cache_ttl: float = 0.0
) -> tuple[GigaChatProvider, list[list[Any]]]:
    settings = Settings(
        _env_file=None,  # локальный .env не должен решать, что проверяет тест
        gigachat_api_key="test-key",
        llm_cache_ttl_seconds=cache_ttl,
    )
    subject = GigaChatProvider(settings, metrics=RecordingRegistry())  # type: ignore[arg-type]
    prompt_log: list[list[Any]] = []
    pending = list(responses)

    async def fake_request(messages):
        prompt_log.append(list(messages))
        outcome = pending.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    subject._request = fake_request  # type: ignore[method-assign]
    return subject, prompt_log


def _broken_runs(count: int) -> list[SimpleNamespace]:
    return [_completion("это не json") for _ in range(count)]


@pytest.mark.asyncio
async def test_malformed_json_is_repaired_and_second_attempt_returns_result() -> None:
    subject, prompts = _provider(
        [_completion("это не json"), _completion('{"summary": "готово", "finding_ids": ["f1"]}')]
    )
    result = await subject.complete_model("система", "вопрос", Answer)

    assert result.summary == "готово"
    assert len(prompts) == 2
    # Repair-запрос обязан сохранять пользовательский промпт: иначе модель чинит не тот ответ.
    assert "вопрос" in prompts[1][-1].content


@pytest.mark.asyncio
async def test_schema_violation_retries_and_succeeds() -> None:
    subject, prompts = _provider(
        [_completion('{"finding_ids": []}'), _completion('{"summary": "есть", "finding_ids": []}')]
    )
    result = await subject.complete_model("система", "вопрос", Answer)

    assert result.summary == "есть"
    assert len(prompts) == 2
    assert subject._metrics.retries == ["Answer"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_repair_attempts_are_capped_and_failure_is_explicit() -> None:
    attempts = provider_module._SCHEMA_REPAIR_ATTEMPTS
    subject, prompts = _provider(_broken_runs(attempts))

    with pytest.raises(ModelUnavailableError) as error:
        await subject.complete_model("система", "вопрос", Answer)

    assert len(prompts) == attempts
    assert "не является JSON" in str(error.value)


@pytest.mark.asyncio
async def test_truncation_at_max_tokens_is_named_as_the_cause() -> None:
    attempts = provider_module._SCHEMA_REPAIR_ATTEMPTS
    subject, _ = _provider([_completion('{"summary": "обрезано', finish="length")] * attempts)

    with pytest.raises(ModelUnavailableError) as error:
        await subject.complete_model("система", "вопрос", Answer)

    assert "max_tokens" in str(error.value)


@pytest.mark.asyncio
async def test_provider_error_payload_fails_without_retries() -> None:
    subject, prompts = _provider([_completion('{"error": "quota exceeded"}')])

    with pytest.raises(ModelUnavailableError, match="quota exceeded"):
        await subject.complete_model("система", "вопрос", Answer)

    assert len(prompts) == 1


@pytest.mark.asyncio
async def test_tokens_accumulate_across_repair_attempts() -> None:
    subject, _ = _provider(
        [
            _completion("не json", tokens=(100, 20)),
            _completion('{"summary": "ок", "finding_ids": []}', tokens=(120, 30)),
        ]
    )
    await subject.complete_model("система", "вопрос", Answer)

    usage = subject._metrics.llm_calls[-1]  # type: ignore[attr-defined]
    assert usage["prompt_tokens"] == 220
    assert usage["completion_tokens"] == 50
    assert usage["success"] is True


@pytest.mark.asyncio
async def test_cancellation_after_repair_attempt_still_accounts_spent_tokens() -> None:
    """Отмена посреди repair-цикла не обнуляет уже оплаченное обращение к модели."""
    subject, _ = _provider([_completion("не json", tokens=(70, 7))])
    seen: list[int] = []

    async def cancel_after_first(messages):
        seen.append(1)
        if len(seen) == 1:
            return _completion("не json", tokens=(70, 7))
        raise asyncio.CancelledError

    subject._request = cancel_after_first  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await subject.complete_model("система", "вопрос", Answer)

    calls = subject._metrics.llm_calls  # type: ignore[attr-defined]
    assert len(seen) == 2, "отмена случилась на второй попытке, после оплаченной первой"
    assert len(calls) == 1, "учёт обязан случиться ровно один раз на вызов complete_model"
    assert calls[0]["success"] is False
    assert calls[0]["prompt_tokens"] == 70


@pytest.mark.asyncio
async def test_failed_call_is_not_cached() -> None:
    attempts = provider_module._SCHEMA_REPAIR_ATTEMPTS
    subject, prompts = _provider(_broken_runs(attempts * 2), cache_ttl=900.0)
    provider_module.invalidate_llm_cache()

    for _ in range(2):
        with pytest.raises(ModelUnavailableError):
            await subject.complete_model("система", "вопрос", Answer)

    # Оба прогона прошли в модель: кэш копил бы отказ и отдавал бы его повторно.
    assert len(prompts) == attempts * 2


@pytest.mark.asyncio
async def test_identical_call_is_served_from_cache_without_second_request() -> None:
    subject, prompts = _provider(
        [_completion('{"summary": "раз", "finding_ids": []}')] * 2, cache_ttl=900.0
    )
    provider_module.invalidate_llm_cache()

    first = await subject.complete_model("система", "вопрос", Answer)
    second = await subject.complete_model("система", "вопрос", Answer)

    assert first.summary == second.summary == "раз"
    assert len(prompts) == 1, "повтор identical-промпта не должен идти в модель"
    assert second is not first, "кэш обязан отдавать новый экземпляр, а не общую ссылку"


@pytest.mark.asyncio
async def test_transport_timeout_is_counted_as_timeout_not_garbage_answer() -> None:
    """Таймаут транспорта обязано видно отдельным счётчиком: он лечится не repair-промптом."""
    subject, prompts = _provider([httpx.TimeoutException("read timeout")])

    with pytest.raises(ModelUnavailableError, match="timeout"):
        await subject.complete_model("система", "вопрос", Answer)

    assert len(prompts) == 1, "таймаут не должен запускать repair-цикл"
    assert subject._metrics.timeouts == ["Answer"]  # type: ignore[attr-defined]
    assert subject._metrics.llm_calls[-1]["success"] is False  # type: ignore[attr-defined]
