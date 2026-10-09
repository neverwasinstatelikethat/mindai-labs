from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Protocol

import httpx
from gigachat import GigaChat
from gigachat.exceptions import GigaChatException
from psycopg import Error as PsycopgError

from scientific_tangle.config import Settings
from scientific_tangle.services.model_slot import model_slot

logger = logging.getLogger(__name__)

# Максимум текстов в одном запросе к /embeddings: GigaChat ограничивает размер батча,
# а слишком большой батч отбивает весь retry-бюджет preload на одном 4xx.
_MAX_BATCH = 8
_MAX_CHARS = 8000
# Число сетевых попыток при транзиентном отказе; бюджет ``_embed`` делится между
# ними, поэтому константа участвует и в расчёте таймаута одной попытки.
_MAX_ATTEMPTS = 4
# Потолок TTL-мемоизации: 1024-мерный float-вектор ~8 КБ, 512 записей ~4 МБ.
_MEMO_MAX_ENTRIES = 512


class EmbeddingError(RuntimeError):
    """Эмбеддинг не получен — семантическая ветка retrieval деградирует."""


# Повторные попытки лечат задержку и перегрузку, но не приговор аккаунта.
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429})


def _refused_for_good(error: BaseException) -> bool:
    """4xx кроме перегрузочных — отказ, который повтором не снять.

    Живой контур с неоплаченным `/embeddings` отдавал `402` на каждом поиске, а
    четыре попытки с backoff 2+4+8 = 14 с под общим замком превращали постоянный
    отказ тарифа в минуты ожидания: новичок получал «модель не ответила» там, где
    корпус просто не успевал отдать лексическую ветку.
    """
    status = _status_code(error)
    return isinstance(status, int) and 400 <= status < 500 and status not in _RETRYABLE_STATUSES


def _status_code(error: BaseException) -> int | None:
    """HTTP-статус отказа — для названия сбоя в деградации, без тела ответа."""
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


class _TerminalRefusalGate:
    """Короткий негативный кэш терминального отказа /embeddings.

    При живом 402 каждый новый текст вопроса платил полный round-trip, да ещё и
    под глобальным замком: отказ тарифа превращался в очередь всего процесса.
    Пауза короче TTL мемо и обнуляется успешным ответом, поэтому «починенный»
    тариф снова работает сам, без перезапуска.
    """

    def __init__(self, cooldown_seconds: float) -> None:
        self._cooldown = max(float(cooldown_seconds), 0.0)
        self._lock = threading.Lock()
        self._blocked_until = 0.0
        self._reason = ""

    @property
    def enabled(self) -> bool:
        return self._cooldown > 0

    def trip(self, error: BaseException) -> None:
        if not self.enabled:
            return
        status = _status_code(error)
        reason = (
            f"HTTP {status}" if status is not None else type(error).__name__
        )
        with self._lock:
            self._blocked_until = time.monotonic() + self._cooldown
            self._reason = reason

    def clear(self) -> None:
        with self._lock:
            self._blocked_until = 0.0
            self._reason = ""

    def remaining(self) -> float:
        """Сколько ещё длится пауза; <= 0 — рубильник уже отпущен."""
        with self._lock:
            return self._blocked_until - time.monotonic()

    def describe(self) -> str:
        with self._lock:
            return self._reason


class EmbeddingClient(Protocol):
    @property
    def dimensions(self) -> int: ...

    def document(self, text: str) -> list[float]: ...

    def query(self, text: str) -> list[float]: ...

    def documents(self, texts: list[str]) -> list[list[float]]: ...


class _EmbeddingCache:
    """TTL-мемоизация входа /embeddings с LRU-потолком (потокобезопасно).

    ``infrastructure._semantic_ids`` пересчитывал эмбеддинг одного и того же
    вопроса на каждом retrieval-действии каждого раунда, а переиндексация
    неизменного документа заново оплачивала те же векторы. Ключ — (модель,
    текст): при той же модели эмбеддинг детерминирован, поэтому инвалидация по
    мутациям корпуса ему не нужна, а TTL и потолок ограничивают память и
    последствия смены размерности на стороне провайдера.

    Выдаёт и хранит только копии списков: вызывающий вправе мутировать свой
    вектор (knn-запрос), не портя кэш остальным. Нулевой или отрицательный TTL
    выключает мемоизацию полностью.
    """

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max(max_entries, 1)
        self._entries: OrderedDict[str, tuple[list[float], float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> list[float] | None:
        if self._ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            vector, expires_at = entry
            if expires_at <= now:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return list(vector)

    def put(self, key: str, vector: list[float]) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            self._entries[key] = (list(vector), time.monotonic() + self._ttl)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class GigaChatEmbeddingClient:
    """Клиент GigaChat /embeddings с переиспользуемым токеном и батчингом.

    Прежний Yandex-клиент создавал HTTP-соединение и делал по одному запросу на
    каждый finding; здесь соединение и токен живут в одном клиенте, а индексация
    корпуса идёт батчами.

    Два ограничения держат общий ``threading.Lock`` в разумных границах:
    TTL-мемоизация входа (повторный текст не идёт в сеть вообще) и бюджет
    ``gigachat_timeout_seconds`` на всю последовательность попыток — раньше четыре
    сетевые попытки с полным таймаутом и backoff 2+4+8 могли приковать замок на
    минуты, заблокировав и вопрос аналитика, и индексацию. Замок теперь удерживается
    только на время самой сетевой попытки: пауза backoff идёт вне его.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.gigachat_api_key:
            raise ValueError("GIGACHAT_API_KEY обязателен для эмбеддингов")
        self._settings = settings
        self._model = settings.gigachat_embeddings_model
        self._dimensions = settings.embedding_dimensions
        # Бюджет всех сетевых попыток одного ``_embed``: сумма таймаутов и пауз
        # backoff не вылезает за ``gigachat_timeout_seconds`` — иначе зависший
        # эндпоинт держит замок далеко за пределами пер-колл таймаута.
        self._retry_budget_seconds = float(settings.gigachat_timeout_seconds)
        self._client = GigaChat(
            credentials=settings.gigachat_api_key,
            base_url=settings.gigachat_base_url,
            verify_ssl_certs=settings.gigachat_verify_ssl_certs,
            ssl_context=settings.gigachat_ssl_context,
            scope=settings.gigachat_scope,
            # Одна транспортная попытка получает долю бюджета: при зависшем
            # эндпоинте замок освобождается в границах общего бюджета, а не после
            # четырёх полных таймаутов подряд.
            timeout=self._retry_budget_seconds / _MAX_ATTEMPTS,
            max_retries=settings.gigachat_max_retries,
            retry_backoff_factor=0.5,
        )
        self._cache = _EmbeddingCache(
            ttl_seconds=settings.embedding_cache_ttl_seconds,
            max_entries=_MEMO_MAX_ENTRIES,
        )
        self._refusals = _TerminalRefusalGate(settings.embedding_refusal_cooldown_seconds)
        self._lock = threading.Lock()

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def document(self, text: str) -> list[float]:
        return self._embed([text])[0]

    def query(self, text: str) -> list[float]:
        # GigaChat использует одну модель для doc и query; разделение по role
        # сохраняем в сигнатуре, чтобы вызывающий код не зависел от деталей SDK.
        return self._embed([text])[0]

    def documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _MAX_BATCH):
            vectors.extend(self._embed(texts[start : start + _MAX_BATCH]))
        return vectors

    def _cache_key(self, text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{self._model}\x1f{digest}"

    def _embed(self, texts: list[str]) -> list[list[float]]:
        payload = [(text or "")[:_MAX_CHARS] for text in texts]
        if not payload:
            return []
        # Мемоизация до замка: полное попадание не трогает ни блокировку, ни сеть —
        # повторный вопрос в каждом раунде retrieval перестал быть переплатой.
        vectors: list[list[float] | None] = [None] * len(payload)
        missing: list[int] = []
        for index, text in enumerate(payload):
            cached = self._cache.get(self._cache_key(text))
            if cached is None:
                missing.append(index)
            else:
                vectors[index] = cached
        if missing:
            fetched = self._embed_uncached([payload[index] for index in missing])
            for offset, index in enumerate(missing):
                vector = fetched[offset]
                self._cache.put(self._cache_key(payload[index]), vector)
                vectors[index] = vector
        complete = [vector for vector in vectors if vector is not None]
        if len(complete) != len(payload):
            raise EmbeddingError("Внутренняя ошибка кэша эмбеддингов: векторы собраны не все")
        return complete

    def _embed_uncached(self, payload: list[str]) -> list[list[float]]:
        call_slice = self._retry_budget_seconds / _MAX_ATTEMPTS
        deadline = time.monotonic() + self._retry_budget_seconds
        last_error: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            paused = self._refusals.remaining()
            if paused > 0:
                # Рубильник по терминальному отказу: новый текст не платит round-trip
                # и не берёт замок, пока пауза не истекла.
                raise EmbeddingError(
                    f"Эмбеддинги недоступны: {self._refusals.describe()} "
                    f"— повтор через {paused:.0f} с (лексическая ветка работает)"
                )
            remaining = deadline - time.monotonic()
            if attempt and remaining < call_slice:
                # Бюджета на полноценную попытку не осталось: честнее
                # деградировать сейчас, чем держать замок ещё один таймаут.
                break
            try:
                # Замок держит ровно одну сетевую попытку. Сон backoff — вне его:
                # раньше одна цепочка повторов приковывала эмбеддинги всего процесса
                # на 2+4+8 с, а вместе с ними и вопрос аналитика.
                with self._lock:
                    try:
                        with model_slot(self._settings):
                            response = self._client.embeddings(payload, model=self._model)
                    except PsycopgError:
                        raise EmbeddingError("Общий слот GigaChat недоступен или занят") from None
                ordered = sorted(response.data, key=lambda item: item.index)
                vectors = [[float(value) for value in item.embedding] for item in ordered]
                if not vectors:
                    raise EmbeddingError("GigaChat вернул пустой список эмбеддингов")
                self._reconcile_dimensions(len(vectors[0]))
                self._refusals.clear()
                return vectors
            except (
                GigaChatException,
                EmbeddingError,
                httpx.HTTPError,
                OSError,
                ValueError,
            ) as error:
                # Граница сети: наружу уходит только EmbeddingError, чтобы
                # отказ эмбеддингов деградировал векторную ветку, а не валил
                # индексацию целиком. Ошибка в мемо не попадает никогда.
                last_error = error
                if _refused_for_good(error):
                    # Приговор аккаунта повторяется на каждом тексте: фиксируем его
                    # на короткую паузу вместо нового обращения к сети.
                    self._refusals.trip(error)
                    break
                if attempt == _MAX_ATTEMPTS - 1:
                    break
                pause = min(2.0 ** (attempt + 1), 8.0)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Пауза отрезается остатком бюджета: сумма sleep + attempts
                # остаётся внутри gigachat_timeout_seconds.
                time.sleep(min(pause, remaining))
        raise EmbeddingError(f"Эмбеддинги недоступны: {last_error}") from last_error

    def _reconcile_dimensions(self, actual: int) -> None:
        """Фиксирует фактическую размерность: маппинг ES создаётся по ней.

        Повторный захват замка больше не нужен: запись целого ``int`` атомарна под
        GIL, а гонка двух потоков может дать только одинаковое значение и лишнюю
        строку в журнале — маппинг индекса от этого не разъезжается.
        """
        if actual != self._dimensions:
            logger.warning(
                "Размерность эмбеддингов %s не совпадает с EMBEDDING_DIMENSIONS=%s; "
                "используем фактическую",
                actual,
                self._dimensions,
            )
            self._dimensions = actual
