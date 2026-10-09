from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from scientific_tangle.agents.tools import ResearchToolExecutor
from scientific_tangle.agents.workflow import ResearchWorkflow, _ungrounded_answer_numbers
from scientific_tangle.domain.contracts import (
    AgentActionPlan,
    AgentControlDecision,
    CritiqueResult,
    Finding,
    PlanningBundle,
    QueryRequest,
    ReasoningResult,
    ToolAction,
)
from scientific_tangle.domain.models import EvidenceLocator, QueryPlan
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from scientific_tangle.services.public_sources import MAX_RESPONSE_BYTES, PublicSourceSearch
from tests.fakes import ScriptedProvider


def source(
    identifier: str = "study-a", quote: str = "В исследовании участвовали 24 человека.",
) -> Finding:
    return Finding(
        id=identifier, statement=quote, confidence=0.8,
        evidence=[EvidenceLocator(document_id=uuid4(), page=1, quote=quote)],
    )


@pytest.mark.asyncio
async def test_open_search_uses_actual_abstract_not_title_or_generated_answer() -> None:
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
    assert calls == ["caffeine attention", "caffeine attention"]
    assert second.findings[0].id == identifier
    assert isinstance(second.findings[0].evidence[0].source_url, str)
    assert knowledge.claim_history(identifier) == []  # Общий корпус не загрязняется.


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
