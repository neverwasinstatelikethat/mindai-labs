import logging
from functools import lru_cache
from ssl import SSLContext, create_default_context
from typing import Literal
from urllib.parse import urlsplit

import certifi
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Оценка токенов для GigaChat: кириллическая морфема дороже латиницы,
# поэтому len/4 (эвристика GPT-латиницы) недооценивал русский текст почти вдвое.
CHARS_PER_TOKEN_RU = 2.4

# Консервативные оценки окна известных моделей: точное значение зависит от
# тарифа, цель предупреждения — поймать заведомо недостижимый бюджет, а не
# назвать точный лимит. Неизвестные имена не проверяются.
_GIGACHAT_CONTEXT_WINDOWS: dict[str, int] = {
    "GigaChat": 8192,
    "GigaChat-Pro": 8192,
    "GigaChat-Max": 8192,
}
# Резерв под систему, JSON-схему structured output, историю ветки и вывод модели.
_PROMPT_OVERHEAD_TOKENS = 3072

# Доверенные источники cookie-запросов (CSRF). Порты контура взяты из диапазона
# 20000–49000 и выбраны нестандартно: 3000/8000/9090 на рабочих машинах обычно
# заняты сторонними сервисами, а коллизия проявляется как «странный отказ входа».
# Совпадают с defaults в compose.yaml и примером в .env.example.
DEFAULT_TRUSTED_ORIGINS = ",".join(
    (
        "http://localhost:43119",
        "http://127.0.0.1:43119",
        "http://localhost:5173",
        "http://localhost:46617",
        "http://127.0.0.1:46617",
    )
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    cors_origins: str = "http://localhost:5173,http://localhost:43119"

    # GigaChat — единственный LLM-провайдер платформы.
    gigachat_api_key: str | None = Field(default=None, repr=False)
    gigachat_base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"
    gigachat_model: str = "GigaChat"
    gigachat_embeddings_model: str = "Embeddings"
    # TLS проверяется всегда: ключ провайдера не должен уходить в незащищённый канал.
    gigachat_verify_ssl_certs: bool = True
    # GigaChat отдаёт цепочку, упирающуюся в российский доверенный корневой УЦ
    # (Минцифры), которого нет в хранилище certifi. Путь к такому корню добавляется
    # к доверенным якорям, а не заменяет их: верификация остаётся включённой.
    gigachat_trusted_roots: str | None = None
    # Область API: GIGACHAT_API_PERS (физлица), GIGACHAT_API_B2B или GIGACHAT_API_CORP
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    # Бюджет вывода одного обращения. Без него остаётся дефолт провайдера: он
    # не документирован в контуре и режет structured output на середине JSON —
    # молча, без finish_reason в тексте отказа.
    gigachat_max_output_tokens: int = Field(default=2048, ge=256)
    # Individual tier (GIGACHAT_API_PERS) — только 1 одновременный запрос.
    gigachat_max_concurrent: int = Field(default=1, ge=1)
    # Транспортные повторы внутри SDK; поверх — не больше двух попыток schema-repair.
    gigachat_max_retries: int = Field(default=3, ge=0, le=10)

    embedding_dimensions: int = Field(default=1024, ge=8)

    # Бюджет одного агентного запроса: общий дедлайн ограничивает и LLM, и retrieval.
    agent_deadline_seconds: float = Field(default=300.0, gt=0)
    agent_max_tool_rounds: int = Field(default=2, ge=1, le=4)
    agent_max_revisions: int = Field(default=1, ge=0, le=2)
    # Сколько токенов доказательства помещается в промпт reasoner/critic/improver.
    context_token_budget: int = Field(default=24000, ge=500)
    # Потолок одновременных исследований: сверх него запрос получает 429, а не
    # очередь внутри чужого дедлайна. Держится ниже GIGACHAT_MAX_CONCURRENT косвенно:
    # один агентный запрос делает несколько вызовов модели, поэтому большой порог
    # лишь перекладывает ожидание семафора внутрь `agent_deadline_seconds`.
    agent_max_concurrent_runs: int = Field(default=4, ge=1, le=16)

    knowledge_backend: Literal["memory", "neo4j"] = "memory"
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = Field(default="change-me-now", repr=False)
    elasticsearch_url: str = "http://elasticsearch:9200"
    # Очередей задач и кэша в контуре нет: redis поднят в compose, но значение
    # не читается ни одним модулем — оставлено, чтобы не ломать локальные .env.
    redis_url: str = "redis://redis:6379/0"
    database_url: str = Field(
        default="postgresql://mindai:change-me-now@postgres:5432/mindai",
        repr=False,
    )
    source_root: str = "/data/sources"
    corpus_preload_limit: int = 900
    corpus_max_file_bytes: int = 20 * 1024 * 1024
    preload_mode: Literal["full", "structural", "semantic"] = "full"

    # ── Серверная аутентификация (замена role-заголовков) ────────────────────
    # accounts_backend выбирается так же, как knowledge_backend: postgres —
    # рабочий контур аналитиков, memory — тесты и запуск без базы.
    accounts_backend: Literal["postgres", "memory"] = "postgres"
    password_min_length: int = Field(default=10, ge=6)
    session_ttl_days: int = Field(default=14, ge=1)
    # Троттлинг входа считается по паре (email, client host) в пределах окна.
    login_max_attempts: int = Field(default=10, ge=1)
    login_window_seconds: int = Field(default=900, ge=1)
    trusted_origins: str = DEFAULT_TRUSTED_ORIGINS

    @property
    def use_gigachat(self) -> bool:
        """GigaChat настроен (ключ задан)."""
        return bool(self.gigachat_api_key)

    @property
    def gigachat_ssl_context(self) -> SSLContext | None:
        """Контекст TLS GigaChat: публичные корни certifi + корень из ``gigachat_trusted_roots``.

        ``None`` — когда корень не задан или верификацию выключили явно: при
        выключенной проверке молча вернуть её включённой значило бы переопределить
        настроенное исключение вместо того, чтобы показать его.
        """
        if not self.gigachat_verify_ssl_certs or not self.gigachat_trusted_roots:
            return None
        context = create_default_context(cafile=certifi.where())
        context.load_verify_locations(cafile=self.gigachat_trusted_roots)
        return context

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @model_validator(mode="after")
    def warn_budget_above_model_window(self) -> "Settings":
        """Бюджет доказательства больше окна модели — гарантированный 400 на первом узле.

        Бюджет ограничивает только evidence-секции, но при переполнении окна
        промпт отклоняется самой моделью, и это выглядит как деградация LLM,
        а не как ошибка конфигурации. Предупреждение делает связь видимой.
        """
        window = _GIGACHAT_CONTEXT_WINDOWS.get(self.gigachat_model)
        if window is not None and self.context_token_budget > window - _PROMPT_OVERHEAD_TOKENS:
            logger.warning(
                "CONTEXT_TOKEN_BUDGET=%d превышает оценку окна модели %s (%d токенов): "
                "промпты reasoner/critic будут отклонены моделью; уменьшите бюджет "
                "или укажите модель с большим окном",
                self.context_token_budget,
                self.gigachat_model,
                window,
            )
        return self

    @property
    def cookie_secure(self) -> bool:
        """Cookie флаг `secure`: только production, иначе локальный http-контур
        на портах 43119/46617 не сохранит сессию."""
        return self.app_env == "production"

    @property
    def trusted_origin_hosts(self) -> frozenset[str]:
        """Хосты (host:port) из ``trusted_origins``.

        Scheme сознательно не участвует: фронтент проксирует запрос на backend
        внутри docker-сети по http, а браузер видит origin по https-терминалу.
        """
        hosts: set[str] = set()
        for raw in self.trusted_origins.split(","):
            entry = raw.strip().lower().rstrip("/")
            if not entry:
                continue
            hosts.add(urlsplit(entry).netloc or entry)
        return frozenset(hosts)


@lru_cache
def get_settings() -> Settings:
    return Settings()
