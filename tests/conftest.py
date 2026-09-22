"""Тестовый контур поднимается без внешней инфраструктуры.

Порядок важен: ``Settings`` читает окружение при первом импорте приложения и
кешируется (``lru_cache``), поэтому переменные задаются до ``import
scientific_tangle``.
"""

import os

os.environ["KNOWLEDGE_BACKEND"] = "memory"
# Учётные записи и сессии — в памяти: иначе адаптер полез бы в DATABASE_URL из
# compose-контура, и тесты ждали бы подключения к Postgres.
os.environ["ACCOUNTS_BACKEND"] = "memory"
os.environ["GIGACHAT_API_KEY"] = ""
# Файл корневого сертификата в тестях не читается: иначе поведение TLS-контекста
# зависело бы от локального .env разработчика.
os.environ["GIGACHAT_TRUSTED_ROOTS"] = ""
