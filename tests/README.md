# Тесты — `tests/`

Набор тестов платформы «Научный Клубок»: от модульных проверок моделей до интеграционных сценариев рабочего процесса и оценочного контура. Используют pytest + pytest-asyncio + FastAPI TestClient.

---

## Содержание

1. [Организация тестов](#организация-тестов)
2. [Конфигурация и фикстуры](#конфигурация-и-фикстуры)
3. [Описание тестовых файлов](#описание-тестовых-файлов)
4. [Запуск и проверки](#запуск-и-проверки)

---

## Организация тестов

```
tests/
├── conftest.py             # Глобальные настройки окружения для всех тестов
├── fakes.py                # Поддельный LLM-провайдер для детерминированных тестов
├── auth_support.py         # Помощники контура доступа: регистрация/вход, cookie, Origin
├── test_domain_models.py   # Валидация доменных моделей (Claim, Evidence, NumericObservation)
├── test_api.py             # Тесты REST API (здоровье, валидация, деградация)
├── test_auth.py            # Серверная сессия: регистрация, вход, TTL, троттлинг, Origin/CSRF
├── test_api_http.py        # HTTP-контур: ACL по классам данных и правам аккаунта, SSE, экспорт, приём прогонов
├── test_ingestion.py       # Импорт документов и запись в граф
├── test_workflow.py        # Рабочий процесс LangGraph (полный цикл, перепланирование, самообучение)
├── test_agent_reliability.py # Ненадёжные ветки: ревизии, дедлайны, чекпоинтер, ACL ответа
├── test_tools_pool_analysis.py # Конфликты и пробелы по объединённому пулу доказательств
├── test_graphrag_invariants.py # GraphRAG: сообщества, retrieval, классы данных, RRF, бюджеты контекста
├── test_retrieval_persistence.py # Neo4j/ES-контур: восстановление чанков, атомарность импорта, размерность эмбеддингов
├── test_ontology_relations.py # Реестр связей: разрешённые имена, тиры, глубина обхода
├── test_ontology_shapes.py    # SHACL-проверки и один повтор извлечения с текстом нарушения
├── test_benchmark_metrics.py  # Метрики retrieval-бенчмарка: recall против hit@k, идеал NDCG
├── test_orchestrator_fixes.py # Регрессии вне выделенных зон: сравнение, корпус, прелоад, эволюция, метрики
├── test_stage_two.py       # Интеграционные тесты этапа 2 (поиск, слияние сущностей)
└── test_stage_three_four.py # Интеграционные тесты этапов 3–4 (аналитика, доступ, аудит)
```

---

## Конфигурация и фикстуры

### `conftest.py` (7 строк)

Задаёт переменные окружения до загрузки модулей приложения:

| Переменная | Значение | Назначение |
|---|---|---|
| `KNOWLEDGE_BACKEND` | `memory` | InMemory-адаптер вместо Neo4j |
| `GIGACHAT_API_KEY` | (пусто) | Живой LLM-провайдер отключён: `build_provider()` даёт `UnavailableProvider` |

Это обеспечивает полную изоляцию тестов от внешних сервисов.

### `fakes.py` (29 строк)

**`ScriptedProvider`** — детерминированная замена LLM-провайдера:
- Принимает список Pydantic-моделей при создании
- При вызове `complete_model()` возвращает предзаписанную модель по типу запрашиваемой схемы
- Фиксирует порядок вызовов в `self.calls` для проверки последовательности агентов
- Позволяет тестировать полный рабочий процесс без реальных LLM-вызовов
- Отвечает `mode = "scripted"`, который попадает в `model_mode` ответа; режим живого GigaChat (`gigachat`) при этом не подменяется

---

## Описание тестовых файлов

### `test_domain_models.py` (58 строк)

Проверяет Pydantic-валидацию доменных моделей.

| Тест | Что проверяет |
|---|---|
| `test_claim_requires_exactly_one_object_representation` | Утверждение (Claim) должно иметь ровно одно представление объекта: `object_id` или `literal_value`, но не оба и не ни одного |
| `test_numeric_range_preserves_source_and_normalized_units` | Числовое наблюдение с оператором `between` сохраняет и исходные, и нормализованные значения с единицами измерения |
| `test_evidence_rejects_invalid_offsets` | Доказательство (EvidenceLocator) отклоняет некорректные символьные смещения (`char_end ≤ char_start`) |

### `test_api.py` (28 строк)

Проверяет HTTP-эндпоинты через FastAPI TestClient.

| Тест | Что проверяет |
|---|---|
| `test_liveness` | Эндпоинт `/health/live` возвращает `200 OK` с `{"status": "ok"}` |
| `test_query_plan_rejects_unbounded_graph_depth` | Валидация плана запроса отклоняет `max_hops=10` (лимит — 4), возвращает `422` |
| `test_live_query_requires_configured_model` | Демо-запрос `/api/v1/demo` возвращает `503` без настроенного LLM-провайдера |

### `test_api_http.py` (HTTP-контур поверх реального ASGI)

Проверяет то, что видно только через транспорт: права из заголовков, форму SSE-стрима и
сохранение экспертного решения при недоступной модели.

| Тест | Что проверяет |
|---|---|
| `test_query_over_http_returns_grounded_answer_and_evaluation` | `POST /api/v1/query` отдаёт ответ с `model_mode`, цитатами и оценкой `citation_coverage = 1` |
| `test_stream_completes_with_the_same_answer_shape` | `POST /api/v1/query/stream`: `text/event-stream`, порядок событий `start → step* → answer → done`, тот же ответ, что и в JSON-пути, и ни одного второго прогона |
| `test_restricted_findings_are_hidden_per_role` | Одна и та же находка видна `project_manager` и отсутствует у `researcher`; в `/api/v1/graph` нет ни закрытых узлов, ни висячих рёбер |
| `test_expert_supersede_requires_restricted_permission` | Замена утверждения без права `restricted:read` → `403`; с правом — новая версия в истории, а закрытый тезис по-прежнему скрыт от исследователя |
| `test_expert_correction_survives_unavailable_model` | Без живого LLM правка эксперта всё равно записана (`superseded`), `proposal` пустой, причина зафиксирована в `degradation_reasons` |
| `test_audit_trail_records_real_user_id` | Аудит пишет действие и субъекта из серверной сессии, `/api/v1/audit` доступен только с `audit:read` |
| `test_client_supplied_identity_headers_are_ignored` | Заголовки `X-User-Role` / `X-User-Id` от клиента не дают никаких прав: закрытые данные скрыты, аудит — `403` |

### `test_ingestion.py` (84 строки)

Проверяет конвейер импорта документов.

| Тест | Что проверяет |
|---|---|
| `test_ingestion_writes_extracted_claim_and_provenance_to_graph` | Полный цикл импорта: LLM-извлечение → запись в граф → доказательные связи. Проверяет: создана 1 утверждение, узел документа присутствует, статус `extracted`, связь `SUPPORTED_BY`, источник доказательства совпадает |
| `test_json_and_xlsx_parsers_preserve_source_fragments` | Парсеры JSON и XLSX сохраняют структурные фрагменты: JSON — страница 1, XLSX — название листа и диапазон ячеек |

### `test_workflow.py` (189 строк)

Проверяет полный рабочий процесс LangGraph с `ScriptedProvider`.

| Тест | Что проверяет |
|---|---|
| `test_llm_driven_workflow_reaches_grounded_answer` | **Основной сценарий:** планирование → исполнение → контроль → рассуждение → критик → завершение. Проверяет трассировку из шести записей в правильном порядке (`planning_agent`, `tool_executor`, `controller`, `reasoner`, `critic`, `synthesizer`), две находки в ответе, четыре вызова LLM (PlanningBundle, AgentControlDecision, ReasoningResult, CritiqueResult) и режим `scripted` |
| `test_controller_autonomously_replans_missing_evidence` | **Перепланирование:** контроллер решает добрать доказательства → второй обход инструментов без участия пользователя. Проверяет полный порядок трассировки (один `action_planner`, два `tool_executor`, два `controller`) и четыре наблюдения инструментов |
| `test_ingestion_and_self_evolve_are_model_driven` | **Импорт + самообучение:** LLM управляет извлечением и эволюцией. Проверяет: импорт создаёт документ, обратная связь генерирует предложение типа `gold_case`, вызовы LLM — IngestionBundle и EvolutionDraft |

### `test_agent_reliability.py` (ненадёжные ветки рабочего процесса)

Проверяет то, что не видно на зелёном пути: лимиты, отказ сервисов и права на готовом ответе.

| Тест | Что проверяет |
|---|---|
| `test_critic_rejection_runs_improver_and_second_critique` | Отвергнутый критикой ответ уходит в `improver` и возвращается на вторую проверку критиком; порядок вызовов LLM соответствует циклу |
| `test_revision_loop_is_capped_by_budget` | `AGENT_MAX_REVISIONS` ограничивает цикл ревизий: один `improver`, два `critic`, ответ наружу, а не бесконечный цикл |
| `test_tool_round_budget_forces_reasoning` | `AGENT_MAX_TOOL_ROUNDS`: контроллер не вправе зациклить инструменты — по исчерпании лимита решение перебивается на `reason` и помечается `revised` |
| `test_deadline_salvages_evidence_instead_of_hanging` | `AGENT_DEADLINE_SECONDS`: прерванный по времени прогон отдаёт уже собранные находки, наблюдения и граф с явной причиной деградации |
| `test_failed_node_degrades_with_explicit_reason` | Ошибка узла превращается в частичный ответ с указанием узла, а не в «успешный» пустой ответ |
| `test_missing_provider_surfaces_as_unavailable_not_empty_answer` | Недоступная модель поднимается как `ModelUnavailableError` (в API — `503`), а не как пустой ответ |
| `test_checkpointer_does_not_leak_state_between_runs` | Два прогона в одном `thread_id` не наследуют трассировку, находки и наблюдения друг у друга: чекпоинтер скоупится на уровень запуска |
| `test_stream_reports_the_same_nodes_as_run` | SSE-стрим отдаёт те же узлы, что и синхронный прогон, — второй прогон рабочего процесса не запускается |
| `test_acl_cut_hides_restricted_layers_from_the_answer` | `apply_acl` срезает закрытые классы данных из находок, графа (без висячих рёбер), конфликтов и пробелов и добавляет явную причину деградации |

### `test_graphrag_invariants.py` (инварианты GraphRAG-контура)

| Тест | Что проверяет |
|---|---|
| `test_detect_communities_splits_two_dense_cliques` | Сообщества считаются по графу (Leiden/Louvain/greedy), а не берутся из seed-строк: две плотные клики разбираются в разные сообщества |
| `test_detect_communities_is_deterministic` | Разные запуски на одинаковом графе дают одинаковые метки сообществ |
| `test_isolated_nodes_get_a_single_fallback_community` | Рёбер нет — одно резервное сообщество, и оно честно названо резервным |
| `test_memory_backend_returns_computed_communities` | `full_graph` отдаёт вычисленные сообщества и проставленный `metadata.community` (порядок копирования не теряет разметку) |
| `test_retrieval_reports_no_evidence_instead_of_seeding` | Запрос мимо корпуса возвращает пустые находки и `no_evidence=True`, а не сиды «для правдоподобия» |
| `test_data_class_propagates_from_document_to_findings_and_graph` | `data_class` из `DocumentRequest` доезжает до находок, узлов и рёбер; ACL-срез не оставляет висячих рёбер |
| `test_restricted_findings_are_cut_from_ranking` | Ранжирование не отдаёт закрытые находки ограниченной роли |
| `test_tool_plan_inherits_global_mode_and_hop_limit` / `test_local_mode_keeps_graph_and_drops_global_context` | `mode` и `max_hops` плана запроса реально управляют retrieval-планом |
| `test_rrf_*` | Взвешенное RRF-слияние веток: совпадение веток вознаграждается, нулевой вклад не обгоняет доказательства |
| `test_two_observations_of_one_finding_do_not_self_conflict` | Два числовых наблюдения одного тезиса имеют разные идентичности и помечаются как противоречие внутри одного источника, а не «X против X» |
| `test_qualitative_finding_produces_no_numeric_claim` | Тезис без наблюдений и без доказательств не порождает чисел и не превращается в диапазон `[0, 0]` |
| `test_gap_summary_is_capped_and_reports_omitted` | Пробелы ограничены лимитом, `omitted` честно считает отсечённые комбинации |
| `test_russian_tokens_are_not_underestimated` | Оценка токенов для русского текста не делит длину на четыре |
| `test_fit_sections_*` | При переполнении бюджета отбрасываются нижние секции, а не хвост с доказательствами; усечение возможно только для единственной секции |
| `test_select_relevant_prefers_match_over_arrival_order` | В контекст попадают релевантные находки, а не первые по порядку поступления |
| `test_embeddings_are_batched_and_keep_order` | Эмбеддинги GigaChat собираются в батчи без потери порядка |
| `test_dimension_reconciliation_does_not_deadlock` | Сверка размерности не перезахватывает незащищённый замок (иначе индексация виснет) |
| `test_embedding_failure_raises_instead_of_returning_garbage` | Сбой эмбеддингов поднимается как `EmbeddingError` — векторная ветка деградирует явно |

### `test_stage_two.py` (35 строк)

Интеграционные тесты для поискового контура и слияния сущностей.

| Тест | Что проверяет |
|---|---|
| `test_multilingual_gold_retrieval_recall` | Бенчмарк поиска на 10 эталонных случаях при `top_k=3`: инварианты методики отделены от качества, на in-memory-корпусе ни один кейс не засчитывается (`scored_cases=0`), и именно это — причина непрохождения, а не заведомо невыполнимое условие |
| `test_entity_merge_is_reviewable_and_reversible` | Верстак слияния сущностей: регистрация предложения → проверка (accept) → откат (revert). Проверяет полный жизненный цикл предложения |

### `test_stage_three_four.py` (123 строки)

Интеграционные тесты аналитического слоя, контроля доступа и журнала аудита.

| Тест | Что проверяет |
|---|---|
| `test_conflicts_require_compatible_conditions_and_disjoint_ranges` | Обнаружение конфликтов: два утверждения с непересекающимися диапазонами при совместимых условиях → конфликт-кандидат. Утверждения с разными географическими областями не конфликтуют |
| `test_gap_analysis_uses_explicit_research_space` | Обнаружение пробелов: при заданном research space (география × тип воды) система находит непокрытые комбинации |
| `test_acl_filters_restricted_documents_before_retrieval` | Предпоисковая фильтрация ACL: аккаунт без экспертного права видит только публичные и внутренние ресурсы, закрытые отфильтрованы до поиска |
| `test_audit_log_returns_copies_and_filters_by_correlation` | Журнал аудита: возвращает глубокие копии (мутация не влияет на оригинал), поддерживает фильтрацию по `correlation_id` |
| `test_audit_log_bounds_its_own_memory` | Журнал аудита ограничен ёмкостью: старые события вытесняются, память процесса не растёт неограниченно |

---

## Запуск и проверки

### Запуск всех тестов

```bash
python -m pytest
```

### Запуск с подробным выводом

```bash
python -m pytest -v
```

### Запуск отдельного файла

```bash
python -m pytest tests/test_workflow.py -v
```

### Асинхронные тесты

Тесты с декоратором `@pytest.mark.asyncio` используют `pytest-asyncio` для запуска асинхронных функций. Все тесты рабочего процесса (`test_workflow.py`) и конвейера (`test_stage_three_four.py`) — асинхронные.

### Линтер и типизация

```bash
python -m ruff check .    # Линтер (стиль, ошибки)
python -m mypy src         # Статическая проверка типов
```

### Принципы тестирования

1. **Без внешних зависимостей** — тесты не требуют Neo4j, Elasticsearch, LLM API. Все зависимости подменяются in-memory реализациями.
2. **Детерминированность** — `ScriptedProvider` возвращает предсказуемые результаты, тесты воспроизводимы.
3. **Изоляция** — `conftest.py` устанавливает окружение до импорта модулей, предотвращая случайные подключения.
4. **Проверка контрактов** — доменные модели тестируются через валидацию Pydantic (негативные сценарии).
5. **Покрытие сценариев** — тесты покрывают полный путь: импорт → граф → поиск → ответ → обратная связь → эволюция.
