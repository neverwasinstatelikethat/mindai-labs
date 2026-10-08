from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import wraps
from typing import Any, Protocol, cast, runtime_checkable
from uuid import UUID, uuid5

from scientific_tangle.domain.contracts import (
    CorpusStats,
    DocumentFragment,
    DocumentReceipt,
    DocumentRequest,
    ExtractionResult,
    Finding,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    NodeType,
    RetrievalPlan,
    StructuralDocumentReceipt,
)
from scientific_tangle.domain.hypotheses import HypothesisSignal, HypothesisWindow
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import (
    EvidenceLocator,
    NumericObservation,
    QueryPlan,
)
from scientific_tangle.services.communities import (
    DEFAULT_PROFILE_LIMIT,
    MAX_PROFILE_CLAIMS,
    CommunityProfile,
    build_community_profiles,
    detect_communities,
    rank_community_profiles,
)
from scientific_tangle.services.governance import AccessPolicyEngine
from scientific_tangle.services.reranking import query_tokens, rerank_findings
from scientific_tangle.services.retrieval_semantics import (
    DEMO_ORIGIN,
    RETRIEVAL_TOP_K,
    SCOPE_GEOGRAPHY,
    SCOPE_ORIGIN,
    SCOPE_YEAR,
    AbortCheck,
    abort_note,
    apply_numeric_filters,
    candidate_window,
    cap_notes,
    dedupe_findings,
    demo_notes,
    enforce_scope,
    global_context_notes,
    is_aborted,
    numeric_notes,
    scope_notes,
    split_demo,
)

NAMESPACE = UUID("f83e9ec0-5094-4ae5-b2ba-178777d09443")

logger = logging.getLogger(__name__)

# Константы окна обхода и слияния общие для обоих бэкендов: разные значения в
# memory- и neo4j-ветке означали бы разный ответ на один и тот же план.
MAX_ANCHORS = 5
_RRF_K = 5

# Окно чтения каталога находок: списочным маршрутам не нужен весь список с
# копией каждой находки. Потолок страницы держит ответ в разумном размере, а
# запрос с большим ``limit`` обрезается до него, а не читает корпус целиком.
FINDINGS_WINDOW_DEFAULT = 50
FINDINGS_WINDOW_MAX = 500

# Окно и шаг структурного чанкинга — единые для обоих бэкендов: идентификатор
# чанка вычисляется от смещения внутри фрагмента, поэтому разные константы
# дали бы два несводимых контракта id на один и тот же документ.
CHUNK_WINDOW = 4000
CHUNK_STRIDE = 3700
CHUNK_QUOTE_LIMIT = 1200
CHUNK_STATEMENT_LIMIT = 4000
CHUNK_MIN_CHARS = 40

# Сколько живут производные от графа сводки сообществ и сколько ключей держится в
# кэше. Значения совпадают с TTL полного графа в production-ветке
# (``GRAPH_CACHE_TTL_SECONDS`` в services/infrastructure.py): производный кэш не
# имеет права переживать граф, из которого собран.
BRIEF_CACHE_TTL_SECONDS = 30.0
BRIEF_CACHE_MAX_ENTRIES = 8

# Ключ кэша сводок: (поколение графа, срез доступа, набор сообществ). Поколение
# меняет любой вызов ``_invalidate()``/``_invalidate_graph_cache()``, то есть
# любая запись в корпус, поэтому ключ сам по себе исключает устаревший профиль.
GraphStamp = tuple[object, ...]
BriefKey = tuple[GraphStamp, frozenset[str] | None, tuple[str, ...]]


def stable_uuid(value: str) -> UUID:
    return uuid5(NAMESPACE, value)


__all__ = [
    "BRIEF_CACHE_TTL_SECONDS",
    "CHUNK_STRIDE",
    "CHUNK_WINDOW",
    "BriefKey",
    "ChunkPiece",
    "CommunityBriefCache",
    "CommunityViews",
    "FindingWindow",
    "GraphStamp",
    "InMemoryKnowledgeBase",
    "KnowledgeBase",
    "NoChunksError",
    "RetrievalContext",
    "brief_key",
    "cached_community_briefs",
    "chunk_document",
    "chunk_finding",
    "document_scope",
    "finalize_retrieval",
    "finding_communities",
    "graph_element_ids",
    "invalidate_derived_llm_cache",
    "normalize_window",
    "quote_offsets",
    "stable_uuid",
]


def normalize_window(
    limit: int, offset: int, *, max_limit: int = FINDINGS_WINDOW_MAX
) -> tuple[int, int]:
    """Окно каталога к одному виду для обоих бэкендов.

    Отрицательное смещение и «безразмерная» страница — это не «как повезёт»:
    ``offset`` меньше нуля читался бы с конца списка, а ``limit`` без потолка
    доставал бы весь корпус, то есть ровно та цена, ради которой окно и ввели.
    """
    size = min(max(int(limit), 1), max(int(max_limit), 1))
    start = max(int(offset), 0)
    return size, start


def invalidate_derived_llm_cache(context: str) -> None:
    """Сбрасывает кэш structured output провайдера после мутации корпуса.

    Промпты модели собираются из текста корпуса, поэтому закэшированный ответ
    устаревает вместе с находками — даже когда промпт буквально совпадает. Вызов
    отложенный: ``services/provider.py`` сам не импортирует knowledge-модуль, но
    импорт верхнего уровня связал бы два контура в цикл при сборке пакета.
    Отсутствие кэша или его сбой не имеют права валить запись в корпус — наружу
    уходит только предупреждение в лог.
    """
    try:
        from scientific_tangle.services import provider

        invalidate = getattr(provider, "invalidate_llm_cache", None)
        if callable(invalidate):
            invalidate()
    except Exception as error:  # noqa: BLE001 - инвалидация чужого кэша не блокирует запись
        logger.warning("Кэш LLM не сброшен после изменения %s: %s", context, error)


@dataclass(frozen=True, slots=True)
class FindingWindow:
    """Окно каталога находок вместе с полным числом подходящих записей.

    ``total`` — сколько находок подходит под фильтр доступа во всём каталоге, а
    не в странице: интерфейс режет список по нему. ``note`` обязана быть, когда
    страница оказалась короче ``limit`` по причинам, которые запрос к индексу не
    описывает (заменённые версии и демо-контент в индексах прежнего маппинса).
    """

    findings: list[Finding]
    total: int
    offset: int
    limit: int
    note: str | None = None



def brief_key(
    stamp: GraphStamp,
    allowed_data_classes: set[DataClass] | None,
    communities: Sequence[str],
) -> BriefKey:
    """Ключ производного от графа кэша: поколение, срез доступа, набор сообществ.

    Срез доступа входит в ключ обязательно: профиль, собранный по одному набору
    разрешённых классов данных, не имеет права доставаться другому — иначе
    restricted-метки уедут в промпт модели в обход политик.
    """
    allowed = (
        None
        if allowed_data_classes is None
        else frozenset(item.value for item in allowed_data_classes)
    )
    return (stamp, allowed, tuple(communities))


def cached_community_briefs(
    cache: CommunityBriefCache,
    key: BriefKey,
    snapshot: Callable[[], GraphSnapshot],
    findings: Iterable[Finding],
    tokens: Iterable[str],
    limit: int = DEFAULT_PROFILE_LIMIT,
) -> list[str]:
    """Сводки сообществ через кэш профилей — общий путь для обоих бэкендов.

    ``snapshot`` вызывается только на промахе: на попадании профили зависят
    исключительно от графовой части, а ACL-срез входит в ключ.
    """
    views = cache.views(key)
    if views is None:
        built = snapshot()
        views = cache.store(key, built.nodes, built.edges)
    return cache.briefs(views, findings, tokens, limit)


@dataclass(frozen=True, slots=True)
class CommunityViews:
    """Графовая часть сводок: профили сообществ и принадлежность узлов.

    Профиль сообщества — чистая функция от узлов и рёбер (метки, состав по типам,
    источники, датировка, территория). Находки плана влияют только на поле
    ``claims``, поэтому в кэш идёт именно эта независимая от запроса часть, а
    утверждения кандидатов подмешиваются на каждый вызов.
    """

    key: BriefKey
    profiles: tuple[CommunityProfile, ...]
    membership: Mapping[str, str]


class CommunityBriefCache:
    """Кэш профилей сообществ, общий для memory- и neo4j-контура.

    ``community_briefs`` пересобирает профили по всему ACL-графу на каждое
    действие: исследовательский запрос делает это до 24 раз подряд на одном и том
    же графе (`AGENT_MAX_TOOL_ROUNDS` ≤ 4 × не более 6 действий в плане). Кэш
    короткоживущий и инвалидируется по поколению графа — в тех же точках записи,
    что и кэш полного графа.

    Замок обязателен: действия одного запроса исполняются параллельно в
    worker-потоках (``asyncio.to_thread`` в agents/tools.py), а ``OrderedDict`` с
    LRU-перестановкой при чтении не атомарен. Значение собирается целиком до
    записи, так что гонка потоков дала бы только повторный пересчёт, но не
    частичный профиль.
    """

    def __init__(
        self,
        ttl_seconds: float = BRIEF_CACHE_TTL_SECONDS,
        max_entries: int = BRIEF_CACHE_MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max(1, max_entries)
        self._lock = threading.Lock()
        self._entries: OrderedDict[BriefKey, tuple[float, CommunityViews]] = OrderedDict()

    def invalidate(self) -> None:
        """Сбрасывает всё: любое изменение корпуса делает профили неверными."""
        with self._lock:
            self._entries.clear()

    def views(self, key: BriefKey) -> CommunityViews | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, views = entry
            if time.monotonic() - stored_at >= self._ttl:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return views

    def store(
        self, key: BriefKey, nodes: Sequence[GraphNode], edges: Sequence[GraphEdge]
    ) -> CommunityViews:
        # Профили без находок: ``claims`` такого профиля — метки узлов-утверждений,
        # то есть ровно та часть поля, которую даёт один граф; утверждения
        # кандидатов подмешиваются сверх неё (см. ``_with_finding_claims``).
        profiles = build_community_profiles(nodes, edges)
        membership = {
            node.id: str(node.metadata["community"])
            for node in nodes
            if isinstance(node.metadata.get("community"), str)
        }
        views = CommunityViews(key=key, profiles=tuple(profiles), membership=membership)
        with self._lock:
            self._entries[key] = (time.monotonic(), views)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return views

    def briefs(
        self,
        views: CommunityViews,
        findings: Iterable[Finding],
        tokens: Iterable[str],
        limit: int,
    ) -> list[str]:
        """Сводки поверх кэшированных профилей — то же, что возвращает ``community_briefs``."""
        profiles = _with_finding_claims(views, findings)
        if not profiles:
            return []
        return [profile.summary() for profile in rank_community_profiles(profiles, tokens, limit)]


def _with_finding_claims(
    views: CommunityViews, findings: Iterable[Finding]
) -> list[CommunityProfile]:
    """Подмешивает утверждения кандидатов в профили — дословно как communities.py.

    ``build_community_profiles`` собирает ``claims`` как
    ``dict.fromkeys([*метки_узлов_утверждений, *утверждения_находок])[:MAX_PROFILE_CLAIMS]``.
    Кэш держит первую половину этого слияния, уже обрезанную по потолку: если в
    кэшированном профиле набрался полный потолок, утверждениям находок места не
    достаётся и профиль уходит как есть, а иначе слияние повторяется здесь.
    Порядок и потолок обязаны совпадать — расхождение тест сравнивает напрямую
    (``test_cached_briefs_match_community_briefs`` и
    ``test_cached_briefs_match_community_briefs_on_merged_claims``).
    """
    statements: dict[str, list[str]] = {}
    for finding in findings:
        community = views.membership.get(finding.id.removeprefix("finding-"))
        if community is None:
            continue
        bucket = statements.setdefault(community, [])
        if len(bucket) < MAX_PROFILE_CLAIMS:
            bucket.append(finding.statement[:160].strip())
    if not statements:
        return list(views.profiles)
    merged: list[CommunityProfile] = []
    for profile in views.profiles:
        extra = statements.get(profile.name)
        if not extra or len(profile.claims) >= MAX_PROFILE_CLAIMS:
            merged.append(profile)
            continue
        merged.append(
            replace(
                profile,
                claims=tuple(dict.fromkeys([*profile.claims, *extra]))[:MAX_PROFILE_CLAIMS],
            )
        )
    return merged


def graph_element_ids(
    nodes: Iterable[GraphNode], edges: Iterable[GraphEdge]
) -> tuple[set[str], set[str]]:
    """Множества id узлов и рёбер снимка — единственное, что нужно диффу импорта.

    Состояние «до записи» раньше сравнивали через ``full_graph()``: он строит глубокую
    копию всего графа и поднимает пересчёт сообществ, то есть на загрузке корпуса из
    сотен документов стоимость росла квадратично при том, что из снимка «до» читают
    только ``id`` — сами элементы для диффа берёт снимок «после».
    """
    return ({node.id for node in nodes}, {edge.id for edge in edges})


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    findings: list[Finding]
    graph: GraphSnapshot
    community_summaries: list[str]
    # Явный признак «доказательств нет»: вызывает подстановку нерелевантных seed-данных
    # вместо честного warning-наблюдения.
    no_evidence: bool = False
    vectors_used: bool = False
    # Что retrieval реально сделал с планом: сколько срезано numeric/scope-ограничениями,
    # чем заполнена выдача и где потолок. Исполнитель обязан донести это до
    # ``degradation_reasons``, а не только доложить число оставшихся доказательств.
    degradation_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class KnowledgeState:
    """Снимок каталога знаний: опора для отката прерванной записи."""

    documents: dict[str, DocumentRequest]
    findings: dict[str, Finding]
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    chains: dict[str, list[str]]


class NoChunksError(ValueError):
    """Структурный импорт не породил ни одного чанка — молча «создано» вернуть нельзя.

    Порог разбора файла (20 символов суммарного текста) ниже порога чанкинга
    (40 символов на кусок): документ из одного короткого фрагмента проходит
    парсер, но в индекс не легло ничего. Прежний ``status="created"`` при
    ``chunks=0`` считался покрытым файлом в отчёте компиляции и в статистике
    корпуса, а в окне поиска его нет — аналитик получал «в корпусе такого нет»
    на текст, который он в корпус положил.
    """


@dataclass(frozen=True, slots=True)
class ChunkPiece:
    """Кусок текста с известным смещением внутри фрагмента — база для локатора.

    ``char_start``/``char_end`` равны ``None``, когда смещение вывести нельзя:
    нули и догадки в локаторе хуже пустого поля — их нечем проверить.
    """

    id: str
    text: str
    fragment: DocumentFragment
    char_start: int | None
    char_end: int | None


def quote_offsets(text: str, quote: str) -> tuple[int, int] | None:
    """Точные символьные смещения цитаты внутри фрагмента, либо ``None``.

    Цитата приходит из модели и часто нормализована (регистр, пробелы, перенос
    строки), поэтому после точного поиска пробуется сравнение без учёта регистра
    при той же длине — оно не сдвигает границы. Ни одно совпадение не найдено —
    смещения не выставляются: локатор обязан указывать на текст, который в
    источнике действительно есть.
    """
    if not quote or not text:
        return None
    needle = quote.strip()
    if not needle:
        return None
    start = text.find(needle)
    if start < 0:
        lowered = text.casefold()
        start = lowered.find(needle.casefold())
    if start < 0 or needle not in text and needle.casefold() not in text.casefold():
        return None
    return start, start + len(needle)


def document_scope(document: DocumentRequest) -> dict[str, str]:
    """Документированные условия применимости источника — год и территория.

    ``QueryPlan.year_from``/``year_to`` и ``countries`` имеют смысл только если
    находка несёт датировку и регион самого источника; отсутствие значения —
    это неизвестность, а не «год 0» и не «вся планета».
    """
    scope: dict[str, str] = {}
    if document.year is not None:
        scope[SCOPE_YEAR] = str(document.year)
    if document.geography and document.geography.strip():
        scope[SCOPE_GEOGRAPHY] = document.geography.strip()
    return scope


def chunk_document(document: DocumentRequest, document_id: UUID) -> list[ChunkPiece]:
    """Структурный чанкинг: один идентификатор чанка на оба бэкенда.

    Идентификатор документа обязателен в ключе: без него два документа с
    одинаковым title и совпадающим началом фрагмента коллапсировали бы в один
    chunk-id, и трассировка «документ → фрагмент» стала бы фиктивной.
    """
    pieces: list[ChunkPiece] = []
    for fragment_index, fragment in enumerate(document.fragments):
        text = fragment.text.strip()
        for offset in range(0, len(text), CHUNK_STRIDE):
            part = text[offset : offset + CHUNK_WINDOW].strip()
            if len(part) < CHUNK_MIN_CHARS:
                continue
            stable_key = f"{document_id}:{fragment_index}:{offset}:{part[:80]}"
            start, end = (
                quote_offsets(fragment.text, part[:CHUNK_QUOTE_LIMIT]) or (None, None)
            )
            pieces.append(
                ChunkPiece(
                    id=f"chunk-{stable_uuid(stable_key)}",
                    text=part,
                    fragment=fragment,
                    char_start=start,
                    char_end=end if start is not None else None,
                )
            )
    return pieces


def chunk_finding(document: DocumentRequest, document_id: UUID, piece: ChunkPiece) -> Finding:
    """Находка структурного чанка: локатор со смещениями и условиями источника."""
    quote = piece.text[:CHUNK_QUOTE_LIMIT]
    char_end = piece.char_end
    if piece.char_start is not None and char_end is not None and char_end <= piece.char_start:
        char_end = None
    return Finding(
        id=piece.id,
        statement=piece.text[:CHUNK_STATEMENT_LIMIT],
        confidence=0.7,
        data_class=document.data_class,
        evidence=[
            EvidenceLocator(
                document_id=document_id,
                source_title=document.title,
                page=piece.fragment.page,
                sheet=piece.fragment.sheet,
                cell_range=piece.fragment.cell_range,
                char_start=piece.char_start,
                char_end=char_end,
                quote=quote or document.title,
            )
        ],
        status="hypothesis",
        scope=document_scope(document),
    )


def finding_communities(
    nodes: Iterable[GraphNode],
    findings: Iterable[Finding],
) -> dict[str, str]:
    """``finding.id → имя сообщества`` уже посчитанного графа.

    Связь по id узла: находка называется ``finding-<узел>``, а структурная находка
    и есть узел чанка; опираться на ``subject`` нельзя — у структурных находок он пуст.
    """
    membership = {
        node.id: str(node.metadata["community"])
        for node in nodes
        if isinstance(node.metadata.get("community"), str)
    }
    resolved: dict[str, str] = {}
    for finding in findings:
        bare = finding.id.removeprefix("finding-")
        community = membership.get(bare) or membership.get(finding.id)
        if community:
            resolved[finding.id] = community
    return resolved


def finalize_retrieval(
    candidates: Sequence[Finding],
    plan: QueryPlan,
    *,
    graph: GraphSnapshot,
    community_summaries: Sequence[str],
    vectors_used: bool,
    notes: Sequence[str] = (),
    top_k: int = RETRIEVAL_TOP_K,
) -> RetrievalContext:
    """Единая семантика исполнения плана retrieval для обоих бэкендов.

    Порядок шагов один и тот же: числовые ограничения → год/география по
    документированному значению источника → демо-контент → потолок выдачи. Каждый
    срез называется в ``degradation_reasons`` числом, поэтому «доказательств после
    фильтрации: N» перестаёт быть утверждением, которое зависит от бэкенда.
    """
    pool = dedupe_findings(candidates)
    after_numeric = apply_numeric_filters(pool, plan.numeric_filters)
    degradation = [
        *notes,
        *numeric_notes(len(pool), len(after_numeric)),
    ]
    scope = enforce_scope(after_numeric, plan)
    degradation.extend(scope_notes(scope, plan))
    real, demo = split_demo(scope.kept)
    kept = [*real, *demo][:top_k]
    if demo:
        degradation.extend(demo_notes(len(real), len(demo)))
    degradation.extend(cap_notes(len(pool), len(scope.kept), len(kept), top_k))
    return RetrievalContext(
        findings=[finding.model_copy(deep=True) for finding in kept],
        graph=graph,
        community_summaries=list(community_summaries),
        no_evidence=not kept,
        vectors_used=vectors_used,
        degradation_reasons=_dedupe_strings(degradation),
    )


def _dedupe_strings(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _document_node_metadata(document: DocumentRequest) -> dict[str, str | int | float | bool]:
    """Метаданные публикации: год и территория источника, если они документированы.

    ``year = 0`` вместо неизвестного года раньше превращал «датировки нет» в
    «документ 1970 года» для сводок сообществ и для отчёта по корпусу.
    """
    metadata: dict[str, str | int | float | bool] = {}
    if document.year is not None:
        metadata["year"] = document.year
    geography = (document.geography or "").strip()
    if geography:
        metadata["geography"] = geography
    return metadata


def _chunk_piece_metadata(piece: ChunkPiece) -> dict[str, str | int | float | bool]:
    """Локализация чанка в узле графа: страница, лист, диапазон ячеек."""
    metadata: dict[str, str | int | float | bool] = {}
    if piece.fragment.page is not None:
        metadata["page"] = piece.fragment.page
    for key, value in (("sheet", piece.fragment.sheet), ("cell_range", piece.fragment.cell_range)):
        if isinstance(value, str) and value:
            metadata[key] = value
    if piece.char_start is not None:
        metadata["char_start"] = piece.char_start
    if piece.char_end is not None:
        metadata["char_end"] = piece.char_end
    return metadata


def _chunk_state_nodes(pieces: Sequence[ChunkPiece], document: DocumentRequest) -> list[GraphNode]:
    return [
        GraphNode(
            id=piece.id,
            label=piece.text[:160],
            type=NodeType.CHUNK,
            data_class=document.data_class,
            metadata=_chunk_piece_metadata(piece),
        )
        for piece in pieces
    ]


def _chunk_state_edges(
    pieces: Sequence[ChunkPiece], document_node_id: str, document: DocumentRequest
) -> list[GraphEdge]:
    return [
        GraphEdge(
            id=f"{document_node_id}-has-{piece.id}",
            source=document_node_id,
            target=piece.id,
            relation="HAS_CHUNK",
            data_class=document.data_class,
        )
        for piece in pieces
    ]


def _synchronized[MethodT: Callable[..., Any]](method: MethodT) -> MethodT:
    """Метод целиком под замком каталога памяти.

    В memory-контуре нет сетевого ввода-вывода: весь метод — это работа со
    словарями и списками процесса, поэтому критическая секция короткая по
    построению, а не по выверенному выбору строк. Разрывать её «где попало»
    означало бы вернуться к прежнему дефекту: обход ``self._findings`` в одном
    потоке против вставки в другом поднимает ``RuntimeError: dictionary changed
    size during iteration``, то есть 500 на ровном месте вместо ответа.

    Сигнатура сохраняется через generic-декоратор (PEP 695): без этого декоратор
    стирал бы типы публичных методов, и production-адаптер, вызывающий seed-контур,
    терял бы проверку возвращаемых значений.
    """

    @wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        store = cast("InMemoryKnowledgeBase", args[0])
        with store._lock:
            return method(*args, **kwargs)

    return cast("MethodT", wrapper)


@runtime_checkable
class KnowledgeBase(Protocol):
    """Полный контракт, который реально используют API и CLI.

    Методы, необходимые обработчикам, объявлены здесь: раньше их вызывали через
    ``hasattr(knowledge, ...)``, и бэкенд без метода молча возвращал ``200 []``.
    """

    def ingest(
        self,
        document: DocumentRequest,
        extraction: ExtractionResult,
        *,
        source_document_id: UUID | None = None,
        semantic_part_id: str | None = None,
        semantic_part_total: int | None = None,
    ) -> DocumentReceipt: ...

    def semantic_parts_completed(self, source_document_id: UUID, part_total: int) -> set[str]: ...

    def retrieve(
        self,
        plan: QueryPlan,
        retrieval_plan: RetrievalPlan,
        allowed_data_classes: set[DataClass] | None = None,
        *,
        abort: AbortCheck | None = None,
    ) -> RetrievalContext:
        """Исполняет план retrieval. ``abort`` — кооперативная отмена прогона:
        предикат читается на границе платных фаз, пропущенные фазы называются в
        деградации ответа. Без него retrieval исполняет план целиком."""
        ...

    def full_graph(self, allowed_data_classes: set[DataClass] | None = None) -> GraphSnapshot: ...

    def rank_findings(
        self,
        query: str,
        top_k: int = RETRIEVAL_TOP_K,
        mode: str = "hybrid",
        allowed_data_classes: set[DataClass] | None = None,
        semantic_query: str | None = None,
    ) -> list[Finding]: ...

    def document_count(self) -> int: ...

    def index_document(
        self, document: DocumentRequest, source_path: str
    ) -> StructuralDocumentReceipt: ...

    def corpus_stats(self) -> CorpusStats: ...

    def all_findings(self, allowed_data_classes: set[DataClass] | None = None) -> list[Finding]: ...

    def semantic_findings(self) -> list[Finding]: ...

    def semantic_graph(self) -> GraphSnapshot: ...

    def upsert_hypotheses(self, signals: Sequence[HypothesisSignal]) -> None: ...

    def hypotheses_window(
        self,
        *,
        limit: int,
        offset: int,
        allowed_data_classes: set[DataClass] | None = None,
    ) -> HypothesisWindow: ...

    def hypothesis_by_id(
        self, signal_id: UUID, allowed_data_classes: set[DataClass] | None = None
    ) -> HypothesisSignal | None: ...

    def findings_window(
        self,
        *,
        limit: int = FINDINGS_WINDOW_DEFAULT,
        offset: int = 0,
        allowed_data_classes: set[DataClass] | None = None,
        status: str | None = None,
        subject: str | None = None,
    ) -> FindingWindow: ...

    def warmup(self) -> None:
        """Прогрев хранилища до первого запроса (схема, индексы, каталог)."""
        ...

    def supersede_finding(
        self,
        finding_id: str,
        new_statement: str,
        new_confidence: float,
        observations: list[NumericObservation] | None = None,
        reviewer_id: str | None = None,
        review_date: str | None = None,
        review_reason: str | None = None,
    ) -> Finding: ...

    def set_finding_status(self, finding_id: str, status: str) -> Finding: ...

    def claim_history(self, finding_id: str) -> list[Finding]: ...

    def close(self) -> None: ...


def supersede_node_id(finding_id: str, existing_ids: set[str]) -> str:
    """id узла графа для находки каталога.

    У находок каталога префикс ``finding-``, у узлов графа его нет, и прямое
    снятие префикса угадывает: seed-тезис ``finding-ro`` живёт в узле
    ``claim-ro``, извлечённое утверждение ``finding-claim-<uuid>`` — в узле
    ``claim-<uuid>``, у структурного чанка узел совпадает с находкой. Перебор
    по реальным узлам вместо угадывания: ребро SUPERSEDES обязано соединять
    существующие узлы, иначе оно висит в memory-графе, а в Neo4j молча не
    создаётся на MERGE концов.
    """
    if finding_id in existing_ids:
        return finding_id
    suffix = finding_id.removeprefix("finding-")
    for candidate in (suffix, f"claim-{suffix}"):
        if candidate in existing_ids:
            return candidate
    return suffix if suffix.startswith(("claim-", "chunk-")) else f"claim-{suffix}"


class InMemoryKnowledgeBase:
    """In-memory KG-adapter с тем же контрактом, что и production репозитории.

    Контур исполняется в рабочих потоках (``asyncio.to_thread`` в API и в
    инструментах агента), поэтому каталог, граф и цепочки версий закрыты одним
    замком: ``RLock`` — потому что публичные методы вызывают друг друга
    (``retrieve`` → ``full_graph``, ``ingest`` → ``_invalidate``), и обычный Lock
    умер бы на повторном входе того же потока.

    Отдельного замка на документ здесь нет: вся операция с каталогом проходит под
    общим, поэтому второй параллельный импорт того же файла встаёт в ожидание и
    повторно проверяет дедуп уже на записанном состоянии — гонки «проверил до,
    написал после» не остаётся. В production-ветке так нельзя (замок держался бы
    сквозь Neo4j и Elasticsearch), там сериализация на документ своя.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._documents: dict[str, DocumentRequest] = {}
        self._findings: dict[str, Finding] = {
            finding.id: finding for finding in self._seed_findings()
        }
        self._hypotheses: dict[UUID, HypothesisSignal] = {}
        self._graph = self._seed_graph()
        self._version_chains: dict[str, list[str]] = {}
        self._communities: list[str] | None = None
        # Поколение графа: растёт на каждую запись и входит в ключ кэша сводок,
        # поэтому устаревший профиль не может пережить изменившийся корпус.
        self._graph_epoch = 0
        self._brief_cache = CommunityBriefCache()

    # ── Инварианты версии графа ─────────────────────────────────────────────

    @_synchronized
    def _invalidate(self) -> None:
        # Единая точка сброса производных кэшей: сюда приводят все записи каталога
        # (ingest, index_document, supersede_finding, register_findings, откат),
        # поэтому ни сводки сообществ, ни список кластеров не переживают мутацию.
        # Кэш structured output снимается и здесь: в memory-контуре промпты
        # собираются из тех же находок, и закэшированный ответ переживал бы импорт,
        # показывая аналитику корпус до правки источника.
        self._communities = None
        self._graph_epoch += 1
        self._brief_cache.invalidate()
        invalidate_derived_llm_cache("каталог memory-контура")

    @_synchronized
    def _ensure_communities(self) -> list[str]:
        if self._communities is None:
            self._communities = detect_communities(self._graph.nodes, self._graph.edges)
        return self._communities

    # ── Ingestion ───────────────────────────────────────────────────────────

    @_synchronized
    def ingest(
        self,
        document: DocumentRequest,
        extraction: ExtractionResult,
        *,
        source_document_id: UUID | None = None,
        semantic_part_id: str | None = None,
        semantic_part_total: int | None = None,
    ) -> DocumentReceipt:
        checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        part_mode = source_document_id is not None
        if part_mode != (semantic_part_id is not None and semantic_part_total is not None):
            raise ValueError("Для частичного импорта нужны все идентификаторы semantic part")
        document_id = source_document_id or stable_uuid(checksum)
        if part_mode and semantic_part_id in self.semantic_parts_completed(
            document_id, semantic_part_total or 0
        ):
            return DocumentReceipt(
                document_id=document_id,
                checksum=checksum,
                status="duplicate",
                extracted_claims=0,
            )
        document_key = f"{document_id}:{semantic_part_id}" if part_mode else checksum
        if document_key in self._documents:
            return DocumentReceipt(
                document_id=document_id,
                checksum=checksum,
                status="duplicate",
                extracted_claims=0,
            )

        # Снимок до записи: недоизвлечённый документ не должен оставлять половину
        # узлов, рёбер и находок — иначе «частичный импорт» выглядит как корпус.
        state = self.snapshot_state()
        self._documents[document_key] = document
        try:
            self._add_extraction(
                document, document_id, extraction, semantic_part_id=semantic_part_id
            )
            if part_mode:
                self._mark_semantic_part(
                    document_id, semantic_part_id or "", semantic_part_total or 0
                )
        except Exception:
            self.restore_state(state)
            raise
        self._invalidate()
        return DocumentReceipt(
            document_id=document_id,
            checksum=checksum,
            status="created",
            extracted_claims=len(extraction.claims),
        )

    @_synchronized
    def semantic_parts_completed(self, source_document_id: UUID, part_total: int) -> set[str]:
        node = next(
            (item for item in self._graph.nodes if item.id == f"document-{source_document_id}"),
            None,
        )
        if node is None:
            return set()
        stored_total = node.metadata.get("semantic_part_total")
        if stored_total is not None and stored_total != part_total:
            raise ValueError("Состав semantic parts документа изменился между запусками")
        parts_value = node.metadata.get("semantic_parts_completed", "[]")
        if not isinstance(parts_value, str):
            return set()
        parts = json.loads(parts_value)
        return {str(item) for item in parts} if isinstance(parts, list) else set()

    @_synchronized
    def _mark_semantic_part(self, document_id: UUID, part_id: str, part_total: int) -> None:
        node_id = f"document-{document_id}"
        for index, node in enumerate(self._graph.nodes):
            if node.id != node_id:
                continue
            completed = self.semantic_parts_completed(document_id, part_total)
            completed.add(part_id)
            metadata = {
                **node.metadata,
                "semantic_parts_completed": json.dumps(sorted(completed)),
                "semantic_part_total": part_total,
                "semantic_extracted": len(completed) == part_total,
            }
            self._graph.nodes[index] = node.model_copy(update={"metadata": metadata})
            return

    @_synchronized
    def snapshot_state(self) -> KnowledgeState:
        """Снимок каталога до записи — опора для отката production-импорта."""
        return KnowledgeState(
            documents=dict(self._documents),
            findings=dict(self._findings),
            nodes=list(self._graph.nodes),
            edges=list(self._graph.edges),
            chains={key: list(value) for key, value in self._version_chains.items()},
        )

    @_synchronized
    def restore_state(self, state: KnowledgeState) -> None:
        """Возвращает каталог к снимку: половину импорта держать в контуре нельзя."""
        self._documents = dict(state.documents)
        self._findings = dict(state.findings)
        self._graph.nodes = list(state.nodes)
        self._graph.edges = list(state.edges)
        self._version_chains = {key: list(value) for key, value in state.chains.items()}
        self._invalidate()

    @_synchronized
    def register_findings(self, findings: Iterable[Finding]) -> int:
        """Принимает находки, обнаруженные вне локального каталога.

        Production-бэкенд находит структурные чанки в Elasticsearch и обязан
        дать им ту же версию по ``supersede_finding``, что и семантическим
        тезисам: без этого замена утверждения по идентификатору чанка
        закончится ошибкой «finding not found», а API ответит 409 на корректный
        запрос.
        """
        added = 0
        for finding in findings:
            if finding.id in self._findings:
                continue
            self._findings[finding.id] = finding
            added += 1
        if added:
            self._invalidate()
        return added

    def _add_extraction(
        self,
        document: DocumentRequest,
        document_id: UUID,
        extraction: ExtractionResult,
        *,
        semantic_part_id: str | None = None,
    ) -> None:
        existing_nodes = {node.id for node in self._graph.nodes}
        document_node_id = f"document-{document_id}"
        if document_node_id not in existing_nodes:
            self._graph.nodes.append(
                GraphNode(
                    id=document_node_id,
                    label=document.title,
                    type=NodeType.PUBLICATION,
                    data_class=document.data_class,
                    metadata=_document_node_metadata(document),
                )
            )
            existing_nodes.add(document_node_id)

        entity_ids: dict[str, str] = {}
        for entity in extraction.entities:
            entity_id = f"entity-{stable_uuid(entity.canonical_name.lower())}"
            entity_ids[entity.name.lower()] = entity_id
            entity_ids[entity.canonical_name.lower()] = entity_id
            if entity_id not in existing_nodes:
                self._graph.nodes.append(
                    GraphNode(
                        id=entity_id,
                        label=entity.canonical_name,
                        type=entity.type,
                        data_class=document.data_class,
                        metadata={"aliases": ", ".join(entity.aliases)},
                    )
                )
                existing_nodes.add(entity_id)

        for index, claim in enumerate(extraction.claims):
            claim_key = (
                f"{document_id}:{semantic_part_id}:{index}"
                if semantic_part_id is not None
                else f"{document_id}:{index}:{claim.statement}"
            )
            claim_id = f"claim-{stable_uuid(claim_key)}"
            subject_id = entity_ids.get(claim.subject.lower())
            object_id = entity_ids.get(claim.object.lower())
            if not subject_id:
                subject_id = f"entity-{stable_uuid(claim.subject.lower())}"
                if subject_id not in existing_nodes:
                    self._graph.nodes.append(
                        GraphNode(
                            id=subject_id,
                            label=claim.subject,
                            type=NodeType.MATERIAL,
                            data_class=document.data_class,
                        )
                    )
                    existing_nodes.add(subject_id)
            if not object_id:
                # Симметрично subject: без фолбэка предикатное ребро молча не
                # записывалось, и утверждение оседало в графе без связи.
                object_id = f"entity-{stable_uuid(claim.object.lower())}"
                if object_id not in existing_nodes:
                    self._graph.nodes.append(
                        GraphNode(
                            id=object_id,
                            label=claim.object,
                            type=NodeType.MATERIAL,
                            data_class=document.data_class,
                        )
                    )
                    existing_nodes.add(object_id)
            self._graph.nodes.append(
                GraphNode(
                    id=claim_id,
                    label=claim.statement,
                    type=NodeType.CLAIM,
                    confidence=claim.confidence,
                    data_class=document.data_class,
                    metadata={
                        "knowledge_status": "extracted",
                        "observations": json.dumps(
                            [item.model_dump(mode="json") for item in claim.observations],
                            ensure_ascii=False,
                        ),
                    },
                )
            )
            self._graph.edges.extend(
                [
                    GraphEdge(
                        id=f"{claim_id}-asserts",
                        source=subject_id,
                        target=claim_id,
                        relation="ASSERTS",
                        confidence=claim.confidence,
                        data_class=document.data_class,
                    ),
                    GraphEdge(
                        id=f"{claim_id}-evidence",
                        source=claim_id,
                        target=document_node_id,
                        relation="SUPPORTED_BY",
                        confidence=claim.confidence,
                        data_class=document.data_class,
                    ),
                ]
            )
            self._graph.edges.append(
                GraphEdge(
                    id=f"{claim_id}-object",
                    source=claim_id,
                    target=object_id,
                    relation=claim.predicate,
                    confidence=claim.confidence,
                    data_class=document.data_class,
                )
            )
            self._findings[f"finding-{claim_id}"] = Finding(
                id=f"finding-{claim_id}",
                statement=claim.statement,
                confidence=claim.confidence,
                evidence=[self._locate_evidence(document, document_id, claim.evidence_quote)],
                status="hypothesis",
                observations=claim.observations,
                subject=claim.subject,
                predicate=claim.predicate,
                # Год и территория источника — условия применимости тезиса: без них
                # ограничения плана «с 2015 года» и «Россия» нечем исполнять.
                scope=document_scope(document),
                data_class=document.data_class,
            )

    @staticmethod
    def _locate_evidence(
        document: DocumentRequest,
        document_id: UUID,
        quote: str,
    ) -> EvidenceLocator:
        """Локатор цитаты: фрагмент, страница/лист и смещения символов.

        Смещения заполняются только когда цитата реально найдена в тексте
        фрагмента. Модель возвращает пересказанную цитату чаще, чем хотелось бы, и
        «char_start=0» на такое находке выглядел бы проверяемым адресом, которым
        он не является.
        """
        fragment = next(
            (item for item in document.fragments if quote and quote in item.text),
            None,
        )
        if fragment is None:
            fragment = next(
                (
                    item
                    for item in document.fragments
                    if quote and quote.casefold() in item.text.casefold()
                ),
                None,
            )
        if fragment is None:
            anchor = document.fragments[0] if document.fragments else None
            return EvidenceLocator(
                document_id=document_id,
                source_title=document.title,
                page=anchor.page if anchor else 1,
                sheet=anchor.sheet if anchor else None,
                cell_range=anchor.cell_range if anchor else None,
                quote=quote or document.title,
            )
        offsets = quote_offsets(fragment.text, quote)
        return EvidenceLocator(
            document_id=document_id,
            source_title=document.title,
            page=fragment.page,
            sheet=fragment.sheet,
            cell_range=fragment.cell_range,
            char_start=(fragment.source_char_start + offsets[0]) if offsets else None,
            char_end=(fragment.source_char_start + offsets[1]) if offsets else None,
            quote=quote or document.title,
        )

    # ── Retrieval ───────────────────────────────────────────────────────────

    @_synchronized
    def retrieve(
        self,
        plan: QueryPlan,
        retrieval_plan: RetrievalPlan,
        allowed_data_classes: set[DataClass] | None = None,
        *,
        abort: AbortCheck | None = None,
    ) -> RetrievalContext:
        """Исполняет план retrieval теми же хелперами, что и production-контур.

        Ранее обход графа подменялся фиксированным набором id
        (``{"water", "sulfates", ...}``), вместо сообществ возвращались две
        захардкоженные строки, а ``countries``/``year_from``/``year_to``/
        ``semantic_query``/``use_global_context`` не читались нигде. Теперь окно
        кандидатов шире потолка выдачи, ограничения исполняются
        ``finalize_retrieval``, а неприменимое ограничение называется вслух.

        ``abort`` здесь не декорация: тот же предикат, что и в neo4j-ветке, снимает
        с прогона обход и сводки сообществ, и оба контура рапортуют пропущенные фазы
        одними словами.
        """
        notes: list[str] = []
        if (retrieval_plan.semantic_query or "").strip() and (
            retrieval_plan.semantic_query.strip() != retrieval_plan.lexical_query.strip()
        ):
            notes.append(
                "Семантическая ветка в memory-контуре недоступна (эмбеддингов нет): "
                "использован лексический запрос, отдельного векторного поиска не было."
            )
        if not retrieval_plan.use_local_graph:
            graph = GraphSnapshot(nodes=[], edges=[], communities=[])
        elif is_aborted(abort):
            notes.append(abort_note("обход графа"))
            graph = GraphSnapshot(nodes=[], edges=[], communities=[])
        else:
            graph, graph_notes = self._neighbourhood(retrieval_plan, allowed_data_classes)
            notes.extend(graph_notes)
        # Копия находок здесь не нужна: `finalize_retrieval` копирует ровно то, что
        # ушло в выдачу, — двойное глубокое копирование кандидатов на каждое
        # действие только удваивало цену ранжирования.
        candidates = self._rank_view(
            retrieval_plan.lexical_query,
            candidate_window(plan, RETRIEVAL_TOP_K),
            "hybrid",
            allowed_data_classes,
        )
        wants_global = retrieval_plan.use_community_context or retrieval_plan.use_global_context
        briefs: list[str] = []
        if wants_global and is_aborted(abort):
            notes.append(abort_note("сводки сообществ"))
        elif wants_global:
            briefs = self._community_briefs(
                allowed_data_classes,
                candidates,
                query_tokens(retrieval_plan.lexical_query),
            )
        if wants_global:
            notes.extend(global_context_notes(retrieval_plan.use_global_context, briefs))
        return finalize_retrieval(
            candidates,
            plan,
            graph=graph,
            community_summaries=briefs,
            vectors_used=False,
            notes=notes,
        )

    def _community_briefs(
        self,
        allowed_data_classes: set[DataClass] | None,
        findings: Sequence[Finding],
        tokens: Iterable[str],
    ) -> list[str]:
        """Сводки сообществ с кэшем профилей по поколению графа и срезу доступа.

        Сводки — часть ответа: граф под них срезается по ACL до построения, иначе
        restricted-метка узла уходит в промпт модели в обход политик. Срез входит в
        ключ кэша, поэтому профиль, собранный для одного набора классов данных, не
        может быть выдан другому.
        """
        # Сообщества считаются один раз на поколение графа, и здесь они только
        # читаются: ключ обязан описывать граф, а не строку запроса.
        communities = tuple(self._ensure_communities())
        key = brief_key((self._graph_epoch,), allowed_data_classes, communities)
        return cached_community_briefs(
            self._brief_cache,
            key,
            lambda: self.full_graph(allowed_data_classes),
            findings,
            tokens,
            DEFAULT_PROFILE_LIMIT,
        )

    def _neighbourhood(
        self,
        retrieval_plan: RetrievalPlan,
        allowed_data_classes: set[DataClass] | None,
    ) -> tuple[GraphSnapshot, list[str]]:
        if not retrieval_plan.use_local_graph:
            return GraphSnapshot(nodes=[], edges=[], communities=[]), []
        adjacency = self._adjacency(retrieval_plan.relation_types)
        anchors = self._anchors(retrieval_plan.entity_names)
        notes: list[str] = []
        allowed = set(allowed_data_classes) if allowed_data_classes is not None else None
        visited: dict[str, GraphNode] = {}
        node_by_id = {node.id: node for node in self._graph.nodes}
        depth_limit = max(min(retrieval_plan.max_hops, 4), 1)
        for anchor in anchors:
            queue: deque[tuple[str, int]] = deque([(anchor.id, 0)])
            seen = {anchor.id}
            while queue:
                node_id, depth = queue.popleft()
                node = node_by_id.get(node_id)
                if node is None or (allowed is not None and node.data_class not in allowed):
                    continue
                visited[node.id] = node
                if depth >= depth_limit:
                    continue
                for neighbor_id in adjacency.get(node_id, ()):
                    if neighbor_id not in seen:
                        seen.add(neighbor_id)
                        queue.append((neighbor_id, depth + 1))
        visible = set(visited)
        edges = [
            edge
            for edge in self._graph.edges
            if edge.source in visible
            and edge.target in visible
            and (allowed is None or edge.data_class in allowed)
        ]
        return (
            GraphSnapshot(
                nodes=list(visited.values()),
                edges=edges,
                communities=self._ensure_communities(),
            ),
            notes,
        )

    def _adjacency(self, relation_types: list[str]) -> dict[str, list[str]]:
        allowed = set(relation_types)
        adjacency: dict[str, list[str]] = {}
        for edge in self._graph.edges:
            if allowed and edge.relation.upper() not in allowed:
                continue
            adjacency.setdefault(edge.source, []).append(edge.target)
            adjacency.setdefault(edge.target, []).append(edge.source)
        return adjacency

    def _anchors(self, entity_names: list[str]) -> list[GraphNode]:
        """Якоря обхода: двустороннее совпадение имени с меткой узла."""
        needles = [name.strip().lower() for name in entity_names if name.strip()]
        anchors: list[GraphNode] = []
        seen: set[str] = set()
        for node in self._graph.nodes:
            if not needles or node.id in seen:
                continue
            label = node.label.lower()
            if any(needle in label or label in needle for needle in needles):
                anchors.append(node)
                seen.add(node.id)
        return anchors[:MAX_ANCHORS]

    @_synchronized
    def rank_findings(
        self,
        query: str,
        top_k: int = RETRIEVAL_TOP_K,
        mode: str = "hybrid",
        allowed_data_classes: set[DataClass] | None = None,
        semantic_query: str | None = None,
    ) -> list[Finding]:
        """Публичная выдача каталога: наружу уходят копии, а не объекты хранилища.

        Вызывающий вне каталога (оценка качества, обработчик API) вправе менять
        результат, не трогая корпус. ``retrieve`` берёт ``_rank_view`` и копированием
        на границе занимается ``finalize_retrieval`` — один раз на итоговую выдачу,
        а не дважды на окно кандидатов.
        """
        return [
            finding.model_copy(deep=True)
            for finding in self._rank_view(query, top_k, mode, allowed_data_classes, semantic_query)
        ]

    def _rank_view(
        self,
        query: str,
        top_k: int = RETRIEVAL_TOP_K,
        mode: str = "hybrid",
        allowed_data_classes: set[DataClass] | None = None,
        semantic_query: str | None = None,
    ) -> list[Finding]:
        """Ранжирует findings и один раз прогоняет их через общий re-rank.

        Возвращает общие с каталогом модели — только для внутреннего контура, где
        находки читают (``finalize_retrieval``, сводки сообществ).

        * ``lexical``  — совпадение токенов в statement (baseline).
        * ``hybrid``   — лексика + evidence-контекст + источник + subject.
        * ``semantic`` — в этом бэкенде равна lexical: векторов нет, и мы
          заявляем об этом явно вместо имитации «семантического» поиска.

        ``semantic_query`` здесь не используется по существу: эмбеддингов в
        memory-контуре нет, и честный сигнал об этом отдаёт вызывающий ``retrieve``
        через ``degradation_reasons``, а не тихая подмена лексическим запросом.
        """
        del semantic_query
        tokens = query_tokens(query)
        active = [
            finding
            for finding in self._findings.values()
            if finding.superseded_by is None
            and (allowed_data_classes is None or finding.data_class in allowed_data_classes)
        ]
        scored = [(self._score(finding, tokens, mode, query), finding) for finding in active]
        scored.sort(key=lambda item: item[1].id)
        scored.sort(key=lambda item: item[0], reverse=True)
        # Совпадения нет — совпадения нет: нулевой score не делает тезис
        # «худшим из найденного», иначе запрос мимо корпуса возвращает
        # произвольные findings, а tool-исполнитель докладывает их как успех.
        pool = [finding for score, finding in scored if score[0] > 0]
        fusion = {
            finding.id: 1.0 / (_RRF_K + rank) for rank, finding in enumerate(pool, start=1)
        }
        self._ensure_communities()
        reranked = rerank_findings(
            query,
            pool,
            top_k=top_k,
            fusion=fusion,
            communities=finding_communities(self._graph.nodes, pool),
        )
        if reranked.degraded:
            logger.warning("Re-rank не содержательных сигналов: выдача могла остаться случайной.")
        return list(reranked.findings)

    @staticmethod
    def _score(
        finding: Finding, tokens: set[str], mode: str, query: str
    ) -> tuple[float, int, float]:
        statement_hits = sum(token in finding.statement.lower() for token in tokens)
        if mode == "lexical" or mode == "semantic":
            return (float(statement_hits), statement_hits, finding.confidence)
        evidence_text = " ".join(item.quote for item in finding.evidence).lower()
        evidence_hits = sum(token in evidence_text for token in tokens) if evidence_text else 0
        source_title = finding.evidence[0].source_title.lower() if finding.evidence else ""
        source_hits = sum(token in source_title for token in tokens) if source_title else 0
        subject_hits = 1 if finding.subject and finding.subject.lower() in query.lower() else 0
        total = statement_hits + 0.3 * evidence_hits + 0.5 * source_hits + 0.5 * subject_hits
        return (total, statement_hits, finding.confidence)

    @_synchronized
    def document_count(self) -> int:
        return len(
            {
                str(evidence.document_id)
                for finding in self._findings.values()
                for evidence in finding.evidence
            }
        )

    @_synchronized
    def index_document(
        self, document: DocumentRequest, source_path: str
    ) -> StructuralDocumentReceipt:
        """Структурный импорт: чанки видимы как находки и как узлы графа.

        ``source_path`` здесь не кладётся в ``scope``: из scope строятся измерения
        research space и совместимость условий конфликтов, а путь к файлу — не
        условие применимости числа, два разных документа дали бы «несовместимые»
        тезисы и противоречие между источниками перестало бы находиться.
        """
        checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        document_id = stable_uuid(checksum)
        if checksum in self._documents:
            return StructuralDocumentReceipt(
                document_id=document_id,
                checksum=checksum,
                status="duplicate",
                chunks=0,
            )
        state = self.snapshot_state()
        try:
            pieces = chunk_document(document, document_id)
            if not pieces:
                raise NoChunksError(
                    f"фрагменты короче порога чанкинга ({CHUNK_MIN_CHARS} символов): "
                    "в индекс не лёг ни один кусок текста"
                )
            document_node_id = f"document-{document_id}"
            known_nodes = {node.id for node in self._graph.nodes}
            if document_node_id not in known_nodes:
                self._graph.nodes.append(
                    GraphNode(
                        id=document_node_id,
                        label=document.title,
                        type=NodeType.PUBLICATION,
                        data_class=document.data_class,
                        metadata=_document_node_metadata(document),
                    )
                )
            self._graph.nodes.extend(_chunk_state_nodes(pieces, document))
            self._graph.edges.extend(_chunk_state_edges(pieces, document_node_id, document))
            for piece in pieces:
                self._findings[piece.id] = chunk_finding(document, document_id, piece)
            self._documents[checksum] = document
        except Exception:
            self.restore_state(state)
            raise
        self._invalidate()
        return StructuralDocumentReceipt(
            document_id=document_id,
            checksum=checksum,
            status="created",
            chunks=len(pieces),
        )

    @_synchronized
    def corpus_stats(self) -> CorpusStats:
        chunks = sum(finding.id.startswith("chunk-") for finding in self._findings.values())
        return CorpusStats(
            documents=self.document_count(),
            chunks=chunks,
            claims=sum(node.type == NodeType.CLAIM for node in self._graph.nodes),
            entities=sum(
                node.type not in {NodeType.CLAIM, NodeType.PUBLICATION, NodeType.CHUNK}
                for node in self._graph.nodes
            ),
            semantic_documents=len(self._documents),
            vectors_indexed=0,
        )

    @_synchronized
    def full_graph(self, allowed_data_classes: set[DataClass] | None = None) -> GraphSnapshot:
        """Снимок графа для чтения: новые списки-контейнеры, общие модели элементов.

        Прежняя политика копировала вглубь каждый узел и каждое ребро — на
        синтетическом графе 5 000 узлов / 15 000 рёбер это ~115 мс из ~169 мс одного
        retrieval-действия с глобальным контекстом (замер локального контура,
        memory-бэкенд), при том что снимок только читают (профили сообществ, обход,
        каталог находок). Правка возвращённого списка по-прежнему не достаёт до
        каталога: список всегда новый, а ``AccessPolicyEngine.filter_graph``
        собирает собственные списки из входа.

        Кто реально пишет в модели графа — ``detect_communities``, проставляющий
        ``node.metadata["community"]``. В memory-контуре он вызывается исключительно
        по собственному списку узлов (``_ensure_communities``), то есть до построения
        снимка, поэтому общий с каталогом узел неотличим от копии по метке
        сообщества: на этом стоит тест ``test_memory_backend_returns_computed_communities``.
        """
        # Сообщества проставляются в metadata узлов до построения снимка: иначе
        # копия уходит без community, а кэш-объект — с ним.
        communities = list(self._ensure_communities())
        if allowed_data_classes is None:
            return GraphSnapshot(
                nodes=list(self._graph.nodes),
                edges=list(self._graph.edges),
                communities=communities,
            )
        # Один движок политик на весь контур: он срезает и рёбра, у которых
        # конец попал под ограничение, иначе граф остаётся с висячими ссылками.
        return AccessPolicyEngine().filter_graph(
            GraphSnapshot(
                nodes=self._graph.nodes,
                edges=self._graph.edges,
                communities=communities,
            ),
            allowed_data_classes,
        )

    @_synchronized
    def all_findings(self, allowed_data_classes: set[DataClass] | None = None) -> list[Finding]:
        """Каталог находок для внешних потребителей — копия на границе.

        Список отдают API и оценка качества, и они вправе менять результат:
        правка ``statement`` в выданной модели не должна переписывать корпус.
        Внутренним путям (сверка диффа при импорте) копии не нужны — там читают
        только ``id`` и ``superseded_by``.
        """
        return [
            finding.model_copy(deep=True)
            for finding in self._findings.values()
            if finding.superseded_by is None
            and (allowed_data_classes is None or finding.data_class in allowed_data_classes)
        ]

    @_synchronized
    def semantic_findings(self) -> list[Finding]:
        return [
            finding.model_copy(deep=True)
            for finding in self._findings.values()
            if not finding.id.startswith("chunk-")
            and finding.superseded_by is None
        ]

    @_synchronized
    def semantic_graph(self) -> GraphSnapshot:
        return GraphSnapshot(
            nodes=list(self._graph.nodes),
            edges=list(self._graph.edges),
            communities=self._ensure_communities(),
        )

    @_synchronized
    def upsert_hypotheses(self, signals: Sequence[HypothesisSignal]) -> None:
        for signal in signals:
            self._hypotheses[signal.id] = signal.model_copy(deep=True)

    @_synchronized
    def hypotheses_window(
        self,
        *,
        limit: int,
        offset: int,
        allowed_data_classes: set[DataClass] | None = None,
    ) -> HypothesisWindow:
        return HypothesisWindow.from_signals(
            list(self._hypotheses.values()),
            allowed_data_classes=(
                set(DataClass)
                if allowed_data_classes is None
                else allowed_data_classes
            ),
            limit=limit,
            offset=offset,
        )

    @_synchronized
    def hypothesis_by_id(
        self, signal_id: UUID, allowed_data_classes: set[DataClass] | None = None
    ) -> HypothesisSignal | None:
        signal = self._hypotheses.get(signal_id)
        if signal is None or (
            allowed_data_classes is not None and signal.data_class not in allowed_data_classes
        ):
            return None
        return signal.model_copy(deep=True)

    @_synchronized
    def finding_catalog(self) -> Mapping[str, Finding]:
        """Каталог актуальных находок без копирования — для внутреннего диффа.

        Только для чтения: словарь отдаёт объекты хранилища, чтобы сверку «какие
        id появились после записи» не оплачивали глубоким копированием всего
        каталога на каждый документ загрузки.
        """
        return {
            key: value
            for key, value in self._findings.items()
            if value.superseded_by is None
        }

    @_synchronized
    def findings_window(
        self,
        *,
        limit: int = FINDINGS_WINDOW_DEFAULT,
        offset: int = 0,
        allowed_data_classes: set[DataClass] | None = None,
        status: str | None = None,
        subject: str | None = None,
    ) -> FindingWindow:
        """Окно каталога: отобрано под замком, скопирована ровно страница.

        Отбор идёт по тому же правилу, что у ``all_findings`` (заменённые версии и
        срез доступа), а порядок — по идентификатору: он не зависит от порядка
        вставки, поэтому вторая страница не «уплывает» после параллельного импорта.
        Глубокое копирование — только у возвращаемого окна: прежняя реализация
        копировала весь каталог на каждый вызов, и постраничный список обходился
        дороже, чем весь корпус целиком.

        ``status`` и ``subject`` отсекаются здесь, а не в индексе: в in-memory
        контуре индекса нет, и маршрут обязан получить то же самое окно, что и на
        рабочем контуре (там те же предикаты уходят в запрос Elasticsearch).
        """
        size, start = normalize_window(limit, offset)
        allowed = None if allowed_data_classes is None else set(allowed_data_classes)
        needle = subject.lower() if subject else None
        selected = sorted(
            (
                finding
                for finding in self._findings.values()
                if finding.superseded_by is None
                and (allowed is None or finding.data_class in allowed)
                and (status is None or finding.status == status)
                and (needle is None or needle in (finding.subject or "").lower())
            ),
            key=lambda finding: finding.id,
        )
        return FindingWindow(
            findings=[finding.model_copy(deep=True) for finding in selected[start : start + size]],
            total=len(selected),
            offset=start,
            limit=size,
            note=None,
        )

    def warmup(self) -> None:
        """In-memory-каталог собирается в конструкторе: прогревать нечего."""

    @_synchronized
    def set_finding_status(self, finding_id: str, status: str) -> Finding:
        """Меняет степень консенсуса находки, не порождая версию.

        ``supersede_finding`` переписывает сам тезис и для этого создаёт
        следующую версию с ребром SUPERSEDES. Подтверждённое противоречие источник
        не переписывает: у двух находок меняется только ``status`` — это тот
        переход, которого ждёт экспертный разбор пар.
        """
        current = self._findings.get(finding_id)
        if current is None:
            raise KeyError(f"Finding not found: {finding_id}")
        updated = current.model_copy(update={"status": status})
        self._findings[finding_id] = updated
        return updated.model_copy(deep=True)

    @_synchronized
    def supersede_finding(
        self,
        finding_id: str,
        new_statement: str,
        new_confidence: float,
        observations: list[NumericObservation] | None = None,
        reviewer_id: str | None = None,
        review_date: str | None = None,
        review_reason: str | None = None,
    ) -> Finding:
        """Создаёт новую версию утверждения и связывает старую через SUPERSEDES.

        Работает и по идентификатору структурного чанка (``chunk-<uuid>``): чанки
        регистрируются в каталоге при ``index_document`` и принимаются извне через
        ``register_findings``, поэтому контракт id единый для обоих бэкендов.

        Проверка «уже заменён» и запись идут под одним замком: второй эксперт,
        который открыл ту же карточку, получает ``ValueError`` (наружу — 409), а
        не вторую версию одного тезиса с двумя рёбрами SUPERSEDES.
        """
        old = self._findings.get(finding_id)
        if old is None:
            raise KeyError(f"Finding not found: {finding_id}")
        if old.superseded_by is not None:
            raise ValueError(f"Finding {finding_id} уже заменён на {old.superseded_by}")
        new_id = f"{finding_id}-v{old.version + 1}"
        self._findings[finding_id] = old.model_copy(update={"superseded_by": new_id})
        new_finding = Finding(
            id=new_id,
            statement=new_statement,
            confidence=new_confidence,
            evidence=old.evidence,
            status=old.status,
            observations=observations or old.observations,
            version=old.version + 1,
            subject=old.subject,
            predicate=old.predicate,
            scope=old.scope,
            data_class=old.data_class,
            reviewer_id=reviewer_id,
            review_date=review_date,
            review_reason=review_reason,
        )
        self._findings[new_id] = new_finding
        root_id = next(
            (key for key, chain in self._version_chains.items() if finding_id in chain),
            finding_id,
        )
        self._version_chains.setdefault(root_id, [root_id]).append(new_id)
        # Концы SUPERSEDES — реальные id узлов графа (см. supersede_node_id).
        existing_ids = {node.id for node in self._graph.nodes}
        new_node_id = supersede_node_id(new_finding.id, existing_ids)
        old_node_id = supersede_node_id(finding_id, existing_ids)
        if new_node_id not in existing_ids:
            self._graph.nodes.append(
                GraphNode(
                    id=new_node_id,
                    label=new_statement[:200],
                    type=NodeType.CLAIM,
                    confidence=new_confidence,
                    data_class=old.data_class,
                    metadata={
                        "knowledge_status": "superseding",
                        "version": new_finding.version,
                    },
                )
            )
        self._graph.edges.append(
            GraphEdge(
                id=f"supersedes-{new_id}",
                source=new_node_id,
                target=old_node_id,
                relation="SUPERSEDES",
                confidence=new_confidence,
                data_class=old.data_class,
            )
        )
        self._invalidate()
        return new_finding

    @_synchronized
    def claim_history(self, finding_id: str) -> list[Finding]:
        """Версии тезиса: из `_version_chains`, после перезапуска — из связей `superseded_by`.

        Словарь цепочек жил только в памяти процесса, и восстановленные из
        индексов находки теряли историю: аналитик видел одну версию и не мог
        узнать, что тезис уже правили. Замены возвращаются вместе с
        `superseded_by` и `version`, поэтому цепочка пересчитывается по ним.
        """
        chain = self._version_chains.get(finding_id) or self._chain_from_links(finding_id)
        return [self._findings[fid] for fid in chain if fid in self._findings]

    def _chain_from_links(self, finding_id: str) -> list[str]:
        replaced_by = {
            fid: item.superseded_by
            for fid, item in self._findings.items()
            if item.superseded_by
        }
        # Голова цепочки — версия, которую ещё не заменили; от неё идём назад
        # по «кого заменила эта версия» и собираем всех предков.
        head = finding_id
        walked: set[str] = set()
        while (
            (nxt := replaced_by.get(head)) is not None
            and nxt not in walked
            and nxt in self._findings
        ):
            walked.add(head)
            head = nxt
        predecessors: dict[str, list[str]] = {}
        for source, target in replaced_by.items():
            predecessors.setdefault(target, []).append(source)
        chain = [head]
        current = head
        seen = {head}
        while True:
            parents = [
                fid
                for fid in predecessors.get(current, [])
                if fid not in seen and fid in self._findings
            ]
            if not parents:
                break
            current = parents[0]
            seen.add(current)
            chain.append(current)
        known = [fid for fid in chain if fid in self._findings]
        return sorted(known, key=lambda fid: self._findings[fid].version)

    def close(self) -> None:
        return None

    @staticmethod
    def _evidence(slug: str, title: str, page: int, quote: str) -> EvidenceLocator:
        """Локатор seed-документа: смещений нет и быть не может.

        Демонстрационные «источники» не лежат в «Источниках информации», их текст
        некуда процитировать, поэтому ``char_start``/``char_end`` остаются пустыми,
        а выдача с такими находками помечается как демо (``origin=demo``).
        """
        return EvidenceLocator(
            document_id=stable_uuid(slug),
            source_title=title,
            page=page,
            quote=quote,
        )

    @staticmethod
    def _point_obs(
        property_name: str,
        value: float,
        unit: str,
        raw_text: str,
    ) -> NumericObservation:
        return NumericObservation(
            property_name=property_name,
            operator="eq",
            value=value,
            unit=unit,
            normalized_value=value,
            normalized_unit=unit,
            raw_text=raw_text,
        )

    @staticmethod
    def _range_obs(
        property_name: str,
        min_value: float,
        max_value: float,
        unit: str,
        raw_text: str,
    ) -> NumericObservation:
        return NumericObservation(
            property_name=property_name,
            operator="between",
            min_value=min_value,
            max_value=max_value,
            unit=unit,
            normalized_min=min_value,
            normalized_max=max_value,
            normalized_unit=unit,
            raw_text=raw_text,
        )

    def _seed_findings(self) -> list[Finding]:
        """Синтетический корпус memory-контура для локального запуска и тестов.

        Каждой находке проставляется ``origin=demo``: рабочий контур обязан
        отличать примеры от пользовательских документов и не выдавать их за
        доказательства из рабочего пространства.
        """
        corpus = [
            Finding(
                id="finding-ro",
                statement=(
                    "Общий шаблон ответа сокращает среднее время обработки запроса до 18–22 минут."
                ),
                confidence=0.92,
                evidence=[
                    self._evidence(
                        "request-time-review",
                        "Синтетический пример: время обработки запросов",
                        14,
                        "После введения общего шаблона среднее время обработки запроса "
                        "составило 18–22 минуты.",
                    )
                ],
                subject="шаблон обработки запроса",
                predicate="REDUCES_PROCESSING_TIME",
                scope={"group": "все запросы"},
                observations=[
                    self._range_obs("processing_time", 18, 22, "min", "18–22 минуты"),
                ],
            ),
            Finding(
                id="finding-ro-pilot",
                statement=(
                    "В пилотной группе запросы обрабатывали за 24–29 минут при среднем "
                    "времени 31 минута."
                ),
                confidence=0.70,
                evidence=[
                    self._evidence(
                        "request-time-pilot",
                        "Синтетический пример: пилотная группа",
                        5,
                        "Среднее время обработки запроса составило 24–29 минут.",
                    )
                ],
                subject="обработка запросов",
                predicate="HAS_PROCESSING_TIME",
                scope={"group": "группа сравнения"},
                observations=[
                    self._range_obs("processing_time", 24, 29, "min", "24–29 минут")
                ],
            ),
            Finding(
                id="finding-ion",
                statement=(
                    "Автоматическая проверка находит 82–91% записей с пропущенными полями."
                ),
                confidence=0.84,
                evidence=[
                    self._evidence(
                        "missing-fields-review",
                        "Синтетический пример: проверка записей",
                        7,
                        "Проверка выявила пропуски в 82–91% записей.",
                    )
                ],
                subject="проверка заполнения записей",
                predicate="FINDS_MISSING_FIELDS",
                scope={},
                observations=[self._range_obs("missing_records", 82, 91, "%", "82–91%")],
            ),
            Finding(
                id="finding-thermal",
                statement=(
                    "В часы пик очередь задач вырастает в 3–5 раз относительно среднего потока."
                ),
                confidence=0.78,
                status="disputed",
                data_class=DataClass.RESTRICTED,
                evidence=[
                    self._evidence(
                        "peak-load-review",
                        "Синтетический пример: нагрузка на очередь",
                        22,
                        "В часы пик число ожидающих задач было в 3–5 раз выше среднего.",
                    )
                ],
                subject="очередь задач",
                predicate="HAS_PEAK_LOAD",
                scope={},
                observations=[self._range_obs("queue_growth", 3, 5, "ratio", "3–5 раз")],
            ),
            Finding(
                id="finding-climate",
                statement=(
                    "Для дополнительной проверки одной заявки требуется не менее 8 минут."
                ),
                confidence=0.81,
                evidence=[
                    self._evidence(
                        "high-load-review",
                        "Синтетический пример: обработка при высокой нагрузке",
                        9,
                        "При загрузке выше 80% дополнительная проверка занимала более 8 минут.",
                    )
                ],
                subject="дополнительная проверка заявки",
                predicate="HAS_MINIMUM_PROCESSING_TIME",
                scope={"load": "high"},
                observations=[self._point_obs("check_duration", 8, "min", "не менее 8 минут")],
            ),
        ]
        return [
            finding.model_copy(update={"scope": {**finding.scope, SCOPE_ORIGIN: DEMO_ORIGIN}})
            for finding in corpus
        ]

    @staticmethod
    def _seed_graph() -> GraphSnapshot:
        nodes = [
            GraphNode(id="water", label="Обработка запросов", type=NodeType.PROCESS),
            GraphNode(id="sulfates", label="Входящий поток", type=NodeType.CONDITION),
            GraphNode(id="chlorides", label="Очередь задач", type=NodeType.CONDITION),
            GraphNode(id="reverse-osmosis", label="Общий шаблон ответа", type=NodeType.PROCESS),
            GraphNode(id="ion-exchange", label="Автоматическая проверка", type=NodeType.PROCESS),
            GraphNode(id="evaporation", label="Обработка в часы пик", type=NodeType.PROCESS),
            GraphNode(
                id="claim-ro",
                label="18–22 минуты на запрос",
                type=NodeType.CLAIM,
                confidence=0.92,
            ),
            GraphNode(
                id="claim-energy",
                label="Очередь выросла в 3–5 раз",
                type=NodeType.CLAIM,
                confidence=0.78,
                data_class=DataClass.RESTRICTED,
            ),
            GraphNode(id="expert", label="Экспертная группа", type=NodeType.EXPERT),
        ]
        edges = [
            GraphEdge(id="e1", source="water", target="sulfates", relation="HAS_INPUT"),
            GraphEdge(id="e2", source="water", target="chlorides", relation="HAS_QUEUE"),
            GraphEdge(id="e3", source="water", target="reverse-osmosis", relation="IMPROVED_BY"),
            GraphEdge(id="e4", source="water", target="ion-exchange", relation="CHECKED_BY"),
            GraphEdge(id="e5", source="reverse-osmosis", target="claim-ro", relation="PRODUCES"),
            GraphEdge(
                id="e6",
                source="evaporation",
                target="claim-energy",
                relation="HAS_PEAK_LOAD",
                data_class=DataClass.RESTRICTED,
            ),
            GraphEdge(id="e7", source="expert", target="reverse-osmosis", relation="EXPERT_IN"),
        ]
        return GraphSnapshot(nodes=nodes, edges=edges)
