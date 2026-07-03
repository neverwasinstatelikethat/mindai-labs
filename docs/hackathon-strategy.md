# Стратегия среди 170 команд

## Позиционирование

Не «ещё один RAG», а **верифицируемый цифровой исследователь**, который понимает условия эксперимента, не искажает числа и показывает, где наука спорит или молчит.

## Четыре доказательства преимущества

1. **Baseline battle:** один запрос к vector RAG и к «Научному Клубку»; показать потерянные numeric/geography constraints у baseline.
2. **Evidence zoom:** клик от вывода до Claim, graph path и конкретной страницы/ячейки.
3. **Contradiction/gap intelligence:** не пересказ документов, а обнаружение несовместимых режимов и непокрытой комбинации условий.
4. **Learning in public:** эксперт отклоняет Claim; trusted graph, ответ и evaluation score обновляются.

## Showcase scope

В интерфейсе должны быть реально связаны:

- живой ingestion timeline с агентами и quality gates;
- research chat + filters;
- comparison table;
- evidence graph;
- conflicts/gaps;
- expert review;
- coverage dashboard и alert preview;
- evaluation panel с baseline comparison.

## Очерёдность реализации

### P0 — сквозная доказательность

Ontology core, 10–20 качественных документов, extraction в claims/observations/evidence, hybrid retrieval, один безупречный multi-hop answer.

### P1 — конкурсные дифференциаторы

Conflict/gap detection, community summaries, baseline comparison, expert feedback loop, graph visualization.

### P2 — ширина продукта

Dashboard, alerts, exports, RBAC demo personas, второй и третий demo query.

### P3 — production credibility

Load benchmark, monitoring dashboard, deployment diagram/Kubernetes manifests, security policy and failure demo.

## Демонстрация на 4–5 минут

- 0:00–0:30 — боль и сложный вопрос;
- 0:30–1:10 — ingestion и построение evidence graph;
- 1:10–2:15 — ответ, filters, comparison и citations;
- 2:15–3:00 — conflict/gap и evidence graph;
- 3:00–3:35 — expert correction/self-improvement;
- 3:35–4:10 — baseline/evaluation и dashboard;
- 4:10–4:30 — эффект: быстрее обзор, меньше повторов, безопаснее решение.

## Что не сработает

- список технологий без работающего перехода между ними;
- граф-«волосатый шар»;
- уверенные рекомендации без evidence;
- заранее записанные статические результаты, не реагирующие на фильтры;
- недоказанные 1M entities / 3 seconds;
- восемь LLM-персон, которые только передают текст друг другу.

