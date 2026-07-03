# Требования и трассировка

Источник: `C:\Users\hehehe\Desktop\task.txt`. Формулировки нормализованы в проверяемые требования.

## Функциональные требования

| ID | Требование | Acceptance criterion | Компонент |
|---|---|---|---|
| FT-01 | Импорт RU/EN статей, отчётов, патентов, нормативов, справочников | PDF/DOCX/XLSX/JSON регистрируются, имеют checksum и статус | ingestion/parsing |
| FT-02 | OCR и структурный parsing | сохраняются страницы/листы, таблицы, offsets и ошибки | parsing/object storage |
| FT-03 | Извлечение сущностей и отношений | schema-valid candidates с evidence span | Extractor |
| FT-04 | Извлечение чисел, диапазонов и условий | operator/value/range/unit/context + normalization | Extractor/Validator |
| FT-05 | RU/EN синонимы и термины | aliases сводятся к canonical entity, merge аудируем | Resolver |
| FT-06 | Доменная онтология | поддержаны типы/relations из ТЗ и их расширение | ontology/Neo4j |
| FT-07 | Сложные graph traversals | параметризованный поиск путей 2–4 hops | Retriever/Neo4j |
| FT-08 | Версионирование фактов | новая версия сохраняет историю и `SUPERSEDES` | knowledge/review |
| FT-09 | Natural-language search | QueryPlan выделяет intent/entities/filters | Planner |
| FT-10 | Фильтры process/material/geography/year/confidence | фильтры применены до synthesis и видимы в ответе | ES/Neo4j/UI |
| FT-11 | Числовые фильтры | корректные границы и conversion единиц | Retriever |
| FT-12 | Сравнительные запросы | единая таблица вариантов с одинаковыми dimensions | Compare service |
| FT-13 | Evidence graph | релевантная цепочка раскрывает Claim и источник | Graph API/UI |
| FT-14 | Подсветка conflicts | несовместимые claims показаны с условиями и evidence | Conflict detector |
| FT-15 | Выявление gaps | gap связан с заданным search space и coverage | Gap detector |
| FT-16 | Поиск экспертов/лабораторий | ranking объясняется публикациями/экспериментами | Graph reasoner |
| FT-17 | Структурированный обзор | группировка по методу/году/географии/детальности | Answering |
| FT-18 | Consensus/disagreements | source count, confidence breakdown и citations | Critic/Answering |
| FT-19 | Рекомендации похожих кейсов/тем | hypotheses отделены от verified claims | Recommender |
| FT-20 | RBAC для пяти ролей | restricted evidence отсутствует до retrieval | Policy layer |
| FT-21 | Audit запросов, просмотров, review, export | actor/action/object/time/policy result | Audit log |
| FT-22 | Expert graph correction | accept/reject/edit с автором, датой и причиной | Review console |
| FT-23 | Экспорт PDF/Markdown/JSON-LD | citations и access policy сохраняются | Exporter |
| FT-24 | Уведомления | подписка получает новые релевантные accepted claims | Alerts |
| FT-25 | Executive dashboards | coverage/conflict/gap/activity из live graph | Analytics UI |
| FT-26 | Comparative technology workspace | эффективность/CAPEX/климат/экология в одной схеме | Compare UI |
| FT-27 | Расширение источников/типов/доменов | новый adapter/type не меняет query API | ports/adapters |
| FT-28 | Feedback/self-improvement | review превращается в eval case/rule proposal | Improver |

## Нефункциональные требования

| ID | Требование | Проверка |
|---|---|---|
| NFT-01 | Понятный UX без знания Cypher | usability test: вопрос → evidence без обучения |
| NFT-02 | 3–5 с при 1M сущностей и 3–4 hops | отдельный benchmark с dataset/hardware/p95; synthesis измеряется отдельно |
| NFT-03 | Fidelity терминов, чисел и единиц | gold set: exact match + F1; zero tolerance на demo critical numbers |
| NFT-04 | Надёжный разнородный импорт | partial failures, retries, idempotency, diagnostics |
| NFT-05 | Модульная расширяемость | contract tests для source/model/storage adapters |
| NFT-06 | RU/EN | bilingual gold questions и aliases |
| NFT-07 | Traceability/FAIR | persistent IDs, metadata, provenance, machine-readable export |
| NFT-08 | Безопасность/on-prem | data classes, pre-retrieval ACL, provider policy, audit |
| NFT-09 | Масштаб до 1M+ entities | load profile, indexes, bounded traversal, cache strategy |
| NFT-10 | Наблюдаемость | traces по стадиям, p50/p95, error/token/cost metrics |

## Definition of Done для каждого FT

Требование не считается реализованным только по наличию экрана или функции. Нужны работающий контракт, реалистичные данные, автоматическая проверка основного сценария и наблюдаемый результат в demo seed.

