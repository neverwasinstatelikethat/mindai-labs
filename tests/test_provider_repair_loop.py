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
from pydantic import BaseModel, Field, ValidationError

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    AgentActionPlan,
    CritiqueResult,
    ReasoningResult,
)
from scientific_tangle.services import provider as provider_module
from scientific_tangle.services.provider import GigaChatProvider, ModelUnavailableError


class Answer(BaseModel):
    summary: str = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)


class RecordingRegistry:
    """Заглушка реестра метрик: фиксирует учёт обращений к модели и repair-повторы.

    ``observe_llm`` собирает свободные ``**usage`` — так же, как это сделает
    реестр после того, как зона метрик добавит параметр ``cancelled``.
    """

    def __init__(self) -> None:
        self.llm_calls: list[dict[str, Any]] = []
        self.retries: list[str] = []
        self.timeouts: list[str] = []
        self.cancelled: list[str] = []

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

    def observe_llm_cancelled(self, schema_name: str) -> None:
        self.cancelled.append(schema_name)


class StrictRegistry(RecordingRegistry):
    """Реестр без ``**kwargs`` и без ``cancelled``: так он выглядит до правки зоны метрик.

    Новый параметр в ``finally`` провайдера не должен ронять учёт TypeError
    поверх уже оплаченного обращения к модели.
    """

    def observe_llm(  # type: ignore[override]
        self,
        schema: str,
        duration_ms: float,
        *,
        success: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        self.llm_calls.append(
            {
                "schema": schema,
                "duration_ms": duration_ms,
                "success": success,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )


def _completion(
    content: str, *, finish: str = "stop", tokens: tuple[int, int] = (10, 5)
) -> SimpleNamespace:
    """Ответ формы SDK: ``usage`` суммируется по попыткам, ``choices`` — источник текста."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=tokens[0], completion_tokens=tokens[1]),
    )


def _provider(
    responses: list[Any],
    *,
    cache_ttl: float = 0.0,
    metrics: Any | None = None,
    max_output_tokens: int | None = None,
) -> tuple[GigaChatProvider, list[list[Any]]]:
    overrides: dict[str, Any] = {}
    if max_output_tokens is not None:
        overrides["gigachat_max_output_tokens"] = max_output_tokens
    settings = Settings(
        _env_file=None,  # локальный .env не должен решать, что проверяет тест
        gigachat_api_key="test-key",
        llm_cache_ttl_seconds=cache_ttl,
        **overrides,
    )
    subject = GigaChatProvider(settings, metrics=metrics or RecordingRegistry())
    prompt_log: list[list[Any]] = []
    limits: list[int | None] = []
    subject.request_limits = limits  # type: ignore[attr-defined]
    pending = list(responses)

    async def fake_request(messages: Any, *, max_tokens: int | None = None) -> Any:
        prompt_log.append(list(messages))
        limits.append(max_tokens)
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
async def test_truncated_output_retries_with_a_bigger_limit_and_no_dead_weight() -> None:
    """Обрыв по длине чинится бюджетом вывода, а не дописыванием оборванного хвоста.

    Прежний повтор слал ТОТ ЖЕ промпт плюс `previous[:4000]`: вход рос, лимит
    вывода оставался тем же, и третья оплаченная попытка давала 503 гарантированно.
    """
    subject, prompts = _provider(
        [
            _completion('{"summary": "обрезано', finish="length"),
            _completion('{"summary": "обрезано', finish="length"),
            _completion('{"summary": "было бы третьим", "finding_ids": []}'),
        ]
    )

    with pytest.raises(ModelUnavailableError) as error:
        await subject.complete_model("система", "вопрос про медь", Answer)

    limits = subject.request_limits  # type: ignore[attr-defined]
    assert limits == [None, provider_module._OUTPUT_TOKENS_CEILING]
    assert "max_tokens" in str(error.value)
    # Третья попытка не оплачивается: контракт деградации честнее второго платёжа.
    assert len(prompts) == 2
    # Оборванный хвост в промпт не попадает — он и есть причина повтора.
    assert "обрезано" not in prompts[1][-1].content
    assert "вопрос про медь" in prompts[1][-1].content


@pytest.mark.asyncio
async def test_truncation_at_the_ceiling_is_not_retried_at_all() -> None:
    """Лимит уже на потолке: повтор с тем же бюджетом — тот же обрыв за новую плату."""
    subject, prompts = _provider(
        [_completion('{"summary": "обрыв', finish="length")],
        max_output_tokens=provider_module._OUTPUT_TOKENS_CEILING,
    )

    with pytest.raises(ModelUnavailableError, match="лимите вывода"):
        await subject.complete_model("система", "вопрос", Answer)

    assert len(prompts) == 1
    assert subject._metrics.retries == []  # type: ignore[attr-defined]


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
    """Отмена посреди repair-цикла не обнуляет уже оплаченное обращение к модели.

    Токены закрываются ровно одним `observe_llm`, но с признанием отмены:
    `status="failure"` в Prometheus смешивал отказы модели с закрытой вкладкой
    пользователя, и доля отказов зависела бы от поведения клиента.
    """
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
    assert calls[0]["cancelled"] is True, "отмена различима в учёте, а не числится отказом"
    assert subject._metrics.cancelled == ["Answer"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cancellation_is_accounted_without_the_new_registry_parameter() -> None:
    """Реестр без параметра `cancelled` (зона метрик правится параллельно) не падает.

    Возможность передавать новый аргумент провайдер узнаёт по сигнатуре:
    TypeError в `finally` означал бы потерю уже потраченных токенов.
    """
    registry = StrictRegistry()
    subject, _ = _provider([_completion("не json", tokens=(70, 7))], metrics=registry)

    async def cancel_at_once(messages):
        raise asyncio.CancelledError

    subject._request = cancel_at_once  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await subject.complete_model("система", "вопрос", Answer)

    assert len(registry.llm_calls) == 1, "учёт закрывается один раз и без TypeError"
    assert registry.llm_calls[0]["success"] is False
    assert registry.llm_calls[0]["prompt_tokens"] == 0
    assert "cancelled" not in registry.llm_calls[0]
    assert registry.cancelled == ["Answer"]


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


@pytest.mark.asyncio
async def test_reasoning_without_lists_is_not_a_schema_violation() -> None:
    """Ответ «противоречий не нашёл» не имеет права терять прогон.

    Требовательные списки в ``ReasoningResult`` роняли валидацию иначе полного
    ответа: модель возвращала ``null`` или не перечисляла ключ, прогон уходил в
    repair и останавливался на потолке — холодный ``GET /demo`` терял ответ
    целиком (находка ревью бэкенда, 2026-09-28).
    """
    subject, prompts = _provider(
        [
            _completion('{"summary": "Плотность 1,8 г/см³.", "conflicts": null}'),
        ]
    )

    result = await subject.complete_model("система", "вопрос", ReasoningResult)

    assert len(prompts) == 1, "полный ответ не должен попадать в repair-цикл"
    assert result.summary == "Плотность 1,8 г/см³."
    assert (result.finding_ids, result.conflicts, result.knowledge_gaps) == ([], [], [])


@pytest.mark.asyncio
async def test_critique_without_issues_keeps_its_verdict_and_needs_no_repair() -> None:
    """Отсутствие замечаний — это «одобрено без правок», а вердикт остаётся обязательным."""
    subject, prompts = _provider([_completion('{"approved": true, "issues": null}')])

    result = await subject.complete_model("система", "вопрос", CritiqueResult)

    assert len(prompts) == 1
    assert (result.approved, result.issues, result.revision_instructions) == (True, [], [])
    with pytest.raises(ValidationError, match="approved"):
        CritiqueResult.model_validate({"issues": []})


@pytest.mark.asyncio
async def test_sections_returned_as_strings_need_no_repair() -> None:
    """Дословный ответ из журнала контейнера: `"conflicts": "противоречий нет"`.

    Ровно этот ответ давал `ReasoningResult не пройден (conflicts: Input should be
    a valid list; …)` и 503 на холодном `GET /api/v1/demo` — приводится на уровне
    схемы, поэтому до repair-промпта и второй оплаты дело не доходит.
    """
    subject, prompts = _provider(
        [
            _completion(
                '{"summary": "Плотность 1,8 г/см³.", "finding_ids": [], '
                '"conflicts": "противоречий нет", "knowledge_gaps": "нет", '
                '"recommendations": "-"}'
            )
        ]
    )

    result = await subject.complete_model("система", "вопрос", ReasoningResult)

    assert len(prompts) == 1, "полный по смыслу ответ не чинится повтором"
    assert subject._metrics.retries == []  # type: ignore[attr-defined]
    assert (result.conflicts, result.knowledge_gaps, result.recommendations) == (
        ["противоречий нет"],
        ["нет"],
        ["-"],
    )


@pytest.mark.asyncio
async def test_plan_without_actions_is_repaired_once_not_refused() -> None:
    """`actions: "[]"` — пустой план (рабочий исход), а не ValidationError прогона."""
    subject, prompts = _provider(
        [_completion('{"rationale": "данных достаточно", "actions": "[]"}')]
    )

    plan = await subject.complete_model("система", "вопрос", AgentActionPlan)

    assert len(prompts) == 1
    assert plan.actions == []
    assert plan.dropped_action_ids() == set()


@pytest.mark.asyncio
async def test_cache_key_carries_the_schema_shape_not_only_its_name() -> None:
    """Одинаковое `__name__` — не одинаковая схема: форма обязана быть в ключе.

    pydantic с `extra="ignore"` спокойно переводалидирует payload широкой схемы
    узкой, поэтому закэшированный ответ одного контракта уезжал бы другому с тем
    же именем класса.
    """
    wide = type("Answer", (BaseModel,), {"__annotations__": {"text": str, "note": str}})
    narrow = type("Answer", (BaseModel,), {"__annotations__": {"text": str}})
    assert wide.__name__ == narrow.__name__ == "Answer"

    subject, prompts = _provider(
        [
            _completion('{"text": "раз", "note": "два"}'),
            _completion('{"text": "три"}'),
        ],
        cache_ttl=900.0,
    )
    provider_module.invalidate_llm_cache()

    first = await subject.complete_model("система", "вопрос", wide)  # type: ignore[arg-type]
    second = await subject.complete_model("система", "вопрос", narrow)  # type: ignore[arg-type]

    assert len(prompts) == 2, "промах обязан идти в модель, а не отдавать чужую форму"
    assert (first.text, first.note) == ("раз", "два")
    assert second.text == "три" and not hasattr(second, "note")

    third = await subject.complete_model("система", "вопрос", narrow)  # type: ignore[arg-type]
    assert len(prompts) == 2, "свой же ответ отдаётся из кэша: TTL/LRU не сломаны"
    assert third.text == "три"


@pytest.mark.asyncio
async def test_absent_section_survives_a_cache_hit() -> None:
    """Попадание в кэш не превращает «секция не вернулась» в «секция пуста».

    Иначе один и тот же ответ давал бы разные `degradation_reasons` в зависимости
    от того, платили модели или сняли запись с кэша.
    """
    subject, prompts = _provider([_completion('{"summary": "s"}')] * 2, cache_ttl=900.0)
    provider_module.invalidate_llm_cache()

    first = await subject.complete_model("система", "вопрос", ReasoningResult)
    second = await subject.complete_model("система", "вопрос", ReasoningResult)

    assert len(prompts) == 1
    assert first.absent_list_sections() == second.absent_list_sections()
    assert "conflicts" in second.absent_list_sections()
