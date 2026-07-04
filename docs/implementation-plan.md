# План реализации «Научного Клубка»

## Целевой результат

Работающий конкурсный продукт MindAI, который на реальном RU/EN корпусе строит evidence-centric knowledge graph и превосходит baseline vector RAG в multi-hop вопросах с числовыми, географическими и временными ограничениями.

## Definition of victory

- один безупречный end-to-end demo и ещё два устойчивых сценария;
- любой ключевой тезис раскрывается до source span и graph path;
- числа и единицы проходят deterministic validation;
- система находит минимум один реальный conflict и один обоснованный gap;
- expert feedback меняет trusted graph и повторный ответ;
- на экране evaluation видна разница с baseline RAG;
- проект воспроизводимо запускается и имеет fallback demo seed.

## Потоки работ

### W1. Data foundation и ontology

**Результат:** воспроизводимый корпус, ontology v1 и gold set.

- Инвентаризировать материалы кейса и лицензии/классы доступа.
- Выбрать 10–20 demo-grade документов вокруг одной технологической темы.
- Описать canonical entities, Claim, Observation, Evidence, Review и aliases.
- Создать SHACL/JSON Schema validation и Neo4j constraints.
- Разметить gold set: entities, relations, claims, числа, конфликты и 10 вопросов.

**DoD:** seed загружается повторно без дублей; gold set проходит schema validation.

### W2. Ingestion и extraction

**Результат:** PDF/DOCX/XLSX/JSON превращаются в evidence-bearing candidates.

- Реализовать document registry, checksum и job status.
- Подключить structural parsing/OCR с page/sheet/cell locators.
- Реализовать Extractor agent со строгим JSON output.
- Добавить deterministic unit/range/date parsing.
- Сохранять model/prompt/parser version и partial failures.

**DoD:** критические demo-числа извлечены без ошибок; source span открывается из Claim.

### W3. Entity resolution и graph build

**Результат:** RU/EN aliases и claims записаны в Neo4j без потери provenance.

- Dictionary-first canonicalization + embedding candidates.
- Resolver выдаёт merge proposal, а не необратимое слияние.
- Атомарная запись Claim–Evidence–Observation.
- Версии через `SUPERSEDES`; proposed/accepted/rejected states.
- Community detection и generation summaries для GraphRAG global search.

**DoD:** bilingual aliases находят одну сущность; merge/review аудируемы.

### W4. Hybrid retrieval и agent workflow

**Результат:** типизированный multi-hop query pipeline.

- Query planner: entities, ranges, geography, years, comparison intent.
- Elasticsearch BM25/facets + Neo4j vector candidates.
- Reciprocal Rank Fusion и reranker.
- Allowlisted Cypher traversal 2–4 hops с path budget.
- LangGraph state machine: planner → parallel retrieval → reasoner → critic.
- Redis cache для plans, retrieval results и community summaries.

**DoD:** все demo queries возвращают нужное evidence в top-k; недоступные данные отфильтрованы до synthesis.

### W5. Answer intelligence

**Результат:** проверяемая карточка исследовательского ответа.

- Structured synthesis: summary, scope, comparison, evidence, conflicts, gaps.
- Citation coverage validator для каждого содержательного тезиса.
- Numeric verifier против Observation.
- Conflict detector с проверкой совместимости условий.
- Gap detector относительно явно заданного research space.
- Recommender отделяет verified findings от hypotheses.

**DoD:** unsupported claim rate на gold queries равен нулю; все числа имеют evidence.

### W6. Product experience MindAI

**Результат:** один связный full-interaction demo flow.

- Research workspace: question composer, filters, saved demo prompts.
- Answer canvas: findings, comparison, consensus/disagreements, gaps.
- Evidence explorer: source viewer + query-scoped graph.
- Ingestion timeline с агентами и quality gates.
- Expert review console.
- R&D dashboard, alerts preview, exports и evaluation/baseline panel.
- Loading, partial, error, empty и permission states.

**DoD:** сценарий выполняется без перехода в Neo4j Browser или терминал.

### W7. Security, observability и deployment

**Результат:** технически убедительный enterprise-контур.

- Data classes и пять ролей; pre-retrieval ACL.
- Audit log для query/view/review/export.
- Provider policy для external/on-prem LLM.
- OpenTelemetry traces; Prometheus/Grafana dashboard.
- Docker Compose one-command demo; Kubernetes/Helm production profile.
- Backup/restore notes и failure-mode demo.

**DoD:** readiness отражает зависимости; trace показывает latency каждого agent node.

### W8. Evaluation и self-improvement

**Результат:** измеримое преимущество, а не декларация.

- Baseline vector RAG на том же corpus/questions.
- Extraction F1, numeric exact match, resolution F1.
- Retrieval recall@k/precision@k.
- Citation correctness/coverage и unsupported claim rate.
- p50/p95/cost per query.
- Feedback → EvaluationCase → regression run → rule/prompt proposal.

**DoD:** comparison panel воспроизводит результаты из versioned evaluation run.

## Этапы и зависимости

| Этап | Содержание | Зависит от | Выход |
|---|---|---|---|
| E0 Foundation | repo, CI, contracts, Compose, seed layout | — | воспроизводимая среда |
| E1 Evidence slice | W1 + W2 + Neo4j write | E0 | документ → Claim → source |
| E2 Answer slice | W3 + W4 + basic W5 | E1 | вопрос → grounded answer |
| E3 Differentiators | conflict/gap, communities, feedback | E2 | конкурсное преимущество |
| E4 Product | утверждённая MindAI identity + W6 | E2 | полный demo flow |
| E5 Proof | W7 + W8 + baseline | E2–E4 | метрики и production story |
| E6 Submission | QA, video, deck, deploy, archive | E5 | комплект для жюри |

## Первые задачи в backlog

| Priority | Задача | Проверяемый выход |
|---|---|---|
| P0 | Получить и каталогизировать case corpus | manifest + data classes |
| P0 | Выбрать тему и три demo queries | утверждённый demo script |
| P0 | Закрепить ontology v1 | schema + constraints + examples |
| P0 | Реализовать document registry/parser | источник с page/sheet locators |
| P0 | Реализовать LLM-driven extraction contract | claims/observations/evidence JSON |
| P0 | Записать seed graph | идемпотентный import |
| P0 | Реализовать first hybrid query | evidence top-k |
| P0 | Собрать grounded answer | zero unsupported claims на query #1 |
| P1 | Baseline RAG comparison | versioned evaluation run |
| P1 | Conflict и gap detectors | validated examples |
| P1 | Expert review loop | trusted answer changes after review |
| P1 | MindAI product flow | выбранный visual direction + prototype |
| P2 | Dashboard, alerts, exports, RBAC personas | связанные live modules |
| P2 | Monitoring и load profile | Grafana + benchmark report |

## Quality gates

- **G1 Data:** source locator присутствует у каждого published Claim.
- **G2 Numeric:** exact match значения/operator/unit после normalization.
- **G3 Retrieval:** gold evidence найдено в top-k.
- **G4 Answer:** citation coverage 100% для ключевых тезисов.
- **G5 Security:** ACL применяется до retrieval.
- **G6 UX:** пользователь проходит demo без технических инструментов.
- **G7 Demo:** cold-start и prerecorded fallback проверены на отдельной машине.

## Организация GitLab

- Protected branch: `main`; короткоживущие feature branches.
- Merge request требует зелёных lint/type/test jobs.
- Issues маркируются `P0/P1/P2`, workstream и demo impact.
- Milestones: Evidence Slice, Answer Slice, Differentiators, Submission.
- CI: Ruff → mypy → pytest → image build → integration/eval по отдельному trigger.
- Dataset и секреты не хранятся в Git; в репозитории только manifest, synthetic fixtures и download instructions.

## Главные риски

| Риск | Ранний сигнал | Действие |
|---|---|---|
| Корпус слишком широкий | нет стабильного query #1 | сузить тему, не продуктовые возможности |
| Ошибки таблиц/OCR | числа теряют source locator | table-first parser и ручной gold slice |
| Agent latency | p95 превышает demo budget | parallel retrieval, cache, bounded critic loop |
| UI опережает данные | статические dashboard/graph | contract fixtures из реального schema |
| Много технологий без интеграции | ручные переходы в demo | единый correlation ID и end-to-end flow |
