"""ACL-срез графа в сводках сообществ — регрессия утечки restricted-текста.

Профили сообществ собираются из меток узлов и утверждений, поэтому граф под
сводки обязан приходить уже срезанным по ``data_class``: иначе restricted-текст
попадает в промпт модели в обход AccessPolicyEngine. ``None`` по-прежнему
означает «без ограничений» — полный граф.

Production-ветка проверяется на подделках хранилищ из ``test_retrieval_persistence``:
это доказательство семантики запросов и текста сводок, а не совместимости с
живым Neo4j/Elasticsearch (её проверяет контейнерный контур).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_retrieval_persistence import Harness, build_backend, neo_node

from scientific_tangle.domain.contracts import RetrievalPlan
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase

RESTRICTED_MARKER = "Энергия в 3–5 раз"
RESTRICTED_LABEL = "Энергия в 3–5 раз выше"
PUBLIC_CLASSES = {DataClass.PUBLIC}
OPEN_CLASSES = {DataClass.PUBLIC, DataClass.RESTRICTED}


def global_query(question: str) -> QueryPlan:
    return QueryPlan(question=question, language="ru", mode="global")


def plan(query: str, *, use_local_graph: bool = False) -> RetrievalPlan:
    return RetrievalPlan(
        lexical_query=query,
        semantic_query=query,
        # Якорь «Выпаривание» делает restricted-узел claim-energy достижимым:
        # иначе проверка на его отсутствие в обходе ничего бы не доказывала.
        entity_names=["выпаривание"] if use_local_graph else [],
        relation_types=["REQUIRES"],
        max_hops=2,
        use_global_context=True,
        use_local_graph=use_local_graph,
    )


# ── Memory-контур ───────────────────────────────────────────────────────────


def test_memory_community_briefs_hide_restricted_claim() -> None:
    """Seed содержит restricted-узел claim-energy: без среза его метка ушла бы в сводку."""
    knowledge = InMemoryKnowledgeBase()

    context = knowledge.retrieve(
        global_query("выпаривание энергия"),
        plan("выпаривание энергия", use_local_graph=True),
        PUBLIC_CLASSES,
    )

    assert context.community_summaries
    assert all(RESTRICTED_MARKER not in brief for brief in context.community_summaries)
    assert "claim-energy" not in {node.id for node in context.graph.nodes}


def test_memory_community_briefs_keep_restricted_claim_when_allowed() -> None:
    """Санити против перевыполнения: разрешённым классам restricted-текст доступен."""
    knowledge = InMemoryKnowledgeBase()

    context = knowledge.retrieve(
        global_query("выпаривание энергия"),
        plan("выпаривание энергия", use_local_graph=True),
        OPEN_CLASSES,
    )

    assert RESTRICTED_MARKER in "\n".join(context.community_summaries)
    assert "claim-energy" in {node.id for node in context.graph.nodes}


# ── Production-контур (Neo4j/Elasticsearch на подделках) ────────────────────


def restricted_neo_node() -> dict[str, Any]:
    return {
        "id": "claim-energy",
        "label": RESTRICTED_LABEL,
        "type": "claim",
        "confidence": 0.78,
        "data_class": "restricted",
        "metadata": json.dumps({}, ensure_ascii=False),
    }


def build_restricted_graph(monkeypatch: pytest.MonkeyPatch) -> Harness:
    """Тот же seed, что в memory: restricted-утверждение достижимо из публичного якоря."""
    harness = build_backend(
        monkeypatch,
        graph_nodes=[
            neo_node("water", "Шахтная вода"),
            neo_node("evaporation", "Выпаривание", "process"),
            restricted_neo_node(),
        ],
        graph_edges=[
            {
                "id": "e-pub",
                "source": "water",
                "target": "evaporation",
                "relation": "TREATED_BY",
                "confidence": 0.9,
                "data_class": "public",
            },
            {
                "id": "e-res",
                "source": "evaporation",
                "target": "claim-energy",
                "relation": "REQUIRES",
                "confidence": 0.78,
                "data_class": "restricted",
            },
        ],
    )
    harness.driver.anchors = [
        neo_node("evaporation", "Выпаривание", "process"),
        restricted_neo_node(),
    ]
    return harness


def test_production_community_briefs_hide_restricted_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_restricted_graph(monkeypatch)

    context = harness.knowledge.retrieve(
        global_query("выпаривание энергия"),
        plan("выпаривание энергия", use_local_graph=True),
        PUBLIC_CLASSES,
    )

    assert context.community_summaries
    assert all(RESTRICTED_MARKER not in brief for brief in context.community_summaries)
    # Обход графа фильтруется в Cypher: в запрос обязаны попасть только
    # разрешённые классы, иначе restricted-узел покидает хранилище.
    anchor_calls = harness.driver.issued_containing("MATCH (anchor:Entity)")
    assert anchor_calls
    assert all(params["classes"] == ["public"] for _, params in anchor_calls)


def test_production_community_briefs_keep_restricted_claim_when_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_restricted_graph(monkeypatch)

    context = harness.knowledge.retrieve(
        global_query("выпаривание энергия"),
        plan("выпаривание энергия"),
        OPEN_CLASSES,
    )

    assert RESTRICTED_MARKER in "\n".join(context.community_summaries)
