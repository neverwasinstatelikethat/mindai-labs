from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

import httpx
from gigachat import GigaChat
from gigachat.exceptions import GigaChatException

from scientific_tangle.config import Settings

logger = logging.getLogger(__name__)

# Максимум текстов в одном запросе к /embeddings: GigaChat ограничивает размер батча,
# а слишком большой батч отбивает весь retry-бюджет preload на одном 4xx.
_MAX_BATCH = 8
_MAX_CHARS = 8000


class EmbeddingError(RuntimeError):
    """Эмбеддинг не получен — семантическая ветка retrieval деградирует."""


class EmbeddingClient(Protocol):
    @property
    def dimensions(self) -> int: ...

    def document(self, text: str) -> list[float]: ...

    def query(self, text: str) -> list[float]: ...

    def documents(self, texts: list[str]) -> list[list[float]]: ...


class GigaChatEmbeddingClient:
    """Клиент GigaChat /embeddings с переиспользуемым токеном и батчингом.

    Прежний Yandex-клиент создавал HTTP-соединение и делал по одному запросу на
    каждый finding; здесь соединение и токен живут в одном клиенте, а индексация
    корпуса идёт батчами.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.gigachat_api_key:
            raise ValueError("GIGACHAT_API_KEY обязателен для эмбеддингов")
        self._model = settings.gigachat_embeddings_model
        self._dimensions = settings.embedding_dimensions
        self._client = GigaChat(
            credentials=settings.gigachat_api_key,
            base_url=settings.gigachat_base_url,
            verify_ssl_certs=settings.gigachat_verify_ssl_certs,
            ssl_context=settings.gigachat_ssl_context,
            scope=settings.gigachat_scope,
            timeout=settings.gigachat_timeout_seconds,
            max_retries=settings.gigachat_max_retries,
            retry_backoff_factor=0.5,
        )
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

    def _embed(self, texts: list[str]) -> list[list[float]]:
        payload = [(text or "")[:_MAX_CHARS] for text in texts]
        if not payload:
            return []
        with self._lock:
            last_error: Exception | None = None
            for attempt in range(4):
                try:
                    response = self._client.embeddings(payload, model=self._model)
                    ordered = sorted(response.data, key=lambda item: item.index)
                    vectors = [[float(value) for value in item.embedding] for item in ordered]
                    if not vectors:
                        raise EmbeddingError("GigaChat вернул пустой список эмбеддингов")
                    self._reconcile_dimensions(len(vectors[0]))
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
                    # индексацию целиком.
                    last_error = error
                    if attempt == 3:
                        break
                    time.sleep(min(2.0 ** (attempt + 1), 8.0))
            raise EmbeddingError(f"Эмбеддинги недоступны: {last_error}") from last_error

    def _reconcile_dimensions(self, actual: int) -> None:
        """Фиксирует фактическую размерность: маппинг ES создаётся по ней.

        Вызывается только из ``_embed``, где замок уже удерживается;
        ``threading.Lock`` не реентерабелен, повторный захват остановил бы
        индексацию навсегда.
        """
        if actual != self._dimensions:
            logger.warning(
                "Размерность эмбеддингов %s не совпадает с EMBEDDING_DIMENSIONS=%s; "
                "используем фактическую",
                actual,
                self._dimensions,
            )
            self._dimensions = actual
