"""Кэш structured output и учёт отменённого обращения (F5).

Один исследовательский запрос делает ~6-10 structured-output обращений, и
повторные вопросы (а также ветки JSON и demo) дают побуквенно тот же промпт.
Кэш в `services/provider.py` обязан: попадать только по полному ключу
(модель, схема, system, user), никогда не отдавать разделённый Pydantic-экземпляр,
не кэшировать ошибки и промежуточные schema-repair ответы, честно считаться в
метриках попаданий/промахов — а при отмене запроса закрывать учёт токенов ровно
один раз в `finally`. Сети здесь нет: обращение к модели — scripted-заглушка в
стиле `tests/fakes.py`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from gigachat.exceptions import GigaChatException
from pydantic import BaseModel

from scientific_tangle.config import Settings
from scientific_tangle.services import provider as provider_module
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry
from scientific_tangle.services.provider import (
    GigaChatProvider,
    ModelUnavailableError,
    invalidate_llm_cache,
)


class Answer(BaseModel):
    text: str


class OtherAnswer(BaseModel):
    note: str


class _FakeMetrics:
    """Тот же контракт, что у `AgentMetricsRegistry`, плюс два новых наблюдателя.

    `observe_llm_cache`/`observe_llm_cancelled` добавляет другой агент — тесты
    фиксируют, что провайдер дёргает их защитно и переживает их отсутствие.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.retries: list[str] = []
        self.cache_reports: list[bool] = []
        self.cancelled: list[str] = []
        self.queue_waits = 0
        self.capacity: int | None = None

    def set_llm_capacity(self, limit: int) -> None:
        self.capacity = limit

    def observe_llm_queue(self, *, in_flight: int, waiting: int, limit: int) -> None:
        return None

    def observe_llm_queue_wait(self) -> None:
        self.queue_waits += 1

    def observe_llm_retry(self, schema: str) -> None:
        self.retries.append(schema)

    def observe_llm(
        self,
        schema: str,
        duration_ms: float,
        *,
        success: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        self.calls.append(
            {
                "schema": schema,
                "success": success,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )

    def observe_llm_cache(self, hit: bool) -> None:
        self.cache_reports.append(hit)

    def observe_llm_cancelled(self, schema_name: str) -> None:
        self.cancelled.append(schema_name)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "knowledge_backend": "memory",
        "gigachat_api_key": "test-key",
        "llm_cache_ttl_seconds": 900.0,
        "llm_cache_max_entries": 8,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _completion(content: str, *, prompt: int = 0, completion: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def _echo_request(seen: list[str]):
    """Возвращает ответ, зависящий от user-промпта: так видно подмену ключа.

    Payload валиден и для `Answer`, и для `OtherAnswer` — тест чувствительности
    ключа различает записи по схеме, а не по удачному совпадению валидации.
    """

    async def _request(
        messages: Any, *, max_tokens: int | None = None, model: str | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> SimpleNamespace:
        user = messages[-1].content
        seen.append(user)
        return _completion(json.dumps({"text": user, "note": user}), prompt=3, completion=7)

    return _request


def _provider(settings: Settings, metrics: Any, request: Any) -> GigaChatProvider:
    provider = GigaChatProvider(settings, metrics)  # type: ignore[arg-type]
    provider._request = request
    return provider


@pytest.fixture(autouse=True)
def _clean_llm_cache() -> Any:
    invalidate_llm_cache()
    yield
    invalidate_llm_cache()


@pytest.mark.asyncio
async def test_identical_prompt_is_served_from_cache_once() -> None:
    """Второй побуквенно идентичный запрос не платится модели и не считается вызовом."""
    seen: list[str] = []
    metrics = _FakeMetrics()
    provider = _provider(_settings(), metrics, _echo_request(seen))

    first = await provider.complete_model("система", "вопрос про медь", Answer)
    second = await provider.complete_model("система", "вопрос про медь", Answer)

    assert len(seen) == 1
    assert first == second
    # Значение в кэше — JSON, экземпляр собирается заново: объекты не разделяются.
    assert first is not second
    assert metrics.cache_reports == [False, True]
    # Попадание не добавляет обращений к модели в учёт LLM-вызовов.
    assert len(metrics.calls) == 1
    assert metrics.calls[0] == {
        "schema": "Answer",
        "success": True,
        "prompt_tokens": 3,
        "completion_tokens": 7,
    }


@pytest.mark.asyncio
async def test_cache_survives_provider_instances() -> None:
    """Кэш уровня процесса: ветки JSON и demo с отдельными провайдерами общие."""
    seen: list[str] = []
    json_route = _provider(_settings(), _FakeMetrics(), _echo_request(seen))
    demo_route = _provider(_settings(), _FakeMetrics(), _echo_request(seen))

    await json_route.complete_model("система", "один и тот же вопрос", Answer)
    await demo_route.complete_model("система", "один и тот же вопрос", Answer)

    assert len(seen) == 1


@pytest.mark.asyncio
async def test_disabled_cache_behaves_as_before() -> None:
    """llm_cache_ttl_seconds=0 — кэш выключен полностью, включая метрики кэша."""
    seen: list[str] = []
    metrics = _FakeMetrics()
    provider = _provider(
        _settings(llm_cache_ttl_seconds=0), metrics, _echo_request(seen)
    )

    await provider.complete_model("система", "вопрос", Answer)
    await provider.complete_model("система", "вопрос", Answer)

    assert len(seen) == 2
    assert metrics.cache_reports == []
    assert len(metrics.calls) == 2


@pytest.mark.asyncio
async def test_ttl_expiry_turns_hit_back_into_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    metrics = _FakeMetrics()
    provider = _provider(_settings(llm_cache_ttl_seconds=60), metrics, _echo_request(seen))
    clock = [1000.0]
    monkeypatch.setattr(provider_module, "monotonic", lambda: clock[0])

    await provider.complete_model("система", "вопрос", Answer)
    clock[0] += 30.0
    await provider.complete_model("система", "вопрос", Answer)
    clock[0] += 31.0
    await provider.complete_model("система", "вопрос", Answer)

    assert len(seen) == 2
    assert metrics.cache_reports == [False, True, False]


@pytest.mark.asyncio
async def test_lru_bound_evicts_the_least_recently_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    provider = _provider(_settings(llm_cache_max_entries=8), _FakeMetrics(), _echo_request(seen))
    clock = [0.0]
    monkeypatch.setattr(provider_module, "monotonic", lambda: clock[0])

    for index in range(8):
        await provider.complete_model("система", f"вопрос {index}", Answer)
    assert len(seen) == 8

    # Обращение к «вопрос 0» освежает его: теперь давнее — «вопрос 1».
    await provider.complete_model("система", "вопрос 0", Answer)
    assert len(seen) == 8
    await provider.complete_model("система", "вопрос 8", Answer)
    assert len(seen) == 9

    await provider.complete_model("система", "вопрос 0", Answer)
    assert len(seen) == 9  # жив
    await provider.complete_model("система", "вопрос 1", Answer)
    assert len(seen) == 10  # вытеснен как самый давний
    assert len(provider_module._llm_cache) == 8


@pytest.mark.asyncio
async def test_key_is_sensitive_to_schema_prompt_and_model() -> None:
    """Всё, что меняет ответ, меняет и запись: ни один из четырёх компонентов не лишний."""
    seen: list[str] = []
    provider = _provider(_settings(), _FakeMetrics(), _echo_request(seen))

    await provider.complete_model("система", "вопрос", Answer)
    # Та же prompt-пара, другая схема — другой ключ (у схем разные экземпляры).
    await provider.complete_model("система", "вопрос", OtherAnswer)
    # Смена user и system по отдельности.
    await provider.complete_model("система", "другой вопрос", Answer)
    await provider.complete_model("другая система", "вопрос", Answer)
    assert len(seen) == 4

    # Смена модели — ответ провайдера другой, ключ обязан это заметить.
    provider._settings.gigachat_agent_model = "GigaChat-Max"
    await provider.complete_model("система", "вопрос", Answer)
    assert len(seen) == 5


@pytest.mark.asyncio
async def test_invalidate_drops_every_entry() -> None:
    seen: list[str] = []
    provider = _provider(_settings(), _FakeMetrics(), _echo_request(seen))

    await provider.complete_model("система", "вопрос", Answer)
    invalidate_llm_cache()
    await provider.complete_model("система", "вопрос", Answer)

    assert len(seen) == 2


@pytest.mark.asyncio
async def test_transport_error_is_not_cached() -> None:
    seen: list[str] = []
    metrics = _FakeMetrics()
    provider = _provider(_settings(), metrics, _echo_request(seen))
    calls = iter([GigaChatException("перегрузка"), None])

    original = provider._request

    async def _flaky(
        messages: Any, *, max_tokens: int | None = None, model: str | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> Any:
        error = next(calls)
        if error is not None:
            raise error
        return await original(messages, max_tokens=max_tokens, model=model,
                              response_schema=response_schema)

    provider._request = _flaky

    with pytest.raises(ModelUnavailableError, match="GigaChat error"):
        await provider.complete_model("система", "вопрос", Answer)
    result = await provider.complete_model("система", "вопрос", Answer)

    assert result.text == "вопрос"
    assert metrics.cache_reports == [False, False]
    assert len(metrics.calls) == 2
    assert [call["success"] for call in metrics.calls] == [False, True]


@pytest.mark.asyncio
async def test_provider_error_payload_is_not_cached() -> None:
    """Ответ-ошибка провайдера (`{"error": ...}`) — отказ, а не контент для кэша."""
    async def _error(
        messages: Any, *, max_tokens: int | None = None, model: str | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> SimpleNamespace:
        return _completion(json.dumps({"error": "quota exceeded"}), prompt=2, completion=1)

    metrics = _FakeMetrics()
    provider = _provider(_settings(), metrics, _error)

    with pytest.raises(ModelUnavailableError, match="вернул ошибку"):
        await provider.complete_model("система", "вопрос", Answer)
    with pytest.raises(ModelUnavailableError, match="вернул ошибку"):
        await provider.complete_model("система", "вопрос", Answer)

    assert len(provider_module._llm_cache) == 0
    # Каждый отказ учтён ровно один раз — и с уже потраченными токенами.
    assert len(metrics.calls) == 2
    assert all(call["success"] is False for call in metrics.calls)
    assert metrics.calls[0]["prompt_tokens"] == 2


@pytest.mark.asyncio
async def test_unrepairable_response_is_not_cached() -> None:
    """Схема не пройдена после всех repair-попыток — записи в кэше нет."""
    async def _garbage(
        messages: Any, *, max_tokens: int | None = None, model: str | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> SimpleNamespace:
        return _completion(json.dumps({"нет такого поля": True}), prompt=4, completion=2)

    metrics = _FakeMetrics()
    provider = _provider(_settings(), metrics, _garbage)

    with pytest.raises(ModelUnavailableError, match="не соответствует Answer"):
        await provider.complete_model("система", "вопрос", Answer)

    assert len(provider_module._llm_cache) == 0
    # Ровно один учёт на вызов, но со всеми тремя оплаченными попытками.
    assert len(metrics.calls) == 1
    assert metrics.calls[0]["success"] is False
    assert metrics.calls[0]["prompt_tokens"] == 12
    assert metrics.calls[0]["completion_tokens"] == 6
    assert metrics.retries == ["Answer", "Answer"]


@pytest.mark.asyncio
async def test_schema_repair_accumulates_tokens_and_caches_only_the_final_answer() -> None:
    """Промежуточный битый ответ не кэшируется, но его токены учтены в сумме."""
    responses = iter(
        [
            _completion(json.dumps({"нет поля": 1}), prompt=10, completion=5),
            _completion(json.dumps({"text": "готово"}), prompt=11, completion=6),
        ]
    )
    seen: list[str] = []

    async def _scripted(
        messages: Any, *, max_tokens: int | None = None, model: str | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> SimpleNamespace:
        seen.append(messages[-1].content)
        return next(responses)

    metrics = _FakeMetrics()
    provider = _provider(_settings(), metrics, _scripted)

    first = await provider.complete_model("система", "вопрос", Answer)
    assert first.text == "готово"
    assert len(seen) == 2
    assert metrics.retries == ["Answer"]
    # Успех — один учёт с токенами обеих попыток (иначе расход занижен).
    assert metrics.calls == [
        {
            "schema": "Answer",
            "success": True,
            "prompt_tokens": 21,
            "completion_tokens": 11,
        }
    ]

    second = await provider.complete_model("система", "вопрос", Answer)
    assert second.text == "готово"
    assert len(seen) == 2  # с моделью больше не разговаривали


@pytest.mark.asyncio
async def test_cancelled_call_closes_accounting_once() -> None:
    """F5: отмена посреди обращения не обнуляет уже потраченные токены.

    Клиент закрыл вкладку во второй попытке: учёт закрывается один раз в
    `finally` с накопленными за repair-попытку токенами, отдельный наблюдатель
    отмены вызван, CancelledError передан наружу как есть — без конвертации в
    ModelUnavailableError и без записи в кэш.
    """
    responses = iter(
        [
            _completion(json.dumps({"битый": "json"}), prompt=10, completion=5),
        ]
    )

    async def _cancel_midway(
        messages: Any, *, max_tokens: int | None = None, model: str | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> SimpleNamespace:
        item = next(responses, None)
        if item is None:
            raise asyncio.CancelledError()
        return item

    metrics = _FakeMetrics()
    provider = _provider(_settings(), metrics, _cancel_midway)

    with pytest.raises(asyncio.CancelledError):
        await provider.complete_model("система", "вопрос", Answer)

    assert metrics.calls == [
        {
            "schema": "Answer",
            "success": False,
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
    ]
    assert metrics.cancelled == ["Answer"]
    assert len(provider_module._llm_cache) == 0


class _LegacyMetrics(_FakeMetrics):
    """Метрики без новых наблюдателей — так реестр выглядит до выкатки агента метрик."""

    def __getattribute__(self, name: str) -> Any:
        if name in {"observe_llm_cache", "observe_llm_cancelled"}:
            raise AttributeError(name)
        return super().__getattribute__(name)


@pytest.mark.asyncio
async def test_provider_works_without_the_new_metric_hooks() -> None:
    """Методы кэша/отмены добавляет другой агент: до его выкатки провайдер жив.

    Защитный getattr обязан проглотить отсутствие методов, а не уронить
    `complete_model` — иначе порядок слияния двух задач превращается в аварию.
    """
    seen: list[str] = []
    metrics = _LegacyMetrics()
    provider = _provider(_settings(), metrics, _echo_request(seen))

    first = await provider.complete_model("система", "вопрос", Answer)
    second = await provider.complete_model("система", "вопрос", Answer)

    assert first == second
    assert len(seen) == 1
    assert len(metrics.calls) == 1


@pytest.mark.asyncio
async def test_complete_model_against_the_real_registry() -> None:
    """Смоук на живом `AgentMetricsRegistry`: есть методы или нет — без разницы."""
    seen: list[str] = []
    provider = GigaChatProvider(_settings(), AgentMetricsRegistry())
    provider._request = _echo_request(seen)

    result = await provider.complete_model("система", "вопрос", Answer)

    assert result.text == "вопрос"
    snapshot = provider._metrics.snapshot()
    assert any(entry.schema_name == "Answer" for entry in snapshot.llm)
