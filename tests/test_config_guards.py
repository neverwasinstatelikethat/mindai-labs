"""Защита от молчаливой деградации конфигурации в production-контуре."""

import logging

from scientific_tangle.config import Settings


def _settings(**overrides: object) -> Settings:
    # _env_file=None: иначе локальный .env подмешивается в тест и результат
    # зависит от машины, на которой он запущен.
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_production_with_memory_backends_warns(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="scientific_tangle.config"):
        _settings(
            app_env="production",
            knowledge_backend="memory",
            accounts_backend="memory",
        )
    text = caplog.text
    assert "KNOWLEDGE_BACKEND=memory" in text
    assert "ACCOUNTS_BACKEND=memory" in text


def test_development_with_memory_backends_stays_quiet(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="scientific_tangle.config"):
        _settings(
            app_env="development",
            knowledge_backend="memory",
            accounts_backend="memory",
        )
    assert "не переживают перезапуск" not in caplog.text


def test_production_with_working_backends_stays_quiet(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="scientific_tangle.config"):
        _settings(
            app_env="production",
            knowledge_backend="neo4j",
            accounts_backend="postgres",
        )
    assert caplog.text == ""
