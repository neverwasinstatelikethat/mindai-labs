"""Инварианты GraphRAG-контура: retrieval, сообщества, права, бюджеты контекста.

Регрессии на находки G-1..G-10 и раздел ACL: класс данных обязан доезжать до
графа, «нет доказательств» не подменяется seed-данными, а бюджет токенов
срезает нижние секции, а не доказательства.

Живых Neo4j/Elasticsearch в тестовом контуре нет, поэтому production-ветка
проверяется на фейках хранилищ: это доказательство семантики и текста запросов,
а не совместимость с реальным сервером (её проверяет контейнерный контур).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from uuid import uuid4

import pytest

from scientific_tangle.agents.tools import ResearchToolExecutor
from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    DocumentRequest,
    EvidenceLocator,
    ExtractedClaim,
    ExtractedEntity,
    ExtractionResult,
    Finding,
    GraphEdge,
    GraphNode,
    NodeType,
    RetrievalPlan,
    ToolAction,
)
from scientific_tangle.domain.intelligence import (
    ComparableValue,
    DataClass,
    KnowledgeKind,
    ResearchClaim,
    ScopeDimension,
)
from scientific_tangle.domain.models import NumericObservation, QueryPlan
from scientific_tangle.services.communities import (
    FALLBACK_COMMUNITY,
    MIN_COMMUNITY_SIZE,
    detect_communities,
)
from scientific_tangle.services.context_budget import (
    estimate_tokens,
    fit_sections,
    select_relevant,
)
from scientific_tangle.services.embeddings import GigaChatEmbeddingClient
from scientific_tangle.services.infrastructure import (
    ANCHORS_CYPHER,
    EDGES_CYPHER,
    EXPAND_CYPHER,
    reciprocal_rank_fusion,
)
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from scientific_tangle.services.research_intelligence import (
    DEFAULT_GAP_LIMIT,
    ResearchIntelligenceService,
    build_research_space,
    to_research_claims,
)

DOC_ID = uuid4()
PUBLIC_SCOPE = {DataClass.PUBLIC, DataClass.INTERNAL}


def observation(prop: str, low: float, high: float, unit: str = "%") -> NumericObservation:
    return NumericObservation(
        property_name=prop,
        operator="between",
        min_value=low,
        max_value=high,
        normalized_min=low,
        normalized_max=high,
        unit=unit,
        normalized_unit=unit,
        raw_text=f"{low:g}–{high:g} {unit}",
    )


def make_finding(
    identifier: str,
    statement: str,
    observations: list[NumericObservation],
    scope: dict[str, str] | None = None,
    data_class: DataClass = DataClass.PUBLIC,
) -> Finding:
    return Finding(
        id=identifier,
        statement=statement,
        confidence=0.8,
        data_class=data_class,
        evidence=[
            EvidenceLocator(
                document_id=DOC_ID,
                source_title="Отчёт по пилоту",
                page=1,
                quote=statement[:40],
            )
        ],
        observations=observations,
        scope=scope or {"geography": "RU"},
    )


def claim(identifier: str, prop: str, low: float, high: float, scope: list[str]) -> ResearchClaim:
    return ResearchClaim(
        id=f"claim-{identifier}",
        finding_id=f"finding-{identifier}",
        subject_id="reverse-osmosis",
        predicate="HAS_SALT_REJECTION",
        value=ComparableValue(property_name=prop, min_value=low, max_value=high, unit="%"),
        scope=[ScopeDimension(name="geography", value=value) for value in scope],
        evidence_ids=[f"evidence-{identifier}"],
        knowledge_kind=KnowledgeKind.EXTRACTED,
    )


def clique(prefix: str, size: int) -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes = [
        GraphNode(id=f"{prefix}-{index}", label=f"{prefix} узел {index}", type=NodeType.MATERIAL)
        for index in range(size)
    ]
    edges = [
        GraphEdge(
            id=f"{prefix}-{left}-{right}",
            source=f"{prefix}-{left}",
            target=f"{prefix}-{right}",
            relation="CONTAINS",
        )
        for left in range(size)
        for right in range(left + 1, size)
    ]
    return nodes, edges


def plan(
    query: str,
    *,
    semantic_query: str | None = None,
    use_global_context: bool = False,
) -> RetrievalPlan:
    return RetrievalPlan(
        lexical_query=query,
        semantic_query=semantic_query or query,
        entity_names=[],
        relation_types=["TREATED_BY"],
        max_hops=3,
        use_global_context=use_global_context,
    )


# ── Сообщества (G-1) ────────────────────────────────────────────────────────


def test_detect_communities_splits_two_dense_cliques() -> None:
    left, left_edges = clique("left", MIN_COMMUNITY_SIZE)
    right, right_edges = clique("right", MIN_COMMUNITY_SIZE)
    bridge = GraphEdge(id="bridge", source=left[0].id, target=right[0].id, relation="CONTAINS")

    names = detect_communities([*left, *right], [*left_edges, *right_edges, bridge])

    assert len(names) == 2
    left_labels = {node.metadata["community"] for node in left}
    right_labels = {node.metadata["community"] for node in right}
    assert len(left_labels) == 1
    assert len(right_labels) == 1
    assert left_labels != right_labels


def test_detect_communities_is_deterministic() -> None:
    first_nodes, first_edges = clique("a", MIN_COMMUNITY_SIZE)
    second_nodes, second_edges = clique("a", MIN_COMMUNITY_SIZE)

    detect_communities(first_nodes, first_edges)
    detect_communities(second_nodes, second_edges)

    assert [node.metadata["community"] for node in first_nodes] == [
        node.metadata["community"] for node in second_nodes
    ]


def test_isolated_nodes_get_a_single_fallback_community() -> None:
    nodes, _ = clique("lonely", 3)

    names = detect_communities(nodes, [])

    assert names == [FALLBACK_COMMUNITY]
    assert all(node.metadata["community"] == FALLBACK_COMMUNITY for node in nodes)


def test_memory_backend_returns_computed_communities() -> None:
    graph = InMemoryKnowledgeBase().full_graph()

    assert graph.communities
    assert all(node.metadata.get("community") for node in graph.nodes)


# ── Retrieval и отсутствие доказательств (G-3) ─────────────────────────────


def test_retrieval_reports_no_evidence_instead_of_seeding() -> None:
    context = InMemoryKnowledgeBase().retrieve(
        QueryPlan(question="ыфывфыв", language="ru", mode="hybrid"),
        plan("аааа-нет-такого-в-корпусе-фывфыв"),
    )

    assert context.findings == []
    assert context.no_evidence is True


def test_retrieval_returns_evidence_and_disables_the_flag() -> None:
    context = InMemoryKnowledgeBase().retrieve(
        QueryPlan(question="обратный осмос", language="ru", mode="hybrid"),
        plan("шахтная вода обратный осмос"),
    )

    assert context.findings
    assert context.no_evidence is False


# ── Поля плана retrieval исполняются, а не докладываются (G-4) ──────────────


def test_global_context_flag_changes_the_briefs_in_memory() -> None:
    """``use_global_context`` обязан влиять на выдачу, а не только на notes."""
    knowledge = InMemoryKnowledgeBase()
    question = QueryPlan(question="обратный осмос", language="ru", mode="global")

    requested = knowledge.retrieve(question, plan("обратный осмос", use_global_context=True))
    skipped = knowledge.retrieve(question, plan("обратный осмос", use_global_context=False))

    assert requested.community_summaries
    assert skipped.community_summaries == []
    # Сводка содержательна: называет состав и утверждения сообщества, а не только ярлык.
    assert any("сущности:" in brief for brief in requested.community_summaries)
    assert all("сущностей" in brief for brief in requested.community_summaries)


def test_unbuildable_global_context_is_named_in_degradation_reasons() -> None:
    """Сообществ нет — ответ обязан сказать об этом, а не молча сузиться до чанков."""
    knowledge = InMemoryKnowledgeBase()
    knowledge._graph.nodes = []
    knowledge._graph.edges = []

    context = knowledge.retrieve(
        QueryPlan(question="обратный осмос", language="ru", mode="global"),
        plan("обратный осмос", use_global_context=True),
    )

    assert context.community_summaries == []
    assert any(
        "глобальный контекст" in reason.lower() for reason in context.degradation_reasons
    )


def test_semantic_query_is_declared_unexecutable_in_memory() -> None:
    knowledge = InMemoryKnowledgeBase()
    question = QueryPlan(question="обратный осмос", language="ru", mode="hybrid")

    diverged = knowledge.retrieve(
        question,
        plan("осмос шахтная вода", semantic_query="мембранное обессоливание рассола"),
    )
    identical = knowledge.retrieve(question, plan("обратный осмос"))

    assert diverged.vectors_used is False
    assert any(
        "Семантическая ветка" in reason and "недоступна" in reason
        for reason in diverged.degradation_reasons
    )
    # Отдельного векторного поиска не было, и план с совпадающими запросами не
    # должен притворяться деградацией.
    assert not any("Семантическая ветка" in reason for reason in identical.degradation_reasons)


# ── Класс данных в контуре доказательства (G-10 / ACL) ─────────────────────


def test_data_class_propagates_from_document_to_findings_and_graph() -> None:
    knowledge = InMemoryKnowledgeBase()
    extraction = ExtractionResult(
        entities=[
            ExtractedEntity(name="Мембрана", canonical_name="Мембрана", type=NodeType.MATERIAL)
        ],
        claims=[
            ExtractedClaim(
                subject="Мембрана",
                predicate="HAS_PROPERTY",
                object="Мембрана",
                statement="Мембрана задерживает 97% солей по ограниченному отчёту.",
                confidence=0.7,
                evidence_quote="задерживает 97% солей",
                observations=[observation("salt_rejection", 96, 98)],
            )
        ],
    )

    knowledge.ingest(
        DocumentRequest(
            title="Ограниченный отчёт",
            text="Сквозная проверка прав доступа к ограниченным данным пилотного проекта.",
            data_class=DataClass.RESTRICTED,
        ),
        extraction,
    )

    visible = knowledge.all_findings(PUBLIC_SCOPE)
    restricted = knowledge.all_findings({DataClass.RESTRICTED})
    graph = knowledge.full_graph({DataClass.PUBLIC})

    assert restricted
    assert all(item.data_class is DataClass.RESTRICTED for item in restricted)
    assert not any(item.data_class is DataClass.RESTRICTED for item in visible)
    assert all(node.data_class is DataClass.PUBLIC for node in graph.nodes)
    # Висячих рёбер после ACL-среза остаться не должно.
    kept = {node.id for node in graph.nodes}
    assert all(edge.source in kept and edge.target in kept for edge in graph.edges)


def test_restricted_findings_are_cut_from_ranking() -> None:
    knowledge = InMemoryKnowledgeBase()

    public = knowledge.rank_findings("обратный осмос", 5, "hybrid", {DataClass.PUBLIC})
    everything = knowledge.rank_findings("обратный осмос", 5, "hybrid")

    assert public
    assert all(item.data_class is not DataClass.RESTRICTED for item in public)
    assert len(public) <= len(everything)


# ── План retrieval наследует решение Planner (G-2) ─────────────────────────


def test_tool_plan_inherits_global_mode_and_hop_limit() -> None:
    executor = ResearchToolExecutor(InMemoryKnowledgeBase())
    query_plan = QueryPlan(
        question="Сравнение методов",
        language="ru",
        mode="global",
        entity_mentions=["шахтная вода"],
        max_hops=4,
    )
    action = ToolAction(
        id="community-1",
        tool="community_search",
        purpose="Обзор сообществ",
        query="методы обессоливания",
        max_hops=4,
    )

    retrieval = executor._retrieval_plan(action, query_plan)

    assert retrieval.use_community_context is True
    assert retrieval.use_global_context is True
    assert retrieval.use_local_graph is False
    assert retrieval.max_hops == 4


def test_local_mode_keeps_graph_and_drops_global_context() -> None:
    executor = ResearchToolExecutor(InMemoryKnowledgeBase())
    query_plan = QueryPlan(question="Факт", language="ru", mode="local", max_hops=2)
    action = ToolAction(
        id="graph-1",
        tool="graph_traverse",
        purpose="Путь в графе",
        query="вода метод очистки",
        max_hops=4,
    )

    retrieval = executor._retrieval_plan(action, query_plan)

    assert retrieval.use_community_context is False
    assert retrieval.use_global_context is False
    assert retrieval.use_local_graph is True
    assert retrieval.max_hops == 2


# ── RRF-слияние веток (G-6) ─────────────────────────────────────────────────


def test_rrf_rewards_agreement_between_legs() -> None:
    scores = reciprocal_rank_fusion(
        [(["a", "b", "c"], 1.0), (["b", "c", "a"], 0.6)],
        k=5,
    )

    assert scores["b"] > scores["c"]
    assert set(scores) == {"a", "b", "c"}


def test_rrf_zero_weight_leg_cannot_outrank_evidence() -> None:
    scores = reciprocal_rank_fusion([(["a"], 1.0), (["b"], 0.0)])

    assert scores["a"] > 0
    assert scores.get("b", 0.0) == 0.0


# ── Числовые тезисы: без самоконфликтов и без выдуманных нулей (G-5) ───────


def test_two_observations_of_one_finding_do_not_self_conflict() -> None:
    item = make_finding(
        "two-numbers",
        "RO задерживает 95–99% солей, на пилоте 80–85%.",
        [observation("salt_rejection", 95, 99), observation("salt_rejection", 80, 85)],
    )

    claims = to_research_claims([item])
    conflicts = ResearchIntelligenceService().detect_conflicts(claims)

    assert len(claims) == 2
    assert {claim.finding_id for claim in claims} == {"two-numbers"}
    # Идентичности наблюдений различаются: раньше оба конца конфликта
    # назывались одним id, и спор «X против X» нельзя было разобрать.
    assert conflicts
    assert all(conflict.left_claim_id != conflict.right_claim_id for conflict in conflicts)
    assert all("внутри одного тезиса" in conflict.reason for conflict in conflicts)


def test_qualitative_finding_produces_no_numeric_claim() -> None:
    without_observations = make_finding("no-numbers", "Качественное описание без чисел.", [])
    without_evidence = without_observations.model_copy(update={"evidence": []})

    assert to_research_claims([without_observations, without_evidence]) == []


def test_gap_summary_is_capped_and_reports_omitted() -> None:
    service = ResearchIntelligenceService()
    claims = [
        claim(f"{index}", "salt_rejection", 90, 95, [f"G{index % 4}"])
        for index in range(4)
    ]
    space = build_research_space(claims)
    assert space is not None
    space.dimensions["reagent"] = ["r1", "r2", "r3", "r4", "r5"]

    summary = service.summarize_gaps(claims, space)

    assert len(summary.gaps) == DEFAULT_GAP_LIMIT
    assert summary.total > DEFAULT_GAP_LIMIT
    assert summary.omitted == summary.total - summary.covered - len(summary.gaps)
    assert summary.omitted > 0


# ── Бюджет контекста (P-4 / V-6) ────────────────────────────────────────────


def test_russian_tokens_are_not_underestimated() -> None:
    text = "Обратный осмос обеспечивает удаление растворённых солей"

    assert estimate_tokens(text) > len(text) // 4
    assert estimate_tokens("") == 0


def test_fit_sections_drops_low_priority_instead_of_truncating_evidence() -> None:
    evidence = "Доказательство: " + ("данные испытаний мембраны. " * 40)
    budget = estimate_tokens("ВОПРОС\nКакие методы?") + estimate_tokens(evidence) + 8

    context = fit_sections(
        [("ВОПРОС", "Какие методы?"), ("FINDINGS", evidence), ("GAPS", "Пробелы по климату.")],
        budget,
    )

    assert "FINDINGS" in context.text
    assert "Доказательство:" in context.text
    assert context.dropped == ("GAPS",)
    assert context.truncated is False


def test_fit_sections_truncates_only_when_nothing_else_fits() -> None:
    long_body = "д" * 4000

    context = fit_sections([("FINDINGS", long_body)], 300)

    assert context.truncated is True
    assert "контекст усечён" in context.text
    assert len(context.text) < len(long_body)


def test_select_relevant_prefers_match_over_arrival_order() -> None:
    items = [
        make_finding("old", "Устаревший тезис про выпаривание рассола", []),
        make_finding("match", "Обратный осмос для шахтной воды", []),
    ]

    selected = select_relevant(items, "шахтная вода обратный осмос", 1)

    assert [item.id for item in selected] == ["match"]


# ── Эмбеддинги: батчинг и отсутствие взаимных блокировок ───────────────────


class _Item:
    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class _Payload:
    def __init__(self, items: list[_Item]) -> None:
        self.data = items


class _FakeGigaChat:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension
        self.batches: list[list[str]] = []

    def embeddings(self, texts: list[str], model: str) -> _Payload:
        self.batches.append(list(texts))
        vectors = [_Item(index, [float(index)] * self.dimension) for index in range(len(texts))]
        return _Payload(vectors)


def _client(dimension: int = 4) -> GigaChatEmbeddingClient:
    settings = Settings(knowledge_backend="memory", gigachat_api_key="test-key")
    client = GigaChatEmbeddingClient(settings)
    client._client = _FakeGigaChat(dimension)  # type: ignore[assignment]
    return client


def test_embeddings_are_batched_and_keep_order() -> None:
    client = _client()

    vectors = client.documents([f"текст {index}" for index in range(20)])

    batches = client._client.batches  # type: ignore[union-attr]
    assert [len(batch) for batch in batches] == [8, 8, 4]
    assert len(vectors) == 20
    assert vectors[0][0] == 0.0
    assert vectors[1][0] == 1.0


def test_dimension_reconciliation_does_not_deadlock() -> None:
    """_reconcile_dimensions вызывается из-под замка: повторный захват вешал индексацию."""
    client = _client(dimension=5)
    client._dimensions = 3

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: (client.document("проверка размерности"), client.dimensions))
        try:
            _, dimensions = future.result(timeout=10)
        except FutureTimeout as error:
            raise AssertionError("reconcile_dimensions заблокировал замок") from error

    assert dimensions == 5


def test_embedding_failure_raises_instead_of_returning_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scientific_tangle.services.embeddings import EmbeddingError

    client = _client()

    def broken(texts: list[str], model: str) -> object:
        raise ConnectionError("сервер недоступен")

    monkeypatch.setattr(client._client, "embeddings", broken)
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    with pytest.raises(EmbeddingError):
        client.document("что-то пошло не так")


class _Counters:
    def __init__(self, nodes: int, relationships: int) -> None:
        self.nodes_created = nodes
        self.relationships_created = relationships


class _Summary:
    def __init__(self, counters: _Counters) -> None:
        self.counters = counters


class _Result:
    def __init__(self, counters: _Counters) -> None:
        self._counters = counters

    def consume(self) -> _Summary:
        return _Summary(self._counters)


class _Session:
    """Идемпотентный MERGE: повторное слияние тех же идентификаторов ничего не создаёт.

    Узел и рёбра различаются по тексту запроса, идентификаторы берутся из
    параметров — этого достаточно, чтобы проверить источник счётчиков.
    """

    def __init__(self, nodes: set[str], relationships: set[str]) -> None:
        self._nodes = nodes
        self._relationships = relationships

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exception: object) -> bool:
        return False

    @staticmethod
    def _merge(seen: set[str], key: str) -> int:
        if key in seen:
            return 0
        seen.add(key)
        return 1

    def run(self, query: str, **params: object) -> _Result:
        is_edge = "]->" in query
        relationships = 0
        if is_edge:
            edge_id = (
                params.get("asserts_id")
                or params.get("pred_id")
                or params.get("support_id")
                or params.get("id")
                or ""
            )
            relationships = self._merge(self._relationships, str(edge_id))
        nodes = 0
        if "MERGE (pub:" in query:
            nodes = self._merge(self._nodes, str(params.get("pub_id") or ""))
        elif not is_edge:
            nodes = self._merge(self._nodes, str(params.get("id") or ""))
        return _Result(_Counters(nodes, relationships))


class _Driver:
    def __init__(self) -> None:
        self.nodes: set[str] = set()
        self.relationships: set[str] = set()

    def session(self) -> _Session:
        return _Session(self.nodes, self.relationships)


def test_traversal_cypher_keeps_chunks_out_of_the_graph() -> None:
    """Чанк — доказательство, а не сущность обхода: в Cypher он отсекается.

    Граница зафиксирована в тексте запросов, потому что её нарушение вернуло бы
    в подграф тысячи фрагментов и вытеснило бы сущности из окна обхода. При этом
    каталог находок чанки отдаёт — это проверяется на fake-сессии в
    ``test_retrieval_persistence.py``.
    """
    traversal = (ANCHORS_CYPHER, EXPAND_CYPHER, EDGES_CYPHER)
    assert all("type <> 'chunk'" in query for query in traversal)
    assert "LIMIT $anchors" in ANCHORS_CYPHER
    assert "[0..$cap]" in EXPAND_CYPHER
    assert ANCHORS_CYPHER.index("ORDER BY") < ANCHORS_CYPHER.index("LIMIT")
    assert EXPAND_CYPHER.index("ORDER BY") < EXPAND_CYPHER.index("[0..$cap]")


def test_domain_seed_stats_report_actual_writes() -> None:
    """preload печатал «создано 447 узлов» на каждом старте контейнера.

    Счётчики снимаются с ответа Neo4j, поэтому повторная перестройка уже
    заполненного графа возвращает нули, а не число попыток слияния.
    """
    from scientific_tangle.services.graph_rebuilder import rebuild_semantic_graph

    driver = _Driver()
    first = rebuild_semantic_graph(driver)  # type: ignore[arg-type]

    # Каждый созданный узел учтён ровно одним счётчиком: узлы утверждений —
    # в claims_created, остальные (сущности и публикации) — в nodes_created.
    assert first["nodes_created"] + first["claims_created"] == len(driver.nodes) > 0
    assert first["edges_created"] == len(driver.relationships) > 0

    assert rebuild_semantic_graph(driver) == {  # type: ignore[arg-type]
        "nodes_created": 0,
        "edges_created": 0,
        "claims_created": 0,
    }