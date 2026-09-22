from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from scientific_tangle.domain.contracts import (
    AgentActionPlan,
    Finding,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    RetrievalPlan,
    ToolAction,
    ToolObservation,
)
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.domain.relations import resolve_relations
from scientific_tangle.services.knowledge import KnowledgeBase, RetrievalContext
from scientific_tangle.services.research_intelligence import (
    DEFAULT_GAP_LIMIT,
    MAX_VALUES_PER_DIMENSION,
    ResearchIntelligenceService,
    build_research_space,
    to_research_claims,
)

logger = logging.getLogger(__name__)

# Описание в observation уходит Reasoner-у в контекст: полного списка конфликтов
# на большом пуле там быть не должно, поэтому факт срезается, а не молча теряется.
MAX_CONFLICT_FACTS = 8

# Строка среза — единственное место, где «не показано N» уезжает в ответ вместе со
# списком фактов; читается и человеком, и ``_omitted_count``.
OMITTED_NOTE_PREFIX = "Не показано"


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    observations: list[ToolObservation]
    findings: list[Finding]
    graph: GraphSnapshot
    community_summaries: list[str]
    conflicts: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    degradation_reasons: list[str] = field(default_factory=list)
    gaps_omitted: int = 0


@dataclass(frozen=True, slots=True)
class _ActionOutcome:
    action: ToolAction
    context: RetrievalContext | None
    error: Exception | None
    rejected_relations: tuple[str, ...] = ()


Observer = Callable[[ToolAction, QueryPlan, RetrievalContext, Sequence[Finding]], ToolObservation]


class ResearchToolExecutor:
    """Безопасный action space: LLM выбирает tools, код ограничивает их исполнение.

    Действия независимы, поэтому retrieval выполняется параллельно в worker-потоках:
    синхронные драйверы Neo4j/Elasticsearch не должны блокировать event loop FastAPI.

    ``conflict_scan`` и ``gap_scan`` считаются по объединённому пулу доказатель всего
    запроса (``prior_findings`` плюс найденное в этом раунде), а не по top-6 одного
    действия: конфликт — это пара, и обе половины должны попасть в один анализ.
    """

    def __init__(self, knowledge: KnowledgeBase) -> None:
        self._knowledge = knowledge
        self._intelligence = ResearchIntelligenceService()
        self._registry: dict[str, Observer] = {
            "hybrid_search": self._retrieval_observation,
            "graph_traverse": self._graph_observation,
            "community_search": self._community_observation,
            "numeric_filter": self._numeric_observation,
            "conflict_scan": self._conflict_observation,
            "gap_scan": self._gap_observation,
            "expert_lookup": self._expert_observation,
        }

    async def execute(
        self,
        plan: AgentActionPlan,
        query_plan: QueryPlan,
        allowed_data_classes: set[DataClass] | None = None,
        *,
        prior_findings: Sequence[Finding] = (),
    ) -> ToolExecutionResult:
        """Исполняет план; ``prior_findings`` — доказательства всех предыдущих раундов.

        Пул передается именованно и со значением по умолчанию, чтобы рабочий процесс
        мог подключиться к нему в любом порядке: без него поведение не меняется
        (анализ идёт по найденному в этом раунде), с ним conflict/gap видят пары
        между действиями разных раундов.
        """
        # Одинаковые действия исполняются один раз: дубль в плане не должен
        # удваивать ни retrieval, ни счётчики конфликтов и пробелов.
        keys = [
            (
                action.tool,
                action.query,
                tuple(action.entities),
                tuple(sorted(action.relation_types)),
                action.max_hops,
            )
            for action in plan.actions
        ]
        first_positions = {keys.index(key) for key in keys}
        unique_outcomes = await asyncio.gather(
            *(
                self._run_action(plan.actions[position], query_plan, allowed_data_classes)
                for position in sorted(first_positions)
            )
        )
        outcome_by_position = dict(
            zip(sorted(first_positions), unique_outcomes, strict=True)
        )
        outcomes: list[_ActionOutcome] = []
        for position in range(len(plan.actions)):
            outcome = outcome_by_position[keys.index(keys[position])]
            if position not in first_positions:
                # Дубликат получает результат первого исполнения, но наблюдение
                # уходит с его собственным action_id.
                outcome = _ActionOutcome(
                    action=plan.actions[position],
                    context=outcome.context,
                    error=outcome.error,
                    rejected_relations=outcome.rejected_relations,
                )
            outcomes.append(outcome)
        pool = _merge_pool(prior_findings, outcomes)

        observations: list[ToolObservation] = []
        findings: dict[str, Finding] = {}
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}
        communities: list[str] = []
        conflicts: list[str] = []
        gaps: list[str] = []
        degradation: list[str] = []
        gaps_omitted = 0
        failures = 0
        rejected: list[str] = []

        for position, outcome in enumerate(outcomes):
            action = outcome.action
            rejected.extend(outcome.rejected_relations)
            if outcome.context is None:
                failures += 1
                observations.append(self._error_observation(action, outcome.error))
                continue
            context = outcome.context
            observation = self._observe(action, query_plan, context, pool)
            if outcome.rejected_relations:
                # Молча исполненный «подмножество запрошенного» Planner следующего
                # раунда прочитал бы как пустой корпус, а не как несуществующую связь.
                observation = _note_rejected(observation, outcome.rejected_relations)
            observations.append(observation)
            findings.update({item.id: item for item in context.findings})
            for node in context.graph.nodes:
                nodes[node.id] = node
            for edge in context.graph.edges:
                edges[edge.id] = edge
            communities.extend(context.community_summaries)
            # Что retrieval сделал с планом (срез numeric/scope-ограничений, заполнение
            # выдачи, потолок) — обязано доехать до ответа, а не остаться в контексте.
            degradation.extend(context.degradation_reasons)
            if position in first_positions:
                if action.tool == "conflict_scan":
                    conflicts.extend(observations[-1].facts)
                elif action.tool == "gap_scan":
                    gaps.extend(observations[-1].facts)
                    gaps_omitted += _omitted_count(observations[-1])
            if context.no_evidence:
                degradation.append(
                    f"{action.tool}: доказательств по запросу не найдено — "
                    "ответ опирается только на остальные действия."
                )

        if rejected:
            degradation.append(
                "Отношения вне реестра отброшены: "
                f"{', '.join(_dedupe(rejected))} — обход шёл по подписи "
                "domain/relations.py (provenance-рёбра запрашиваются явно)."
            )

        if failures:
            degradation.append(f"{failures} из {len(outcomes)} действий tools завершились ошибкой.")

        snapshot = GraphSnapshot(
            nodes=list(nodes.values()),
            edges=list(edges.values()),
            communities=list(dict.fromkeys(communities)),
        )
        return ToolExecutionResult(
            observations=observations,
            findings=list(findings.values()),
            graph=snapshot,
            community_summaries=list(dict.fromkeys(communities)),
            conflicts=_dedupe(conflicts),
            gaps=_dedupe(gaps),
            degradation_reasons=_dedupe(degradation),
            gaps_omitted=gaps_omitted,
        )

    async def _run_action(
        self,
        action: ToolAction,
        query_plan: QueryPlan,
        allowed_data_classes: set[DataClass] | None,
    ) -> _ActionOutcome:
        relations, rejected = resolve_relations(action.relation_types)
        try:
            context = await asyncio.to_thread(
                self._knowledge.retrieve,
                query_plan,
                self._retrieval_plan(action, query_plan, relations=relations),
                allowed_data_classes,
            )
        except Exception as error:  # noqa: BLE001 - граница действия: фиксируем и деградируем
            logger.warning("Tool %s failed: %s", action.tool, error, exc_info=True)
            return _ActionOutcome(action=action, context=None, error=error,
                                  rejected_relations=tuple(rejected))
        return _ActionOutcome(
            action=action, context=context, error=None, rejected_relations=tuple(rejected)
        )

    @staticmethod
    def _retrieval_plan(
        action: ToolAction,
        query_plan: QueryPlan,
        *,
        relations: Sequence[str] | None = None,
    ) -> RetrievalPlan:
        """Планирование retrieval наследует решение Planner-а о режиме запроса.

        Ранее ``query_plan.mode`` и ``query_plan.max_hops`` не читались нигде: выбор
        local/global/hybrid оставался декоративным полем схемы.
        """
        community_tool = action.tool in {"community_search", "gap_scan"}
        # Явный разбор плана приходит из _run_action (там же формируется список
        # отклонённых имён); без него фильтр обязан разбираться из action, иначе
        # вызов «в лоб» молча игнорировал бы relation_types действия.
        if relations is None:
            relations = resolve_relations(action.relation_types)[0]
        accepted = list(relations)
        return RetrievalPlan(
            lexical_query=action.query,
            semantic_query=action.query,
            entity_names=action.entities or query_plan.entity_mentions,
            # Если от запроса ничего не осталось, обход идёт по ярусу по умолчанию:
            # пустой список в памяти означает «без фильтра» (то есть и provenance-рёбра),
            # а в Neo4j — «ни одного пути»; оба варианта молча врут про то, что
            # запросил план.
            relation_types=accepted or list(resolve_relations(None)[0]),
            max_hops=min(action.max_hops, query_plan.max_hops),
            use_global_context=query_plan.mode in {"global", "hybrid"},
            use_community_context=community_tool or query_plan.mode == "global",
            use_local_graph=query_plan.mode != "global" and action.tool != "community_search",
        )

    def _observe(
        self,
        action: ToolAction,
        query_plan: QueryPlan,
        context: RetrievalContext,
        pool: Sequence[Finding] = (),
    ) -> ToolObservation:
        return self._registry[action.tool](action, query_plan, context, pool)

    @staticmethod
    def _error_observation(action: ToolAction, error: Exception | None) -> ToolObservation:
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="error",
            summary=f"Tool execution failed: {type(error).__name__ if error else 'unknown'}",
            next_actions=["Перепланировать запрос с более узкими аргументами"],
            root_cause_hint=(str(error)[:300] if error else "unknown") or type(error).__name__,
            safe_retry="Повторить один раз с меньшим max_hops и узким entity anchor.",
            stop_condition="Остановить tool loop после второго неуспешного раунда.",
        )

    @staticmethod
    def _retrieval_observation(
        action: ToolAction, _: QueryPlan, context: RetrievalContext, _pool: Sequence[Finding]
    ) -> ToolObservation:
        found = len(context.findings)
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if found else "warning",
            summary=(
                f"Найдено {found} evidence-backed findings"
                if found
                else "Доказательств по запросу не найдено"
            ),
            next_actions=[] if found else ["Расширить semantic query или сменить entity anchor"],
            artifacts=[f"finding:{item.id}" for item in context.findings],
            finding_ids=[item.id for item in context.findings],
        )

    @staticmethod
    def _graph_observation(
        action: ToolAction, _: QueryPlan, context: RetrievalContext, _pool: Sequence[Finding]
    ) -> ToolObservation:
        nodes = len(context.graph.nodes)
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if nodes else "warning",
            summary=(
                f"Получен subgraph: {nodes} узлов, {len(context.graph.edges)} связей"
                if nodes
                else "Связей по выбранным сущностям не найдено"
            ),
            next_actions=[] if nodes else ["Использовать другие entity anchors"],
            graph_node_ids=[item.id for item in context.graph.nodes],
        )

    @staticmethod
    def _community_observation(
        action: ToolAction, _: QueryPlan, context: RetrievalContext, _pool: Sequence[Finding]
    ) -> ToolObservation:
        summaries = context.community_summaries
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if summaries else "warning",
            summary=(
                f"Проанализировано сообществ: {len(summaries)}"
                if summaries
                else "Сообщества графа не построены"
            ),
            artifacts=[f"community:{name}" for name in summaries],
            finding_ids=[item.id for item in context.findings],
            facts=summaries,
        )

    @staticmethod
    def _numeric_observation(
        action: ToolAction, plan: QueryPlan, context: RetrievalContext, _pool: Sequence[Finding]
    ) -> ToolObservation:
        """Рапортует фактический эффект фильтра, а не число ограничений в плане."""
        filters = len(plan.numeric_filters)
        remaining = len(context.findings)
        if not filters:
            return ToolObservation(
                action_id=action.id,
                tool=action.tool,
                status="warning",
                summary="Числовые ограничения в запросе не заданы — фильтр не применён",
                next_actions=["Извлечь границы значений из вопроса или пропустить действие"],
            )
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if remaining else "warning",
            summary=f"Числовых ограничений: {filters}, доказательств после фильтрации: {remaining}",
            next_actions=[] if remaining else ["Ослабить границы числового фильтра"],
            finding_ids=[item.id for item in context.findings],
        )

    def _conflict_observation(
        self,
        action: ToolAction,
        _: QueryPlan,
        __: RetrievalContext,
        pool: Sequence[Finding],
    ) -> ToolObservation:
        """Пары конфликт ищутся по объединённому пулу, а не по top-6 одного действия."""
        claims = to_research_claims(pool)
        conflicts = self._intelligence.detect_conflicts(claims)
        by_claim = {claim.id: claim for claim in claims}
        descriptions = [
            (
                f"Конфликт '{c.property_name}': {c.reason} "
                f"[{by_claim[c.left_claim_id].finding_id} ↔ "
                f"{by_claim[c.right_claim_id].finding_id}]"
            )
            for c in conflicts
            if c.left_claim_id in by_claim and c.right_claim_id in by_claim
        ]
        shown = descriptions[:MAX_CONFLICT_FACTS]
        omitted = len(descriptions) - len(shown)
        if omitted:
            shown.append(_omitted_note(omitted, "пар противоречий"))
        scope_note = f"в пуле запроса из {len(pool)} доказательств"
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if conflicts else "warning",
            summary=(
                f"Обнаружено конфликтов: {len(conflicts)} {scope_note}"
                if conflicts
                else (
                    f"Противоречий в числовых диапазонах не найдено {scope_note}. "
                    "Пара должна попасть в один пул — на малом пуле это не доказательство "
                    "согласованности корпуса."
                )
            ),
            next_actions=(
                []
                if conflicts
                else ["Расширить retrieval по тем же сущностям: конфликт — это пара тезисов"]
            ),
            finding_ids=sorted(
                {
                    by_claim[claim_id].finding_id
                    for c in conflicts
                    for claim_id in (c.left_claim_id, c.right_claim_id)
                    if claim_id in by_claim
                }
            ),
            facts=shown,
            omitted_count=omitted,
        )

    def _gap_observation(
        self,
        action: ToolAction,
        _: QueryPlan,
        context: RetrievalContext,
        pool: Sequence[Finding],
    ) -> ToolObservation:
        """Пробел = непокрытая комбинация измерений, построенная из реальных данных пула.

        Синтетический research space из ``plan.entity_mentions`` убран: одно
        измерение ``entity``, в котором каждый тезис покрывает ровно одно значение,
        заполнено всегда, поэтому ``total == covered`` и инструмент возвращал 0
        при любом корпусе. Теперь измерения берутся либо из scope тезисов, либо из
        матрицы «субъект × числовое свойство» (см. build_research_space), а при
        вырожденной матрице честно сообщается об отсутствии research space.
        """
        claims = to_research_claims(pool)
        research_space = build_research_space(claims)
        analyzed_note = (
            f"проанализировано {len(claims)} числовых тезисов из пула в {len(pool)} доказательств"
        )
        if research_space is None:
            return ToolObservation(
                action_id=action.id,
                tool=action.tool,
                status="warning",
                summary=(
                    f"Research space не определён: {analyzed_note}. "
                    + (
                        "В пуле нет ни одного числового тезиса с источником."
                        if not claims
                        else "Сопоставлять нечего: нужно минимум два субъекта и два показателя "
                        "либо два размеченных условия применимости."
                    )
                ),
                next_actions=[
                    "Добавить hybrid_search или numeric_filter: пробел считается только по числам"
                ],
                artifacts=[f"community:{name}" for name in context.graph.communities],
            )
        summary = self._intelligence.summarize_gaps(
            claims, research_space, limit=DEFAULT_GAP_LIMIT
        )
        descriptions = [
            f"Пробел: {', '.join(f'{d.name}={d.value}' for d in gap.dimensions)}. {gap.reason}"
            for gap in summary.gaps
        ]
        if summary.omitted:
            descriptions.append(
                _omitted_note(summary.omitted, "непокрытых комбинаций сверх лимита")
            )
        capped = sorted(
            name
            for name, values in research_space.dimensions.items()
            if len(values) >= MAX_VALUES_PER_DIMENSION
        )
        dimensions_note = "×".join(
            f"{name}:{len(values)}" for name, values in sorted(research_space.dimensions.items())
        )
        uncovered = summary.total - summary.covered
        parts = [
            f"измерения {dimensions_note}",
            analyzed_note,
            f"матрица {summary.total} комбинаций, покрыто тезисами {summary.covered}",
        ]
        if uncovered:
            parts.append(f"непокрытых {uncovered}, показано {len(summary.gaps)}")
        else:
            parts.append("непокрытых комбинаций в этой матрице нет")
        if capped:
            parts.append(f"широкие измерения: {', '.join(capped)}")
        if summary.partial:
            parts.append("перебор остановлен по бюджету: часть комбинаций не просмотрена")
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if summary.gaps else "warning",
            summary=(
                "Исследованы "
                + "; ".join(parts)
                + ". Полнота корпуса не заявляется: вывод относится к пулу доказатель "
                "запроса, а не ко всему графу знаний."
            ),
            artifacts=[f"community:{name}" for name in context.graph.communities],
            finding_ids=sorted({claim.finding_id for claim in claims}),
            facts=descriptions,
            omitted_count=summary.omitted,
        )

    @staticmethod
    def _expert_observation(
        action: ToolAction, _: QueryPlan, context: RetrievalContext, _pool: Sequence[Finding]
    ) -> ToolObservation:
        experts = [node for node in context.graph.nodes if node.type.value == "expert"]
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if experts else "warning",
            summary=(
                f"Найдено экспертов/лабораторий: {len(experts)}"
                if experts
                else "Эксперты по теме не найдены"
            ),
            artifacts=[f"node:{item.id}" for item in experts],
            graph_node_ids=[item.id for item in experts],
            facts=[item.label for item in experts],
        )


def _omitted_note(count: int, what: str) -> str:
    """Одна строка на срез: и человеку про потолок выдачи, и числу для ``gaps_omitted``."""
    return f"{OMITTED_NOTE_PREFIX}: {count} {what} — срез потолка выдачи, а не полный список."


def _omitted_count(observation: ToolObservation) -> int:
    """Сколько фактов срезано потолком: из поля наблюдения, а не из строки среза."""
    return observation.omitted_count


def _note_rejected(observation: ToolObservation, rejected: Sequence[str]) -> ToolObservation:
    """Отброшенные имена связей — в самом observation, а не только в деградации.

    ``degradation_reasons`` читает пользователь ответа, а следующее решение принимает
    Planner по observations: без строки в observation отказ по несуществующей связи
    выглядел бы как «в корпусе ничего нет».
    """
    names = ", ".join(_dedupe(rejected))
    return observation.model_copy(
        update={
            "status": "warning",
            "summary": (
                f"{observation.summary} Связи вне реестра не исполнялись: {names}."
            ),
            "next_actions": [
                *observation.next_actions,
                "Запросите имена подписи domain/relations.py вместо: " + names,
            ],
        }
    )


def _dedupe(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _merge_pool(prior: Sequence[Finding], outcomes: Sequence[_ActionOutcome]) -> list[Finding]:
    """Доказательства всех раундов: предыдущие плюс найденные в текущем действии.

    Дедупликация по id обязательна: один и тот же тезис, попавший в пул дважды,
    дал бы conflict_scan пару «сам с собой» и расхождение там, где его нет.
    """
    merged: dict[str, Finding] = {}
    for finding in prior:
        merged.setdefault(finding.id, finding)
    for outcome in outcomes:
        if outcome.context is None:
            continue
        for finding in outcome.context.findings:
            merged.setdefault(finding.id, finding)
    return list(merged.values())
