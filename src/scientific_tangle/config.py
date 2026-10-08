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
    "GigaChat-Pro": 128_000,
    "GigaChat-Max": 8192,
    "GigaChat-3-Ultra": 128_000,
}
# Резерв под систему, JSON-схему structured output, историю ветки и вывод модели.
_PROMPT_OVERHEAD_TOKENS = 3072
# Это бюджет доказательств, которые приложение включает в запрос, а не размер
# окна модели: контекстное окно 128k не означает, что нужно отправлять все 128k.
_DEFAULT_CONTEXT_TOKEN_BUDGET = 5120

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

# ── Эвристика приёма агентных прогонов ───────────────────────────────────────
# Один прогон рабочего процесса делает не меньше восьми обращений к модели
# (planning, controller, action_planner, controller, reasoner, critic, improver,
# critic), а семафор провайдера сериализует их в одном процессе. Потолок приёма
# обязан следовать за пропускной способностью модели, иначе «принятый» второй
# прогон тратит свой дедлайн на очередь чужих обращений.
_ADMISSION_CALLS_PER_RUN = 8
# Оценка одного structured-output обращения: консервативно, по бюджету вывода
# 16384 токенов, а не по лучшему случаю.
_ADMISSION_SECONDS_PER_CALL = 15.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    cors_origins: str = "http://localhost:5173,http://localhost:43119"

    # GigaChat — единственный LLM-провайдер платформы.
    gigachat_api_key: str | None = Field(default=None, repr=False)
    gigachat_base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"
    gigachat_agent_model: str = "GigaChat-3-Ultra"
    gigachat_graphrag_model: str = "GigaChat-Pro"
    # Legacy adapter retained for compatibility; production retrieval does not create it.
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
    gigachat_max_output_tokens: int = Field(default=16384, ge=256, le=16384)
    # Individual tier (GIGACHAT_API_PERS) — только 1 одновременный запрос.
    gigachat_max_concurrent: int = Field(default=1, ge=1)
    # Транспортные повторы внутри SDK; поверх — не больше двух попыток schema-repair.
    gigachat_max_retries: int = Field(default=3, ge=0, le=10)

    embedding_dimensions: int = Field(default=1024, ge=8)

    # Бюджет одного агентного запроса: общий дедлайн ограничивает и LLM, и retrieval.
    agent_deadline_seconds: float = Field(default=300.0, gt=0)
    agent_max_tool_rounds: int = Field(default=2, ge=1, le=4)
    agent_max_revisions: int = Field(default=1, ge=0, le=2)
    # Потолок времени ОДНОГО узла. Провайдер считает транспортный таймаут делением
    # всего дедлайна на попытки одного обращения, и без этого потолка один медленный
    # узел выжигал бюджет, оставляя остальные не запущенными (см. agents/workflow.py).
    agent_node_budget_seconds: float = Field(default=60.0, gt=0, le=600)
    # Сколько токенов доказательства помещается в промпт reasoner/critic/improver.
    context_token_budget: int = Field(default=_DEFAULT_CONTEXT_TOKEN_BUDGET, ge=500)
    # Потолок candidate policy в системном промпте: активные правки EvolutionService
    # росли вместе с числом принятых предложений и вытесняли доказательства.
    policy_max_tokens: int = Field(default=600, ge=16, le=4096)
    # Потолок одновременных исследований: сверх него запрос получает 429, а не
    # очередь внутри чужого дедлайна. Значение — ЗАПРОС оператора: фактически
    # принимается min с пропускной способностью модели (см.
    # ``expected_agent_admission_limit``), иначе четыре прогона при одном LLM-слоте
    # означали бы ~32 ожидающих обращения и гарантированную деградацию.
    agent_max_concurrent_runs: int = Field(default=4, ge=1, le=16)
    # Явное повышение потолка приёма через env (AGENT_ADMISSION_LIMIT): перекрывает
    # эвристику, когда оператор знает про тариф или несколько воркеров больше, чем
    # видно из настроек процесса.
    agent_admission_limit: int | None = Field(default=None, ge=1, le=64)
    # Пул потоков, в котором исполняются блокирующие обращения к хранилищу
    # (Neo4j, Elasticsearch, файловый корпус). Обращения уходят через
    # ``asyncio.to_thread``, то есть в default executor цикла, а его размер по
    # умолчанию считается от числа ядер хоста — не от ёмкости контейнера и не от
    # числа прогонов. Узкое место отсюда следует неприятное: один медленный обход
    # графа занимает потоки, и соседний запрос на `/findings` ждёт не хранилище, а
    # освободившийся поток. Значение — потолок одновременных блокирующих чтений,
    # он же размер именованного пула (поток видно в журнале и в трассировке).
    storage_thread_pool_size: int = Field(default=16, ge=2, le=128)

    # Кэш structured output: planning/controller-вызовы детерминированы при
    # temperature=0.1 и попадают на одни и те же секции контекста в повторных
    # вопросах. Нулевой TTL выключает кэш полностью — провайдер тогда ходит в
    # модель на каждый вызов. Инвалидируется явно при записи в корпус.
    llm_cache_ttl_seconds: float = Field(default=900.0, ge=0)
    llm_cache_max_entries: int = Field(default=256, ge=8)
    # Потолок ожидания свободной модели внутри одного вызова: без него ожидание
    # семафора съедает дедлайн молча, и деградация выглядит как медленная модель.
    gigachat_queue_wait_seconds: float = Field(default=60.0, gt=0, le=600)
    # Токен для /metrics. Не задан — эндпопункт открыт (локальный контур), задан
    # — Prometheus обязан присылать Bearer: метрики называют корпуса и режимы,
    # и в публичном контуре они не должны читаться кем угодно.
    metrics_token: str | None = Field(default=None, repr=False)
    embedding_cache_ttl_seconds: float = Field(default=3600.0, ge=0)
    # Пауза после терминального 4xx на /embeddings (402, 401, 403…): без неё каждый
    # новый текст вопроса платит round-trip, хотя приговор аккаунта не меняется.
    # 0 выключает негативный кэш — поведение как до его появления.
    embedding_refusal_cooldown_seconds: float = Field(default=30.0, ge=0, le=3600)
    knowledge_backend: Literal["memory", "neo4j"] = "memory"
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = Field(default="change-me-now", repr=False)
    elasticsearch_url: str = "http://elasticsearch:9200"
    # Redis в контуре = зарезервированное место под очереди задач: приложения,
    # читающего это значение, в кодовой базе нет, а кэш structured output живёт
    # в процессе (settings.llm_cache_ttl_seconds). Настройка отсюда убрана,
    # extra="ignore" ниже по-прежнему принимает старый REDIS_URL в локальных .env.
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
    def expected_agent_admission_limit(self) -> int:
        """Сколько прогонов вывезет модель, не выдавив их за дедлайн.

        Модель принимает ``gigachat_max_concurrent`` обращений одновременно, а один
        прогон делает ``_ADMISSION_CALLS_PER_RUN`` последовательных обращений. За
        время одного дедлайна контур обслуживает ``deadline / seconds_per_call``
        обращений — отсюда и потолок прогонов. Эвристика консервативная: она
        ограничивает приём, а не обещает пропускную способность.
        """
        calls_affordable = self.agent_deadline_seconds / _ADMISSION_SECONDS_PER_CALL
        derived = calls_affordable * max(self.gigachat_max_concurrent, 1) / _ADMISSION_CALLS_PER_RUN
        return max(1, int(derived))

    @property
    def effective_agent_admission_limit(self) -> int:
        """Фактический потолок приёма: запрос оператора ограничен способностью модели.

        Без настроенного GigaChat обращений к модели нет вообще, и ограничивать приём
        эвристикой провайдера смысла нет: остаётся запрошенное значение.
        ``AGENT_ADMISSION_LIMIT`` перекрывает эвристику явно — для нескольких
        воркеров uvicorn, где на процесс приходится часть тарифного лимита, и для
        осознанной переподписки «часть прогонов деградирует по времени».
        """
        if self.agent_admission_limit is not None:
            return self.agent_admission_limit
        if not self.use_gigachat:
            return self.agent_max_concurrent_runs
        return min(self.agent_max_concurrent_runs, self.expected_agent_admission_limit)

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
        window = _GIGACHAT_CONTEXT_WINDOWS.get(self.gigachat_agent_model)
        if window is not None and self.context_token_budget > window - _PROMPT_OVERHEAD_TOKENS:
            logger.warning(
                "CONTEXT_TOKEN_BUDGET=%d превышает оценку окна модели %s (%d токенов): "
                "промпты reasoner/critic будут отклонены моделью; уменьшите бюджет "
                "или укажите модель с большим окном",
                self.context_token_budget,
                self.gigachat_agent_model,
                window,
            )
        return self

    @model_validator(mode="after")
    def warn_admission_above_model_capacity(self) -> "Settings":
        """Конфиг обещает больше прогонов, чем вывезет модель.

        Потолок приёма ниже производного значения — это тихая потеря ёмкости, а
        потолок выше него означает очередь обращений внутри чужого дедлайна и
        неполные ответы вместо честного 429. Предупреждение делает выбор видимым на
        старте; явный ``AGENT_ADMISSION_LIMIT`` считается осознанным решением, а без
        настроенного GigaChat сравнивать ёмкость не с чем — провайдер другой, и
        эвристика к нему неприменима.
        """
        if self.agent_admission_limit is not None or not self.use_gigachat:
            return self
        expected = self.expected_agent_admission_limit
        if self.agent_max_concurrent_runs > expected:
            logger.warning(
                "AGENT_MAX_CONCURRENT_RUNS=%d превышает пропускную способность модели "
                "(%d слот(а) × ~%.0f обращений на прогон => %d): лишние прогоны будут "
                "ждать модель внутри своего дедлайна и деградировать. Уменьшите порог "
                "или поднимите GIGACHAT_MAX_CONCURRENT.",
                self.agent_max_concurrent_runs,
                self.gigachat_max_concurrent,
                _ADMISSION_CALLS_PER_RUN,
                expected,
            )
        return self

    @model_validator(mode="after")
    def warn_silent_memory_fallback(self) -> "Settings":
        """production + memory-бэкенд — это молчаливая потеря данных, а не режим.

        Опечатка в ``KNOWLEDGE_BACKEND`` или недоступный Postgres на старте дают
        контур, который выглядит рабочим, но теряет корпус, сессии и серверные
        копии ответов при перезапуске. Аналитик этого не заметит до первого
        «пропал ответ», поэтому конфигурация предупреждает сама.
        """
        if self.app_env != "production":
            return self
        if self.knowledge_backend == "memory":
            logger.warning(
                "KNOWLEDGE_BACKEND=memory в production: корпус живёт в процессе и "
                "обнуляется перезапуском; для рабочего контура аналитиков нужен neo4j."
            )
        if self.accounts_backend == "memory":
            logger.warning(
                "ACCOUNTS_BACKEND=memory в production: аккаунты и сессии не "
                "переживают перезапуск; нужен postgres."
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
