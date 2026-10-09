from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from functools import lru_cache
from time import monotonic, perf_counter
from typing import Any, Protocol, TypeVar, cast

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
from psycopg import Error as PsycopgError
from psycopg.errors import QueryCanceled
from pydantic import BaseModel, ValidationError

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import ModelMode
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry, agent_metrics
from scientific_tangle.services.model_slot import async_model_slot

logger = logging.getLogger(__name__)

StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)

# Ровно одна попытка schema-repair поверх retry-политики SDK перестала быть
# достаточной: на живом GigaChat структурированный ответ портится и после правки
# (то отсутствующий верхний ключ, то оборванный JSON при finish=stop). Вторая
# правка остаётся внутри агентного дедлайна: узел обращения к модели ~20 с,
# transport-повторы при этом не перемножаются — они считаются внутри SDK.
_SCHEMA_REPAIR_ATTEMPTS = 3

# Ответ с `finish_reason == "length"` оборван не из-за качества модели, а из-за
# бюджета вывода: с тем же лимитом он оборвался бы снова, а платить нужно за
# каждую попытку. Поэтому просим бо́льший лимит и останавливаемся на втором
# обрыве — третья оплата заведомо ничего не добавляет.
_TRUNCATION_ATTEMPT_LIMIT = 2
_OUTPUT_TOKENS_STEP = 2
# 16384 — фиксированный рабочий лимит ответа; повтор при обрезке не превышает его.
_OUTPUT_TOKENS_CEILING = 16384


class ModelUnavailableError(RuntimeError):
    """LLM недоступен или вернул не-экземпляр схемы после всех попыток."""


class ModelBusyError(ModelUnavailableError):
    """Слоты модели заняты и ожидание исчерпано: отказ повторимый, это 429.

    ``503`` читается как «сервис сломан», а сервис здоров и занят: потолок
    серверного ожидания (``gigachat_queue_wait_seconds``) уже потрачен, и держать
    в очереди ещё один запрос нечего. Наследование от ``ModelUnavailableError``
    сохраняет честную деградацию внутри рабочего процесса: узел, поймавший
    занятость модели, отдаёт неполный ответ с ``degradation_reasons``, а не
    теряет накопленные находки.
    """

    def __init__(
        self, message: str, *, retry_after: int, active: int, waiting: int, limit: int
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.active = active
        self.waiting = waiting
        self.limit = limit


# ── Сокрытие сбоя провайдера в пользовательских текстах ────────────────────────

_HTTP_MEANINGS: dict[int, str] = {
    400: "провайдер отклонил запрос модели",
    401: "ключ провайдера не принят",
    402: "тариф провайдера не оплачен",
    403: "провайдер запретил обращение",
    404: "метод провайдера не найден",
    408: "провайдер не ответил вовремя",
    429: "превышен лимит запросов провайдера",
}

_REDACT_LIMIT = 120
_STATUS_PREFIX = re.compile(r"(?<!\d)([1-5]\d{2})(?!\d)")
_URL = re.compile(r"(https?://|bolt://|redis://|postgresql(?:\+psycopg)?://)\S+")
# Отказ без статуса в начале строки (сбой подключения к хранилищу, таймаут) даёт
# в ``__str__`` тело ответа и дамп заголовков: там адреса, ``x-request-id`` и
# служебные заголовки провайдера. Ни одно из них не является причиной для
# аналитика, поэтому вырезается до усечения.
_BODY_DUMP = re.compile(r"b'.*?'", re.DOTALL)
_HEADERS_DUMP = re.compile(r"Headers\(.*?\)\s*$|Headers\([^)]*\)", re.DOTALL)


def redact_provider_error(error: BaseException | str, *, context: str = "провайдер") -> str:
    """Называет сбой провайдера для аналитика, не показывая сырой ответ.

    ``gigachat.ResponseError`` приводит к строке вида
    ``402 https://…: b'{\"status\":402}', Headers({'x-request-id': …})``: тело ответа,
    заголовки, адреса и идентификаторы запроса. Аналитику нужен смысл отказа, а не
    разведданные о контуре; полный текст остаётся в журнале процесса.
    """
    raw = str(error)
    match = _STATUS_PREFIX.search(raw[:24])
    if match:
        code = int(match.group(1))
        meaning = _HTTP_MEANINGS.get(code)
        if meaning is None:
            meaning = "провайдер недоступен" if code >= 500 else "провайдер отклонил обращение"
        return f"{context}: {meaning} (HTTP {code})"
    cleaned = _URL.sub(f"{context}:", raw)
    cleaned = _HEADERS_DUMP.sub("", cleaned)
    cleaned = _BODY_DUMP.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;")
    return f"{context}: {cleaned[:_REDACT_LIMIT]}"


# ── Кэш structured output (в пределах процесса) ─────────────────────────────────
#
# Один исследовательский запрос делает ~6-10 обращений к модели, а повторные
# вопросы и ветки JSON и demo нередко просят ровно то же самое: без кэша каждый
# такой повтор заново оплачивается у провайдера. Кэш явный, небольшой и
# ограничен по числу записей (LRU) и по времени жизни (TTL).
#
# Почему совместный кэш безопасен на мультипользовательском контуре:
# 1. Ключ — sha256 от (id модели, путь к классу и полный instance-shape схемы,
#    полный системный промпт, полный пользовательский промпт). Права пользователя
#    (ACL) и история ветки попадают в ТЕКСТ промпта на его сборке (см.
#    ``agents/workflow.py``: секции ``ИСТОРИЯ ВЕТКИ`` и ``FINDINGS`` формируются из
#    уже отфильтрованного по ``allowed_data_classes`` retrieval-слоя). Значит, у
#    пользователей с разным видимым содержанием ключи различаются, и попасть на
#    чужую запись можно только с идентичным промптом — т. е. с тем же самым
#    доказательством.
# 2. Значение — сериализованный JSON валидированной модели (только поля,
#    пришедшие от модели: иначе «секция не вернула» на попадании стало бы «секция
#    пуста»); на попадании ``schema.model_validate`` собирает НОВЫЙ экземпляр,
#    Pydantic-объект никогда не разделяется между вызывающими.
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


def _accepts_keyword(observer: object, keyword: str) -> bool:
    """Принимает ли наблюдатель именованный параметр (или `**kwargs`).

    Имя инициализации провайдера спрашивает это у реестра один раз: параметр
    добавляет другой агент в ``AgentMetricsRegistry``, а вызов неподдерживаемого
    аргумента в ``finally`` упал бы TypeError поверх уже потраченного обращения
    к модели.
    """
    try:
        parameters = inspect.signature(cast("Callable[..., object]", observer)).parameters
    except (TypeError, ValueError):
        return False
    return keyword in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _llm_cache_key(model: str, schema: type[BaseModel], system: str, user: str) -> str:
    """Ключ, чувствительный ко всему, что меняет ответ.

    Разделитель ``\\x1f`` исключает склейку-амбивигуитет: пары строк, которые
    без разделителя дали бы один и тот же материал (переносы внутри промпта),
    не должны давать один sha256.

    Имени класса в ключе мало: ``schema.__name__`` живёт в двух модулях
    одновременно (тестовый `Answer` и продуктовый `Answer`), а pydantic с
    `extra="ignore"` спокойно переводивалидировал бы payload одной схемы
    экземпляром другой — валидный ответ одного контракта уезжал бы в другой.
    Поэтому в ключе полный instance-shape и путь к классу; ни TTL, ни LRU это
    не меняет.
    """
    material = "\x1f".join(
        (
            model,
            f"{schema.__module__}.{schema.__qualname__}",
            _instance_shape(schema),
            system,
            user,
        )
    )
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
        "угловых скобках замени настоящим. Массивы могут быть пустыми ([]): форма "
        "показывает возможный элемент, а не требует придумать его. Если системная "
        "инструкция разрешает null для поля, верни null вместо вложенного объекта. "
        "Описание схемы или JSON Schema не возвращай.\n"
        f"Форма ответа:\n{_instance_shape(schema)}"
    )


class ModelProvider(Protocol):
    mode: ModelMode

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
        *,
        model: str | None = None,
    ) -> StructuredOutput: ...


class UnavailableProvider:
    mode: ModelMode = "unavailable"

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
        *,
        model: str | None = None,
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

    Семафор на ``gigachat_max_concurrent`` ограничивает локальную очередь.
    Общий PostgreSQL-слот сериализует chat и embeddings между воркерами и preload.
    Приём исследований ограничен отдельным слоем (``services/admission.py``),
    а ожидание слота здесь наблюдаемо и ограничено
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
        # Различает ли реестр отмену и отказ в одном учёте (см. ``finally`` в
        # ``_complete_model_uncached``): параметр `cancelled` добавляет зона
        # метрик, до его выкатки отмену считаем отдельным наблюдателем.
        self._reports_cancelled = _accepts_keyword(
            getattr(self._metrics, "observe_llm", None), "cancelled"
        )
        self._metrics.set_llm_capacity(self._slots)

    async def _get_client(self) -> GigaChat:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = GigaChat(
                        credentials=self._settings.gigachat_api_key,
                        base_url=self._settings.gigachat_base_url,
                        model=self._settings.gigachat_agent_model,
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

    @asynccontextmanager
    async def _global_model_slot(self) -> AsyncIterator[None]:
        """Сериализует обращения GigaChat между backend и CLI-процессами.

        Advisory lock привязан к PostgreSQL-соединению: PostgreSQL сам освободит
        его при отмене задачи или падении процесса. Для memory/test контура нет
        общей БД и остаётся локальный семафор.
        """
        try:
            async with async_model_slot(self._settings):
                yield
        except QueryCanceled:
            raise ModelBusyError(
                "GigaChat: сервис занят — ожидание общего слота превысило "
                f"{self._queue_wait_seconds:g} с; повторите запрос позже",
                retry_after=max(1, int(self._queue_wait_seconds)),
                active=1,
                waiting=1,
                limit=1,
            ) from None
        except PsycopgError as exc:
            logger.warning("GigaChat: общий слот PostgreSQL недоступен (%s)", type(exc).__name__)
            raise ModelUnavailableError(
                "GigaChat: не удалось получить общий слот; повторите запрос позже"
            ) from None

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
        *,
        model: str | None = None,
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
        selected_model = model or self._settings.gigachat_agent_model
        if self._cache_ttl_seconds <= 0:
            # Кэш выключен: путь выполнения идентичен до-кэшному, метрики
            # попаданий/промахов не вызываются вообще.
            return await self._complete_model_uncached(system, user, schema, selected_model)
        key = _llm_cache_key(selected_model, schema, system, user)
        cached = _llm_cache_take(key, schema)
        _report_llm_cache(self._metrics, hit=cached is not None)
        if cached is not None:
            return cached
        result = await self._complete_model_uncached(system, user, schema, selected_model)
        self._cache_result(key, result)
        return result

    def _cache_result(self, key: str, result: BaseModel) -> None:
        """Сериализует валидный ответ для кэша; несериализуемое — не кэшируется.

        `exclude_unset` записывает только поля, которые пришли от модели: иначе
        на попадании «модель не вернула секцию» превращалось бы в «секция пуста»,
        и причина деградации исчезала бы из кэшированного ответа.
        """
        try:
            payload = result.model_dump_json(exclude_unset=True)
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
        model: str,
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
        cancelled = False
        # Бюджет вывода поднимаем только по признаку обрезки, поэтому базовое
        # значение остаётся настройкой, а не ещё одной настройкой.
        base_limit = self._settings.gigachat_max_output_tokens
        output_limit = base_limit
        truncations = 0
        try:
            for attempt in range(_SCHEMA_REPAIR_ATTEMPTS):
                try:
                    # Лимит передаём только когда он поднят: базовый вызов
                    # `_request` остаётся одноаргументным.
                    response = (
                        await self._request(messages, model=model)
                        if output_limit == base_limit
                        else await self._request(messages, max_tokens=output_limit, model=model)
                    )
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
                        f"вывод обрезан на лимите max_tokens={output_limit}"
                        if truncated
                        else f"ответ не является JSON ({exc.msg} на позиции {exc.pos})"
                    )
                if isinstance(parsed, dict) and "error" in parsed and len(parsed) <= 2:
                    raise ModelUnavailableError(
                        redact_provider_error(
                            str(parsed.get("error")), context="GigaChat вернул ошибку"
                        )
                    )
                if isinstance(parsed, dict):
                    try:
                        result = schema.model_validate(parsed)
                    except ValidationError as exc:
                        if truncated:
                            # Неполный JSON ломает валидацию по другой причине, чем
                            # мусор: подсказка «исправь схему» тут бесполезна.
                            messages, output_limit, truncations = self._retry_after_truncation(
                                instruction,
                                user,
                                schema,
                                limit=output_limit,
                                truncations=truncations,
                                defect=last_defect,
                            )
                            continue
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
                    if truncated:
                        # Провайдер сам заявил обрезку, а схема прошла только
                        # потому, что усечённый хвост оказался необязательным:
                        # молча выдавать такой ответ за полный нельзя.
                        logger.warning(
                            "GigaChat: %s прошёл валидацию с finish_reason=length "
                            "(max_tokens=%d) — ответ может быть неполным",
                            schema.__name__,
                            output_limit,
                        )
                    # Успех засчитывается только на валидированном результате;
                    # учёт расходования (duration, токены) закроет ``finally``.
                    success = True
                    return result
                if truncated:
                    messages, output_limit, truncations = self._retry_after_truncation(
                        instruction,
                        user,
                        schema,
                        limit=output_limit,
                        truncations=truncations,
                        defect=last_defect,
                    )
                    continue
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
                messages = self._repair_messages(instruction, user, content, last_defect)
                self._metrics.observe_llm_retry(schema.__name__)
            raise ModelUnavailableError(
                f"GigaChat: structured output retry исчерпан ({schema.__name__}): {last_defect}"
            )
        except asyncio.CancelledError:
            # Отмена (клиент закрыл вкладку) — не ошибка модели: её видит
            # отдельный наблюдатель, а ``finally`` ниже честно закрывает учёт
            # уже потраченных токенов. Never swallow, never convert.
            cancelled = True
            _report_llm_cancelled(self._metrics, schema.__name__)
            raise
        finally:
            # Ровно один учёт на вызов: раньше каждая ветка отказа звала
            # observe_llm сама, и отмена посреди обращения оставляла потраченные
            # провайдером токены неучтёнными.
            account: dict[str, object] = {
                "success": success,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
            if cancelled and self._reports_cancelled:
                # Отмена не должна попадать в `status="failure"`: доля отказов
                # модели перестала бы зависеть от закрытой вкладки клиента.
                account["cancelled"] = True
            self._metrics.observe_llm(
                schema.__name__,
                self._elapsed(started),
                **account,  # type: ignore[arg-type]
            )

    async def _acquire_slot(self) -> None:
        """Занять слот модели не дольше ``gigachat_queue_wait_seconds``.

        Выделено потому, что отказ обязан быть различимым: занятое ожидание —
        повторяемый сбой (429 с ``Retry-After``), сбой провайдера — нет (503).
        ``retry_after`` равен потраченному потолку ожидания: раньше слот, как
        правило, не освобождается, а молча держать запрос во второй очереди
        нечего.
        """
        try:
            await asyncio.wait_for(self._semaphore.acquire(), self._queue_wait_seconds)
        except TimeoutError:
            raise ModelBusyError(
                "GigaChat: сервис занят — все слоты модели заняты, ожидание "
                f"свободного слота превысило {self._queue_wait_seconds:g} с; "
                "повторите запрос позже",
                retry_after=max(1, int(self._queue_wait_seconds)),
                active=self._in_flight,
                waiting=self._waiting,
                limit=self._slots,
            ) from None

    async def _request(
        self,
        messages: Sequence[Messages],
        *,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> ChatCompletion:
        request = Chat(
            model=model or self._settings.gigachat_agent_model,
            messages=list(messages),
            temperature=0.1,
            max_tokens=max_tokens or self._settings.gigachat_max_output_tokens,
        )
        logger.debug("GigaChat: запрос к модели %s", model or self._settings.gigachat_agent_model)
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
            # таймаут там, где честный ответ — «сервис занят, повторите позже».
            await self._acquire_slot()
            entered = True
            if will_wait:
                self._metrics.observe_llm_queue_wait()
            self._waiting -= 1
            self._in_flight += 1
            self._report_queue()
            try:
                async with self._global_model_slot():
                    # Получение токена тоже обращение к GigaChat и должно
                    # проходить через тот же межпроцессный слот.
                    client = await self._get_client()
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
        hint = f"Ошибка ответа: {errors}" if errors else "Ответ не является JSON-объектом."
        # Обрезанный префикс скрывает место ошибки и заставляет модель чинить
        # JSON, которого она не видит целиком. Исходная задача достаточна для
        # повторной генерации; передавать частичный ответ как полный нельзя.
        previous_section = (
            f"Исправь ответ и верни только data object, без пояснений:\n{previous}"
            if len(previous) <= 4000
            else "Предыдущий ответ слишком большой для ремонта. Сформируй JSON "
            "заново по исходной задаче и форме, без пояснений."
        )
        return [
            Messages(role=MessagesRole.SYSTEM, content=instruction),
            Messages(
                role=MessagesRole.USER,
                content=(
                    f"Исходная задача:\n{user}\n\n{hint}\n"
                    f"{previous_section}"
                ),
            ),
        ]

    @staticmethod
    def _truncation_messages(instruction: str, user: str, limit: int) -> list[Messages]:
        """Повтор после обрезки: тот же запрос, бо́льший лимит, без оборванного хвоста.

        Дописывать оборванный JSON в repair-промпт — увеличивать вход при
        неизменном выходе: модель обрезала бы ответ на том же месте, а промпт
        ещё и съедал бы бюджет контекста.
        """
        return [
            Messages(role=MessagesRole.SYSTEM, content=instruction),
            Messages(
                role=MessagesRole.USER,
                content=(
                    f"{user}\n\nПредыдущий ответ оборвался на лимите вывода. "
                    f"Верни полный JSON целиком, без пояснений (max_tokens={limit})."
                ),
            ),
        ]

    def _retry_after_truncation(
        self,
        instruction: str,
        user: str,
        schema: type[StructuredOutput],
        *,
        limit: int,
        truncations: int,
        defect: str,
    ) -> tuple[list[Messages], int, int]:
        """Второй заход при `finish_reason == "length"`: больше лимит, не больше оплат.

        Обрыв по длине — не мусор модели, а нехватка места, поэтому повтор с тем
        же `max_tokens` давал бы тот же исход на третьей оплаченной попытке и
        503 вместо ответа. Дальше второго обрыва не идём: контракт деградации
        (`ModelUnavailableError` → `degradation_reasons`) честнее третьего платёжа.
        """
        attempts = truncations + 1
        raised = min(limit * _OUTPUT_TOKENS_STEP, _OUTPUT_TOKENS_CEILING)
        if attempts >= _TRUNCATION_ATTEMPT_LIMIT or raised <= limit:
            raise ModelUnavailableError(
                f"GigaChat: structured output оборван на лимите вывода "
                f"({schema.__name__}, max_tokens={limit}, попыток {attempts}): {defect}"[:400]
            )
        self._metrics.observe_llm_retry(schema.__name__)
        logger.warning(
            "GigaChat: %s обрезан на max_tokens=%d, повтор с лимитом %d",
            schema.__name__,
            limit,
            raised,
        )
        return self._truncation_messages(instruction, user, raised), raised, attempts

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
        # Текст `ModelUnavailableError` уходит наружу: в 503 всего `/api/v1/...`
        # и в `degradation_reasons`, которые потом читаются моделью в промпте.
        # ``GigaChatException.__str__`` — это строка подключения, тело ответа и
        # заголовки с ``x-request-id``, поэтому наружу идёт отредактированная
        # формулировка, а полный текст остаётся в журнале процесса.
        return ModelUnavailableError(redact_provider_error(error, context=f"GigaChat {kind}"))

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
