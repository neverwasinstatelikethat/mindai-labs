from uuid import uuid4

import pytest

from scientific_tangle.agents.workflow import ResearchWorkflow
from scientific_tangle.domain.contracts import (
    AgentActionPlan,
    AgentControlDecision,
    CritiqueResult,
    EvolutionDraft,
    ExtractionResult,
    FeedbackRequest,
    IngestionBundle,
    IntentClassification,
    PlanningBundle,
    QueryRequest,
    ReasoningResult,
    ToolAction,
)
from scientific_tangle.domain.models import NumericFilter, QueryPlan
from scientific_tangle.services.evolution import EvolutionService
from scientific_tangle.services.ingestion import IngestionService
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from tests.fakes import ScriptedProvider


def query_plan() -> QueryPlan:
    return QueryPlan(
        question="Какие методы обессоливания подходят для шахтной воды?",
        language="ru",
        mode="hybrid",
        entity_mentions=["шахтная вода", "обессоливание"],
        numeric_filters=[
            NumericFilter(property_name="dry_residue", operator="lte", value=1000, unit="mg/L")
        ],
        max_hops=3,
    )


def action_plan() -> AgentActionPlan:
    return AgentActionPlan(
        rationale="Нужны hybrid evidence и graph path.",
        actions=[
            ToolAction(
                id="search-1",
                tool="hybrid_search",
                purpose="Найти evidence-backed методы",
                query="шахтная вода обессоливание обратный осмос",
                entities=["Шахтная вода", "Обратный осмос"],
                relation_types=["TREATED_BY", "PRODUCES", "SUPPORTED_BY"],
                max_hops=3,
            ),
            ToolAction(
                id="graph-1",
                tool="graph_traverse",
                purpose="Построить объяснимый путь",
                query="связи воды с методами очистки",
                entities=["Шахтная вода"],
                relation_types=["TREATED_BY", "PRODUCES"],
                max_hops=3,
            ),
        ],
        completion_criteria=["Найдены методы и evidence paths"],
    )


def planning_bundle() -> PlanningBundle:
    return PlanningBundle(
        intent=IntentClassification(primary="technology_comparison", entities=["шахтная вода"]),
        query_plan=query_plan(),
        action_plan=action_plan(),
    )


@pytest.mark.asyncio
async def test_llm_driven_workflow_reaches_grounded_answer() -> None:
    provider = ScriptedProvider(
        planning_bundle(),
        AgentControlDecision(
            decision="reason",
            rationale="Собранных evidence paths достаточно.",
        ),
        ReasoningResult(
            summary="Комбинированная схема использует обратный осмос после pretreatment.",
            finding_ids=["finding-ro", "finding-ion"],
            conflicts=["Энергозатраты требуют нормализации условий."],
            knowledge_gaps=["Мало пилотных данных для холодного климата."],
            recommendations=["Система поставила пилот на реальном составе воды в очередь."],
        ),
        CritiqueResult(approved=True, issues=[], revision_instructions=[]),
    )
    workflow = ResearchWorkflow(provider=provider)

    answer = await workflow.run(
        QueryRequest(question="Какие методы обессоливания подходят для шахтной воды?")
    )

    assert answer.model_mode == "scripted"
    assert [event.agent for event in answer.trace] == [
        "intent_router",
        "planner",
        "action_planner",
        "tool_executor",
        "controller",
        "reasoner",
        "critic",
        "synthesizer",
    ]
    assert {finding.id for finding in answer.findings} == {"finding-ro", "finding-ion"}
    assert provider.calls == [
        "PlanningBundle",
        "AgentControlDecision",
        "ReasoningResult",
        "CritiqueResult",
    ]


@pytest.mark.asyncio
async def test_controller_autonomously_replans_missing_evidence() -> None:
    provider = ScriptedProvider(
        planning_bundle().model_copy(
            update={
                "intent": IntentClassification(primary="gap_analysis", entities=["шахтная вода"])
            }
        ),
        AgentControlDecision(
            decision="continue_tools",
            rationale="Не закрыт сравнительный контекст.",
            missing_evidence=["community coverage"],
        ),
        action_plan(),
        AgentControlDecision(decision="reason", rationale="Evidence достаточно."),
        ReasoningResult(
            summary="Evidence собрано автономно за два tool rounds.",
            finding_ids=["finding-ro"],
            conflicts=[],
            knowledge_gaps=[],
            recommendations=[],
        ),
        CritiqueResult(approved=True, issues=[], revision_instructions=[]),
    )

    answer = await ResearchWorkflow(provider=provider).run(
        QueryRequest(question="Найди пробелы по очистке шахтной воды")
    )

    agents = [event.agent for event in answer.trace]
    assert agents.count("action_planner") == 2
    assert agents.count("tool_executor") == 2
    assert agents.count("controller") == 2
    assert len(answer.tool_observations) == 4


@pytest.mark.asyncio
async def test_ingestion_and_self_evolve_are_model_driven() -> None:
    provider = ScriptedProvider(
        IngestionBundle(extraction=ExtractionResult(entities=[], claims=[])),
        EvolutionDraft(
            kind="gold_case",
            title="Добавить экспертную корректировку в regression set",
            change="Зафиксировать исправленный вывод как ожидаемый ответ.",
            impact=["critic", "evaluation harness"],
        ),
    )
    knowledge = InMemoryKnowledgeBase()
    ingestion = IngestionService(knowledge, provider)
    evolution = EvolutionService(provider)

    from scientific_tangle.domain.contracts import DocumentRequest

    receipt = await ingestion.ingest(
        DocumentRequest(
            title="Пилотный отчёт",
            text="Пилотный отчёт содержит результаты испытаний мембранной очистки воды.",
        )
    )
    proposal = await evolution.propose(
        FeedbackRequest(
            query_id=uuid4(),
            verdict="correct",
            comment="Уточнить область применимости",
            correction="Вывод применим только после предварительной очистки.",
        )
    )

    assert receipt.status == "created"
    assert proposal.kind == "gold_case"
    assert provider.calls == ["IngestionBundle", "EvolutionDraft"]
