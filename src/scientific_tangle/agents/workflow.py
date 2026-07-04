from __future__ import annotations

import logging
from time import perf_counter
from typing import Any, Literal, TypedDict, cast
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from scientific_tangle.agents.tools import ResearchToolExecutor
from scientific_tangle.config import get_settings
from scientific_tangle.domain.contracts import (
    AgentActionPlan,
    AgentControlDecision,
    AgentEvent,
    AnswerPayload,
    CritiqueResult,
    Finding,
    GraphSnapshot,
    IntentClassification,
    PlanningBundle,
    QueryRequest,
    ReasoningResult,
    ToolObservation,
)
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry, agent_metrics
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase, KnowledgeBase
from scientific_tangle.services.provider import ModelProvider, build_provider

INTENT_SYSTEM = """Ты Intent Router платформы MindAI.
Разложи запрос на primary intent, secondary intents, entities и constraints.
Требовать подтверждение можно только для необратимых изменений trusted graph, прав доступа,
публикации или внешней передачи чувствительных данных. Исследовательские действия выполняются
автономно и не требуют подтверждения.
"""

PLANNER_SYSTEM = """Ты Planner Agent научной GraphRAG-системы MindAI.
Преобразуй вопрос в строгий QueryPlan. Не отвечай на вопрос.
Выдели сущности, числовые ограничения, географию и временной диапазон.
Выбери local для точечного факта, global для обзора communities, hybrid для сложного сравнения.
max_hops не больше 4. Сохрани исходный вопрос без изменения смысла.
"""

ACTION_SYSTEM = """Ты Autonomous Action Planner платформы MindAI.
Выбери и упорядочи tools, необходимые для полного выполнения пользовательского запроса.
Не перекладывай исследовательскую работу на пользователя. Используй hybrid_search,
graph_traverse, community_search, numeric_filter, conflict_scan, gap_scan и expert_lookup.
Аргументы должны быть узкими и исполнимыми. relation_types только из allowlist: CONTAINS,
TREATED_BY, PRODUCES, REQUIRES, OPERATES_AT, SUPPORTED_BY, CONTRADICTS, EXPERT_IN, ASSERTS.
План должен самостоятельно собрать достаточно evidence для completion_criteria.
Если переданы предыдущие observations, устрани обнаруженные пробелы
и не повторяй успешные действия без причины.
"""

PLANNING_SYSTEM = f"""{INTENT_SYSTEM}\n{PLANNER_SYSTEM}\n{ACTION_SYSTEM}
Выполни классификацию intent, QueryPlan и первый AgentActionPlan за один проход.
Каждая часть результата обязательна и согласована с другими частями.
"""

CONTROL_SYSTEM = """Ты Autonomous Control Agent платформы MindAI.
Сопоставь completion criteria с tool observations. Выбери continue_tools, если доступные tools могут
закрыть конкретный пробел, иначе reason. Не проси пользователя выполнять исследовательские действия.
После двух раундов tools выбирай reason и явно сохраняй оставшиеся пробелы в missing_evidence.
"""

REASONER_SYSTEM = """Ты Reasoner Agent платформы MindAI.
Синтезируй ответ только из переданных findings, evidence и community summaries.
Укажи IDs использованных findings. Не добавляй числа, которых нет в evidence.
Отдели conflicts, knowledge gaps и recommendations. Не давай пользователю поручений вида
«проверьте документ» или «найдите данные»: все доступные действия уже должны быть выполнены tools.
Recommendations описывают следующие автономные действия системы или готовые решения.
"""

CRITIC_SYSTEM = """Ты Critic Agent научной GraphRAG-системы.
Проверь соответствие вопросу, условия применимости, citations, числовую fidelity,
неподдержанные выводы и корректность conflict/gap. Верни approved=false при любой
содержательной проблеме и дай конкретные revision_instructions.
"""

IMPROVER_SYSTEM = """Ты Improver Agent.
Перепиши структурированный ответ строго по замечаниям Critic и тому же evidence context.
Нельзя добавлять новые факты или источники. Сохрани IDs реально использованных findings.
"""

_logger = logging.getLogger(__name__)


def _truncate_text(text: str, max_tokens: int = 30000) -> str:
    """Оценка токенов как len(text)/4; обрезка при превышении лимита."""
    estimated = len(text) // 4
    if estimated <= max_tokens:
        return text
    _logger.warning(
        "Контекст обрезан: оценено %d токенов, лимит %d", estimated, max_tokens
    )
    return text[: max_tokens * 4]


class ResearchState(TypedDict, total=False):
    question: str
    language: str
    requested_mode: str
    intent: IntentClassification
    query_plan: QueryPlan
    action_plan: AgentActionPlan
    control: AgentControlDecision
    action_round: int
    observations: list[ToolObservation]
    findings: list[Finding]
    graph: GraphSnapshot
    community_summaries: list[str]
    intelligence_conflicts: list[str]
    intelligence_gaps: list[str]
    reasoning: ReasoningResult
    critique: CritiqueResult
    revision_count: int
    trace: list[AgentEvent]
    answer: AnswerPayload
    allowed_data_classes: set[str] | None


class ResearchWorkflow:
    def __init__(
        self,
        knowledge: KnowledgeBase | None = None,
        provider: ModelProvider | None = None,
        metrics: AgentMetricsRegistry | None = None,
        checkpointer: Any | None = None,
        extra_policy: str = "",
    ) -> None:
        self.knowledge = knowledge or InMemoryKnowledgeBase()
        self.provider = provider or build_provider(get_settings())
        self.tool_executor = ResearchToolExecutor(self.knowledge)
        self.metrics = metrics or agent_metrics
        self.checkpointer = checkpointer
        self.extra_policy = extra_policy.strip()
        self.graph = self._build()

    @staticmethod
    def _event(agent: str, message: str, duration_ms: int = 0) -> AgentEvent:
        return AgentEvent(
            agent=agent,
            status="completed",
            message=message,
            duration_ms=duration_ms,
        )

    async def intent_router(self, state: ResearchState) -> dict[str, object]:
        intent = await self.provider.complete_model(
            INTENT_SYSTEM,
            state["question"],
            IntentClassification,
        )
        return {
            "intent": intent,
            "trace": [
                *state.get("trace", []),
                self._event(
                    "intent_router",
                    f"Primary intent: {intent.primary}; secondary: {len(intent.secondary)}",
                ),
            ],
        }

    async def planning_agent(self, state: ResearchState) -> dict[str, object]:
        bundle = await self.provider.complete_model(
            self._system(PLANNING_SYSTEM),
            _truncate_text(
                f"Язык: {state.get('language', 'ru')}\n"
                f"Предпочтительный режим: {state.get('requested_mode', 'hybrid')}\n"
                f"Вопрос: {state['question']}"
            ),
            PlanningBundle,
        )
        return {
            "intent": bundle.intent,
            "query_plan": bundle.query_plan,
            "action_plan": bundle.action_plan,
            "trace": [
                *state.get("trace", []),
                self._event("intent_router", f"Primary intent: {bundle.intent.primary}"),
                self._event("planner", "LLM сформировал согласованный QueryPlan"),
                self._event(
                    "action_planner",
                    f"LLM выбрал tools: {len(bundle.action_plan.actions)}",
                ),
            ],
        }

    async def planner(self, state: ResearchState) -> dict[str, object]:
        plan = await self.provider.complete_model(
            PLANNER_SYSTEM,
            (
                f"Язык: {state.get('language', 'ru')}\n"
                f"Предпочтительный режим: {state.get('requested_mode', 'hybrid')}\n"
                f"Интенты: {state['intent'].model_dump_json()}\n"
                f"Вопрос: {state['question']}"
            ),
            QueryPlan,
        )
        return {
            "query_plan": plan,
            "trace": [
                *state.get("trace", []),
                self._event(
                    "planner",
                    (
                        f"LLM QueryPlan: {len(plan.entity_mentions)} сущностей, "
                        f"{len(plan.numeric_filters)} числовых фильтров"
                    ),
                ),
            ],
        }

    async def action_planner(self, state: ResearchState) -> dict[str, object]:
        previous = "\n".join(item.model_dump_json() for item in state.get("observations", []))
        action_plan = await self.provider.complete_model(
            self._system(ACTION_SYSTEM),
            _truncate_text(
                f"INTENT:\n{state['intent'].model_dump_json(indent=2)}\n\n"
                f"QUERY PLAN:\n{state['query_plan'].model_dump_json(indent=2)}\n\n"
                f"PREVIOUS OBSERVATIONS:\n{previous or 'none'}"
            ),
            AgentActionPlan,
        )
        return {
            "action_plan": action_plan,
            "trace": [
                *state.get("trace", []),
                self._event(
                    "action_planner",
                    f"LLM выбрал tools: {len(action_plan.actions)}",
                ),
            ],
        }

    async def tool_executor_node(self, state: ResearchState) -> dict[str, object]:
        result = self.tool_executor.execute(
            state["action_plan"], state["query_plan"],
            state.get("allowed_data_classes"),
        )
        old_findings = {item.id: item for item in state.get("findings", [])}
        old_findings.update({item.id: item for item in result.findings})
        previous_graph = state.get("graph", GraphSnapshot(nodes=[], edges=[]))
        old_nodes = {item.id: item for item in previous_graph.nodes}
        old_nodes.update({item.id: item for item in result.graph.nodes})
        old_edges = {item.id: item for item in previous_graph.edges}
        old_edges.update({item.id: item for item in result.graph.edges})
        communities = list(
            dict.fromkeys([*state.get("community_summaries", []), *result.community_summaries])
        )
        return {
            "observations": [*state.get("observations", []), *result.observations],
            "findings": list(old_findings.values()),
            "intelligence_conflicts": [
                *state.get("intelligence_conflicts", []), *result.conflicts
            ],
            "intelligence_gaps": [
                *state.get("intelligence_gaps", []), *result.gaps
            ],
            "graph": GraphSnapshot(
                nodes=list(old_nodes.values()),
                edges=list(old_edges.values()),
                communities=communities,
            ),
            "community_summaries": communities,
            "action_round": state.get("action_round", 0) + 1,
            "trace": [
                *state.get("trace", []),
                self._event(
                    "tool_executor",
                    (
                        f"Автономно исполнено {len(result.observations)} tools; "
                        f"получено {len(result.findings)} findings"
                    ),
                ),
            ],
        }

    async def controller(self, state: ResearchState) -> dict[str, object]:
        control = await self.provider.complete_model(
            self._system(CONTROL_SYSTEM),
            _truncate_text(
                f"ROUND: {state.get('action_round', 0)}\n"
                f"COMPLETION CRITERIA: {state['action_plan'].completion_criteria}\n"
                "OBSERVATIONS:\n"
                + "\n".join(item.model_dump_json() for item in state["observations"])
            ),
            AgentControlDecision,
        )
        if state.get("action_round", 0) >= 2 and control.decision == "continue_tools":
            control = control.model_copy(update={"decision": "reason"})
        return {
            "control": control,
            "trace": [
                *state.get("trace", []),
                self._event("controller", f"Autonomous decision: {control.decision}"),
            ],
        }

    @staticmethod
    def route_after_controller(state: ResearchState) -> Literal["action_planner", "reasoner"]:
        if state["control"].decision == "continue_tools":
            return "action_planner"
        return "reasoner"

    @staticmethod
    def _evidence_context(state: ResearchState) -> str:
        # Ограничиваем объём данных для соблюдения token limit
        findings_list = state["findings"][:10]
        findings = "\n".join(finding.model_dump_json() for finding in findings_list)
        communities = "\n".join(state.get("community_summaries", [])[:3])
        observations = "\n".join(
            item.model_dump_json() for item in state.get("observations", [])
        )
        return _truncate_text(
            f"ВОПРОС:\n{state['question']}\n\n"
            f"QUERY PLAN:\n{state['query_plan'].model_dump_json()}\n\n"
            f"TOOL OBSERVATIONS:\n{observations}\n\n"
            f"FINDINGS:\n{findings}\n\nCOMMUNITIES:\n{communities}"
        )

    async def reasoner(self, state: ResearchState) -> dict[str, object]:
        reasoning = await self.provider.complete_model(
            self._system(REASONER_SYSTEM),
            self._evidence_context(state),
            ReasoningResult,
        )
        return {
            "reasoning": reasoning,
            "trace": [
                *state.get("trace", []),
                self._event("reasoner", "LLM сформировал grounded structured answer"),
            ],
        }

    async def critic(self, state: ResearchState) -> dict[str, object]:
        critic_input = _truncate_text(
            f"{self._evidence_context(state)}\n\n"
            f"DRAFT:\n{state['reasoning'].model_dump_json(indent=2)}"
        )
        critique = await self.provider.complete_model(
            self._system(CRITIC_SYSTEM),
            critic_input,
            CritiqueResult,
        )

        valid_ids = {finding.id for finding in state["findings"]}
        invalid_ids = set(state["reasoning"].finding_ids) - valid_ids
        guardrail_issues = list(critique.issues)
        if invalid_ids:
            guardrail_issues.append(f"Неизвестные finding IDs: {sorted(invalid_ids)}")
        if invalid_ids or any(not finding.evidence for finding in state["findings"]):
            critique = critique.model_copy(
                update={
                    "approved": False,
                    "issues": guardrail_issues,
                    "revision_instructions": [
                        *critique.revision_instructions,
                        "Использовать только существующие finding IDs и evidence.",
                    ],
                }
            )
        return {
            "critique": critique,
            "trace": [
                *state.get("trace", []),
                AgentEvent(
                    agent="critic",
                    status="completed" if critique.approved else "revised",
                    message=(
                        "LLM Critic одобрил ответ"
                        if critique.approved
                        else "; ".join(critique.issues)
                    ),
                    duration_ms=0,
                ),
            ],
        }

    async def improver(self, state: ResearchState) -> dict[str, object]:
        input_text = _truncate_text(
            f"{self._evidence_context(state)}\n\n"
            f"DRAFT:\n{state['reasoning'].model_dump_json()}\n\n"
            f"CRITIQUE:\n{state['critique'].model_dump_json()}"
        )
        reasoning = await self.provider.complete_model(
            self._system(IMPROVER_SYSTEM),
            input_text,
            ReasoningResult,
        )
        return {
            "reasoning": reasoning,
            "revision_count": state.get("revision_count", 0) + 1,
            "trace": [
                *state.get("trace", []),
                self._event("improver", "LLM выполнил ограниченную ревизию ответа"),
            ],
        }

    async def finalize(self, state: ResearchState) -> dict[str, object]:
        selected = set(state["reasoning"].finding_ids)
        findings = [finding for finding in state["findings"] if finding.id in selected]
        if not findings:
            findings = state["findings"]
        confidence = sum(finding.confidence for finding in findings) / max(len(findings), 1)
        trace = [
            *state.get("trace", []),
            self._event("synthesizer", "Проверяемый ответ опубликован"),
        ]
        answer = AnswerPayload(
            query_id=uuid4(),
            question=state["question"],
            summary=state["reasoning"].summary,
            intent=state["intent"],
            query_plan=state["query_plan"],
            tool_observations=state.get("observations", []),
            findings=findings,
            conflicts=[
                *state.get("intelligence_conflicts", []),
                *state["reasoning"].conflicts,
            ],
            knowledge_gaps=[
                *state.get("intelligence_gaps", []),
                *state["reasoning"].knowledge_gaps,
            ],
            recommendations=state["reasoning"].recommendations,
            graph=state["graph"],
            trace=trace,
            confidence=round(confidence, 3),
            model_mode=cast(
                Literal["yandex", "gigachat", "fallback", "scripted"],
                self.provider.mode,
            ),
        )
        return {"trace": trace, "answer": answer}

    @staticmethod
    def route_after_critic(state: ResearchState) -> Literal["improver", "finalize"]:
        if not state["critique"].approved and state.get("revision_count", 0) < 1:
            return "improver"
        return "finalize"

    def _build(self) -> Any:
        builder = StateGraph(ResearchState)
        retry = RetryPolicy(max_attempts=3, initial_interval=0.5)
        builder.add_node(
            "planning_agent",
            self._instrument("planning_agent", self.planning_agent),
            retry_policy=retry,
        )
        builder.add_node(
            "action_planner",
            self._instrument("action_planner", self.action_planner),
            retry_policy=retry,
        )
        builder.add_node(
            "tool_executor",
            self._instrument("tool_executor", self.tool_executor_node),
            retry_policy=retry,
        )
        builder.add_node(
            "controller", self._instrument("controller", self.controller), retry_policy=retry
        )
        builder.add_node(
            "reasoner", self._instrument("reasoner", self.reasoner), retry_policy=retry
        )
        builder.add_node("critic", self._instrument("critic", self.critic), retry_policy=retry)
        builder.add_node(
            "improver", self._instrument("improver", self.improver), retry_policy=retry
        )
        builder.add_node("finalize", self._instrument("synthesizer", self.finalize))
        builder.add_edge(START, "planning_agent")
        builder.add_edge("planning_agent", "tool_executor")
        builder.add_edge("tool_executor", "controller")
        builder.add_conditional_edges(
            "controller",
            self.route_after_controller,
            {"action_planner": "action_planner", "reasoner": "reasoner"},
        )
        builder.add_edge("action_planner", "tool_executor")
        builder.add_edge("reasoner", "critic")
        builder.add_conditional_edges(
            "critic",
            self.route_after_critic,
            {"improver": "improver", "finalize": "finalize"},
        )
        builder.add_edge("improver", "critic")
        builder.add_edge("finalize", END)
        return builder.compile(checkpointer=self.checkpointer)

    def _instrument(
        self,
        agent: str,
        node: Any,
    ) -> Any:
        async def wrapped(state: ResearchState) -> dict[str, object]:
            started = perf_counter()
            try:
                update = cast(dict[str, object], await node(state))
            except Exception:
                duration_ms = (perf_counter() - started) * 1000
                self.metrics.observe(agent, duration_ms, False)
                raise
            duration_ms = (perf_counter() - started) * 1000
            self.metrics.observe(agent, duration_ms, True)
            trace = update.get("trace")
            if isinstance(trace, list) and trace and isinstance(trace[-1], AgentEvent):
                updated_trace = list(trace)
                updated_trace[-1] = trace[-1].model_copy(update={"duration_ms": round(duration_ms)})
                update["trace"] = updated_trace
            return update

        return wrapped

    def _system(self, base: str) -> str:
        if not self.extra_policy:
            return base
        return f"{base}\n\nCANDIDATE POLICY:\n{self.extra_policy}"

    async def run(
        self, request: QueryRequest, allowed_data_classes: set[str] | None = None
    ) -> AnswerPayload:
        result = await self.graph.ainvoke(
            {
                "question": request.question,
                "language": request.language,
                "requested_mode": request.mode,
                "revision_count": 0,
                "action_round": 0,
                "trace": [],
                "allowed_data_classes": allowed_data_classes,
            },
            config={"configurable": {"thread_id": str(request.thread_id)}},
        )
        return cast(AnswerPayload, result["answer"])


workflow = ResearchWorkflow()
graph = workflow.graph
