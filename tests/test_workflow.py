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
            finding_ids=["finding-ro", "finding-ro-pilot"],
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
        "planning_agent",
        "tool_executor",
        "controller",
        "reasoner",
        "critic",
        "synthesizer",
    ]
    assert {finding.id for finding in answer.findings} == {"finding-ro", "finding-ro-pilot"}
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
    # Контроллер сам решает добрать доказательства: второй проход
    # action_planner → tool_executor → controller без участия пользователя.
    assert agents == [
        "planning_agent",
        "tool_executor",
        "controller",
        "action_planner",
        "tool_executor",
        "controller",
        "reasoner",
        "critic",
        "synthesizer",
    ]
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


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["graph_edit", "report_generation"])
async def test_intents_without_retrieval_exit_before_the_tool_loop(intent: str) -> None:
    """Запросам вне action space не нужен ни retrieval, ни цикл критика."""
    provider = ScriptedProvider(
        planning_bundle().model_copy(
            update={"intent": IntentClassification(primary=intent, entities=["шахтная вода"])}
        )
    )

    answer = await ResearchWorkflow(provider=provider).run(
        QueryRequest(question="Перестрой граф по новым документам и собери отчёт")
    )

    assert provider.calls == ["PlanningBundle"], "ни tools, ни reasoner, ни critic не запускаются"
    assert [event.agent for event in answer.trace] == ["planning_agent", "synthesizer"]
    assert answer.findings == []
    assert answer.degradation_reasons, "ответ вне action space обязан быть помечен"


@pytest.mark.asyncio
async def test_revision_prompts_keep_draft_and_critique_sections() -> None:
    """Секции, ради которых узел вызывается, обязаны доходить до модели.

    Без резерва в бюджете ``fit_sections`` сбрасывала хвост: Critic получал
    доказательство без черновика, Improver — черновик без замечаний. Экранирование
    кириллицы в юникод-последовательности раздувало промпт и само по себе
    выталкивало секции за бюджет.
    """
    provider = ScriptedProvider(
        planning_bundle(),
        AgentControlDecision(decision="reason", rationale="Доказательств достаточно."),
        ReasoningResult(
            summary="Обратный осмос обеспечивает удаление 95–99% растворённых солей.",
            finding_ids=["finding-ro"],
            conflicts=[],
            knowledge_gaps=[],
            recommendations=[],
        ),
        CritiqueResult(
            approved=False,
            issues=["Не названа границ применимости по сухому остатку."],
            revision_instructions=["Указать сухой остаток ≤1000 мг/л."],
        ),
        ReasoningResult(
            summary="Обратный осмос даёт 95–99% задержания при сухом остатке ≤1000 мг/л.",
            finding_ids=["finding-ro"],
            conflicts=[],
            knowledge_gaps=[],
            recommendations=[],
        ),
        CritiqueResult(approved=True, issues=[], revision_instructions=[]),
    )

    answer = await ResearchWorkflow(provider=provider).run(
        QueryRequest(question="Какие методы обессоливания подходят для шахтной воды?")
    )

    assert [event.agent for event in answer.trace].count("improver") == 1
    critic_prompt = provider.prompt_for("CritiqueResult", 0)
    assert "DRAFT" in critic_prompt
    assert "Обратный осмос обеспечивает удаление" in critic_prompt
    improver_prompt = provider.prompt_for("ReasoningResult", 1)
    assert "CRITIQUE" in improver_prompt
    assert "Не названа границ применимости" in improver_prompt
    for prompt in (critic_prompt, improver_prompt):
        assert "\\u04" not in prompt, "кириллица не должна уходить в ASCII-экранирование"
        assert "FINDINGS" in prompt, "доказательство не вправо жертвовать бюджет"
