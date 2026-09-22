"""Единый реестр отношений графа доказательств (подпись онтологии).

Подпись графа жила в двух независимых местах: allowlist агента
(``agents/tools.py``) и доменный сеятель (``services/graph_rebuilder.py``).
Списки разошлись: из 29 отношений, которые граф реально создаёт, агенту были
доступны 11, а ``_traverse`` с фильтром
``ALL(rel IN rels WHERE type(rel) IN $relations)`` молча отрезал все
кросс-доменные рёбра (FEEDS, PRODUCED_FROM, USED_BY, PROCESSES, REMOVES, ...) —
то есть ровно то, ради чего нужен GraphRAG. Обратная сторона расхождения:
CONTRADICTS состоял в allowlist, но не создаётся нигде — противоречия в продукте
считаются численно, а не рёбрами графа.

Здесь один декларативный источник истины. Его потребляют:
  * ``services/graph_rebuilder.py`` — проверка домена/диапазона перед записью
    (:func:`spec_for`, :func:`validate_edge`);
  * ``agents/tools.py`` — обход по умолчанию и фильтр плана retrieval
    (:func:`resolve_relations`);
  * ``services/ontology.py`` — словарь предикатов для промпта и текста нарушения
    (:func:`relations_for_extraction`);
  * ``agents/workflow.py`` — allowlist в промпте планирования обязан строиться из
    :func:`relations_for_prompt`, а не из литеры в тексте промпта;
  * ``ontology/shapes.ttl`` — перечисление сигнатуры для SHACL;
  * ``tests/test_ontology_relations.py`` — инвариант «что создаётся, то достижимо
    и наоборот», ``tests/test_ontology_shapes.py`` — согласованность shapes.ttl с
    реестром и NodeType. Расхождение списков падает в тесте, а не пустым ответом.

Ярусы. SEMANTIC — доменные отношения, обходятся по умолчанию. PROVENANCE —
служебные рёбра происхождения; по умолчанию выключены и включаются только по
явному имени или ключевым словом ``PROVENANCE``. Причина: трассировка
«тезис → документ → лист → цитата» уже приезжает вместе с ``Finding.evidence``,
а в обходе каждое такое ребро уводит шаг в claim/chunk-узел и домножает число
допустимых троп на размер множества утверждений — на реальном корпусе это взрыв
ветвления ради данных, которые уже в руках аналитика. Когда нужен разбор
происхождения («какие утверждения опираются на этот отчёт», «чем заменили этот
тезис»), ярус запрашивается явно — поэтому он остаётся в reachability-наборе,
а не удаляется.

Свободные предикаты LLM-extraction (``ExtractedClaim.predicate``) подписью не
покрываются: они попадают в граф как есть. Регистр задаёт словарь, который промпт
экстракции обязан предлагать модели (:func:`relations_for_extraction`), и этот же
словарь проверяется :file:`ontology/shapes.ttl` при импорте — отношение вне
словаря не доезжает до графа, иначе навсегда остаётся «немым» ребром, которое
обход агента не пересечёт.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from scientific_tangle.domain.contracts import NodeType

MAT = NodeType.MATERIAL
PROC = NodeType.PROCESS
EQ = NodeType.EQUIPMENT
COND = NodeType.CONDITION
CLAIM = NodeType.CLAIM
PUB = NodeType.PUBLICATION
CHUNK = NodeType.CHUNK
LOC = NodeType.LOCATION
EXPERT = NodeType.EXPERT

ANY: frozenset[NodeType] = frozenset()

# Файлы-создатели рёбер: декларация проверяется тестом по исходнику, чтобы
# в реестре не оставалось отношений, которых в графе на самом деле нет.
DOMAIN = "services/graph_rebuilder.py"
MEMORY = "services/knowledge.py"
NEO4J = "services/infrastructure.py"


class RelationTier(StrEnum):
    SEMANTIC = "semantic"
    PROVENANCE = "provenance"


class RelationContractError(ValueError):
    """Отношение не объявлено либо нарушен домен/диапазон: в граф такое не пишем."""


@dataclass(frozen=True, slots=True)
class RelationSpec:
    """Одно отношение подписи: кому доступно, что связывает и кто его создаёт."""

    name: str
    tier: RelationTier
    meaning: str
    # Пустое множество = без ограничения по типу узла.
    source_types: frozenset[NodeType] = ANY
    target_types: frozenset[NodeType] = ANY
    # Может быть предикатом claim-узла: (claim)-[pred]->(object).
    claim_predicate: bool = False
    created_by: tuple[str, ...] = (DOMAIN,)


def _t(*types: NodeType) -> frozenset[NodeType]:
    return frozenset(types)


def _rel(
    name: str,
    meaning: str,
    *,
    tier: RelationTier = RelationTier.SEMANTIC,
    src: frozenset[NodeType] = ANY,
    tgt: frozenset[NodeType] = ANY,
    claim: bool = False,
    by: tuple[str, ...] = (DOMAIN,),
) -> RelationSpec:
    return RelationSpec(
        name=name,
        tier=tier,
        meaning=meaning,
        source_types=src,
        target_types=tgt,
        claim_predicate=claim,
        created_by=by,
    )


REGISTRY: tuple[RelationSpec, ...] = (
    # ── Состав, обработка и потоки вещества ──
    _rel("CONTAINS", "содержит компонент или условие", src=_t(MAT), tgt=_t(MAT, COND),
         by=(DOMAIN, MEMORY)),
    _rel("TREATED_BY", "обрабатывается технологией", src=_t(MAT), tgt=_t(PROC),
         by=(DOMAIN, MEMORY)),
    _rel("PROCESSES", "процесс обрабатывает материал", src=_t(PROC), tgt=_t(MAT), claim=True),
    _rel("PROCESSED_BY", "материал перерабатывается процессом", src=_t(MAT), tgt=_t(PROC),
         claim=True),
    _rel("PRODUCES", "процесс производит продукт", src=_t(PROC), tgt=_t(MAT), claim=True,
         by=(DOMAIN, MEMORY)),
    _rel("PRODUCED_FROM", "получено из сырья", src=_t(MAT), tgt=_t(MAT), claim=True),
    _rel("FEEDS", "питает следующую стадию", src=_t(MAT, PROC), tgt=_t(PROC)),
    _rel("REMOVES", "удаляет примесь", src=_t(PROC), tgt=_t(MAT), claim=True),
    _rel("REDUCES", "сокращает содержание или объём", src=_t(PROC), tgt=_t(MAT)),
    _rel("CONVERTED_TO", "преобразуется в вещество", src=_t(MAT), tgt=_t(MAT), claim=True),
    _rel("DISSOLVED_BY", "растворяется реагентом", src=_t(MAT), tgt=_t(MAT)),
    # ── Оборудование, условия и режимы ──
    _rel("USES", "использует оборудование или реагент", src=_t(PROC), tgt=_t(EQ, MAT),
         claim=True, by=(DOMAIN, MEMORY)),
    _rel("USED_BY", "применяется в процессе", src=_t(EQ), tgt=_t(PROC)),
    _rel("USED_IN", "используется на стадии", src=_t(EQ, MAT), tgt=_t(PROC)),
    _rel("USED_FOR", "назначено для материала или стадии", src=_t(EQ, PROC),
         tgt=_t(MAT, PROC), claim=True),
    _rel("REQUIRES", "требует условия", src=_t(PROC), tgt=_t(COND), claim=True,
         by=(DOMAIN, MEMORY)),
    _rel("REQUIRES_MIN_TEMPERATURE", "требует нижнюю температуру", src=_t(PROC),
         tgt=_t(COND), claim=True),
    _rel("OPERATES_AT", "работает при параметре", src=_t(PROC), tgt=_t(COND), claim=True),
    _rel("ACHIEVES", "достигает показателя качества", src=_t(PROC), tgt=_t(COND)),
    _rel("MEASURED_IN", "показатель измерен в среде", src=_t(COND), tgt=_t(MAT, PROC)),
    _rel("AFFECTED_BY", "испытывает влияние условия", src=_t(PROC), tgt=_t(COND)),
    _rel("AFFECTS", "условие влияет на процесс", src=_t(COND), tgt=_t(PROC), claim=True),
    _rel("PRECEDES", "предшествует стадии", src=_t(PROC), tgt=_t(PROC), claim=True),
    # ── Показатели и сравнения ──
    _rel("HAS_GRADE", "содержание или класс материала", src=_t(MAT), tgt=_t(MAT, COND),
         claim=True),
    _rel("HAS_EFFICIENCY", "эффективность процесса", src=_t(PROC), tgt=_t(MAT, PROC),
         claim=True),
    _rel("HAS_ENERGY_RATIO", "относительное энергопотребление", src=_t(PROC), tgt=_t(PROC),
         claim=True),
    _rel("LOCATED_IN", "расположен на площадке или стадии", src=_t(EQ), tgt=_t(LOC, PROC),
         claim=True, by=(MEMORY, NEO4J)),
    _rel("ALTERNATIVE_TO", "взаимозаменяемая технология", src=_t(MAT, PROC),
         tgt=_t(MAT, PROC)),
    _rel("EXPERT_IN", "экспертиза по процессу", src=_t(EXPERT), tgt=_t(PROC),
         by=(DOMAIN, MEMORY)),
    # Общий предикат извлечённого утверждения без специфичного отношения.
    _rel("HAS_PROPERTY", "свойство без специфичного отношения", claim=True, by=(MEMORY,)),
    # ── Ярус PROVENANCE: включается только явным запросом ──
    _rel("ASSERTS", "сущность утверждает тезис", tier=RelationTier.PROVENANCE,
         tgt=_t(CLAIM), by=(DOMAIN, MEMORY)),
    _rel("SUPPORTED_BY", "тезис поддержан источником", tier=RelationTier.PROVENANCE,
         src=_t(CLAIM), tgt=_t(PUB), by=(DOMAIN, MEMORY)),
    _rel("SUPERSEDES", "версия заменяет предыдущую", tier=RelationTier.PROVENANCE,
         src=_t(CLAIM), tgt=_t(CLAIM), by=(MEMORY, NEO4J)),
    # Фрагменты происхождения создаёт и память (seed чанков), и Neo4j-индексация.
    _rel("HAS_CHUNK", "документ содержит фрагмент", tier=RelationTier.PROVENANCE,
         src=_t(PUB), tgt=_t(CHUNK), by=(NEO4J, MEMORY)),
)

BY_NAME: dict[str, RelationSpec] = {spec.name: spec for spec in REGISTRY}

SEMANTIC_RELATIONS: tuple[str, ...] = tuple(
    spec.name for spec in REGISTRY if spec.tier is RelationTier.SEMANTIC
)
PROVENANCE_RELATIONS: tuple[str, ...] = tuple(
    spec.name for spec in REGISTRY if spec.tier is RelationTier.PROVENANCE
)
ALL_RELATIONS: tuple[str, ...] = tuple(spec.name for spec in REGISTRY)

# Ключевые слова ярусов: агенту дешевле попросить весь ярус, чем перечислять имена.
TIER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "SEMANTIC": SEMANTIC_RELATIONS,
    "PROVENANCE": PROVENANCE_RELATIONS,
}


def spec_for(name: str) -> RelationSpec:
    try:
        return BY_NAME[name.strip().upper()]
    except KeyError as error:
        raise RelationContractError(f"Отношение не объявлено в реестре: {name}") from error


def resolve_relations(
    requested: Sequence[str] | None,
    default: tuple[str, ...] = SEMANTIC_RELATIONS,
) -> tuple[list[str], list[str]]:
    """Разбирает запрос плана в пару (принятые имена, отклонённые имена).

    Пустой запрос = обход по умолчанию (ярус semantic). Неизвестные имена
    возвращаются вторым списком, а не тихо выбрасываются: tool-исполнитель
    обязан сказать аналитику, что часть запрошенной signature не исполнена.

    Ключевое слово яруса меняет только словарь рёбер: глубина обхода
    (``max_hops``) к нему не привязана, поэтому запрос ``PROVENANCE`` не добавляет
    агенту шагов тропы.
    """
    if not requested:
        return list(default), []
    accepted: list[str] = []
    rejected: list[str] = []
    for raw in requested:
        name = raw.strip().upper()
        if not name:
            continue
        expanded = TIER_KEYWORDS.get(name)
        if expanded is not None:
            accepted.extend(expanded)
        elif name in BY_NAME:
            accepted.append(name)
        else:
            rejected.append(name)
    return list(dict.fromkeys(accepted)), rejected


def _node_type(value: object) -> NodeType:
    """Строка типа узла → NodeType; неизвестный тип не должен проходить молча."""
    try:
        return NodeType(str(value).strip().lower())
    except ValueError as error:
        raise RelationContractError(f"Неизвестный тип узла: {value!r}") from error


def validate_edge(
    relation: str,
    source_type: object,
    target_type: object,
    *,
    asserted: bool = False,
) -> RelationSpec:
    """Проверяет отношение и типы концов до записи в граф.

    ``asserted=True`` — ребро выходит из claim-узла ( ``(claim)-[pred]->(object)`` ):
    тип субъекта задаёт не предикат, а объект утверждения.
    """
    declared = spec_for(relation)
    source = _node_type(source_type)
    target = _node_type(target_type)
    if asserted and not declared.claim_predicate:
        raise RelationContractError(f"{declared.name} не может быть предикатом утверждения")
    if not asserted and declared.source_types and source not in declared.source_types:
        raise RelationContractError(
            f"{declared.name}: источник {source.value} вне домена "
            f"{sorted(item.value for item in declared.source_types)}"
        )
    if declared.target_types and target not in declared.target_types:
        raise RelationContractError(
            f"{declared.name}: цель {target.value} вне диапазона "
            f"{sorted(item.value for item in declared.target_types)}"
        )
    return declared


def relations_for_prompt(tier: RelationTier = RelationTier.SEMANTIC) -> str:
    """Строки allowlist для промпта: имена и смысл генерируются из реестра."""
    return "\n".join(f"- {spec.name} — {spec.meaning}" for spec in REGISTRY if spec.tier is tier)


def relations_for_extraction() -> str:
    """Словарь предикатов для промпта экстракции: только ярус semantic."""
    return ", ".join(SEMANTIC_RELATIONS)
