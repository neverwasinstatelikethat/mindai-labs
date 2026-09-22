from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
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
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import (
    EvidenceLocator,
    NumericObservation,
    QueryPlan,
)
from scientific_tangle.services.communities import (
    DEFAULT_PROFILE_LIMIT,
    community_briefs,
    detect_communities,
)
from scientific_tangle.services.governance import AccessPolicyEngine
from scientific_tangle.services.reranking import query_tokens, rerank_findings
from scientific_tangle.services.retrieval_semantics import (
    DEMO_ORIGIN,
    RETRIEVAL_TOP_K,
    SCOPE_GEOGRAPHY,
    SCOPE_ORIGIN,
    SCOPE_YEAR,
    apply_numeric_filters,
    candidate_window,
    cap_notes,
    dedupe_findings,
    demo_notes,
    enforce_scope,
    global_context_notes,
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

# Окно и шаг структурного чанкинга — единые для обоих бэкендов: идентификатор
# чанка вычисляется от смещения внутри фрагмента, поэтому разные константы
# дали бы два несводимых контракта id на один и тот же документ.
CHUNK_WINDOW = 4000
CHUNK_STRIDE = 3700
CHUNK_QUOTE_LIMIT = 1200
CHUNK_STATEMENT_LIMIT = 4000
CHUNK_MIN_CHARS = 40


def stable_uuid(value: str) -> UUID:
    return uuid5(NAMESPACE, value)


__all__ = [
    "CHUNK_STRIDE",
    "CHUNK_WINDOW",
    "ChunkPiece",
    "InMemoryKnowledgeBase",
    "KnowledgeBase",
    "RetrievalContext",
    "chunk_document",
    "chunk_finding",
    "document_scope",
    "finalize_retrieval",
    "finding_communities",
    "quote_offsets",
    "stable_uuid",
]


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


@runtime_checkable
class KnowledgeBase(Protocol):
    """Полный контракт, который реально используют API и CLI.

    Методы, необходимые обработчикам, объявлены здесь: раньше их вызывали через
    ``hasattr(knowledge, ...)``, и бэкенд без метода молча возвращал ``200 []``.
    """

    def ingest(
        self, document: DocumentRequest, extraction: ExtractionResult
    ) -> DocumentReceipt: ...

    def retrieve(
        self,
        plan: QueryPlan,
        retrieval_plan: RetrievalPlan,
        allowed_data_classes: set[DataClass] | None = None,
    ) -> RetrievalContext: ...

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
    """In-memory KG-adapter с тем же контрактом, что и production репозитории."""

    def __init__(self) -> None:
        self._documents: dict[str, DocumentRequest] = {}
        self._findings: dict[str, Finding] = {
            finding.id: finding for finding in self._seed_findings()
        }
        self._graph = self._seed_graph()
        self._version_chains: dict[str, list[str]] = {}
        self._communities: list[str] | None = None

    # ── Инварианты версии графа ─────────────────────────────────────────────

    def _invalidate(self) -> None:
        self._communities = None

    def _ensure_communities(self) -> list[str]:
        if self._communities is None:
            self._communities = detect_communities(self._graph.nodes, self._graph.edges)
        return self._communities

    # ── Ingestion ───────────────────────────────────────────────────────────

    def ingest(self, document: DocumentRequest, extraction: ExtractionResult) -> DocumentReceipt:
        checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        document_id = stable_uuid(checksum)
        if checksum in self._documents:
            return DocumentReceipt(
                document_id=document_id,
                checksum=checksum,
                status="duplicate",
                extracted_claims=0,
            )

        # Снимок до записи: недоизвлечённый документ не должен оставлять половину
        # узлов, рёбер и находок — иначе «частичный импорт» выглядит как корпус.
        state = self.snapshot_state()
        self._documents[checksum] = document
        try:
            self._add_extraction(document, document_id, extraction)
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

    def snapshot_state(self) -> KnowledgeState:
        """Снимок каталога до записи — опора для отката production-импорта."""
        return KnowledgeState(
            documents=dict(self._documents),
            findings=dict(self._findings),
            nodes=list(self._graph.nodes),
            edges=list(self._graph.edges),
            chains={key: list(value) for key, value in self._version_chains.items()},
        )

    def restore_state(self, state: KnowledgeState) -> None:
        """Возвращает каталог к снимку: половину импорта держать в контуре нельзя."""
        self._documents = dict(state.documents)
        self._findings = dict(state.findings)
        self._graph.nodes = list(state.nodes)
        self._graph.edges = list(state.edges)
        self._version_chains = {key: list(value) for key, value in state.chains.items()}
        self._invalidate()

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
            claim_id = f"claim-{stable_uuid(f'{document_id}:{index}:{claim.statement}')}"
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
            char_start=offsets[0] if offsets else None,
            char_end=offsets[1] if offsets else None,
            quote=quote or document.title,
        )

    # ── Retrieval ───────────────────────────────────────────────────────────

    def retrieve(
        self,
        plan: QueryPlan,
        retrieval_plan: RetrievalPlan,
        allowed_data_classes: set[DataClass] | None = None,
    ) -> RetrievalContext:
        """Исполняет план retrieval теми же хелперами, что и production-контур.

        Ранее обход графа подменялся фиксированным набором id
        (``{"water", "sulfates", ...}``), вместо сообществ возвращались две
        захардкоженные строки, а ``countries``/``year_from``/``year_to``/
        ``semantic_query``/``use_global_context`` не читались нигде. Теперь окно
        кандидатов шире потолка выдачи, ограничения исполняются
        ``finalize_retrieval``, а неприменимое ограничение называется вслух.
        """
        notes: list[str] = []
        if (retrieval_plan.semantic_query or "").strip() and (
            retrieval_plan.semantic_query.strip() != retrieval_plan.lexical_query.strip()
        ):
            notes.append(
                "Семантическая ветка в memory-контуре недоступна (эмбеддингов нет): "
                "использован лексический запрос, отдельного векторного поиска не было."
            )
        graph, graph_notes = self._neighbourhood(retrieval_plan, allowed_data_classes)
        notes.extend(graph_notes)
        candidates = self.rank_findings(
            retrieval_plan.lexical_query,
            candidate_window(plan, RETRIEVAL_TOP_K),
            "hybrid",
            allowed_data_classes,
        )
        wants_global = retrieval_plan.use_community_context or retrieval_plan.use_global_context
        briefs: list[str] = []
        if wants_global:
            # Сводки — часть ответа: граф под них срезается по ACL до построения,
            # иначе restricted-метка узла уходит в промпт модели в обход политик.
            snapshot = self.full_graph(allowed_data_classes)
            briefs = community_briefs(
                snapshot.nodes,
                snapshot.edges,
                candidates,
                query_tokens(retrieval_plan.lexical_query),
                DEFAULT_PROFILE_LIMIT,
            )
            notes.extend(global_context_notes(retrieval_plan.use_global_context, briefs))
        return finalize_retrieval(
            candidates,
            plan,
            graph=graph,
            community_summaries=briefs,
            vectors_used=False,
            notes=notes,
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

    def rank_findings(
        self,
        query: str,
        top_k: int = RETRIEVAL_TOP_K,
        mode: str = "hybrid",
        allowed_data_classes: set[DataClass] | None = None,
        semantic_query: str | None = None,
    ) -> list[Finding]:
        """Ранжирует findings и один раз прогоняет их через общий re-rank.

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
        return [finding.model_copy(deep=True) for finding in reranked.findings]

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

    def document_count(self) -> int:
        return len(
            {
                str(evidence.document_id)
                for finding in self._findings.values()
                for evidence in finding.evidence
            }
        )

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

    def full_graph(self, allowed_data_classes: set[DataClass] | None = None) -> GraphSnapshot:
        # Сообщества проставляются в metadata узлов до копирования: иначе
        # копия уходит без community, а кэш-объект — с ним.
        communities = list(self._ensure_communities())
        snapshot = GraphSnapshot(
            nodes=[node.model_copy(deep=True) for node in self._graph.nodes],
            edges=[edge.model_copy(deep=True) for edge in self._graph.edges],
            communities=communities,
        )
        if allowed_data_classes is None:
            return snapshot
        # Один движок политик на весь контур: он срезает и рёбра, у которых
        # конец попал под ограничение, иначе граф остаётся с висячими ссылками.
        return AccessPolicyEngine().filter_graph(snapshot, allowed_data_classes)

    def all_findings(self, allowed_data_classes: set[DataClass] | None = None) -> list[Finding]:
        return [
            finding.model_copy(deep=True)
            for finding in self._findings.values()
            if finding.superseded_by is None
            and (allowed_data_classes is None or finding.data_class in allowed_data_classes)
        ]

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
        """Демонстрационный корпус memory-контура.

        Каждой находке проставляется ``origin=demo``: рабочий контур обязан
        отличать фиктивные «источники» seed-адаптера от импортированных документов
        и не ранжировать их как доказательства по горно-металлургическому корпусу.
        """
        corpus = [
            Finding(
                id="finding-ro",
                statement=(
                    "Обратный осмос обеспечивает удаление 95–99% растворённых солей и подходит "
                    "для достижения сухого остатка ≤1000 мг/л после предварительной очистки."
                ),
                confidence=0.92,
                evidence=[
                    self._evidence(
                        "water-treatment-review",
                        "Обзор методов обессоливания шахтных вод",
                        14,
                        "Задержание растворённых солей мембраной обратного осмоса "
                        "составляет 95–99%.",
                    )
                ],
                subject="reverse-osmosis",
                predicate="HAS_SALT_REJECTION",
                scope={"water_type": "mine_water"},
                observations=[
                    self._range_obs("salt_rejection", 95, 99, "%", "95–99%"),
                    self._point_obs("dry_residue", 1000, "mg/L", "сухой остаток ≤1000 мг/л"),
                ],
            ),
            Finding(
                id="finding-ro-pilot",
                statement=(
                    "Пилотные испытания обратного осмоса показали задержание солей "
                    "на уровне 80–85% при пониженном давлении."
                ),
                confidence=0.70,
                evidence=[
                    self._evidence(
                        "ro-pilot-study",
                        "Пилот обратного осмоса",
                        5,
                        "Задержание солей составило 80–85%.",
                    )
                ],
                subject="reverse-osmosis",
                predicate="HAS_SALT_REJECTION",
                scope={"water_type": "mine_water"},
                observations=[self._range_obs("salt_rejection", 80, 85, "%", "80–85%")],
            ),
            Finding(
                id="finding-ion",
                statement=(
                    "Ионный обмен целесообразен как селективная ступень для Ca и Mg, "
                    "но регенерационные стоки ограничивают применение в одиночку."
                ),
                confidence=0.84,
                evidence=[
                    self._evidence(
                        "ion-exchange-protocol",
                        "Протокол пилотных испытаний ионного обмена",
                        7,
                        "Снижение Ca и Mg достигало 82–91%; требовалась регенерация смолы.",
                    )
                ],
                subject="ion-exchange",
                predicate="HAS_REMOVAL_EFFICIENCY",
                scope={"water_type": "mine_water"},
                observations=[self._range_obs("ca_mg_removal", 82, 91, "%", "82–91%")],
            ),
            Finding(
                id="finding-thermal",
                statement=(
                    "Термическое выпаривание устойчиво к широкому составу воды, "
                    "но требует в 3–5 раз больше энергии, чем мембранная схема."
                ),
                confidence=0.78,
                status="disputed",
                data_class=DataClass.RESTRICTED,
                evidence=[
                    self._evidence(
                        "thermal-comparison",
                        "Сравнение технологий концентрирования",
                        22,
                        "Удельные энергозатраты выпаривания превышали мембранный "
                        "вариант в 3–5 раз.",
                    )
                ],
                subject="evaporation",
                predicate="HAS_ENERGY_RATIO",
                scope={"water_type": "mine_water"},
                observations=[self._range_obs("energy_ratio", 3, 5, "ratio", "3–5 раз")],
            ),
            Finding(
                id="finding-climate",
                statement=(
                    "Для холодного климата мембранный блок требует утепления и поддержания "
                    "температуры сырья выше 8 °C."
                ),
                confidence=0.81,
                evidence=[
                    self._evidence(
                        "cold-climate-membranes",
                        "Эксплуатация мембран в холодном климате",
                        9,
                        "Стабильная производительность наблюдалась при температуре "
                        "питания выше 8 °C.",
                    )
                ],
                subject="reverse-osmosis",
                predicate="REQUIRES_MIN_TEMPERATURE",
                scope={"climate": "cold"},
                observations=[self._point_obs("min_temperature", 8, "°C", "выше 8 °C")],
            ),
        ]
        return [
            finding.model_copy(update={"scope": {**finding.scope, SCOPE_ORIGIN: DEMO_ORIGIN}})
            for finding in corpus
        ]

    @staticmethod
    def _seed_graph() -> GraphSnapshot:
        nodes = [
            GraphNode(id="water", label="Шахтная вода", type=NodeType.MATERIAL),
            GraphNode(id="sulfates", label="Сульфаты 200–300 мг/л", type=NodeType.CONDITION),
            GraphNode(id="chlorides", label="Хлориды 200–300 мг/л", type=NodeType.CONDITION),
            GraphNode(id="reverse-osmosis", label="Обратный осмос", type=NodeType.PROCESS),
            GraphNode(id="ion-exchange", label="Ионный обмен", type=NodeType.PROCESS),
            GraphNode(id="evaporation", label="Выпаривание", type=NodeType.PROCESS),
            GraphNode(
                id="claim-ro",
                label="Сухой остаток ≤1000 мг/л",
                type=NodeType.CLAIM,
                confidence=0.92,
            ),
            GraphNode(
                id="claim-energy",
                label="Энергия в 3–5 раз выше",
                type=NodeType.CLAIM,
                confidence=0.78,
                data_class=DataClass.RESTRICTED,
            ),
            GraphNode(id="expert", label="Лаборатория водоподготовки", type=NodeType.EXPERT),
        ]
        edges = [
            GraphEdge(id="e1", source="water", target="sulfates", relation="CONTAINS"),
            GraphEdge(id="e2", source="water", target="chlorides", relation="CONTAINS"),
            GraphEdge(id="e3", source="water", target="reverse-osmosis", relation="TREATED_BY"),
            GraphEdge(id="e4", source="water", target="ion-exchange", relation="TREATED_BY"),
            GraphEdge(id="e5", source="reverse-osmosis", target="claim-ro", relation="PRODUCES"),
            GraphEdge(
                id="e6",
                source="evaporation",
                target="claim-energy",
                relation="REQUIRES",
                data_class=DataClass.RESTRICTED,
            ),
            GraphEdge(id="e7", source="expert", target="reverse-osmosis", relation="EXPERT_IN"),
        ]
        return GraphSnapshot(nodes=nodes, edges=edges)
