from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from scientific_tangle.domain.contracts import Finding, GraphEdge, GraphNode, NodeType

logger = logging.getLogger(__name__)

# Имена сообществ по ключевым словам домена: порядок важен, первым выбирается
# наиболее специфичное совпадение.
COMMUNITY_KEYWORDS: list[tuple[str, list[str]]] = [
    (
        "Водоподготовка",
        [
            "вод",
            "осмос",
            "мембран",
            "фильтрац",
            "обессолив",
            "ионн",
            "выпарив",
            "пермеат",
            "рассол",
            "сухой остаток",
            "смола",
        ],
    ),
    (
        "Гидрометаллургия",
        [
            "выщелачивани",
            "кучн",
            "хлорн",
            "цианидн",
            "экстракц",
            "продуктивн",
            "руд",
            "малахит",
            "азурит",
            "кислот",
            "раствор",
        ],
    ),
    (
        "Пирометаллургия",
        [
            "плавк",
            "конверт",
            "обжиг",
            "шлак",
            "штейн",
            "матт",
            "возгон",
            "печь",
            "концентрат",
            "so2",
        ],
    ),
    (
        "Электролиз",
        [
            "электролиз",
            "электровыскан",
            "электрорафинир",
            "катод",
            "анод",
            "ток",
            "напряжен",
            "выпрям",
            "шлам",
            "плотность",
        ],
    ),
    (
        "Очистка растворов",
        [
            "осаждени",
            "цементац",
            "сорбц",
            "очистк",
            "желез",
            "свинец",
            "мышьяк",
            "сурьма",
            "цинк",
            "гётит",
            "ярозит",
            "силикагел",
        ],
    ),
    (
        "Получение солей",
        [
            "сульфат",
            "кобальт",
            "литий",
            "кристаллиз",
            "сподумен",
            "гидроксид",
            "никель класс",
            "карбонат",
        ],
    ),
    (
        "Переработка штейнов",
        [
            "файнштейн",
            "хибинетт",
            "cesl",
            "никкельвер",
            "niihama",
            "sandouville",
            "штейн",
            "медно-никел",
        ],
    ),
]

TYPE_NAMES: dict[str, str] = {
    "material": "Материалы",
    "process": "Технологические процессы",
    "equipment": "Оборудование",
    "condition": "Условия процесса",
    "claim": "Утверждения",
    "expert": "Эксперты",
    "publication": "Публикации",
    "chunk": "Фрагменты",
    "location": "Локации",
    "organization": "Организации",
    "experiment": "Эксперименты",
}

FALLBACK_COMMUNITY = "База знаний"
SMALL_COMMUNITY = "Прочие сущности"
MIN_COMMUNITY_SIZE = 15


def detect_communities(nodes: list[GraphNode], edges: list[GraphEdge]) -> list[str]:
    """Кластеризует граф и проставляет ``metadata.community`` каждому узлу.

    Используется обоими knowledge-бэкендами: ранее production-ветка считала
    сообщества здесь же, но в retrieval возвращала захардкоженные строки seed-графа,
    из-за чего «global»-контекст не имел отношения к вопросу.
    """
    if not nodes:
        return []

    node_by_id = {node.id: node for node in nodes}
    if not edges:
        for node in nodes:
            node.metadata["community"] = FALLBACK_COMMUNITY
        return [FALLBACK_COMMUNITY]

    graph = _build_graph(nodes, edges, node_by_id)
    partitions = (
        _leiden_partitions(graph) or _louvain_partitions(graph) or _greedy_partitions(graph)
    )
    if not partitions:
        for node in nodes:
            node.metadata["community"] = FALLBACK_COMMUNITY
        return [FALLBACK_COMMUNITY]

    return _label_partitions(nodes, partitions, node_by_id)


def _build_graph(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    node_by_id: dict[str, GraphNode],
) -> Any:
    import networkx as nx

    graph = nx.Graph()
    graph.add_nodes_from(node.id for node in nodes)
    for edge in edges:
        if edge.source in node_by_id and edge.target in node_by_id:
            graph.add_edge(edge.source, edge.target, weight=edge.confidence)
    return graph


def _leiden_partitions(graph: Any) -> list[set[str]]:
    """Leiden предпочтителен; недоступен без igraph/leidenalg — тогда Louvain."""
    try:
        import igraph as ig
        import leidenalg
    except ImportError:
        logger.info("Leiden недоступен (igraph/leidenalg не установлены)")
        return []
    try:
        node_list = list(graph.nodes())
        index = {node: position for position, node in enumerate(node_list)}
        ig_graph = ig.Graph(
            n=len(node_list),
            edges=[(index[u], index[v]) for u, v in graph.edges()],
            directed=False,
        )
        for u, v, data in graph.edges(data=True):
            ig_graph.es[ig_graph.get_eid(index[u], index[v])]["weight"] = data.get("weight", 1.0)
        partition = leidenalg.find_partition(ig_graph, leidenalg.ModularityVertexPartition, seed=42)
        buckets: dict[int, set[str]] = {}
        for position, community in enumerate(partition.membership):
            buckets.setdefault(community, set()).add(node_list[position])
        return list(buckets.values())
    except Exception as error:  # noqa: BLE001 - деградация на Louvain обязательна
        logger.warning("Leiden завершился ошибкой: %s", error)
        return []


def _louvain_partitions(graph: Any) -> list[set[str]]:
    import networkx as nx

    for resolution in (0.2, 0.3, 0.4, 0.6, 0.8, 1.0):
        try:
            partitions = list(
                nx.algorithms.community.louvain_communities(graph, seed=42, resolution=resolution)
            )
        except Exception:  # noqa: BLE001
            continue
        if 3 <= len(partitions) <= 8:
            return partitions
    return []


def _greedy_partitions(graph: Any) -> list[set[str]]:
    import networkx as nx

    try:
        return list(nx.algorithms.community.greedy_modularity_communities(graph))
    except Exception:  # noqa: BLE001
        return []


def _label_partitions(
    nodes: list[GraphNode],
    partitions: list[set[str]],
    node_by_id: dict[str, GraphNode],
) -> list[str]:
    ordered = sorted(partitions, key=len, reverse=True)
    names: list[str] = []
    used: set[str] = set()
    small: set[str] = set()

    for partition in ordered:
        if len(partition) < MIN_COMMUNITY_SIZE:
            small.update(partition)
            continue
        labels = [
            node_by_id[node_id].label.lower() for node_id in partition if node_id in node_by_id
        ]
        types = [node_by_id[node_id].type.value for node_id in partition if node_id in node_by_id]
        name = name_community(labels, types, used)
        used.add(name)
        names.append(name)
        for node_id in partition:
            node = node_by_id.get(node_id)
            if node is not None:
                node.metadata["community"] = name

    if small:
        name = SMALL_COMMUNITY if SMALL_COMMUNITY not in used else f"Сообщество {len(used) + 1}"
        used.add(name)
        names.append(name)
        for node_id in small:
            node = node_by_id.get(node_id)
            if node is not None:
                node.metadata["community"] = name

    if not names:
        for node in nodes:
            node.metadata["community"] = FALLBACK_COMMUNITY
        return [FALLBACK_COMMUNITY]
    return names


def name_community(
    labels: list[str],
    types: list[str],
    used_names: set[str],
) -> str:
    """Подбирает имя сообщества по ключевым словам меток или доминирующему типу."""
    best_match: str | None = None
    best_score = 0
    for community_name, keywords in COMMUNITY_KEYWORDS:
        if community_name in used_names:
            continue
        score = sum(1 for label in labels for keyword in keywords if keyword in label)
        if score > best_score:
            best_score = score
            best_match = community_name
    if best_match:
        return best_match
    if types:
        dominant = Counter(types).most_common(1)[0][0]
        base_name = TYPE_NAMES.get(dominant, "Сообщество")
        if base_name not in used_names:
            return base_name
    return f"Сообщество {len(used_names) + 1}"


# ── Профили сообществ ───────────────────────────────────────────────────────

MAX_PROFILE_LABELS = 8
MAX_PROFILE_CLAIMS = 4
MAX_PROFILE_SOURCES = 3
DEFAULT_PROFILE_LIMIT = 4
SUMMARY_MAX_CHARS = 520


@dataclass(frozen=True, slots=True)
class CommunityProfile:
    """Сжимаемое описание сообщества из его же членов.

    Суммаризация детерминированная: имя, состав по типам, центральные метки,
    утверждения и источники. LLM здесь не вызывается — провайдер принадлежит
    другому контуру, а «global»-контекст обязан работать и без модели.
    """

    name: str
    size: int
    node_ids: frozenset[str] = frozenset()
    types: tuple[tuple[str, int], ...] = ()
    labels: tuple[str, ...] = ()
    claims: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    years: tuple[int, ...] = ()
    geographies: tuple[str, ...] = ()

    def haystack(self) -> str:
        parts: list[str] = [self.name, *self.labels, *self.claims, *self.sources]
        parts.extend(self.geographies)
        parts.extend(TYPE_NAMES.get(kind, kind) for kind, _ in self.types)
        return " ".join(str(part).lower() for part in parts)

    def summary(self, limit: int = SUMMARY_MAX_CHARS) -> str:
        kinds = ", ".join(
            f"{TYPE_NAMES.get(kind, kind).lower()} {count}" for kind, count in self.types[:3]
        )
        pieces = [f"«{self.name}»: {self.size} сущностей"]
        if kinds:
            pieces.append(kinds)
        if self.labels:
            pieces.append("сущности: " + ", ".join(self.labels))
        if self.claims:
            pieces.append("утверждения: " + "; ".join(self.claims))
        if self.sources:
            pieces.append(f"источники: {', '.join(self.sources)}{self._period()}")
        text = " · ".join(pieces)
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _period(self) -> str:
        """Датировка и территория источников — условия применимости выводов."""
        parts: list[str] = []
        if self.years:
            span = (
                f"{self.years[0]}"
                if len(self.years) == 1
                else f"{min(self.years)}–{max(self.years)}"
            )
            parts.append(span)
        if self.geographies:
            parts.append(", ".join(self.geographies[:2]))
        return (", " + "; ".join(parts)) if parts else ""


def build_community_profiles(
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    findings: Iterable[Finding] = (),
) -> list[CommunityProfile]:
    """Профили по уже проставленным ``metadata.community``; без LLM и без сети."""
    buckets: dict[str, list[GraphNode]] = {}
    for node in nodes:
        name = node.metadata.get("community")
        if isinstance(name, str) and name:
            buckets.setdefault(name, []).append(node)
    if not buckets:
        return []

    degrees: Counter[str] = Counter()
    for edge in edges:
        degrees[edge.source] += 1
        degrees[edge.target] += 1
    membership: dict[str, str] = {
        node.id: str(node.metadata["community"])
        for node in nodes
        if node.metadata.get("community")
    }
    statements = _statements_by_community(findings, membership)

    profiles: list[CommunityProfile] = []
    for name, members in buckets.items():
        node_ids = frozenset(node.id for node in members)
        ordered = sorted(members, key=lambda node: (-degrees[node.id], node.label, node.id))
        labels = tuple(dict.fromkeys(node.label for node in ordered))[:MAX_PROFILE_LABELS]
        claims = tuple(node.label for node in ordered if node.type == NodeType.CLAIM)
        merged = tuple(dict.fromkeys([*claims, *statements.get(name, ())]))[:MAX_PROFILE_CLAIMS]
        types = tuple(
            sorted(
                Counter(node.type.value for node in members).items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        sources = tuple(
            dict.fromkeys(node.label for node in ordered if node.type == NodeType.PUBLICATION)
        )[:MAX_PROFILE_SOURCES]
        years = tuple(sorted(_member_years(members)))
        geographies = _member_geographies(members)[:3]
        profiles.append(
            CommunityProfile(
                name=name,
                size=len(members),
                node_ids=node_ids,
                types=types,
                labels=labels,
                claims=merged,
                sources=sources,
                years=years,
                geographies=geographies,
            )
        )
    # Фиксированный порядок профиля: ранжирование тогда детерминировано.
    return sorted(profiles, key=lambda profile: profile.name)


def rank_community_profiles(
    profiles: Sequence[CommunityProfile],
    tokens: Iterable[str],
    limit: int = DEFAULT_PROFILE_LIMIT,
) -> list[CommunityProfile]:
    """Релевантность — число различных токенов доказательств в профиле.

    Раньше релевантность считалась по ``finding.subject``, которого у структурных
    находок нет вовсе: нулевые срабатывания у всех узлов давали алфавитный
    список имён, а fallback — недостижимую ветку. При нулевом совпадении
    возвращаются крупнейшие сообщества: они по-прежнему содержат текст, а не
    только ярлык.
    """
    wanted = {token.lower() for token in tokens if len(token) >= 3}
    if not wanted:
        return sorted(profiles, key=lambda profile: (-profile.size, profile.name))[:limit]

    def key(profile: CommunityProfile) -> tuple[int, int, str]:
        haystack = profile.haystack()
        hits = sum(1 for token in wanted if token in haystack)
        return (-hits, -profile.size, profile.name)

    return sorted(profiles, key=key)[:limit]


def community_briefs(
    nodes: Sequence[GraphNode],
    edges: Sequence[GraphEdge],
    findings: Iterable[Finding],
    tokens: Iterable[str],
    limit: int = DEFAULT_PROFILE_LIMIT,
) -> list[str]:
    """Содержательные сводки ранжированных сообществ — общий текст для обоих бэкендов.

    Ранее retrieval возвращал просто имена кластеров: в секцию GLOSSARY/COMMUNITIS
    попадал список ярлыков без единого факта, то есть «global»-контекст не проверялся
    и не мог поддержать ответ. Профиль собирается из самих узлов сообщества (метки,
    утверждения, источники, датировка), выдуманных фактов здесь нет по построению.
    """
    profiles = build_community_profiles(nodes, edges, findings)
    if not profiles:
        return []
    return [profile.summary() for profile in rank_community_profiles(profiles, tokens, limit)]


def _statements_by_community(
    findings: Iterable[Finding],
    membership: dict[str, str],
) -> dict[str, tuple[str, ...]]:
    """Утверждения находок по сообществам.

    Связь — id узла: находка называется ``finding-<id узла>``, а у структурных
    находок subject пуст, поэтому опираться на него нельзя.
    """
    collected: dict[str, list[str]] = {}
    for finding in findings:
        community = membership.get(finding.id.removeprefix("finding-"))
        if community is None:
            continue
        bucket = collected.setdefault(community, [])
        if len(bucket) < MAX_PROFILE_CLAIMS:
            bucket.append(finding.statement[:160].strip())
    return {name: tuple(statements) for name, statements in collected.items()}


def _member_years(members: Sequence[GraphNode]) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                int(str(node.metadata["year"]))
                for node in members
                if _is_year(node.metadata.get("year"))
            }
        )
    )


def _member_geographies(members: Sequence[GraphNode]) -> tuple[str, ...]:
    values = (
        str(node.metadata.get("geography") or "").strip()
        for node in sorted(members, key=lambda node: node.id)
    )
    return tuple(dict.fromkeys(value for value in values if value))


def _is_year(value: object) -> bool:
    try:
        year = int(str(value))
    except (TypeError, ValueError):
        return False
    return 1800 <= year <= 2100
