from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from scientific_tangle.domain.contracts import (
    AgentActionPlan,
    Finding,
    GraphSnapshot,
    RetrievalPlan,
    ToolAction,
    ToolObservation,
)
from scientific_tangle.domain.intelligence import (
    ComparableValue,
    KnowledgeKind,
    ResearchClaim,
    ResearchSpace,
    ScopeDimension,
)
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.services.knowledge import KnowledgeBase, RetrievalContext
from scientific_tangle.services.research_intelligence import ResearchIntelligenceService


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    observations: list[ToolObservation]
    findings: list[Finding]
    graph: GraphSnapshot
    community_summaries: list[str]
    conflicts: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


class ResearchToolExecutor:
    """Безопасный action space: LLM выбирает tools, код ограничивает их исполнение."""

    def __init__(self, knowledge: KnowledgeBase) -> None:
        self._knowledge = knowledge
        self._intelligence = ResearchIntelligenceService()
        self._registry: dict[
            str, Callable[[ToolAction, QueryPlan, RetrievalContext], ToolObservation]
        ] = {
            "hybrid_search": self._retrieval_observation,
            "graph_traverse": self._graph_observation,
            "community_search": self._community_observation,
            "numeric_filter": self._numeric_observation,
            "conflict_scan": self._conflict_observation,
            "gap_scan": self._gap_observation,
            "expert_lookup": self._expert_observation,
        }

    def execute(
        self,
        plan: AgentActionPlan,
        query_plan: QueryPlan,
        allowed_data_classes: set[str] | None = None,
    ) -> ToolExecutionResult:
        observations: list[ToolObservation] = []
        findings: dict[str, Finding] = {}
        nodes = {}
        edges = {}
        communities: list[str] = []
        conflicts: list[str] = []
        gaps: list[str] = []

        for action in plan.actions:
            retrieval_plan = RetrievalPlan(
                lexical_query=action.query,
                semantic_query=action.query,
                entity_names=action.entities or query_plan.entity_mentions,
                relation_types=action.relation_types
                or [
                    "CONTAINS",
                    "TREATED_BY",
                    "PRODUCES",
                    "REQUIRES",
                    "OPERATES_AT",
                    "SUPPORTED_BY",
                    "CONTRADICTS",
                    "EXPERT_IN",
                    "ASSERTS",
                ],
                max_hops=action.max_hops,
                use_global_context=action.tool in {"community_search", "gap_scan"},
                community_question=action.query,
            )
            try:
                context = self._knowledge.retrieve(
                    query_plan, retrieval_plan, allowed_data_classes
                )
                observation = self._registry[action.tool](action, query_plan, context)
                findings.update({item.id: item for item in context.findings})
                nodes.update({item.id: item for item in context.graph.nodes})
                edges.update({item.id: item for item in context.graph.edges})
                communities.extend(context.community_summaries)
            except Exception as error:
                observation = ToolObservation(
                    action_id=action.id,
                    tool=action.tool,
                    status="error",
                    summary=f"Tool execution failed: {type(error).__name__}",
                    next_actions=["Перепланировать запрос с более узкими аргументами"],
                    root_cause_hint=str(error)[:300] or type(error).__name__,
                    safe_retry="Повторить один раз с меньшим max_hops и узким entity anchor.",
                    stop_condition="Остановить tool loop после второго неуспешного раунда.",
                )
            observations.append(observation)
            if action.tool == "conflict_scan":
                conflicts.extend(observation.facts)
            elif action.tool == "gap_scan":
                gaps.extend(observation.facts)

        return ToolExecutionResult(
            observations=observations,
            findings=list(findings.values()),
            graph=GraphSnapshot(
                nodes=list(nodes.values()),
                edges=list(edges.values()),
                communities=list(dict.fromkeys(communities)),
            ),
            community_summaries=list(dict.fromkeys(communities)),
            conflicts=conflicts,
            gaps=gaps,
        )

    @staticmethod
    def _retrieval_observation(
        action: ToolAction, _: QueryPlan, context: RetrievalContext
    ) -> ToolObservation:
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if context.findings else "warning",
            summary=f"Найдено {len(context.findings)} evidence-backed findings",
            next_actions=[] if context.findings else ["Расширить semantic query"],
            artifacts=[f"finding:{item.id}" for item in context.findings],
            finding_ids=[item.id for item in context.findings],
        )

    @staticmethod
    def _graph_observation(
        action: ToolAction, _: QueryPlan, context: RetrievalContext
    ) -> ToolObservation:
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if context.graph.nodes else "warning",
            summary=(
                f"Получен subgraph: {len(context.graph.nodes)} узлов, "
                f"{len(context.graph.edges)} связей"
            ),
            next_actions=[] if context.graph.nodes else ["Использовать другие entity anchors"],
            graph_node_ids=[item.id for item in context.graph.nodes],
        )

    @staticmethod
    def _community_observation(
        action: ToolAction, _: QueryPlan, context: RetrievalContext
    ) -> ToolObservation:
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if context.community_summaries else "warning",
            summary=f"Проанализировано communities: {len(context.community_summaries)}",
            facts=context.community_summaries,
        )

    @staticmethod
    def _numeric_observation(
        action: ToolAction, plan: QueryPlan, context: RetrievalContext
    ) -> ToolObservation:
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success",
            summary=f"Применено числовых ограничений: {len(plan.numeric_filters)}",
            finding_ids=[item.id for item in context.findings],
        )

    def _conflict_observation(
        self, action: ToolAction, plan: QueryPlan, context: RetrievalContext
    ) -> ToolObservation:
        claims = self._to_research_claims(context.findings)
        conflicts = self._intelligence.detect_conflicts(claims)
        descriptions = [
            f"Конфликт: {c.left_claim_id} vs {c.right_claim_id} "
            f"по свойству '{c.property_name}'. {c.reason}"
            for c in conflicts
        ]
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if conflicts else "warning",
            summary=f"Обнаружено конфликтов: {len(conflicts)}",
            finding_ids=[
                fid for c in conflicts for fid in (c.left_claim_id, c.right_claim_id)
            ],
            facts=descriptions,
        )

    def _gap_observation(
        self, action: ToolAction, plan: QueryPlan, context: RetrievalContext
    ) -> ToolObservation:
        claims = self._to_research_claims(context.findings)
        dimensions: dict[str, set[str]] = {}
        for claim in claims:
            for dim in claim.scope:
                dimensions.setdefault(dim.name, set()).add(dim.value)
        if not dimensions and plan.entity_mentions:
            dimensions = {"entity": set(plan.entity_mentions)}
        research_space = ResearchSpace(
            dimensions={k: sorted(v) for k, v in dimensions.items()}
        )
        gaps = self._intelligence.detect_gaps(claims, research_space)
        descriptions = [
            f"Пробел: {', '.join(f'{d.name}={d.value}' for d in gap.dimensions)}. "
            f"{gap.reason}"
            for gap in gaps
        ]
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if gaps else "warning",
            summary=f"Обнаружено пробелов в покрытии: {len(gaps)}",
            artifacts=[f"community:{name}" for name in context.graph.communities],
            facts=descriptions,
        )

    @staticmethod
    def _expert_observation(
        action: ToolAction, _: QueryPlan, context: RetrievalContext
    ) -> ToolObservation:
        experts = [node for node in context.graph.nodes if node.type.value == "expert"]
        return ToolObservation(
            action_id=action.id,
            tool=action.tool,
            status="success" if experts else "warning",
            summary=f"Найдено экспертов/лабораторий: {len(experts)}",
            graph_node_ids=[item.id for item in experts],
            facts=[item.label for item in experts],
        )

    @staticmethod
    def _to_research_claims(findings: list[Finding]) -> list[ResearchClaim]:
        """Преобразует Finding с numeric observations в типизированные ResearchClaim."""
        claims: list[ResearchClaim] = []
        for finding in findings:
            if not finding.observations:
                continue
            scope = [
                ScopeDimension(name=k, value=v) for k, v in finding.scope.items()
            ]
            kind = (
                KnowledgeKind.EXPERT_VALIDATED
                if finding.status == "consensus"
                else KnowledgeKind.EXTRACTED
            )
            evidence_ids = [str(ev.document_id) for ev in finding.evidence]
            for obs in finding.observations:
                if obs.operator == "between":
                    lo = (
                        obs.normalized_min
                        if obs.normalized_min is not None
                        else (obs.min_value if obs.min_value is not None else 0.0)
                    )
                    hi = (
                        obs.normalized_max
                        if obs.normalized_max is not None
                        else (obs.max_value if obs.max_value is not None else 0.0)
                    )
                else:
                    lo = hi = (
                        obs.normalized_value
                        if obs.normalized_value is not None
                        else (obs.value if obs.value is not None else 0.0)
                    )
                claims.append(
                    ResearchClaim(
                        id=finding.id,
                        subject_id=finding.subject or finding.id,
                        predicate=finding.predicate or "HAS_PROPERTY",
                        value=ComparableValue(
                            property_name=obs.property_name,
                            min_value=lo,
                            max_value=hi,
                            unit=obs.normalized_unit,
                        ),
                        scope=scope,
                        evidence_ids=evidence_ids,
                        knowledge_kind=kind,
                    )
                )
        return claims
