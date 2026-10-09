import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from scientific_tangle.agents.tools import ResearchToolExecutor
from scientific_tangle.agents.workflow import (
    CRITIC_SYSTEM,
    ResearchTurn,
    ResearchWorkflow,
    _link_finding_mentions,
    _ungrounded_answer_numbers,
)
from scientific_tangle.api.app import AppDependencies, app
from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    AgentActionPlan,
    AgentControlDecision,
    CritiqueResult,
    Finding,
    IntentClassification,
    PlanningBundle,
    QueryRequest,
    ReasoningResult,
    ToolAction,
)
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import EvidenceLocator, QueryPlan
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from tests.auth_support import cookie_header, signup
from tests.fakes import ScriptedProvider


def historical_finding() -> Finding:
    return Finding(
        id="historical-claim",
        statement="Рудник выведен на проектную мощность в 1987 году.",
        confidence=0.9,
        status="hypothesis",
        evidence=[EvidenceLocator(
            document_id=uuid4(), source_title="№ 03_24", page=1,
            quote="Рудник выведен на проектную мощность в 1987 году.",
        )],
    )


def lookup(*identifiers: str) -> AgentActionPlan:
    return AgentActionPlan(actions=[ToolAction(
        id="read", tool="finding_lookup", query="Проверить основания тезиса",
        finding_ids=list(identifiers),
    )])


def plan(
    actions: AgentActionPlan | None = None, intent: str = "literature_review",
) -> PlanningBundle:
    return PlanningBundle(
        query_plan=QueryPlan(question="Исследование"),
        intent=IntentClassification(primary=intent),
        action_plan=actions or AgentActionPlan(actions=[]),
    )


def test_dates_and_markdown_metadata_do_not_require_measurements() -> None:
    answer = ReasoningResult(summary=(
        "## История\n\n1. Рудник вышел на мощность в 1987 году "
        "[№ 03_24, стр. 1](finding:historical-claim).\n"
        "2. Это исторический факт, а не результат измерения."
    ))
    assert _ungrounded_answer_numbers(answer, [historical_finding()]) == []
    invented = answer.model_copy(update={"summary": "Рудник вышел на мощность в 1990 году."})
    assert _ungrounded_answer_numbers(invented, [historical_finding()]) == ["1990"]
    invented_link = answer.model_copy(update={
        "summary": "Рудник вышел на мощность [в 1990 году](finding:historical-claim).",
    })
    assert _ungrounded_answer_numbers(invented_link, [historical_finding()]) == ["1990"]
    plain_reference = answer.model_copy(update={
        "summary": "Рудник выведен на мощность в 1987 году: документ № 03_24, стр. 1.",
        "finding_ids": ["historical-claim"],
    })
    assert _ungrounded_answer_numbers(plain_reference, [historical_finding()]) == []
    planned_metrics = answer.model_copy(update={
        "summary": "План проверки: 1) сравнить p50/p95 до и после изменения; измерить P95.",
    })
    assert _ungrounded_answer_numbers(planned_metrics, [historical_finding()]) == []
    inline_steps = answer.model_copy(update={
        "summary": "Проверки: 1) прочитать документ. 2) уточнить дату. 3) обсудить гипотезу.",
        "recommendations": ["Предлагаю сравнить 2 альтернативных объяснения."],
    })
    assert _ungrounded_answer_numbers(inline_steps, [historical_finding()]) == []


def test_known_plain_reference_becomes_clickable_without_nested_markdown() -> None:
    text = "В находке historical-claim. [Источник](finding:historical-claim). `historical-claim`"
    linked = _link_finding_mentions(text, [historical_finding()])
    assert "В находке [№ 03_24](finding:historical-claim)" in linked
    assert "[Источник](finding:historical-claim)" in linked
    assert "`historical-claim`" in linked
    assert _link_finding_mentions("[historical-claim]", [historical_finding()]) == (
        "[№ 03_24](finding:historical-claim)"
    )
    assert _link_finding_mentions("Источник № 03_24, стр. 1.", [historical_finding()]) == (
        "Источник [№ 03_24](finding:historical-claim), стр. 1."
    )


def test_real_evidence_and_long_draft_keep_sources_in_revision_context() -> None:
    finding = historical_finding()
    demo = finding.model_copy(update={"id": "demo", "scope": {"origin": "demo"}})
    workflow = ResearchWorkflow(provider=ScriptedProvider())
    state = {
        "question": "Напиши обсуждение по находке", "query_plan": QueryPlan(question="Обсуждение"),
        "findings": [demo, finding], "reasoning": ReasoningResult(
            summary="Обсуждение гипотезы. " * 150, finding_ids=[finding.id],
        ),
        "research_history": [ResearchTurn(
            run_id="previous", question="Исследуй", summary="Прошлый длинный ответ. " * 500,
        )],
    }
    selected, _ = workflow._fit_findings({**state, "research_history": []}, budget_tokens=3000)
    assert selected == [finding]
    prompt, budget = workflow._revision_context(state, system=CRITIC_SYSTEM)
    assert finding.evidence[0].quote in prompt
    assert "Обсуждение гипотезы." in prompt
    assert "Прошлый длинный ответ." not in prompt
    assert "FINDINGS" not in budget.dropped
    assert not budget.truncated


@pytest.mark.asyncio
async def test_unused_search_results_do_not_attach_sources_to_reference_answer() -> None:
    workflow = ResearchWorkflow(provider=ScriptedProvider())
    result = await workflow.finalize({
        "run_id": str(uuid4()), "question": "Сколько протонов у углерода?", "language": "ru",
        "query_plan": QueryPlan(question="Сколько протонов у углерода?"),
        "findings": [historical_finding()],
        "reasoning": ReasoningResult(
            summary="Число протонов определяется элементом.", finding_ids=[],
        ),
        "critique": CritiqueResult(approved=True),
    })
    assert result["answer"].findings == []
    assert result["answer"].limitations == []
    assert result["answer"].degradation_reasons == []


def test_interrupted_research_keeps_sources_without_publishing_unchecked_draft() -> None:
    request = QueryRequest(question="Исследуй причины задержки")
    finding = historical_finding()
    answer = ResearchWorkflow(provider=ScriptedProvider())._degraded(
        request, uuid4(), {
            "findings": [finding], "reasoning": ReasoningResult(
                summary="Очередь выросла в 5 раз.", action_plan=lookup(finding.id),
            ),
        }, TimeoutError(),
    )
    assert "5 раз" not in answer.summary
    assert answer.findings == [finding]
    assert answer.limitations
    assert "TimeoutError" not in answer.summary


@pytest.mark.asyncio
async def test_inline_citation_is_grounded_without_redundant_finding_ids() -> None:
    finding = historical_finding()
    workflow = ResearchWorkflow(provider=ScriptedProvider())
    result = await workflow.finalize({
        "run_id": str(uuid4()), "question": "Когда?", "language": "ru",
        "query_plan": QueryPlan(question="Когда?"), "findings": [finding],
        "reasoning": ReasoningResult(
            summary="В 1987 году [№ 03_24](finding:historical-claim)."
        ),
        "critique": CritiqueResult(approved=True),
        "degradation_reasons": ["Узел Reasoner: из промпта исключены TOOL OBSERVATIONS"],
    })
    answer = result["answer"]
    assert answer.findings == [finding]
    assert answer.limitations == []
    assert not any("не сослался" in reason for reason in answer.degradation_reasons)


@pytest.mark.asyncio
async def test_discussion_and_article_can_answer_without_a_search() -> None:
    provider = ScriptedProvider(
        plan(intent="report_generation"),
        ReasoningResult(summary=(
            "## План статьи\n\nПредлагаю разделить постановку проблемы и обсуждение гипотезы."
        )),
        CritiqueResult(approved=True),
    )
    answer = await ResearchWorkflow(provider=provider).run(
        QueryRequest(question="Предложи структуру статьи и объясни её")
    )
    assert answer.summary.startswith("## План статьи")
    assert answer.limitations == []
    assert answer.degradation_reasons == []
    assert provider.calls == ["PlanningBundle", "ReasoningResult", "CritiqueResult"]


@pytest.mark.asyncio
async def test_author_can_reopen_research_before_writing_answer() -> None:
    finding = historical_finding()
    knowledge = InMemoryKnowledgeBase()
    knowledge.register_findings([finding])
    provider = ScriptedProvider(
        plan(),
        ReasoningResult(action_plan=lookup(finding.id)),
        AgentControlDecision(decision="reason"),
        ReasoningResult(summary="В 1987 году [документ](finding:historical-claim)."),
        CritiqueResult(approved=True),
    )
    answer = await ResearchWorkflow(knowledge=knowledge, provider=provider).run(
        QueryRequest(question="Проверь дату в найденном тезисе")
    )
    assert answer.findings == [finding]
    assert provider.calls.count("ReasoningResult") == 2
    assert [event.agent for event in answer.trace] == [
        "planning_agent", "reasoner", "tool_executor", "controller", "reasoner",
        "critic", "synthesizer",
    ]
    assert answer.limitations == []


@pytest.mark.asyncio
async def test_controller_supplies_next_tools_without_separate_planning_call() -> None:
    finding = historical_finding()
    knowledge = InMemoryKnowledgeBase()
    knowledge.register_findings([finding])
    provider = ScriptedProvider(
        plan(lookup("missing")),
        AgentControlDecision(decision="continue_tools", action_plan=lookup(finding.id)),
        ReasoningResult(summary="В 1987 году [документ](finding:historical-claim)."),
        CritiqueResult(approved=True),
    )
    answer = await ResearchWorkflow(knowledge=knowledge, provider=provider).run(
        QueryRequest(question="Найди исходный тезис и проверь дату")
    )
    assert answer.findings == [finding]
    assert "AgentActionPlan" not in provider.calls
    assert not any(
        "доказательств по запросу не найдено" in note for note in answer.degradation_reasons
    )


@pytest.mark.asyncio
async def test_followup_reads_history_and_reloads_original_evidence() -> None:
    finding = historical_finding()
    knowledge = InMemoryKnowledgeBase()
    knowledge.register_findings([finding])
    provider = ScriptedProvider(
        plan(lookup(finding.id)), plan(),
        AgentControlDecision(decision="reason"), AgentControlDecision(decision="reason"),
        ReasoningResult(summary="В 1987 году [документ](finding:historical-claim)."),
        ReasoningResult(summary="Эта дата фиксирует событие [источник](finding:historical-claim)."),
        CritiqueResult(approved=True), CritiqueResult(approved=True),
    )
    workflow = ResearchWorkflow(
        knowledge=knowledge, provider=provider, checkpointer=InMemorySaver(),
    )
    request = QueryRequest(question="Когда рудник вышел на мощность?")
    await workflow.run(request)
    await workflow.run(request.model_copy(update={"question": "Что означает эта дата?"}))
    history_prompt = provider.prompt_for("PlanningBundle")
    assert "Когда рудник вышел на мощность?" in history_prompt
    assert "historical-claim" in history_prompt
    assert finding.evidence[0].quote in provider.prompt_for("ReasoningResult")


def test_http_and_stream_resume_conversation_with_account_isolation() -> None:
    provider = ScriptedProvider(
        plan(), plan(), plan(),
        ReasoningResult(summary="Обсуждаем углерод."),
        ReasoningResult(summary="Продолжаем обсуждение изотопов углерода."),
        ReasoningResult(summary="Уточните элемент."),
        CritiqueResult(approved=True), CritiqueResult(approved=True), CritiqueResult(approved=True),
    )
    dependencies = AppDependencies()
    dependencies.provider = provider
    thread = str(uuid4())
    with TestClient(app) as client:
        client.app.state.dependencies = dependencies
        first = signup(client, "dialogue-first@mindai.tech")
        second = signup(client, "dialogue-second@mindai.tech")
        response = client.post("/api/v1/query", json={
            "question": "Расскажи об углероде", "thread_id": thread,
        }, headers=cookie_header(first))
        assert response.status_code == 200
        assert response.json()["answer"]["conversation_id"] == thread
        streamed = client.post("/api/v1/query/stream", json={
            "question": "А его изотопы?", "thread_id": thread,
        }, headers=cookie_header(first))
        assert streamed.status_code == 200
        events = [json.loads(line[6:]) for line in streamed.text.splitlines()
                  if line.startswith("data: ")]
        assert next(event["answer"] for event in events if event.get("answer"))[
            "conversation_id"
        ] == thread
        assert "Обсуждаем углерод." in provider.prompts["PlanningBundle"][1][1]
        isolated = client.post("/api/v1/query", json={
            "question": "Какой элемент мы обсуждали?", "thread_id": thread,
        }, headers=cookie_header(second))
        assert isolated.status_code == 200
        assert "Обсуждаем углерод." not in provider.prompts["PlanningBundle"][2][1]


@pytest.mark.asyncio
async def test_lookup_cannot_read_restricted_finding() -> None:
    finding = historical_finding().model_copy(update={"data_class": DataClass.RESTRICTED})
    knowledge = InMemoryKnowledgeBase()
    knowledge.register_findings([finding])
    result = await ResearchToolExecutor(knowledge).execute(
        lookup(finding.id), QueryPlan(question="Прочитай находку"), {DataClass.PUBLIC},
    )
    assert result.findings == []
    assert result.observations[0].status == "warning"
    assert finding.evidence[0].quote not in result.observations[0].model_dump_json()


@pytest.mark.asyncio
async def test_author_search_loop_stops_at_budget() -> None:
    provider = ScriptedProvider(
        plan(lookup("missing")),
        ReasoningResult(action_plan=lookup("missing")),
        ReasoningResult(summary="Подтверждение даты не найдено; это остаётся предположением."),
        CritiqueResult(approved=True),
    )
    answer = await ResearchWorkflow(
        provider=provider, settings=Settings(agent_max_tool_rounds=1),
    ).run(QueryRequest(question="Проверь историческую дату"))
    assert "остаётся предположением" in answer.summary
    assert len(answer.tool_observations) == 1
    assert answer.limitations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "Число протонов равно атомному номеру элемента.",
    "О каком химическом элементе идёт речь? Число протонов зависит от элемента.",
])
async def test_reference_science_and_clarification_do_not_require_corpus_measurements(
    text: str,
) -> None:
    provider = ScriptedProvider(
        plan(), ReasoningResult(summary=text), CritiqueResult(approved=True),
    )
    answer = await ResearchWorkflow(provider=provider).run(
        QueryRequest(question="Сколько протонов в атоме?")
    )
    assert answer.summary == text
    assert answer.limitations == []
    assert answer.degradation_reasons == []
    assert answer.tool_observations == []
