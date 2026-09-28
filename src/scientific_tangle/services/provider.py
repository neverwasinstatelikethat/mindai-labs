from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Sequence
from functools import lru_cache
from time import monotonic, perf_counter
from typing import Any, Protocol, TypeVar

import httpx
from gigachat import GigaChat
from gigachat.exceptions import (
    AuthenticationError,
    ForbiddenError,
    GigaChatException,
    RateLimitError,
    ServerError,
)
from gigachat.models import Chat, ChatCompletion, Messages, MessagesRole
from pydantic import BaseModel, ValidationError

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import ModelMode
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry, agent_metrics

logger = logging.getLogger(__name__)

StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)

# Ровно одна попытка schema-repair поверх retry-политики SDK перестала быть
# достаточной: на живом GigaChat структурированный ответ портится и после правки
# (то отсутствующий верхний ключ, то оборванный JSON при finish=stop). Вторая
# правка остаётся внутри агентного дедлайна: узел обращения к модели ~20 с,
# transport-повторы при этом не перемножаются — они считаются внутри SDK.
_SCHEMA_REPAIR_ATTEMPTS = 3


class ModelUnavailableError(RuntimeError):
    """LLM недоступен или вернул не-экземпляр схемы после всех попыток."""


# ── Кэш structured output (в пределах процесса) ─────────────────────────────────
#
# Один исследовательский запрос делает ~6-10 обращений к модели, а повторные
# вопросы и ветки JSON и demo нередко просят ровно то же самое: без кэша каждый
# такой повтор заново оплачивается у провайдера. Кэш явный, небольшой и
# ограничен по числу записей (LRU) и по времени жизни (TTL).
#
# Почему совместный кэш безопасен на мультипользовательском контуре:
# 1. Ключ — sha256 от (id модели, имя схемы, полный системный промпт, полный
#    пользовательский промпт). Права пользователя (ACL) и история ветки попадают
#    в ТЕКСТ промпта на его сборке (см. ``agents/workflow.py``: секции
#    ``ИСТОРИЯ ВЕТКИ`` и ``FINDINGS`` формируются из уже отфильтрованного по
#    ``allowed_data_classes`` retrieval-слоя). Значит, у пользователей с разным
#    видимым содержанием ключи различаются, и попасть на чужую запись можно
#    только с идентичным промптом — т. е. с тем же самым доказательством.
# 2. Значение — сериализованный JSON валидированной модели; на попадании
#    ``schema.model_validate`` собирает НОВЫЙ экземпляр, Pydantic-объект
#    никогда не разделяется между вызывающими.
# 3. Ошибки, недоступная модель и промежуточные schema-repair ответы не
#    кэшируются: запись происходит только после валидного результата.
# 4. ``llm_cache_ttl_seconds`` <= 0 выключает кэш полностью — путь выполнения
#    тогда идентичен до-кэшному, включая метрики (ни попаданий, ни промахов).
#
# Замок ``threading.Lock``, а не ``asyncio.Lock``: event loop здесь один, но
# модуль живёт и в потоковых путях (индексация, ``InMemoryKnowledgeBase``), и
# словарь OrderedDict переиспользуется между провайдерами процесса.

_LlmCacheEntry = tuple[str, float]  # JSON ответа модели и момент истечения (monotonic)

_llm_cache: OrderedDict[str, _LlmCacheEntry] = OrderedDict()
_llm_cache_lock = threading.Lock()


def invalidate_llm_cache() -> None:
    """Сбрасывает все кэшированные ответы structured output.

    Вызывается из путей мутации корпуса и записи в граф (собирает другой
    агент): после изменения источников закэшированный ответ устаревает, даже
    если промпт буквально тот же.
    """
    with _llm_cache_lock:
        _llm_cache.clear()


def _report_llm_cache(metrics: object, *, hit: bool) -> None:
    """Отчёт попадания/промаха кэша — защитно, через getattr.

    Метод добавляет другой агент в ``AgentMetricsRegistry``; порядок выкладки
    не должен ронять провайдер, поэтому при отсутствии метода молча пропускаем.
    """
    hook = getattr(metrics, "observe_llm_cache", None)
    if callable(hook):
        hook(hit)


def _report_llm_cancelled(metrics: object, schema_name: str) -> None:
    """Отчёт об отмене обращения к модели — тем же защитным способом."""
    hook = getattr(metrics, "observe_llm_cancelled", None)
    if callable(hook):
        hook(schema_name)


def _report_llm_timeout(metrics: object, schema_name: str) -> None:
    """Таймаут транспорта отдельной метрикой: он чинится таймаутом, а не repair-промптом."""
    hook = getattr(metrics, "observe_llm_timeout", None)
    if callable(hook):
        hook(schema_name)


def _llm_cache_key(model: str, schema: type[BaseModel], system: str, user: str) -> str:
    """Ключ, чувствительный ко всему, что меняет ответ.

    Разделитель ``\\x1f`` исключает склейку-амбивигуитет: пары строк, которые
    без разделителя дали бы один и тот же материал (переносы внутри промпта),
    не должны давать один sha256.
    """
    material = "\x1f".join((model, schema.__name__, system, user))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _llm_cache_take[S: BaseModel](key: str, schema: type[S]) -> S | None:
    """Возвращает НОВЫЙ валидированный экземпляр при живом попадании.

    Протухшую или перевалидируемую только что изменённой схемой запись считаем
    промахом: неверный ответ из кэша хуже двойного обращения к модели.
    """
    now = monotonic()
    with _llm_cache_lock:
        entry = _llm_cache.get(key)
        if entry is None:
            return None
        payload, expires_at = entry
        if expires_at <= now:
            del _llm_cache[key]
            return None
        _llm_cache.move_to_end(key)
    try:
        return schema.model_validate(json.loads(payload))
    except (ValidationError, json.JSONDecodeError):
        with _llm_cache_lock:
            _llm_cache.pop(key, None)
        return None


def _llm_cache_put(key: str, payload: str, *, ttl_seconds: float, max_entries: int) -> None:
    """Записывает ответ и удерживает LRU-потолок: переполнение сбрасывает давнее."""
    with _llm_cache_lock:
        _llm_cache[key] = (payload, monotonic() + ttl_seconds)
        _llm_cache.move_to_end(key)
        while len(_llm_cache) > max_entries:
            _llm_cache.popitem(last=False)


def _clean_json(content: str) -> str:
    """Снимает markdown-обёртку ```json ... ``` из ответа модели."""
    value = content.strip()
    if value.startswith("```json"):
        value = value[7:]
    elif value.startswith("```"):
        value = value[3:]
    if value.endswith("```"):
        value = value[:-3]
    return value.strip()


# Потолок раскрытия `$ref`: считается только по развёрнутым указателям, а не по
# глубине вложенности — иначе нерекурсивная, но глубокая схема обрезалась бы с
# живым `$ref` внутри. Цикл может возникнуть лишь через указатель, поэтому
# счётчик указателей — достаточная защита.
_MAX_SCHEMA_EXPANSIONS = 12


def _resolve_ref(root: dict[str, object], ref: str) -> object:
    """Только локальные указатели `#/$defs/…`; `$defs` — единственный источник."""
    if not ref.startswith("#/"):
        return None
    target: object = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or part not in target:
            return None
        target = target[part]
    return target


def _inline_refs(node: object, root: dict[str, object], expansions: int) -> object:
    """Ставит содержимое `$ref` на место указателя.

    GigaChat отвечает на structured-output дословным куском промпта чаще, чем
    экземпляром схемы: в логе живого прогона ответ начинался с `{"$defs": …}` —
    модель пересказала обёртку, которую ей показали. Без `$defs` в поле зрения
    пересказывать этот ключ нечего, а вложенная форма становится видимой сразу.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and expansions < _MAX_SCHEMA_EXPANSIONS:
            target = _resolve_ref(root, ref)
            if isinstance(target, dict):
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return _inline_refs({**target, **extra}, root, expansions + 1)
        return {k: _inline_refs(v, root, expansions) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, root, expansions) for v in node]
    return node


def _shape_of(node: object) -> object:
    """Форма экземпляра из узла схемы: остаются только ключи данных.

    Всё, что лежит в угловых скобках, модель обязана заменить — ни одно
    значение не похоже на готовый ответ. Числа и булевы намеренно не `0`/`true`:
    продукт сверяет числа, и молча скопированное нулевое значение хуже, чем
    громко неверный тип.
    """
    if not isinstance(node, dict):
        return "<текст>"
    if "enum" in node:
        return "<" + "|".join(str(item) for item in node["enum"]) + ">"
    for branch in node.get("anyOf") or node.get("oneOf") or []:
        if isinstance(branch, dict) and branch.get("type") != "null":
            return _shape_of(branch)
    types = node.get("type")
    kind = types[0] if isinstance(types, list) and types else types
    properties = node.get("properties")
    if kind == "object" or isinstance(properties, dict):
        return {key: _shape_of(value) for key, value in (properties or {}).items()}
    if kind == "array":
        return [_shape_of(node.get("items") or {})]
    if kind == "boolean":
        return "<true|false>"
    if kind in {"integer", "number"}:
        return "<число>"
    return "<текст>"


@lru_cache(maxsize=64)
def _instance_shape(schema: type[BaseModel]) -> str:
    document = schema.model_json_schema()
    return json.dumps(_shape_of(_inline_refs(document, document, 0)), ensure_ascii=False)


def build_instance_instruction(system: str, schema: type[BaseModel]) -> str:
    """Системный промпт structured-output для моделей без response_format.

    JSON Schema в промпте GigaChat пересказывает дословно: в логе живого прогона
    ответ начинался с `{"$defs": …}`, а потом подмешивал `"title": "Question",
    "type": "object"` в данные. Служебных слов схемы в форме экземпляра просто
    нет — пересказывать нечего, а вложенность видна в одном месте.
    """
    return (
        f"{system}\n\nВерни один JSON-объект — экземпляр {schema.__name__} по форме ниже. "
        "Ключи оставь как в форме и верни их все, включая вложенные: модельный ответ "
        "только с первым ключом считается неполным и отклоняется. Каждое значение в "
        "угловых скобках замени настоящим. Описание схемы или JSON Schema не возвращай.\n"
        f"Форма ответа:\n{_instance_shape(schema)}"
    )


class ModelProvider(Protocol):
    mode: ModelMode

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
    ) -> StructuredOutput: ...


class UnavailableProvider:
    mode: ModelMode = "unavailable"

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
    ) -> StructuredOutput:
        raise ModelUnavailableError("LLM не настроен. Укажите GIGACHAT_API_KEY.")


# Текст нарушения, который модель видит при повторе, обязан называть ПОЛЕ:
# `msg="Field required"` без пути не говорит ничего, и правка расходилась — на
# живых прогонах PlanningBundle то пропадали верхние ключи, то JSON обрывался.
_MAX_REPORTED_ERRORS = 3


def _defect_summary(errors: Sequence[Any]) -> str:
    """До трёх нарушений с путём поля — из них складывается подсказка модели."""
    parts = []
    for error in errors[:_MAX_REPORTED_ERRORS]:
        path = "/".join(str(item) for item in error.get("loc", ())) or "<корень>"
        parts.append(f"{path}: {error.get('msg', 'нарушение')}")
    return "; ".join(parts)


class GigaChatProvider:
    """GigaChat-адаптер платформы: structured output через JSON + pydantic-валидацию.

    Клиент и токен переиспользуются между запросами; за задержку обращения к модели
    отвечает одна retry-политика SDK плюс ровно одна schema-repair попытка.
    Повторный identical structured-output обслуживается кэшем
    ``complete_model`` (см. ``_llm_cache`` выше), а не новым обращением к модели.

    Семафор на ``gigachat_max_concurrent`` — в пределах процесса: один воркер
    uvicorn = один счётчик, несколько воркеров перемножают фактическую нагрузку
    на тариф. Поэтому приём запросов ограничен отдельным слоем
    (``services/admission.py``), а ожидание слота здесь наблюдаемо и ограничено
    ``gigachat_queue_wait_seconds``: очередь внутри чужого агентного дедлайна —
    это будущая молчаливая деградация ответа вместо честного «сервис занят».
    """

    mode: ModelMode = "gigachat"

    def __init__(
        self,
        settings: Settings,
        metrics: AgentMetricsRegistry | None = None,
    ) -> None:
        if not settings.gigachat_api_key:
            raise ValueError("GIGACHAT_API_KEY обязателен для GigaChat mode")
        self._settings = settings
        self._metrics = metrics or agent_metrics
        self._slots = max(settings.gigachat_max_concurrent, 1)
        # Потолок ожидания слота: без него запрос с ``gigachat_max_concurrent=1``
        # молча съедал бы весь ``agent_deadline_seconds`` в очереди семафора.
        self._queue_wait_seconds = float(settings.gigachat_queue_wait_seconds)
        # Кэш ответов: <= 0 полностью выключает путь кэша (поведение как до его
        # появления), потолок записей держится LRU-вытеснением.
        self._cache_ttl_seconds = float(settings.llm_cache_ttl_seconds)
        self._cache_max_entries = max(int(settings.llm_cache_max_entries), 1)
        # Контекст TLS строится здесь, а не на первом обращении: отсутствующий файл
        # доверенного корня должен остановить запуск, а не всплыть 500 в середине
        # первого агентного запроса.
        self._ssl_context = settings.gigachat_ssl_context
        self._semaphore = asyncio.Semaphore(self._slots)
        self._in_flight = 0
        self._waiting = 0
        self._client: GigaChat | None = None
        self._client_lock = asyncio.Lock()
        self._metrics.set_llm_capacity(self._slots)

    async def _get_client(self) -> GigaChat:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = GigaChat(
                        credentials=self._settings.gigachat_api_key,
                        base_url=self._settings.gigachat_base_url,
                        model=self._settings.gigachat_model,
                        verify_ssl_certs=self._settings.gigachat_verify_ssl_certs,
                        ssl_context=self._ssl_context,
                        scope=self._settings.gigachat_scope,
                        timeout=self._call_timeout(),
                        max_retries=self._settings.gigachat_max_retries,
                        retry_backoff_factor=0.5,
                    )
                    # Явная авторизация до первого запроса: ошибка ключа должна быть
                    # видна как ModelUnavailableError, а не как 500 на первом узле.
                    # Транспортные отказы (TLS, DNS, коннект) относятся сюда же:
                    # провайдер недоступен — это деградация, а не баг приложения.
                    try:
                        await self._client.aget_token()
                    except (GigaChatException, httpx.TransportError) as exc:
                        self._client = None
                        raise self._wrap(exc) from exc
        return self._client

    def _call_timeout(self) -> int:
        """Потолок одной транспортной попытки, согласованный с агентным дедлайном.

        Худший случай complete_model — repair-попытки × транспортные повторы SDK:
        при дефолтах 90с × 4 × 3 один зависший вызов сжигал бы весь
        ``agent_deadline_seconds`` целиком, и ответ деградировал бы там, где при
        раннем обрыве вызова успел бы собраться.
        """
        attempts = _SCHEMA_REPAIR_ATTEMPTS * (self._settings.gigachat_max_retries + 1)
        ceiling = max(int(self._settings.agent_deadline_seconds // (attempts + 1)), 1)
        return min(int(self._settings.gigachat_timeout_seconds), ceiling)

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
    ) -> StructuredOutput:
        """Один structured-output вызов с TTL-кэшем в пределах процесса.

        Ключевые свойства (см. комментарий у ``_llm_cache``): попадание
        собирается из полного промпта, поэтому ACL-различия пользователей и
        история ветки, уже вплетённые в текст промпта на сборке
        (``agents/workflow.py``), не могут скормить одному пользователю ответ
        другого; на попадании возвращается НОВЫЙ валидированный экземпляр.
        Промах и отключённый кэш идут в модель через ``_complete_model_uncached``,
        где учёт токенов выполняется ровно один раз — включая отмену.
        Ошибки и schema-repair промежуточные ответы в кэш не попадают.
        """
        if self._cache_ttl_seconds <= 0:
            # Кэш выключен: путь выполнения идентичен до-кэшному, метрики
            # попаданий/промахов не вызываются вообще.
            return await self._complete_model_uncached(system, user, schema)
        key = _llm_cache_key(self._settings.gigachat_model, schema, system, user)
        cached = _llm_cache_take(key, schema)
        _report_llm_cache(self._metrics, hit=cached is not None)
        if cached is not None:
            return cached
        result = await self._complete_model_uncached(system, user, schema)
        self._cache_result(key, result)
        return result

    def _cache_result(self, key: str, result: BaseModel) -> None:
        """Сериализует валидный ответ для кэша; несериализуемое — не кэшируется."""
        try:
            payload = result.model_dump_json()
        except (TypeError, ValueError):
            return
        _llm_cache_put(
            key,
            payload,
            ttl_seconds=self._cache_ttl_seconds,
            max_entries=self._cache_max_entries,
        )

    async def _complete_model_uncached(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
    ) -> StructuredOutput:
        instruction = build_instance_instruction(system, schema)
        messages: list[Messages] = [
            Messages(role=MessagesRole.SYSTEM, content=instruction),
            Messages(role=MessagesRole.USER, content=user),
        ]
        started = perf_counter()
        content: str = ""
        last_defect = "ответ не получен"
        # Токены копятся по всем попыткам: usage последнего ответа занижал бы
        # расход ровно на столько, сколько стоили schema-repair повторы.
        prompt_tokens = 0
        completion_tokens = 0
        success = False
        try:
            for attempt in range(_SCHEMA_REPAIR_ATTEMPTS):
                try:
                    response = await self._request(messages)
                except (GigaChatException, httpx.TransportError) as exc:
                    # Транспортная ошибка и таймаут обязаны попасть в отдельный
                    # счётчик: иначе «модель не ответила за 90 с» неотличима от
                    # «модель вернула мусор», а чинятся они разными настройками.
                    if isinstance(exc, httpx.TimeoutException):
                        _report_llm_timeout(self._metrics, schema.__name__)
                    raise self._wrap(exc) from exc
                content = self._extract_content(response)
                usage = getattr(response, "usage", None)
                prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
                # Ответ мог упереться в бюджет вывода: тогда JSON обрывается не из-за
                # качества модели, и чинится это не repair-промптом, а max_tokens.
                truncated = getattr(response.choices[0], "finish_reason", None) == "length"
                try:
                    parsed = json.loads(_clean_json(content))
                except json.JSONDecodeError as exc:
                    parsed = None
                    last_defect = (
                        f"вывод обрезан на лимите max_tokens="
                        f"{self._settings.gigachat_max_output_tokens}"
                        if truncated
                        else f"ответ не является JSON ({exc.msg} на позиции {exc.pos})"
                    )
                if isinstance(parsed, dict) and "error" in parsed and len(parsed) <= 2:
                    raise ModelUnavailableError(f"GigaChat вернул ошибку: {parsed.get('error')}")
                if isinstance(parsed, dict):
                    try:
                        result = schema.model_validate(parsed)
                    except ValidationError as exc:
                        defects = _defect_summary(exc.errors())
                        last_defect = defects
                        if attempt == _SCHEMA_REPAIR_ATTEMPTS - 1:
                            raise ModelUnavailableError(
                                f"GigaChat: ответ не соответствует {schema.__name__}: "
                                f"{defects}"[:400]
                            ) from exc
                        logger.warning(
                            "GigaChat: %s не пройден (%s, попытка %d)",
                            schema.__name__,
                            defects,
                            attempt + 1,
                        )
                        messages = self._repair_messages(instruction, user, content, defects)
                        self._metrics.observe_llm_retry(schema.__name__)
                        continue
                    # Успех засчитывается только на валидированном результате;
                    # учёт расходования (duration, токены) закроет ``finally``.
                    success = True
                    return result
                if not isinstance(parsed, dict):
                    last_defect = (
                        "ответ не является JSON-объектом"
                        if parsed is not None
                        else last_defect
                    )
                    # Без этого отказ был «retry исчерпан» и nothing more: причину
                    # срыва structured output приходилось воспроизводить вручную.
                    logger.warning(
                        "GigaChat: %s (%s, попытка %d); начало ответа: %r",
                        last_defect,
                        schema.__name__,
                        attempt + 1,
                        content[:240],
                    )
                if attempt == _SCHEMA_REPAIR_ATTEMPTS - 1:
                    break
                messages = self._repair_messages(instruction, user, content, None)
                self._metrics.observe_llm_retry(schema.__name__)
            raise ModelUnavailableError(
                f"GigaChat: structured output retry исчерпан ({schema.__name__}): {last_defect}"
            )
        except asyncio.CancelledError:
            # Отмена (клиент закрыл вкладку) — не ошибка модели: её видит
            # отдельный наблюдатель, а ``finally`` ниже честно закрывает учёт
            # уже потраченных токенов. Never swallow, never convert.
            _report_llm_cancelled(self._metrics, schema.__name__)
            raise
        finally:
            # Ровно один учёт на вызов: раньше каждая ветка отказа звала
            # observe_llm сама, и отмена посреди обращения оставляла потраченные
            # провайдером токены неучтёнными.
            self._metrics.observe_llm(
                schema.__name__,
                self._elapsed(started),
                success=success,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )

    async def _request(self, messages: Sequence[Messages]) -> ChatCompletion:
        client = await self._get_client()
        request = Chat(
            model=self._settings.gigachat_model,
            messages=list(messages),
            temperature=0.1,
            max_tokens=self._settings.gigachat_max_output_tokens,
        )
        logger.debug("GigaChat: запрос к модели %s", self._settings.gigachat_model)
        # Ожидание слота считается один раз на обращение: инкремент на каждое
        # обновление gauge давал бы 2+ счёт на одно ждущее обращение.
        will_wait = self._semaphore.locked()
        if will_wait:
            logger.warning(
                "GigaChat: все %d слотов заняты, запрос ждёт освобождения (макс. %g с)",
                self._slots,
                self._queue_wait_seconds,
            )
        # Обновления счётков идут без await: в одном event loop они атомарны,
        # а lock здесь только добавил бы точку переключения.
        self._waiting += 1
        self._report_queue()
        entered = False
        try:
            # Ожидание ограничено ``gigachat_queue_wait_seconds``: без потолка
            # очередь при ``gigachat_max_concurrent=1`` съедала бы
            # ``agent_deadline_seconds`` молча, и аналитик видел бы медленный
            # таймаут там, где честный ответ — «сервис занят» (503).
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), self._queue_wait_seconds
                )
            except TimeoutError:
                raise ModelUnavailableError(
                    "GigaChat: сервис занят — все слоты модели заняты, ожидание "
                    f"свободного слота превысило {self._queue_wait_seconds:g} с; "
                    "повторите запрос позже"
                ) from None
            entered = True
            if will_wait:
                self._metrics.observe_llm_queue_wait()
            self._waiting -= 1
            self._in_flight += 1
            self._report_queue()
            try:
                return await client.achat(request)
            finally:
                self._in_flight -= 1
                self._report_queue()
        finally:
            # Отмена клиента до получения слота не должна оставлять фантом в
            # метрике ожидания; освобождается только фактически взятый слот.
            if entered:
                self._semaphore.release()
            else:
                self._waiting -= 1
                self._report_queue()

    @property
    def slots(self) -> int:
        """Ёмкость LLM-контура этого процесса — проверяется тестами приёма."""
        return self._slots

    def _report_queue(self) -> None:
        self._metrics.observe_llm_queue(
            in_flight=self._in_flight, waiting=self._waiting, limit=self._slots
        )

    @staticmethod
    def _repair_messages(
        instruction: str,
        user: str,
        previous: str,
        errors: str | None,
    ) -> list[Messages]:
        hint = f"Нарушения схемы: {errors}" if errors else "Ответ не является JSON-объектом."
        return [
            Messages(role=MessagesRole.SYSTEM, content=instruction),
            Messages(
                role=MessagesRole.USER,
                content=(
                    f"Исходная задача:\n{user}\n\n{hint}\n"
                    "Исправь ответ и верни только data object, без пояснений:\n"
                    f"{previous[:4000]}"
                ),
            ),
        ]

    @staticmethod
    def _elapsed(started: float) -> float:
        return (perf_counter() - started) * 1000

    @staticmethod
    def _wrap(error: GigaChatException | httpx.TransportError) -> ModelUnavailableError:
        if isinstance(error, AuthenticationError | ForbiddenError):
            kind = "auth error"
        elif isinstance(error, RateLimitError):
            kind = "rate limit"
        elif isinstance(error, ServerError):
            kind = "server error"
        elif isinstance(error, httpx.TransportError):
            kind = "transport error"
        else:
            kind = "error"
        return ModelUnavailableError(f"GigaChat {kind}: {error}")

    @staticmethod
    def _extract_content(response: ChatCompletion) -> str:
        if not response.choices:
            raise ModelUnavailableError("GigaChat вернул пустой ответ (нет choices)")
        content = response.choices[0].message.content
        if not content:
            raise ModelUnavailableError("GigaChat вернул пустой ответ")
        return content


def build_provider(settings: Settings) -> ModelProvider:
    if settings.use_gigachat:
        return GigaChatProvider(settings)
    return UnavailableProvider()
