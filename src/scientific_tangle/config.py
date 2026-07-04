from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"
    yandex_api_key: str | None = Field(default=None, repr=False)
    folder_id: str | None = None
    yandex_base_url: str = "https://ai.api.cloud.yandex.net/v1"
    yandex_model_id: str = "yandexgpt"
    yandex_model_version: str = "latest"
    yandex_model_uri: str | None = None
    yandex_timeout_seconds: float = 90
    # Макс. одновременных запросов к Yandex (free-tier: 10 concurrent, запас = 8)
    yandex_max_concurrent: int = 8
    yandex_embeddings_url: str = (
        "https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding"
    )
    embedding_dimensions: int = 256

    # GigaChat fallback LLM
    # Individual tier (GIGACHAT_API_PERS) — только 1 concurrent thread
    gigachat_max_concurrent: int = 1
    gigachat_api_key: str | None = Field(default=None, repr=False)
    gigachat_base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"
    gigachat_model: str = "GigaChat"
    gigachat_verify_ssl_certs: bool = False
    # Область API: GIGACHAT_API_PERS (физлица), GIGACHAT_API_B2B или GIGACHAT_API_CORP
    gigachat_scope: str = "GIGACHAT_API_PERS"

    model_mode: Literal["auto", "yandex", "gigachat", "fallback"] = "auto"
    knowledge_backend: Literal["memory", "neo4j"] = "memory"
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = Field(default="change-me-now", repr=False)
    elasticsearch_url: str = "http://elasticsearch:9200"
    redis_url: str = "redis://redis:6379/0"
    database_url: str = Field(
        default="postgresql://mindai:change-me-now@postgres:5432/mindai",
        repr=False,
    )
    source_root: str = "/data/sources"
    corpus_preload_limit: int = 900
    corpus_max_file_bytes: int = 20 * 1024 * 1024
    preload_mode: Literal["full", "structural", "semantic"] = "full"

    @property
    def use_yandex(self) -> bool:
        """Yandex LLM доступен (ключи настроены и режим не исключает Yandex)."""
        if self.model_mode == "gigachat":
            return False
        return bool(self.yandex_api_key and self.folder_id)

    @property
    def use_gigachat(self) -> bool:
        """GigaChat доступен (ключ настроен и режим не исключает GigaChat)."""
        if self.model_mode == "yandex":
            return False
        return bool(self.gigachat_api_key)

    @property
    def resolved_yandex_model_uri(self) -> str:
        if self.yandex_model_uri:
            return self.yandex_model_uri
        if not self.folder_id:
            raise ValueError("FOLDER_ID обязателен, если YANDEX_MODEL_URI не задан")
        return f"gpt://{self.folder_id}/{self.yandex_model_id}/{self.yandex_model_version}"

    def yandex_embedding_uri(self, kind: Literal["doc", "query"]) -> str:
        if not self.folder_id:
            raise ValueError("FOLDER_ID обязателен для embeddings")
        model = "text-search-doc" if kind == "doc" else "text-search-query"
        return f"emb://{self.folder_id}/{model}/latest"

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
