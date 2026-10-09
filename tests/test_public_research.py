from __future__ import annotations

import asyncio
import json
import threading
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from scientific_tangle.agents.tools import ResearchToolExecutor
from scientific_tangle.agents.workflow import (
    ResearchWorkflow,
    _ungrounded_answer_numbers,
    _unit_conflicts,
    _unit_unmatched,
)
from scientific_tangle.domain.contracts import (
    AgentActionPlan,
    AgentControlDecision,
    AnswerPayload,
    CritiqueResult,
    Finding,
    GraphSnapshot,
    PlanningBundle,
    QueryRequest,
    ReasoningResult,
    ToolAction,
)
from scientific_tangle.domain.models import EvidenceLocator, QueryPlan
from scientific_tangle.services.exporter import ExportService, _pdf_blocks
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase, RetrievalContext
from scientific_tangle.services.public_sources import (
    MAX_RESPONSE_BYTES,
    PublicSourceSearch,
    _excerpt,
)
from tests.fakes import ScriptedProvider


def source(
    identifier: str = "study-a", quote: str = "В исследовании участвовали 24 человека.",
) -> Finding:
    return Finding(
        id=identifier, statement=quote, confidence=0.8,
        evidence=[EvidenceLocator(document_id=uuid4(), page=1, quote=quote)],
    )


def test_words_after_sample_size_are_not_quote_units() -> None:
    finding = source(quote="The study included 36 young adults.")
    assert _unit_unmatched(ReasoningResult(summary="В исследовании 36 молодых взрослых."),
                           [finding]) == []


def test_web_plan_keeps_existing_internal_read_and_is_idempotent() -> None:
    plan = AgentActionPlan(actions=[
        ToolAction(id="web", tool="public_search", query="caffeine attention"),
        ToolAction(id="read", tool="finding_lookup", query="Проверь находку",
                   finding_ids=["study-a"]),
    ])
    sanitized = ResearchWorkflow._sanitize_action_plan(plan, "Проверь источники")
    assert [item.tool for item in sanitized.actions] == ["public_search", "finding_lookup"]
    assert ResearchWorkflow._sanitize_action_plan(sanitized, "Проверь источники") == sanitized


@pytest.mark.asyncio
async def test_web_only_model_plan_runs_internal_and_public_sources_concurrently() -> None:
    loop = asyncio.get_running_loop()
    internal_started = asyncio.Event()
    web_started = threading.Event()
    internal = source("internal-study", quote="Внутренний отчёт содержит 24 участника.")

    class ConcurrentKnowledge(InMemoryKnowledgeBase):
        def retrieve(self, plan, retrieval_plan, allowed_data_classes=None, *, abort=None):
            loop.call_soon_threadsafe(internal_started.set)
            assert web_started.wait(timeout=3), "Internal search must overlap web search"
            return RetrievalContext(findings=[internal], graph=GraphSnapshot(nodes=[], edges=[]),
                                    community_summaries=[], no_evidence=False)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["query"] == "(caffeine attention) AND HAS_ABSTRACT:Y"
        web_started.set()
        await asyncio.wait_for(internal_started.wait(), timeout=3)
        return httpx.Response(200, json={"resultList": {"result": [{
            "title": "A controlled trial", "source": "MED", "id": "123",
            "abstractText": "The study included 24 participants.",
        }]}})

    identifier = f"public-{uuid5(NAMESPACE_URL, 'https://europepmc.org/article/MED/123')}"
    provider = ScriptedProvider(
        PlanningBundle(query_plan=QueryPlan(question="Сопоставь источники"),
                       action_plan=AgentActionPlan(actions=[ToolAction(
                           id="web", tool="public_search", query="caffeine attention",
                       )])),
        AgentControlDecision(decision="reason"),
        ReasoningResult(summary="Внутренний отчёт содержит 24 участника "
                        "[отчёт](finding:internal-study).\n\n"
                        f"В публикации 24 участника [статья](finding:{identifier})."),
        CritiqueResult(approved=True),
    )
    knowledge = ConcurrentKnowledge()
    workflow = ResearchWorkflow(knowledge=knowledge, provider=provider)
    workflow.tool_executor = ResearchToolExecutor(
        knowledge, PublicSourceSearch(transport=httpx.MockTransport(handler)),
    )
    answer = await workflow.run(
        QueryRequest(question="Сопоставь закрытый отчёт SecretDoc с наукой")
    )
    assert answer.limitations == []
    assert {item.id for item in answer.findings} == {internal.id, identifier}
    assert {item.tool for item in answer.tool_observations} == {"hybrid_search", "public_search"}
    assert all(item.status == "success" for item in answer.tool_observations)


@pytest.mark.asyncio
async def test_open_search_uses_actual_abstract_not_title_or_generated_answer() -> None:
    assert _excerpt("1200", 2) == ""  # Число не превращается в 12 при срезе без пробелов.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.ebi.ac.uk"
        assert request.url.params["resultType"] == "core"
        return httpx.Response(200, json={"resultList": {"result": [
            {"title": "<i>Caffeine</i> trial", "id": "123", "source": "MED",
             "abstractText": "<h4>Results</h4>Twenty volunteers received <b>100 mg</b>."},
            {"title": "A title is not evidence", "id": "456", "source": "MED"},
        ]}, "answer": "Invented improvement of 99%"})

    service = PublicSourceSearch(transport=httpx.MockTransport(handler))
    result = await service.search("caffeine attention")
    assert len(result.findings) == 1
    finding = result.findings[0]
    evidence = finding.evidence[0]
    assert finding.statement == evidence.quote == "Results Twenty volunteers received 100 mg ."
    assert evidence.source_title == "Caffeine trial"
    assert str(evidence.source_url) == "https://europepmc.org/article/MED/123"
    assert evidence.retrieved_at and evidence.page is None
    assert "99%" not in finding.statement


@pytest.mark.asyncio
async def test_tavily_requires_read_page_and_never_uses_generated_answer_or_snippet() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.tavily.com/search"
        body = json.loads(request.content)
        assert body["include_answer"] is False
        assert body["include_raw_content"] == "text"
        return httpx.Response(200, json={"results": [
            {"title": "Read paper", "url": "https://example.org/paper",
             "raw_content": "The actual result is 12.", "content": "99% from snippet"},
            {"title": "Not read", "url": "https://example.org/unread", "content": "99%"},
            {"title": "Unsafe URL", "url": "javascript:alert(1)", "raw_content": "99%"},
        ]})

    result = await PublicSourceSearch("test-token", transport=httpx.MockTransport(handler)).search(
        "test public topic",
    )
    assert len(result.findings) == 1
    assert result.findings[0].evidence[0].quote == "The actual result is 12."


@pytest.mark.asyncio
async def test_source_transport_failure_does_not_fabricate_findings() -> None:
    service = PublicSourceSearch(transport=httpx.MockTransport(
        lambda request: httpx.Response(503),
    ))
    executor = ResearchToolExecutor(InMemoryKnowledgeBase(), service)
    result = await executor.execute(
        AgentActionPlan(actions=[ToolAction(
            id="external", tool="public_search", query="caffeine",
        )]),
        QueryPlan(question="Исследуй кофеин"),
    )
    assert result.findings == []
    assert result.observations[0].status == "error"


@pytest.mark.asyncio
async def test_oversized_public_response_is_rejected() -> None:
    service = PublicSourceSearch(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
    ))
    with pytest.raises(ValueError, match="размер"):
        await service.search("caffeine")


def test_evidence_requires_real_locator_and_http_url() -> None:
    with pytest.raises(ValidationError):
        EvidenceLocator(document_id=uuid4(), quote="Непривязанный текст")
    with pytest.raises(ValidationError):
        EvidenceLocator(document_id=uuid4(), quote="Текст", source_url="file:///secret")


def test_number_from_another_paper_is_not_support_for_cited_paragraph() -> None:
    findings = [source(), source("study-b", "В другой работе участвовали 42 человека.")]
    answer = ReasoningResult(
        summary="В первом исследовании 42 человека [A](finding:study-a).\n\n"
                "Во втором 42 человека [B](finding:study-b).",
        finding_ids=["study-a", "study-b"],
    )
    assert _ungrounded_answer_numbers(answer, findings) == ["42"]
    assert _ungrounded_answer_numbers(ReasoningResult(summary="Ускорение составляет 99%."), [])


def test_known_source_ids_and_hypothesis_ordinals_are_not_measured_values() -> None:
    identifier = "public-f43b9f0d-47a4-5f4c-9631-73903200c7bc"
    finding = source(identifier)
    answer = ReasoningResult(summary=(
        f"## 1. Основания\n\nИсточник {identifier} содержит 24 участника.\n\n"
        "**Гипотеза 7:** проверить возможное влияние режима сна."
    ), finding_ids=[identifier])
    assert _ungrounded_answer_numbers(answer, [finding]) == []
    formula = ReasoningResult(summary="Формула Δ% = 100·(M_after − M_before)/M_before.")
    assert _ungrounded_answer_numbers(formula, []) == []
    assert _ungrounded_answer_numbers(ReasoningResult(summary="Улучшение составило 100%."), [])


def test_quote_units_do_not_require_separate_numeric_observations() -> None:
    finding = source(quote="The dose was 100-400mg. Another outcome used 200-400mg/day.")
    assert _unit_conflicts(ReasoningResult(summary="Доза 100–400 мг."), [finding]) == []
    assert _unit_conflicts(ReasoningResult(summary="Доза 100–400 мг/сут."), [finding])
    assert _unit_conflicts(ReasoningResult(summary="Доза 100–400 мг в сутки."), [finding])
    assert _unit_conflicts(ReasoningResult(summary="Доза 100–400 г."), [finding])
    assert _unit_conflicts(ReasoningResult(summary="Доза 200–400 мг/сут."), [finding]) == []
    assert _unit_conflicts(ReasoningResult(summary="Длина 100 мм."), [finding])


@pytest.mark.asyncio
async def test_quote_unit_error_is_revised_before_finalization() -> None:
    finding = source(quote="The dose was 100-400mg.")
    knowledge = InMemoryKnowledgeBase()
    knowledge.register_findings([finding])
    provider = ScriptedProvider(
        PlanningBundle(query_plan=QueryPlan(question="Уточни дозу"),
                       action_plan=AgentActionPlan(actions=[ToolAction(
                           id="read", tool="finding_lookup", query="Уточни дозу",
                           finding_ids=[finding.id],
                       )])),
        AgentControlDecision(decision="reason"),
        ReasoningResult(summary="Доза 100–400 мг/сут [A](finding:study-a)."),
        CritiqueResult(approved=True),
        ReasoningResult(summary="Доза 100–400 мг [A](finding:study-a)."),
        CritiqueResult(approved=True),
    )
    answer = await ResearchWorkflow(knowledge=knowledge, provider=provider).run(
        QueryRequest(question="Уточни дозу"),
    )
    assert "мг/сут" not in answer.summary
    assert answer.limitations == []
    assert [item.status for item in answer.trace if item.agent == "critic"] == [
        "revised", "completed",
    ]


@pytest.mark.asyncio
async def test_public_followup_rereads_source_and_persists_plain_url() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["query"])
        return httpx.Response(200, json={"resultList": {"result": [{
            "title": "A controlled trial", "source": "MED", "id": "123",
            "abstractText": "The study included 24 participants.",
        }]}})

    identifier = f"public-{uuid5(NAMESPACE_URL, 'https://europepmc.org/article/MED/123')}"
    external = ToolAction(id="external", tool="public_search", query="caffeine attention")
    provider = ScriptedProvider(
        PlanningBundle(query_plan=QueryPlan(question="Исследуй внимание"),
                       action_plan=AgentActionPlan(actions=[external])),
        AgentControlDecision(decision="reason"),
        ReasoningResult(summary=f"В исследовании 24 участника [A](finding:{identifier}).",
                        finding_ids=[identifier]),
        CritiqueResult(approved=True),
        # Модель просит corpus lookup внешнего ID — маршрут исправляет это.
        PlanningBundle(query_plan=QueryPlan(question="Развить гипотезу"),
                       action_plan=AgentActionPlan(actions=[ToolAction(
                           id="old", tool="finding_lookup", query="Ранее найденный источник",
                           finding_ids=[identifier],
                       )])),
        AgentControlDecision(decision="reason"),
        ReasoningResult(summary=f"Можно проверить иную интерпретацию [A](finding:{identifier}).",
                        finding_ids=[identifier]),
        CritiqueResult(approved=True),
    )
    knowledge = InMemoryKnowledgeBase()
    search = PublicSourceSearch(transport=httpx.MockTransport(handler))
    workflow = ResearchWorkflow(knowledge=knowledge, provider=provider,
                                checkpointer=InMemorySaver())
    workflow.public_search = search
    workflow.tool_executor = ResearchToolExecutor(knowledge, search)
    thread = uuid4()
    first = await workflow.run(QueryRequest(question="Исследуй внимание", thread_id=thread))
    second = await workflow.run(QueryRequest(question="Развить гипотезу", thread_id=thread))
    assert first.limitations == second.limitations == []
    assert calls == ["(caffeine attention) AND HAS_ABSTRACT:Y",
                     "(EXT_ID:123 AND SRC:MED) AND HAS_ABSTRACT:Y"]
    assert second.findings[0].id == identifier
    assert isinstance(second.findings[0].evidence[0].source_url, str)
    assert knowledge.claim_history(identifier) == []  # Общий корпус не загрязняется.


@pytest.mark.asyncio
async def test_disabling_web_search_prevents_followup_source_rereads() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["query"])
        return httpx.Response(200, json={"resultList": {"result": [{
            "title": "A controlled trial", "source": "MED", "id": "123",
            "abstractText": "The study included 24 participants.",
        }]}})

    identifier = f"public-{uuid5(NAMESPACE_URL, 'https://europepmc.org/article/MED/123')}"
    provider = ScriptedProvider(
        PlanningBundle(query_plan=QueryPlan(question="Исследуй"),
                       action_plan=AgentActionPlan(actions=[ToolAction(
                           id="web", tool="public_search", query="caffeine",
                       )])),
        AgentControlDecision(decision="reason"),
        ReasoningResult(summary=f"В исследовании 24 участника [A](finding:{identifier})."),
        CritiqueResult(approved=True),
        PlanningBundle(query_plan=QueryPlan(question="Продолжить"),
                       action_plan=AgentActionPlan(actions=[])),
        ReasoningResult(summary="Без веб-поиска можем обсудить метод проверки."),
        CritiqueResult(approved=True),
        PlanningBundle(query_plan=QueryPlan(question="Проверить снова"),
                       action_plan=AgentActionPlan(actions=[ToolAction(
                           id="web-again", tool="public_search", query="caffeine",
                       )])),
        AgentControlDecision(decision="reason"),
        ReasoningResult(summary=f"В исследовании 24 участника [A](finding:{identifier})."),
        CritiqueResult(approved=True),
    )
    knowledge = InMemoryKnowledgeBase()
    workflow = ResearchWorkflow(knowledge=knowledge, provider=provider,
                                checkpointer=InMemorySaver())
    workflow.tool_executor = ResearchToolExecutor(
        knowledge, PublicSourceSearch(transport=httpx.MockTransport(handler)),
    )
    thread = uuid4()
    await workflow.run(QueryRequest(question="Исследуй", thread_id=thread))
    disabled = await workflow.run(QueryRequest(question="Продолжить", thread_id=thread,
                                               web_search_enabled=False))
    assert len(calls) == 1
    assert disabled.findings == [] and disabled.limitations == []
    assert all(item.tool != "public_search" for item in disabled.tool_observations)
    assert "отключил веб-поиск" in provider.prompts["ReasoningResult"][-1][0]
    enabled = await workflow.run(QueryRequest(question="Проверить снова", thread_id=thread,
                                              web_search_enabled=True))
    assert len(calls) == 2 and enabled.limitations == []
    assert any(item.tool == "public_search" for item in enabled.tool_observations)


@pytest.mark.asyncio
async def test_disabled_web_is_enforced_at_execution_boundary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Disabled public_search must never send an HTTP request")

    knowledge = InMemoryKnowledgeBase()
    knowledge.register_findings([source()])
    workflow = ResearchWorkflow(knowledge=knowledge, provider=ScriptedProvider())
    workflow.tool_executor = ResearchToolExecutor(
        knowledge, PublicSourceSearch(transport=httpx.MockTransport(handler)),
    )
    result = await workflow.tool_executor_node({
        "question": "Проверь находку", "web_search_enabled": False,
        "query_plan": QueryPlan(question="Проверь находку"),
        "action_plan": AgentActionPlan(actions=[
            ToolAction(id="forbidden", tool="public_search", query="caffeine"),
            ToolAction(id="internal", tool="finding_lookup", query="Проверь находку",
                       finding_ids=["study-a"]),
        ]),
    })
    assert [item.id for item in result["findings"]] == ["study-a"]
    assert [item.tool for item in result["observations"]] == ["finding_lookup"]


def test_public_source_url_survives_all_export_formats() -> None:
    finding = source()
    finding.evidence = [EvidenceLocator(
        document_id=uuid4(), quote="The study included 24 participants.",
        source_url="https://europepmc.org/article/MED/123", retrieved_at="2026-10-09T12:00:00Z",
    )]
    answer = AnswerPayload(query_id=uuid4(), question="Проверь публикацию", summary="Обзор",
                           findings=[finding], graph={"nodes": [], "edges": []},
                           confidence=0.8, model_mode="gigachat",
                           query_plan=QueryPlan(question="Проверь публикацию"),
                           conflicts=[], knowledge_gaps=[], recommendations=[], trace=[])
    markdown, _, _ = ExportService().export(answer, "markdown")
    json_ld, _, _ = ExportService().export(answer, "json-ld")
    pdf_text = "\n".join(_pdf_blocks(answer))
    for text in (markdown, json_ld, pdf_text):
        assert "https://europepmc.org/article/MED/123" in text
        assert "2026-10-09T12:00:00Z" in text
        assert "стр. None" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("approved, text, ids", [
    (False, "Непроверенный качественный вывод о причине аварии.", ["study-a"]),
    (True, "Улучшение составило 99% [A](finding:study-a).", ["study-a"]),
    (True, "В источнике 24 человека [поддельная ссылка](finding:invented).", ["invented"]),
    (True, "Ускорение составило 99%.", []),
    (True, "Доказательство [A](finding:study-a) и [статья](https://invented.org/paper).",
     ["study-a"]),
])
async def test_unverified_draft_and_recommendations_never_reach_answer(
    approved: bool, text: str, ids: list[str],
) -> None:
    result = await ResearchWorkflow(provider=ScriptedProvider()).finalize({
        "run_id": str(uuid4()), "question": "Проверь вывод исследования", "language": "ru",
        "query_plan": QueryPlan(question="Проверь вывод исследования"), "findings": [source()],
        "reasoning": ReasoningResult(summary=text, finding_ids=ids,
                                    recommendations=["Выдуманный результат 99%"]),
        "critique": CritiqueResult(approved=approved),
    })
    answer = result["answer"]
    assert text not in answer.summary
    assert "99%" not in answer.summary
    assert "Непроверенный качественный вывод" not in answer.summary
    assert "invented" not in answer.summary
    assert answer.recommendations == []
    assert answer.confidence == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field, text", [
    ("recommendations", "Измеренное улучшение составило 99%."),
    ("knowledge_gaps", "Предыдущее измерение: 99%."),
    ("conflicts", "Источник утверждает улучшение 99%."),
    ("recommendations", "Результат [публикация](https://invented.org/paper)."),
    ("recommendations", "Результат [публикация](finding:invented)."),
    ("recommendations", "Доза 100–400 мг в сутки [A](finding:study-a)."),
])
async def test_auxiliary_answer_fields_cannot_bypass_source_checks(field: str, text: str) -> None:
    result = await ResearchWorkflow(provider=ScriptedProvider()).finalize({
        "run_id": str(uuid4()), "question": "Проверь вывод", "language": "ru",
        "query_plan": QueryPlan(question="Проверь вывод"),
        "findings": [source(quote="The dose was 100-400mg.")],
        "reasoning": ReasoningResult(summary="Уточнение по источнику [A](finding:study-a).",
                                    finding_ids=["study-a"], **{field: [text]}),
        "critique": CritiqueResult(approved=True),
    })
    answer = result["answer"]
    assert answer.confidence == 0
    assert answer.recommendations == answer.conflicts == answer.knowledge_gaps == []
