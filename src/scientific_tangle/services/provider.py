from __future__ import annotations

import asyncio
import json
import logging
from time import perf_counter
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from scientific_tangle.config import Settings

if TYPE_CHECKING:
    from gigachat import GigaChat
    from gigachat.models import Chat, ChatCompletion

logger = logging.getLogger(__name__)

StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ModelUnavailableError(RuntimeError):
    """Базовая ошибка недоступности LLM."""


class YandexRateLimitError(ModelUnavailableError):
    """Yandex 429 после исчерпания повторов — кандидат на fallback."""


class YandexAuthError(ModelUnavailableError):
    """Yandex 401/403 — ключ недействителен, кандидат на fallback."""


class YandexUnavailableError(ModelUnavailableError):
    """Yandex 5xx или ошибка соединения — кандидат на fallback."""


# ---------------------------------------------------------------------------
# Shared helpers — error-response detection
# ---------------------------------------------------------------------------


def _check_error_response(parsed: Any, content: str, provider: str) -> None:
    """Проверяет, не вернул ли LLM JSON-объект с ошибкой вместо результата."""
    if isinstance(parsed, dict) and "error" in parsed and len(parsed) <= 2:
        raise ModelUnavailableError(
            f"{provider} вернул ошибку: {parsed.get('error', content[:200])}"
        )


# ---------------------------------------------------------------------------
# Protocol & unavailable stub
# ---------------------------------------------------------------------------


class ModelProvider(Protocol):
    mode: str

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
    ) -> StructuredOutput: ...


class UnavailableProvider:
    mode = "unavailable"

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
    ) -> StructuredOutput:
        raise ModelUnavailableError(
            "LLM не настроен. Укажите YANDEX_API_KEY и FOLDER_ID или GIGACHAT_API_KEY."
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _clean_json(content: str) -> str:
    """Очищает ответ LLM от markdown-обёрток ```json ... ```."""
    value = content.strip()
    if value.startswith("```json"):
        value = value[7:]
    elif value.startswith("```"):
        value = value[3:]
    if value.endswith("```"):
        value = value[:-3]
    return value.strip()


def _build_instance_instruction(system: str, schema: type[BaseModel]) -> str:
    """Формирует системный промпт для structured output."""
    schema_payload = schema.model_json_schema()
    return (
        f"{system}\n\nВерни один JSON-объект — экземпляр {schema.__name__}. "
        "Не возвращай описание или JSON Schema. Все required поля обязательны.\n"
        f"JSON Schema:\n{json.dumps(schema_payload, ensure_ascii=False)}"
    )


# ---------------------------------------------------------------------------
# Yandex provider
# ---------------------------------------------------------------------------


class YandexAIStudioProvider:
    """OpenAI-compatible Yandex AI Studio adapter со structured output."""

    mode = "yandex"

    def __init__(self, settings: Settings) -> None:
        if not settings.yandex_api_key:
            raise ValueError("YANDEX_API_KEY обязателен для live mode")
        self._model_uri = settings.resolved_yandex_model_uri
        self._settings = settings
        # Семафор ограничивает количество одновременных запросов (free-tier: 10 concurrent)
        self._semaphore = asyncio.Semaphore(settings.yandex_max_concurrent)

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
    ) -> StructuredOutput:
        instance_instruction = _build_instance_instruction(system, schema)
        payload: dict[str, Any] = {
            "model": self._model_uri,
            "messages": [
                {"role": "system", "content": instance_instruction},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Api-Key {self._settings.yandex_api_key}",
            "Content-Type": "application/json",
        }
        # Семафор ограничивает concurrent in-flight запросы (free-tier gauge ≤ 10)
        logger.info("Yandex: отправка запроса к модели %s", self._model_uri)
        _yandex_started = perf_counter()
        async with self._semaphore:
            async with httpx.AsyncClient(
                base_url=self._settings.yandex_base_url,
                timeout=self._settings.yandex_timeout_seconds,
            ) as client:
                for attempt in range(2):
                    try:
                        response = await self._post_with_retry(client, payload, headers)
                    except httpx.TransportError as exc:
                        raise YandexUnavailableError(
                            f"Yandex соединение недоступно: {exc}"
                        ) from exc
                    # Проверяем статус и выбрасываем типизированные ошибки для fallback
                    self._raise_for_status(response)
                    body = response.json()
                    content = body["choices"][0]["message"]["content"]
                    try:
                        parsed = json.loads(_clean_json(content))
                        _check_error_response(parsed, content, "Yandex")
                        elapsed = perf_counter() - _yandex_started
                        logger.info("Yandex: ответ получен за %.1fs", elapsed)
                        return schema.model_validate(parsed)
                    except (json.JSONDecodeError, ValidationError):
                        if attempt == 1:
                            raise
                        payload["messages"] = [
                            {"role": "system", "content": instance_instruction},
                            {
                                "role": "user",
                                "content": (
                                    f"Исходная задача:\n{user}\n\n"
                                    "Предыдущий ответ не является экземпляром схемы. "
                                    "Исправь его и верни только data object:\n"
                                    f"{content[:4000]}"
                                ),
                            },
                        ]
        raise RuntimeError("Yandex structured output retry exhausted")

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Проверяет HTTP статус и выбрасывает типизированные ошибки для fallback."""
        status = response.status_code
        if status < 400:
            return
        detail = response.text[:300]
        if status in (401, 403):
            raise YandexAuthError(f"Yandex auth error ({status}): {detail}")
        if status == 429:
            raise YandexRateLimitError(f"Yandex rate limit (429) после повторов: {detail}")
        if status >= 500:
            raise YandexUnavailableError(f"Yandex server error ({status}): {detail}")
        raise ModelUnavailableError(f"Yandex error ({status}): {detail}")

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        """Повторяет запрос при 429/5xx с экспоненциальной задержкой (до 6 попыток)."""
        for attempt in range(6):
            try:
                response = await client.post("/chat/completions", json=payload, headers=headers)
            except httpx.TransportError:
                if attempt == 5:
                    raise
                await asyncio.sleep(min(2 ** (attempt + 1), 30))
                continue
            if response.status_code != 429 and response.status_code < 500:
                return response
            if attempt == 5:
                return response
            retry_after = response.headers.get("Retry-After")
            delay = (
                float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt + 1)
            )
            await asyncio.sleep(min(delay, 30))
        raise RuntimeError("Yandex retry loop exhausted")


# ---------------------------------------------------------------------------
# GigaChat provider
# ---------------------------------------------------------------------------


class GigaChatProvider:
    """GigaChat (Сбер) адаптер со structured output через JSON-парсинг ответа."""

    mode = "gigachat"

    def __init__(self, settings: Settings) -> None:
        if not settings.gigachat_api_key:
            raise ValueError("GIGACHAT_API_KEY обязателен для GigaChat mode")
        self._settings = settings
        # GigaChat individual tier (GIGACHAT_API_PERS) — только 1 concurrent thread
        self._semaphore = asyncio.Semaphore(settings.gigachat_max_concurrent)

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
    ) -> StructuredOutput:
        from gigachat import GigaChat
        from gigachat.models import Chat, Messages, MessagesRole

        instance_instruction = _build_instance_instruction(system, schema)

        def build_request(messages: list[dict[str, str]]) -> Chat:
            return Chat(
                model=self._settings.gigachat_model,
                messages=[
                    Messages(role=MessagesRole(m["role"]), content=m["content"])
                    for m in messages
                ],
                temperature=0.1,
            )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": instance_instruction},
            {"role": "user", "content": user},
        ]

        # Семафор сериализует запросы (individual tier: 1 concurrent thread)
        logger.info("GigaChat: отправка запроса к модели %s", self._settings.gigachat_model)
        _gigachat_started = perf_counter()
        async with self._semaphore:
            async with GigaChat(
                credentials=self._settings.gigachat_api_key,
                base_url=self._settings.gigachat_base_url,
                model=self._settings.gigachat_model,
                verify_ssl_certs=self._settings.gigachat_verify_ssl_certs,
                scope=self._settings.gigachat_scope,
                timeout=self._settings.yandex_timeout_seconds,
                max_retries=3,
                retry_backoff_factor=0.5,
            ) as client:
                # Явная аутентификация — получаем токен доступа перед первым запросом
                try:
                    client.get_token()
                except Exception as exc:
                    logger.warning("GigaChat: не удалось получить токен доступа: %s", exc)
                    raise ModelUnavailableError(f"GigaChat auth error: {exc}") from exc

                for attempt in range(2):
                    response = await self._call(client, build_request(messages))
                    content = self._extract_content(response)
                    try:
                        parsed = json.loads(_clean_json(content))
                        _check_error_response(parsed, content, "GigaChat")
                        elapsed = perf_counter() - _gigachat_started
                        logger.info("GigaChat: ответ получен за %.1fs", elapsed)
                        return schema.model_validate(parsed)
                    except (json.JSONDecodeError, ValidationError):
                        if attempt == 1:
                            raise
                        messages = [
                            {"role": "system", "content": instance_instruction},
                            {
                                "role": "user",
                                "content": (
                                    f"Исходная задача:\n{user}\n\n"
                                    "Предыдущий ответ не является экземпляром схемы. "
                                    "Исправь его и верни только data object:\n"
                                    f"{content[:4000]}"
                                ),
                            },
                        ]
        raise RuntimeError("GigaChat structured output retry exhausted")

    async def _call(self, client: GigaChat, request: Chat) -> ChatCompletion:
        """Выполняет async-запрос к GigaChat с обработкой исключений."""
        from gigachat.exceptions import (
            AuthenticationError,
            ForbiddenError,
            GigaChatException,
            RateLimitError,
            ServerError,
        )

        try:
            logger.debug("GigaChat: отправка запроса к модели %s", self._settings.gigachat_model)
            return await client.achat(request)
        except (AuthenticationError, ForbiddenError) as exc:
            logger.error("GigaChat: ошибка аутентификации (%s): %s", type(exc).__name__, exc)
            raise ModelUnavailableError(f"GigaChat auth error: {exc}") from exc
        except RateLimitError as exc:
            logger.warning("GigaChat: превышен лимит запросов: %s", exc)
            raise ModelUnavailableError(f"GigaChat rate limit: {exc}") from exc
        except ServerError as exc:
            logger.error("GigaChat: ошибка сервера: %s", exc)
            raise ModelUnavailableError(f"GigaChat server error: {exc}") from exc
        except GigaChatException as exc:
            logger.error("GigaChat: ошибка: %s", exc)
            raise ModelUnavailableError(f"GigaChat error: {exc}") from exc

    @staticmethod
    def _extract_content(response: ChatCompletion) -> str:
        """Извлекает текст из ответа GigaChat (контракт choices[].message.content)."""
        if not response.choices:
            raise ModelUnavailableError("GigaChat вернул пустой ответ (нет choices)")
        content = response.choices[0].message.content
        if not content:
            raise ModelUnavailableError("GigaChat вернул пустой ответ")
        return content


# ---------------------------------------------------------------------------
# Fallback provider: GigaChat → Yandex
# ---------------------------------------------------------------------------


class FallbackProvider:
    """Сначала GigaChat, при сбое — Yandex (с семафором для free-tier)."""

    mode = "fallback"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._yandex: YandexAIStudioProvider | None = None
        self._gigachat: GigaChatProvider | None = None
        if settings.use_yandex:
            self._yandex = YandexAIStudioProvider(settings)
        if settings.use_gigachat:
            self._gigachat = GigaChatProvider(settings)

    @property
    def has_providers(self) -> bool:
        return self._yandex is not None or self._gigachat is not None

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[StructuredOutput],
    ) -> StructuredOutput:
        # GigaChat — основной провайдер (нет жёсткого лимита запросов)
        if self._gigachat:
            logger.info("Fallback: попытка GigaChat (основной)")
            try:
                return await self._gigachat.complete_model(system, user, schema)
            except ModelUnavailableError as exc:
                if self._yandex:
                    logger.warning("Fallback: GigaChat недоступен (%s), переключение на Yandex", exc)
                    return await self._yandex.complete_model(system, user, schema)
                raise
        # GigaChat нет — только Yandex (с семафором)
        if self._yandex:
            return await self._yandex.complete_model(system, user, schema)
        raise ModelUnavailableError("Нет доступных LLM провайдеров")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_provider(settings: Settings) -> ModelProvider:
    mode = settings.model_mode

    if mode == "yandex":
        if settings.use_yandex:
            return YandexAIStudioProvider(settings)
        return UnavailableProvider()

    if mode == "gigachat":
        if settings.use_gigachat:
            return GigaChatProvider(settings)
        return UnavailableProvider()

    # "auto" или "fallback": если доступны оба — fallback (GigaChat → Yandex), иначе единственный
    use_yandex = settings.use_yandex
    use_gigachat = settings.use_gigachat

    if use_yandex and use_gigachat:
        provider = FallbackProvider(settings)
        return provider if provider.has_providers else UnavailableProvider()
    if use_yandex:
        return YandexAIStudioProvider(settings)
    if use_gigachat:
        return GigaChatProvider(settings)
    return UnavailableProvider()
