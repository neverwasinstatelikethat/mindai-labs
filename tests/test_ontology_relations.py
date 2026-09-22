"""Инварианты реестра связей: что создаёт пайплайн, то достижимо агентом, и наоборот.

Тест не перечисляет связи руками — он вычитывает фактические имена из исходников
создателей рёбер (``services/graph_rebuilder.py``, ``services/knowledge.py``,
``services/infrastructure.py``) и из объектов доменного сеятеля, а затем сверяет с
``domain/relations.py``. Расхождение списков — это либо «фантом» в allowlist (обход
по нему всегда пустой), либо «немое» ребро графа, которое агент не пересечёт; в обоих
случаях падает тест, а не ответ аналитика.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from scientific_tangle.agents.tools import ResearchToolExecutor
from scientific_tangle.domain.contracts import RetrievalPlan, ToolAction
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.domain.relations import (
    ALL_RELATIONS,
    BY_NAME,
    DOMAIN,
    MEMORY,
    NEO4J,
    PROVENANCE_RELATIONS,
    SEMANTIC_RELATIONS,
    TIER_KEYWORDS,
    RelationContractError,
    relations_for_prompt,
    resolve_relations,
    spec_for,
    validate_edge,
)
from scientific_tangle.services.graph_rebuilder import CLAIMS, EDGES
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase

SRC = Path(__file__).parents[1] / "src" / "scientific_tangle"

# Файлы-создатели из реестра — они же относительные пути: имена берутся оттуда,
# поэтому новый создатель, добавленный в RelationSpec, обязан существовать.
CREATORS = (DOMAIN, MEMORY, NEO4J)

# Глубину обхода ограничивает схема (ToolAction/RetrievalPlan: le=4), а не реестр.
HOP_CEILING = 4

# Литералы рёбер, которые файл пишет напрямую: Cypher-литерал и GraphEdge(relation=...).
_LITERAL_WRITES = (
    re.compile(r"\[r:(?!\{)([A-Z][A-Z0-9_]*)"),
    re.compile(r'relation="([A-Z][A-Z0-9_]*)"'),
)
# Запись имени из данных: предикат приходит из извлечения либо из диффа памяти.
_DYNAMIC_WRITE = re.compile(r"relation=claim\.predicate|\[r:\{relation\}")

# Что может приехать по динамической записи: у памяти — словарь извлечения, который
# держит shapes.ttl (ярус semantic); Neo4j дополнительно перелицовывает дифф памяти,
# где уже есть и provenance-рёбра, поэтому там словарь — вся подпись.
DYNAMIC_VOCABULARY: dict[str, set[str]] = {
    MEMORY: set(SEMANTIC_RELATIONS),
    NEO4J: set(ALL_RELATIONS),
}


def _source(relative: str) -> str:
    return (SRC / relative).read_text(encoding="utf-8")


def _written_literals(relative: str) -> set[str]:
    found: set[str] = set()
    for pattern in _LITERAL_WRITES:
        found.update(pattern.findall(_source(relative)))
    return found


def _dynamic_vocabulary(relative: str) -> set[str]:
    if not _DYNAMIC_WRITE.search(_source(relative)):
        return set()
    return DYNAMIC_VOCABULARY.get(relative, set())


def _declared_for(module: str) -> set[str]:
    return {spec.name for spec in BY_NAME.values() if module in spec.created_by}


def _seed_relations() -> set[str]:
    """Рёбра и предикаты утверждений доменного сеятеля — то, что пойдёт в Neo4j."""
    return {edge.relation for edge in EDGES} | {claim.predicate for claim in CLAIMS}


def _writes(module: str) -> set[str]:
    """Рёбра, которые файл создаёт: литералы, доменный сеятель и динамический словарь."""
    written = _written_literals(module) | _dynamic_vocabulary(module)
    if module == DOMAIN:
        written |= _seed_relations()
    return written


def _literals_written(module: str) -> set[str]:
    """Только то, что в файле стоит буквой: сеятель относится к доменному файлу."""
    if module == DOMAIN:
        return _written_literals(module) | _seed_relations()
    return _written_literals(module)


# ── Инвариант «создаётся ⇔ достижимо» (п. 1) ───────────────────────────────


def test_domain_seed_writes_exactly_the_declared_domain_relations() -> None:
    created = _written_literals(DOMAIN) | _seed_relations()

    # Симметрическая разница пуста: ни фантомов в реестре, ни немых рёбер в сеятеле.
    assert created ^ _declared_for(DOMAIN) == set()
    assert created, "сеятель не создаёт ни одного ребра — сверять нечего"


@pytest.mark.parametrize("module", CREATORS)
def test_file_writes_only_relations_declared_for_it(module: str) -> None:
    """Буква исходников обязана быть объявлена: иначе рёбра не видны ни в allowlist,
    ни в обходе — «немое» ребро графа, которого тест не прощает."""
    undeclared = _literals_written(module) - _declared_for(module)

    assert undeclared == set(), f"{module} пишет рёбра вне подписи: {sorted(undeclared)}"


def test_graph_can_never_hold_an_unreachable_relation() -> None:
    """Главный инвариант: всё, что способен создать пайплайн, запросопригодно.

    Динамическая запись (предикат из извлечения, дифф памяти в Neo4j) входит в
    проверку: её словарь ограничивает shapes.ttl, поэтому имя вне реестра попасть
    в граф не может.
    """
    creatable: set[str] = set()
    for module in CREATORS:
        creatable |= _writes(module)

    assert creatable - set(ALL_RELATIONS) == set()
    assert creatable, "ни один создатель не объявлен — сверять нечего"


@pytest.mark.parametrize("module", CREATORS)
def test_file_has_no_phantom_declaration(module: str) -> None:
    """Обратная сторона: объявленное этим файлом имя он действительно создаёт."""
    assert _declared_for(module) - _writes(module) == set()


def test_every_declared_relation_has_a_writer() -> None:
    """Фантом общего вида: имя в allowlist, которое никто нигде не создаёт."""
    created: set[str] = set()
    for module in CREATORS:
        created |= _writes(module)

    assert set(ALL_RELATIONS) - created == set()


def test_semantic_tier_is_seeded_or_memory_created() -> None:
    """Новое semantic-имя без создателя падает здесь, а не пустым обходом."""
    assert set(SEMANTIC_RELATIONS) - _declared_for(DOMAIN) <= _declared_for(MEMORY)


def test_domain_seed_relations_are_all_semantic() -> None:
    """Сеятель заводит доменные тропы; provenance — служебные рёбра импорта."""
    assert _seed_relations() & set(PROVENANCE_RELATIONS) == set()
    # ASSERTS/SUPPORTED_BY пишет код утверждений сеятеля, а не список EDGES.
    assert {"ASSERTS", "SUPPORTED_BY"} <= _written_literals(DOMAIN)


def test_graph_seed_survives_the_registry_contract() -> None:
    """Весь сеятель проходит домены/диапазоны реестра до первой записи в Neo4j."""
    from scientific_tangle.services.graph_rebuilder import check_domain_seed

    check_domain_seed()
    assert EDGES and CLAIMS


# ── Ярусы semantic / provenance (п. 2) ─────────────────────────────────────


def test_default_traversal_stays_in_the_semantic_tier() -> None:
    accepted, rejected = resolve_relations(None)

    assert accepted == list(SEMANTIC_RELATIONS)
    assert rejected == []
    assert set(accepted).isdisjoint(PROVENANCE_RELATIONS)


def test_provenance_requires_an_explicit_request() -> None:
    accepted, rejected = resolve_relations(list(PROVENANCE_RELATIONS))

    assert accepted == list(PROVENANCE_RELATIONS)
    assert rejected == []


def test_tier_keywords_expand_to_the_whole_signature() -> None:
    accepted, rejected = resolve_relations(["SEMANTIC", "PROVENANCE"])

    assert accepted == list(TIER_KEYWORDS["SEMANTIC"]) + list(TIER_KEYWORDS["PROVENANCE"])
    assert accepted == list(ALL_RELATIONS)
    assert rejected == []


def test_rejected_names_come_back_instead_of_being_dropped() -> None:
    accepted, rejected = resolve_relations(["CONTAINS", "CONTRADICTS", "feeds_evidence"])

    assert accepted == ["CONTAINS"]
    assert rejected == ["CONTRADICTS", "FEEDS_EVIDENCE"]


def test_retrieval_plan_never_runs_without_a_relation_filter() -> None:
    """Пустой фильтр в памяти означает «без фильтра», то есть и provenance-рёбра."""
    executor = ResearchToolExecutor(InMemoryKnowledgeBase())
    action = ToolAction(
        id="a1",
        tool="graph_traverse",
        purpose="Тропа",
        query="метод очистки",
        relation_types=["CONTRADICTS"],
    )

    plan = executor._retrieval_plan(action, QueryPlan(question="Метод?", language="ru", max_hops=3))

    assert plan.relation_types == list(SEMANTIC_RELATIONS)
    assert set(plan.relation_types).isdisjoint(PROVENANCE_RELATIONS)


def test_tier_expansion_does_not_raise_the_hop_ceiling() -> None:
    """Раскрытие яруса меняет словарь рёбер, но не глубину: потолок min(план, действие)."""
    executor = ResearchToolExecutor(InMemoryKnowledgeBase())
    query_plan = QueryPlan(question="Трассировка?", language="ru", max_hops=2)

    def plan_for(relations: list[str]) -> RetrievalPlan:
        return executor._retrieval_plan(
            ToolAction(
                id="a",
                tool="graph_traverse",
                purpose="Тропа",
                query="метод очистки",
                relation_types=relations,
                max_hops=HOP_CEILING,
            ),
            query_plan,
        )

    semantic = plan_for(["PRODUCES"])
    expanded = plan_for(["PRODUCES", "PROVENANCE"])

    assert set(semantic.relation_types) < set(expanded.relation_types)
    assert expanded.max_hops == semantic.max_hops == 2


def test_hop_ceiling_lives_in_the_schema_not_in_the_tier_keyword() -> None:
    """Запросить весь provenance «глубже потолка» нельзя — схему не обойти."""
    with pytest.raises(ValidationError):
        ToolAction(
            id="a",
            tool="graph_traverse",
            purpose="Тропа",
            query="метод очистки",
            relation_types=["PROVENANCE"],
            max_hops=HOP_CEILING + 1,
        )


def test_traversal_depth_is_the_same_for_both_tiers() -> None:
    """Поведенческая проверка потолка: полный словарь не добавляет шагов обхода."""
    knowledge = InMemoryKnowledgeBase()
    query_plan = QueryPlan(
            question="Трассировка?", language="ru", mode="local",
            entity_mentions=["Вода"],
        )
    everything = list(resolve_relations(["SEMANTIC", "PROVENANCE"])[0])

    def reached(hops: int) -> set[str]:
        context = knowledge.retrieve(
            query_plan,
            RetrievalPlan(
                lexical_query="осмос",
                semantic_query="осмос",
                entity_names=["Вода"],
                relation_types=everything,
                max_hops=hops,
                use_global_context=False,
                use_local_graph=True,
            ),
            None,
        )
        return {node.id for node in context.graph.nodes}

    one_hop, two_hops, ceiling = reached(1), reached(2), reached(HOP_CEILING)

    assert one_hop < two_hops <= ceiling
    assert reached(1) <= reached(HOP_CEILING)


# ── Подпись отношений (п. 3: у реестра есть живые потребители) ──────────────


def test_validate_edge_rejects_out_of_signature_ends() -> None:
    assert validate_edge("PRODUCES", "process", "material").name == "PRODUCES"
    with pytest.raises(RelationContractError):
        validate_edge("PRODUCES", "material", "process")
    with pytest.raises(RelationContractError):
        validate_edge("ASSERTS", "material", "claim", asserted=True)
    with pytest.raises(RelationContractError):
        validate_edge("CONTAINS", "material", "invented-type")


def test_relations_for_prompt_covers_the_default_traversal_tier() -> None:
    """Allowlist в промпте генерируется реестром: имя вне подписи модель не попросит,
    а имя из подписи обязано быть в подсказке, иначе оно недостижимо на практике."""
    prompt = relations_for_prompt()

    assert all(f"- {name} —" in prompt for name in SEMANTIC_RELATIONS)
    assert not any(f"- {name} —" in prompt for name in PROVENANCE_RELATIONS)


def test_spec_for_is_case_insensitive_and_strict() -> None:
    assert spec_for(" produces ").name == "PRODUCES"
    with pytest.raises(RelationContractError):
        spec_for("CONTRADICTS")
