"""Инварианты GraphRAG-контура: retrieval, сообщества, права, бюджеты контекста.

Регрессии на находки G-1..G-10 и раздел ACL: класс данных обязан доезжать до
графа, «нет доказательств» не подменяется seed-данными, а бюджет токенов
срезает нижние секции, а не доказательства.

Живых Neo4j/Elasticsearch в тестовом контуре нет, поэтому production-ветка
проверяется на фейках хранилищ: это доказательство семантики и текста запросов,
а не совместимость с реальным сервером (её проверяет контейнерный контур).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any
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
    GraphSnapshot,
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
from scientific_tangle.services import knowledge as knowledge_module
from scientific_tangle.services.communities import (
    DEFAULT_PROFILE_LIMIT,
    FALLBACK_COMMUNITY,
    MAX_PROFILE_CLAIMS,
    MIN_COMMUNITY_SIZE,
    community_briefs,
    detect_communities,
    name_community,
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
from scientific_tangle.services.knowledge import (
    CommunityBriefCache,
    InMemoryKnowledgeBase,
    brief_key,
    cached_community_briefs,
)
from scientific_tangle.services.reranking import query_tokens
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


def test_community_names_follow_the_actual_topic_labels() -> None:
    sales = name_community(["Лиды", "Воронка продаж"], ["process"], set())
    operations = name_community(["Нагрузка", "Время ответа"], ["process"], set())

    assert sales == "Воронка продаж"
    assert operations == "Время ответа"


def test_isolated_nodes_get_a_single_fallback_community() -> None:
    nodes, _ = clique("lonely", 3)

    names = detect_communities(nodes, [])

    assert names == [FALLBACK_COMMUNITY]
    assert all(node.metadata["community"] == FALLBACK_COMMUNITY for node in nodes)


def test_memory_backend_returns_computed_communities() -> None:
    graph = InMemoryKnowledgeBase().full_graph()

    assert graph.communities
    assert all(node.metadata.get("community") for node in graph.nodes)


def test_memory_demo_sources_are_synthetic_and_industry_neutral() -> None:
    knowledge = InMemoryKnowledgeBase()
    graph = knowledge.full_graph()
    findings = knowledge.all_findings()

    assert findings
    assert all(item.scope.get("origin") == "demo" for item in findings)
    assert all(
        item.evidence[0].source_title.startswith("Синтетический пример:") for item in findings
    )
    public_text = " ".join(
        [*(node.label for node in graph.nodes), *(item.statement for item in findings)]
    ).casefold()
    assert not any(
        term in public_text
        for term in ("шахт", "вода", "осмос", "мембран", "ионный обмен", "выпарив")
    )
    assert all("mine_water" not in item.scope.values() for item in findings)


def test_full_graph_is_a_fresh_container_over_shared_models() -> None:
    """Снимок графа — новый контейнер над общими моделями, а не глубокая копия.

    Глубокое копирование всего графа на каждое действие retrieval стоило дороже
    самого обхода; контракт при этом остаётся: правка возвращённого списка не
    достает до каталога, а ACL-срез обязано видно в тех же объектах, что и кэш.
    """
    knowledge = InMemoryKnowledgeBase()
    nodes_before = len(knowledge._graph.nodes)
    edges_before = len(knowledge._graph.edges)

    snapshot = knowledge.full_graph()
    scoped = knowledge.full_graph({DataClass.PUBLIC})

    assert snapshot.nodes is not knowledge._graph.nodes
    assert snapshot.edges is not knowledge._graph.edges
    assert snapshot.nodes[0] is knowledge._graph.nodes[0]
    assert scoped.nodes[0] is knowledge._graph.nodes[0]
    snapshot.nodes.clear()
    snapshot.edges.clear()
    assert len(knowledge._graph.nodes) == nodes_before
    assert len(knowledge._graph.edges) == edges_before


@pytest.mark.parametrize(
    "allowed",
    [None, {DataClass.PUBLIC}, PUBLIC_SCOPE, {*PUBLIC_SCOPE, DataClass.RESTRICTED}],
)
@pytest.mark.parametrize("candidates", [0, 1, 5])
def test_cached_briefs_match_community_briefs(
    allowed: set[DataClass] | None, candidates: int
) -> None:
    """Кэш сводок отдаёт ровно то, что посчитал бы `community_briefs`.

    Подмешивание утверждений кандидатов в графовую часть профиля — единственное
    место, где кэш повторяет логику services/communities.py: расхождение
    проявилось бы в тексте глобального контекста, а не ошибкой, поэтому сверяется
    напрямую и на промахе, и на попадании.

    Разложение по поколениям графа, по срезам доступа и по насыщению потолка
    утверждений (`MAX_PROFILE_CLAIMS`) добирается в соседних проверках этого же
    блока: ``test_cached_briefs_match_community_briefs_on_merged_claims``,
    ``test_brief_cache_is_keyed_by_acl_slice`` и
    ``test_brief_cache_entry_dies_with_the_graph_epoch``.
    """
    knowledge = InMemoryKnowledgeBase()
    findings = knowledge.all_findings()[:candidates]
    tokens = query_tokens("обратный осмос задержание солей")
    snapshot = knowledge.full_graph(allowed)
    expected = community_briefs(
        snapshot.nodes, snapshot.edges, findings, tokens, DEFAULT_PROFILE_LIMIT
    )

    missed = knowledge._community_briefs(allowed, findings, tokens)
    hit = knowledge._community_briefs(allowed, findings, tokens)

    assert expected
    assert missed == expected
    assert hit == missed
    # Попадание, а не молчаливый пересчёт: на оба вызова — одна запись кэша.
    assert len(knowledge._brief_cache._entries) == 1


def profiled_nodes(name: str, claim_labels: list[str]) -> list[GraphNode]:
    """Узлы одного сообщества с уже проставленной меткой ``community``.

    Профили собираются по ``metadata.community``, а не по результату кластеризации:
    синтетический граф обязан давать предсказуемое число утверждений — ровно то,
    что сравнивается с потоком через кэш. Материалный узел не влияет на ``claims``.
    """
    nodes = [
        GraphNode(
            id=f"claim-{name}-{index}",
            label=label,
            type=NodeType.CLAIM,
            metadata={"community": name},
        )
        for index, label in enumerate(claim_labels)
    ]
    nodes.append(
        GraphNode(
            id=f"entity-{name}",
            label=f"Узел {name}",
            type=NodeType.MATERIAL,
            metadata={"community": name},
        )
    )
    return nodes


def bound_finding(node_id: str, statement: str) -> Finding:
    """Находка, привязанная к узлу графа: ``finding-<id узла>``, как в корпусе.

    Связь утверждения с сообществом идёт только через id узла (``subject`` у
    структурных находок пуст), поэтому seed-находки memory-контура в слиянии
    участвуют лишь после того, как их id совпадут с id узлов.
    """
    return make_finding(f"finding-{node_id}", statement, [])


def claims_of(brief: str) -> list[str]:
    """Поле ``claims`` сводки: секция «утверждения: …» до следующей части профиля.

    Сверять весь текст нельзя: метки участников попадают в секцию «сущности» и
    пересекаются с утверждениями, а проверяется именно состав и порядок ``claims``
    — единственное поле, которое кэш дополняет по находкам запроса.
    """
    head, separator, rest = brief.partition("утверждения: ")
    assert separator, f"в сводке нет секции утверждений: {brief}"
    assert "утверждения: " not in head, f"секция утверждений не одна: {brief}"
    return rest.split(" · ")[0].split("; ")


def test_cached_briefs_match_community_briefs_on_merged_claims() -> None:
    """Слияние утверждений находок с кэшированными профилями не расходится нигде.

    Покрывает (a) и (b) проверки кэша: насыщенный профиль, где утверждениям находок
    места не достаёт и они не дописываются; разреженный, где они дописываются по
    порядку, с дедупом и под потолком ``MAX_PROFILE_CLAIMS``; и профиль без
    графовых утверждений, куда приходят только находки, включая тезис длиннее 160
    символов — он обрезается одинаково в обеих ветках. Сверяется напрямую с
    ``community_briefs`` на тех же входах, поэтому расхождение порядка, потолка или
    дедупа было бы видно в тексте сводки, а не как ошибка.
    """
    saturated = "Насыщенное"
    sparse = "Разреженное"
    empty = "Пустое"
    long_statement = "Длинный тезис про выпаривание, " + "обрезка по 160 символом " * 20

    nodes = [
        *profiled_nodes(saturated, [f"насыщение {index}" for index in range(1, 7)]),
        *profiled_nodes(sparse, ["разрежение 1", "разрежение 2"]),
        *profiled_nodes(empty, []),
    ]
    # Рёбра — только между материалными узлами: степени claim-узлов нулевые, и
    # порядок утверждений в профиле остаётся порядком меток, который сверяется явно.
    edges = [
        GraphEdge(
            id="link-first",
            source=f"entity-{saturated}",
            target=f"entity-{sparse}",
            relation="CONTAINS",
        ),
        GraphEdge(
            id="link-empty",
            source=f"entity-{sparse}",
            target=f"entity-{empty}",
            relation="CONTAINS",
        ),
    ]
    findings = [
        bound_finding(f"claim-{saturated}-0", "тезис находки насыщения"),
        bound_finding(f"claim-{saturated}-1", "второй тезис находки насыщения"),
        bound_finding(f"claim-{sparse}-0", "тезис находки А"),
        bound_finding(f"claim-{sparse}-0", "тезис находки А"),
        bound_finding(f"claim-{sparse}-1", "тезис находки Б"),
        bound_finding(f"entity-{sparse}", "тезис находки В"),
        bound_finding(f"claim-{sparse}-1", "ещё один тезис находки Г"),
        bound_finding(f"entity-{empty}", long_statement),
        bound_finding("claim-net-uzla", "тезис без узла в графе"),
    ]
    tokens = query_tokens("разрежение тезис находки")
    cache = CommunityBriefCache()
    key = brief_key((0,), None, sorted({saturated, sparse, empty}))
    builds: list[int] = []

    def source() -> GraphSnapshot:
        builds.append(len(nodes))
        return GraphSnapshot(
            nodes=list(nodes), edges=list(edges), communities=sorted({saturated, sparse, empty})
        )

    expected = community_briefs(nodes, edges, findings, tokens, DEFAULT_PROFILE_LIMIT)
    missed = cached_community_briefs(cache, key, source, findings, tokens, DEFAULT_PROFILE_LIMIT)
    hit = cached_community_briefs(cache, key, source, findings, tokens, DEFAULT_PROFILE_LIMIT)

    assert expected
    assert missed == expected
    assert hit == expected
    assert builds == [len(nodes)]

    def brief_of(name: str) -> str:
        matches = [brief for brief in missed if brief.startswith(f"«{name}»")]
        assert len(matches) == 1, f"сводка сообщества {name} не найдена в {missed}"
        return matches[0]

    # (a) Насыщенный профиль: графовые утверждения заполнили потолок, и тезисы
    # находок не дописаны ни в каком виде — иначе кэш переписал бы поле claims.
    assert claims_of(brief_of(saturated)) == [
        f"насыщение {index}" for index in range(1, MAX_PROFILE_CLAIMS + 1)
    ]

    # (b) Разреженный профиль: после графовых меток идут утверждения находок в
    # порядке встречи, дедуплицированные и обрезанные по потолку — «тезис находки
    # А» повторяется, «В» и «Г» за потолок не помещаются.
    assert claims_of(brief_of(sparse)) == [
        "разрежение 1",
        "разрежение 2",
        "тезис находки А",
        "тезис находки Б",
    ]

    # Профиль без графовых утверждений: они приходят только из находок, причём
    # длинный тезис обрезан так же, как в services/communities.py.
    assert claims_of(brief_of(empty)) == [long_statement[:160]]
    assert long_statement[160:] not in brief_of(empty)
    # Находка без узла в графе не попала ни в один профиль.
    assert all("тезис без узла в графе" not in brief for brief in missed)

    # Что именно лежит в кэше: графовая часть полей ``claims``. У насыщенного
    # профиля потолок занят метками узлов — сводка отдаёт кэшированный кортеж без
    # изменений; у разреженного и пустого кэш хранит меньше, чем уходит в выдачу.
    cached = cache.views(key)
    assert cached is not None
    stored = {profile.name: profile.claims for profile in cached.profiles}
    assert stored[saturated] == tuple(claims_of(brief_of(saturated)))
    assert stored[sparse] == ("разрежение 1", "разрежение 2")
    assert stored[empty] == ()


def test_brief_cache_entry_dies_with_the_graph_epoch() -> None:
    """Запись в корпус меняет поколение графа, и старый ключ кэша больше не читается.

    Проверка идёт по самому ключу: попадание после мутации дало бы аналитику
    устаревшую сводку без единой ошибки, поэтому сверяется и ``views`` под
    прежним ключом, и текст новой выдачи с ``community_briefs`` на свежем снимке.
    """
    knowledge = InMemoryKnowledgeBase()
    findings = knowledge.all_findings()
    tokens = query_tokens("обратный осмос")

    stale_key = brief_key((knowledge._graph_epoch,), None, tuple(knowledge._ensure_communities()))
    before = knowledge._community_briefs(None, findings, tokens)
    stale = knowledge._brief_cache.views(stale_key)
    assert stale is not None
    assert all("Заменённый тезис" not in brief for brief in before)

    knowledge.supersede_finding("finding-ro", "Заменённый тезис про обратный осмос.", 0.9)
    assert knowledge._graph_epoch == 1
    assert knowledge._brief_cache.views(stale_key) is None

    fresh_findings = knowledge.all_findings()
    after = knowledge._community_briefs(None, fresh_findings, tokens)
    snapshot = knowledge.full_graph(None)

    assert after != before
    assert any("Заменённый тезис" in brief for brief in after)
    # Состав утверждений в кэшированных профилях «до» и «после» различается: если бы
    # старая запись обслуживалась и дальше, правка эксперта просто не появилась бы в
    # глобальном контексте — ошибки бы не было.
    fresh = knowledge._brief_cache.views(
        brief_key((knowledge._graph_epoch,), None, tuple(knowledge._ensure_communities()))
    )
    assert fresh is not None
    assert {profile.claims for profile in stale.profiles} != {
        profile.claims for profile in fresh.profiles
    }
    assert (
        after
        == community_briefs(
            snapshot.nodes, snapshot.edges, fresh_findings, tokens, DEFAULT_PROFILE_LIMIT
        )
    )


def test_brief_cache_is_keyed_by_acl_slice() -> None:
    """Профиль, собранный для одного среза доступа, не достается другому.

    Иначе restricted-метка уезжала бы в промпт модели под видом уже отфильтрованной
    сводки — кэш здесь повторяет границу ``AccessPolicyEngine``, а не обходит её.
    Сверяется и содержимое выдачи, и сами ключи: два разных набора классов данных
    обязаны занять две записи, а равный по составу набор, записанный в другом
    порядке, — одну (ключ держит ``frozenset``, а не список).
    """
    knowledge = InMemoryKnowledgeBase()
    findings = knowledge.all_findings()
    tokens = query_tokens("часы пик очередь")

    base = knowledge._community_briefs(PUBLIC_SCOPE, findings, tokens)
    expert = knowledge._community_briefs({*PUBLIC_SCOPE, DataClass.RESTRICTED}, findings, tokens)

    assert not any("3–5" in brief for brief in base)
    assert any("3–5" in brief for brief in expert)
    # Тот же запрос ещё раз: попадание в кэш не должно менять выдачу.
    assert knowledge._community_briefs(PUBLIC_SCOPE, findings, tokens) == base

    entries = knowledge._brief_cache._entries
    scopes = [scope for (_, scope, _) in entries]
    assert len(entries) == 2, f"срезы доступа поделили одну запись кэша: {scopes}"
    assert len(set(scopes)) == 2

    # Равный по составу набор в другом порядке — та же запись, а не новая.
    reordered = {DataClass.RESTRICTED, DataClass.INTERNAL, DataClass.PUBLIC}
    assert knowledge._community_briefs(reordered, findings, tokens) == expert
    assert len(knowledge._brief_cache._entries) == 2


def test_brief_cache_entry_expires_by_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запись кэша профилей живёт ровно ``ttl_seconds`` и после этого пересобирается.

    Поколение графа ловит только явную запись в корпус. Тухлый TTL означал бы, что
    профиль, собранный по графу другого процесса (или после сбоя кластеризации),
    обслуживает запросы бесконечно — без единой ошибки. Часы подменяются, чтобы
    проверка не спала тридцать секунд.
    """
    clock = {"now": 1_000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    cache = CommunityBriefCache(ttl_seconds=30.0)
    key = brief_key((0,), None, ("А",))
    nodes = profiled_nodes("А", ["утверждение А"])
    cache.store(key, nodes, [])

    assert cache.views(key) is not None
    clock["now"] += 29.0
    assert cache.views(key) is not None, "запись выброшена раньше TTL"
    clock["now"] += 2.0
    assert cache.views(key) is None
    # Просроченная запись именно удалена, а не спрятана: иначе кэш разрастался бы
    # числом ключей, которое никто не чистит.
    assert key not in cache._entries


def test_brief_cache_evicts_least_recently_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сверх потолка хранится свежайшее по чтению, а не по записи.

    Ключей больше, чем записей: порядок вставки зависит от того, какие планы
    запросов пришли первыми, и вытеснение по вставке вышвырнуло бы профиль, по
    которому аналитик работает прямо сейчас, — оставив в кэше брошенный.
    """
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    cache = CommunityBriefCache(ttl_seconds=300.0, max_entries=2)

    def key_of(letter: str) -> Any:
        return brief_key((0,), None, (letter,))

    for letter in ("А", "Б"):
        cache.store(key_of(letter), profiled_nodes(letter, [f"утверждение {letter}"]), [])

    # Чтение «А» делает его свежайшим: следующим уйдёт «Б», хотя вставлена позже.
    assert cache.views(key_of("А")) is not None
    cache.store(key_of("В"), profiled_nodes("В", ["утверждение В"]), [])

    assert set(cache._entries) == {key_of("А"), key_of("В")}
    assert cache.views(key_of("Б")) is None


def test_graph_write_drops_cached_briefs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Профили сообществ считаются один раз на граф и сбрасываются любой записью.

    Счётчик собирает пересчёты ``build_community_profiles``: до фикса он работал на
    каждом действии retrieval (до 24 раз за запрос на неизменившемся графе).
    Нулевой прирост на повторном retrieval доказывает кэш, единица после замены
    тезиса — что кэш не пережил мутацию корпуса. Проверка идёт и по тексту выдачи:
    устаревшая сводка была бы видна аналитику, а не упала бы ошибкой.
    """
    knowledge = InMemoryKnowledgeBase()
    builds: list[int] = []
    original = knowledge_module.build_community_profiles

    def counting(nodes: Any, edges: Any, findings: Any = ()) -> Any:
        builds.append(len(nodes))
        return original(nodes, edges, findings)

    monkeypatch.setattr(knowledge_module, "build_community_profiles", counting)
    question = QueryPlan(question="обратный осмос", language="ru", mode="global")
    retrieval = plan("обратный осмос", use_global_context=True)

    first = knowledge.retrieve(question, retrieval)
    assert first.community_summaries
    assert not any("Заменённый тезис" in brief for brief in first.community_summaries)
    assert builds == [len(knowledge._graph.nodes)]

    second = knowledge.retrieve(question, retrieval)
    assert second.community_summaries == first.community_summaries
    assert builds == [len(knowledge._graph.nodes)]

    knowledge.supersede_finding("finding-ro", "Заменённый тезис про обратный осмос.", 0.9)
    after = knowledge.retrieve(question, retrieval).community_summaries

    assert len(builds) == 2
    assert any("Заменённый тезис" in brief for brief in after)
    # Старая версия больше не кандидат: её текст обязан уйти из сводки вместе с
    # находкой, а не остаться в профиле из-за кэша.
    assert not any("обеспечивает удаление 95–99%" in brief for brief in after)


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
        QueryPlan(question="время обработки запросов", language="ru", mode="hybrid"),
        plan("время обработки запросов"),
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
        plan("осмос технологическая вода", semantic_query="мембранное обессоливание рассола"),
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

    public = knowledge.rank_findings("обработка запросов проверка", 5, "hybrid", {DataClass.PUBLIC})
    everything = knowledge.rank_findings("обработка запросов проверка", 5, "hybrid")

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
        entity_mentions=["технологическая вода"],
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

    selected = select_relevant(items, "технологическая вода обратный осмос", 1)

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


def test_preload_does_not_add_fixed_demo_graph_or_delete_workspace_data() -> None:
    """Preload не должен добавлять отраслевой seed или удалять данные пользователя."""
    from scientific_tangle.services.graph_rebuilder import rebuild_semantic_graph

    driver = _Driver()
    driver.nodes.add("workspace-source-1")

    assert rebuild_semantic_graph(driver) == {  # type: ignore[arg-type]
        "nodes_created": 0,
        "edges_created": 0,
        "claims_created": 0,
    }
    assert driver.nodes == {"workspace-source-1"}
    assert driver.relationships == set()

def test_graph_snapshot_slices_by_connectivity_not_by_alphabet() -> None:
    """Полный снимок режется по степени связности, а рёбра берутся внутри среза.

    Живой прогон на Neo4j скажет числа, но порядок отсечения можно потерять и без
    базы: правка, вернувшая `ORDER BY label`, молча прошла бы все тесты на
    in-memory контуре, а витрина снова начала бы показывать алфавитный префикс
    корпуса (120 узлов из 600 на замере 2 октября). Поэтому форма запроса
    проверяется по исходнику.
    """
    from inspect import getsource

    from scientific_tangle.services.infrastructure import Neo4jElasticsearchKnowledgeBase

    source = " ".join(getsource(Neo4jElasticsearchKnowledgeBase._load_graph_from_neo4j).split())
    assert "ORDER BY degree DESC, coalesce(node.label, node.id), node.id" in source, (
        "узы среза снова идут по алфавиту метки: снимок смещён к началу алфавита"
    )
    assert "COUNT { (node)--() } AS degree" in source, "степень узла больше не считается"
    assert "UNWIND $ids AS id" in source and "b.id IN $ids" in source, (
        "рёбра снова читаются независимо от выбранного набора узлов: снимок "
        "перестаёт быть подграфом"
    )
    # Потолок и полный счётчик — разные числа: раскрытие обязано называть корпус.
    assert 'count(rel) AS total' in source, "полное число рёбер корпуса больше не считается"
    assert "edges_total = _record_int(edges_total_record" in source

