# StormIdea

StormIdea — рабочее пространство для поиска и проверки гипотез, выявления
узких мест и изучения связей между фактами и материалами. Продукт применим к
разным предметным областям: выводы строятся по данным конкретного пространства.

## Возможности

- Диалог с ИИ по материалам пространства.
- Проверка выводов по источникам и фрагментам документов.
- Поиск гипотез, числовых сигналов, противоречий и пробелов.
- Просмотр связей и переход к первоисточнику.
- Загрузка материалов и экспорт ответа.

Основные маршруты интерфейса: `/research`, `/findings`, `/graph`;
настройки аккаунта находятся в `/account`. В `/findings` два вида находок
переключаются фасетами: аналитические гипотезы (по умолчанию) и находки корпуса
(`?facet=findings`), рядом числа, пробелы и склейки названий
(`?facet=numbers`, `?facet=gaps`, `?facet=merges`).

## Архитектура

- Backend: Python 3.12, FastAPI и LangGraph в `src/scientific_tangle/`.
- Frontend: SvelteKit 2, Svelte 5 и TypeScript в `frontend/`.
- Поиск и связи: Elasticsearch и Neo4j.
- Аккаунты и сохранённое состояние: PostgreSQL.
- LLM и эмбеддинги: GigaChat.

Пользовательский доступ проверяется серверной сессией. Источники ответа,
доступные аккаунту, остаются связаны с выводами и их локаторами.

## Локальный запуск

1. Создайте `.env` из `.env.example` и укажите `GIGACHAT_API_KEY`.
2. Запустите сервисы:

   ```bash
   docker compose up -d --build --wait
   ```

3. При необходимости проиндексируйте свои документы из `Источники информации/`:

   ```bash
   docker compose --profile tools run --rm preload
   ```

4. Откройте frontend по адресу `http://localhost:43119`.

Без ключа GigaChat приложение запускается, но ответы, зависящие от модели,
недоступны. Порты можно изменить через `.env`; исходные значения заданы в
`compose.yaml`.

## Разработка и проверки

Backend:

```bash
python -m pip install -e ".[dev]"
python -m uvicorn scientific_tangle.api.app:app --reload --port 46617
```

Frontend:

```bash
cd frontend
npm ci
npm run dev
```

Проверки:

```bash
python -m ruff check .
python -m mypy src
python -m pytest
cd frontend && npm run check && npm run build
docker compose config --quiet
```

## Структура

```text
src/scientific_tangle/   backend, API, workflow и хранилища
frontend/                маршруты, компоненты и стили интерфейса
tests/                   backend-проверки
ops/                     конфигурация Prometheus и Grafana
compose.yaml              локальный контур сервисов
```

Подробнее: [backend](src/README.md), [frontend](frontend/README.md),
[тесты](tests/README.md).
