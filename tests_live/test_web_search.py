"""Опциональная живая проверка Tavily и переключателя веб-поиска."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from scientific_tangle.agents.tools import ResearchToolExecutor
from scientific_tangle.agents.workflow import ResearchWorkflow
from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import AgentActionPlan, QueryRequest, ToolAction
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from scientific_tangle.services.public_sources import PublicSourceSearch

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(os.getenv("STORMIDEA_LIVE_WEB") != "1", reason="opt-in live web search"),
]

QUERY = "carbon atomic number protons Royal Society of Chemistry periodic table"


async def test_live_tavily_reads_source_text(tmp_path: Path) -> None:
    settings = Settings(knowledge_backend="memory", accounts_backend="memory")
    assert settings.tavily_api_key
    executor = ResearchToolExecutor(InMemoryKnowledgeBase(),
                                    PublicSourceSearch(settings.tavily_api_key))
    result = await executor.execute(AgentActionPlan(actions=[ToolAction(
        id="web", tool="public_search", query=QUERY,
    )]), QueryPlan(question="Сколько протонов у углерода?"))
    (tmp_path / "tavily-sources.json").write_text(json.dumps([
        finding.model_dump(mode="json") for finding in result.findings
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    assert result.observations[0].status == "success"
    assert result.findings and all(
        finding.evidence[0].source_url and finding.evidence[0].quote
        for finding in result.findings
    )


async def test_live_agent_honors_web_switch(tmp_path: Path) -> None:
    settings = Settings(knowledge_backend="memory", accounts_backend="memory")
    assert settings.tavily_api_key and settings.gigachat_api_key
    workflow = ResearchWorkflow(knowledge=InMemoryKnowledgeBase(), settings=settings,
                                checkpointer=InMemorySaver())
    thread = uuid4()
    enabled = await workflow.run(QueryRequest(
        question="Сколько протонов в атоме углерода? Используй public_search: " + QUERY
        + ". Подтверди официальным источником. Ответь одним коротким абзацем со ссылкой.",
        thread_id=thread, web_search_enabled=True,
    ))
    disabled = await workflow.run(QueryRequest(
        question="Веб-поиск отключён. Объясни, какие ограничения это накладывает "
        "на следующую проверку внешнего факта. Без новых фактов и чисел, кратко.",
        thread_id=thread, web_search_enabled=False,
    ))
    (tmp_path / "web-switch.json").write_text(json.dumps({
        "enabled": enabled.model_dump(mode="json"),
        "disabled": disabled.model_dump(mode="json"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not enabled.limitations
    assert any(item.tool == "public_search" and item.status == "success"
               for item in enabled.tool_observations)
    assert any(item.tool in {"hybrid_search", "finding_lookup", "graph_traverse", "community_search"}
               for item in enabled.tool_observations)
    assert "6" in enabled.summary and "finding:" in enabled.summary
    assert all(item.tool != "public_search" for item in disabled.tool_observations)
    assert not disabled.limitations
