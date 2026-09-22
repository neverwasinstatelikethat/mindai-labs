from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from elasticsearch import Elasticsearch, helpers
from neo4j import Driver, GraphDatabase

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    CorpusStats,
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
from scientific_tangle.domain.models import NumericObservation, QueryPlan
from scientific_tangle.services.communities import (
    DEFAULT_PROFILE_LIMIT,
    community_briefs,
    detect_communities,
)
from scientific_tangle.services.embeddings import EmbeddingClient, GigaChatEmbeddingClient
from scientific_tangle.services.governance import AccessPolicyEngine
from scientific_tangle.services.knowledge import (
    MAX_ANCHORS,
    ChunkPiece,
    InMemoryKnowledgeBase,
    KnowledgeBase,
    KnowledgeState,
    RetrievalContext,
    chunk_document,
    chunk_finding,
    finalize_retrieval,
    finding_communities,
    stable_uuid,
    supersede_node_id,
)
from scientific_tangle.services.reranking import query_tokens, rerank_findings
from scientific_tangle.services.retrieval_semantics import (
    RETRIEVAL_TOP_K,
    candidate_window,
    exclude_demo,
    global_context_notes,
    is_demo_finding,
)

logger = logging.getLogger(__name__)

FINDING_INDEX = "mindai-findings-v2"
CHUNK_INDEX = "mindai-chunks-v2"
GRAPH_CACHE_TTL_SECONDS = 30.0
GRAPH_NODE_LIMIT = 600
RUS_ANALYSIS = {
    "analyzer": {"ru": {"type": "russian", "stopwords": "_russian_"}},
}


# ── Cypher обхода графа ─────────────────────────────────────────────────────
#
# Каждый запрос содержит ORDER BY до LIMIT/cut: без него «первые N» выбираются в
# порядке обхода хранилища, и один и тот же вопрос на тех же данных возвращает
# разные подграфы от запуска к запуску. Потолки параметризуются, а достижение
# потолка раскрывается в degradation_reasons — скрытой потери данных нет.
MAX_TRAVERSAL_NODES = 200

ANCHORS_CYPHER = """
MATCH (anchor:Entity)
WHERE anchor.type <> 'chunk'
  AND any(
    name IN $entities
    WHERE toLower(anchor.label) CONTAINS toLower(name)
      OR toLower(name) CONTAINS toLower(anchor.label)
  )
  AND ($classes IS NULL OR coalesce(anchor.data_class, 'public') IN $classes)
WITH anchor
ORDER BY coalesce(anchor.data_class, 'public'), anchor.label, anchor.id
LIMIT $anchors
RETURN [a IN collect(anchor) | properties(a)] AS nodes
"""

EXPAND_CYPHER = """
UNWIND $frontier AS fid
MATCH (f:Entity {id: fid})-[rel]-(next:Entity)
WHERE NOT next.id IN $visited
  AND next.type <> 'chunk'
  AND type(rel) IN $relations
  AND ($classes IS NULL OR coalesce(rel.data_class, 'public') IN $classes)
  AND ($classes IS NULL OR coalesce(next.data_class, 'public') IN $classes)
WITH DISTINCT next, rel
ORDER BY next.id, rel.id
WITH collect(DISTINCT properties(next))[0..$cap] AS nodes,
     count(DISTINCT next) AS reached
RETURN nodes, reached
"""

EDGES_CYPHER = """
UNWIND $ids AS id
MATCH (a:Entity {id: id})-[rel]->(b:Entity)
WHERE b.id IN $ids
  AND a.type <> 'chunk'
  AND b.type <> 'chunk'
  AND type(rel) IN $relations
  AND ($classes IS NULL OR coalesce(rel.data_class, 'public') IN $classes)
WITH DISTINCT a, rel, b
ORDER BY coalesce(rel.id, a.id + b.id), a.id, b.id
RETURN [row IN collect({id: rel.id, source: a.id, target: b.id, relation: type(rel),
                        confidence: rel.confidence,
                        data_class: coalesce(rel.data_class, 'public')}) | row] AS edges
"""


@dataclass(frozen=True, slots=True)
class _GraphDiff:
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class Neo4jElasticsearchKnowledgeBase:
    """Production GraphRAG-исполнитель для валидированных retrieval-планов.

    Класс доступа применяется до извлечения: ``data_class`` хранится и в индексе
    Elasticsearch, и в свойствах узлов Neo4j. Фильтрация ответа после генерации
    не считается контролем доступа — restricted-текст к этому моменту уже попал
    бы в промпт модели.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._seed = InMemoryKnowledgeBase()
        self._findings: dict[str, Finding] = {
            finding.id: finding for finding in self._seed.all_findings()
        }
        self._driver: Driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
        )
        self._search = Elasticsearch(settings.elasticsearch_url)
        self._embeddings: EmbeddingClient | None = None
        if settings.use_gigachat:
            try:
                self._embeddings = GigaChatEmbeddingClient(settings)
            except Exception as error:  # noqa: BLE001 - векторная ветка опциональна
                logger.error("Эмбеддинги недоступны: %s", error)
        self._initialized = False
        self._lock = threading.Lock()
        self._graph_cache: GraphSnapshot | None = None
        self._graph_cached_at = 0.0
        self._vectors_indexed = 0
        self._vectors_missing = 0
        # Потолок выборки графа — не скрытая потеря данных: (показано, всего).
        self._graph_window: tuple[int, int] | None = None
        # Явные отказы веток, которые retrieval обязан назвать в ответе.
        self._startup_notes: list[str] = []

    # ── Инициализация ───────────────────────────────────────────────────────

    def _ensure_ready(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._ensure_neo4j_schema()
            self._ensure_es_indices()
            self._restore_findings()
            # Демо-сеятель остаётся каталогом процесса (supersede, история версий):
            # его узлы не пишутся в Neo4j — фиктивные сущности не должны выдаваться
            # за знания корпуса в якорях, сообществах и графе интерфейса.
            self._initialized = True

    def _ensure_neo4j_schema(self) -> None:
        """Индексы, которые исполнители запросов действительно используют.

        FULLTEXT-индекс ``entity_label`` создавался и не вызывался ни разу: якоря
        обхода ищутся подстрокой метки (``CONTAINS``), чему fulltext не служит, а
        неиспользуемый индекс — это расходы на запись и ложное впечатление, что
        лексический поиск по меткам настроен. Создание убрано здесь и снимается
        ``DROP ... IF EXISTS``: в уже поднятой базе индекс переживает перезапуск
        контейнера и продолжает замедлять каждую запись узлов.
        """
        with self._driver.session() as session:
            session.run(
                "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE"
            )
            session.run("CREATE INDEX entity_type IF NOT EXISTS FOR (n:Entity) ON (n.type)")
            session.run("DROP INDEX entity_label IF EXISTS")

    def _ensure_es_indices(self) -> None:
        """Создаёт индексы, если их нет; существующие не удаляются — preload-данные
        должны переживать перезапуск контейнера."""
        finding_mappings = {
            "properties": {
                "statement": {"type": "text", "analyzer": "ru"},
                "status": {"type": "keyword"},
                "data_class": {"type": "keyword"},
                "confidence": {"type": "float"},
                "evidence": {"type": "text", "analyzer": "ru"},
                "scope.geography": {"type": "keyword"},
                "scope.year": {"type": "keyword"},
                "scope.origin": {"type": "keyword"},
                "finding_json": {"type": "keyword", "index": False},
                "embedding": {
                    "type": "dense_vector",
                    "dims": self._embedding_dimensions,
                    "index": True,
                    "similarity": "cosine",
                },
            }
        }
        chunk_mappings = {
            "properties": {
                "text": {"type": "text", "analyzer": "ru"},
                "title": {
                    "type": "text",
                    "analyzer": "ru",
                    "fields": {"keyword": {"type": "keyword"}},
                },
                "data_class": {"type": "keyword"},
                "source_path": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "finding_json": {"type": "keyword", "index": False},
            }
        }
        if self._search.indices.exists(index=FINDING_INDEX):
            # Размерность векторов сверяется до первого knn-запроса: поиск вектором
            # другого размера Elasticsearch отклоняет на каждой ветке, и retrieval
            # тихо терял бы семантическую ветку вместо явной деградации.
            self._reconcile_embedding_dimensions()
        else:
            self._search.indices.create(
                index=FINDING_INDEX, settings=RUS_ANALYSIS, mappings=finding_mappings
            )
            for finding in self._findings.values():
                self._index_finding(finding)
        if not self._search.indices.exists(index=CHUNK_INDEX):
            self._search.indices.create(
                index=CHUNK_INDEX, settings=RUS_ANALYSIS, mappings=chunk_mappings
            )

    def _reconcile_embedding_dimensions(self) -> None:
        """Несоответствие размерности индекса и клиента — явная деградация ветки."""
        if self._embeddings is None:
            return
        try:
            mapping = self._search.indices.get_mapping(index=FINDING_INDEX)
            body = (
                mapping[FINDING_INDEX]
                if FINDING_INDEX in mapping
                else next(iter(mapping.values()))
            )
            properties = (body.get("mappings") or {}).get("properties") or {}
            indexed = (properties.get("embedding") or {}).get("dims")
        except Exception as error:  # noqa: BLE001 - размерность проверяется по возможности
            logger.warning("Размерность индекса embeddings не получена: %s", error)
            return
        expected = self._embeddings.dimensions
        if indexed is not None and int(indexed) == expected:
            return
        # Поле ``embedding`` отсутствует в маппинсе ровно так же нежизнеспособно,
        # как и поле чужой размерности: knn-запрос падает на каждой ветке, а
        # retrieval делает вид, что семантика работала.
        actual = f"размерность {indexed}" if indexed is not None else "поля embedding нет"
        note = (
            f"Векторная ветка отключена: индекс {FINDING_INDEX} — {actual}, а клиент "
            f"GigaChat отдаёт {expected}. Нужен индекс с актуальной EMBEDDING_DIMENSIONS "
            "(существующие индексы этим кодом не пересоздаются), иначе knn-запрос "
            "отваливается на каждой попытке; пока retrieval идёт лексикой и чанками."
        )
        logger.error(note)
        self._embeddings = None
        self._startup_notes.append(note)

    @property
    def _embedding_dimensions(self) -> int:
        if self._embeddings is not None:
            return self._embeddings.dimensions
        return self._settings.embedding_dimensions

    def _restore_findings(self) -> None:
        """Поднимает сохранённые ранее findings из обоих индексов в кэш процесса.

        Чанки живут в CHUNK_INDEX: без их чтения ``all_findings()`` после
        перезапуска терял структурные находки, и версионирование по
        ``chunk-<uuid>`` в интерфейсе исчезало, хотя данные в индексе лежали.
        """
        restored = 0
        for index in (FINDING_INDEX, CHUNK_INDEX):
            if not self._search.indices.exists(index=index):
                continue
            try:
                hits = helpers.scan(
                    self._search,
                    index=index,
                    query={"query": {"match_all": {}}},
                    _source=["finding_json"],
                )
                for hit in hits:
                    payload = (hit.get("_source") or {}).get("finding_json")
                    if not payload:
                        continue
                    finding = Finding.model_validate_json(payload)
                    if finding.id not in self._findings:
                        self._findings[finding.id] = finding
                        restored += 1
            except Exception as error:  # noqa: BLE001 - старт не должен падать на чтении
                self._note_degradation(f"restore_findings:{index}", error)
        logger.info("Восстановлено findings из ES: %d", restored)

    # ── Запись графа ────────────────────────────────────────────────────────

    def _write_graph(self, diff: _GraphDiff, tx_id: str | None = None) -> None:
        """Пакетная запись узлов и рёбер одним запросом на тип.

        Прежняя версия на каждый документ MERGE-ила весь накопленный граф по
        одному транзакционному обходу на узел и на ребро — на загрузке корпуса это
        давало квадратичное число round-trip.

        ``tx_id`` помечает СОЗДАННЫЕ этим вызовом элементы. Откат прерванного
        импорта удаляет по метке, а не по id из диффа: узел сущности или
        публикации мог быть создан другим документом раньше (в том числе в
        прошлом процессе, чей каталог уже потерян), и его удаление разорвало бы
        чужие ссылки. ``ON CREATE`` ставит метку только новым элементам.
        """
        if not diff.nodes and not diff.edges:
            return
        invalid = [edge.relation for edge in diff.edges if not _is_safe_relation(edge.relation)]
        if invalid:
            raise ValueError(f"Небезопасные имена отношений: {sorted(set(invalid))}")
        with self._driver.session() as session:
            if diff.nodes:
                session.run(
                    """
                    UNWIND $rows AS row
                    MERGE (n:Entity {id: row.id})
                    ON CREATE SET n.created_by = $tx
                    SET n.label = row.label, n.type = row.type,
                        n.confidence = row.confidence, n.data_class = row.data_class,
                        n.metadata = row.metadata
                    """,
                    tx=tx_id,
                    rows=[
                        {
                            "id": node.id,
                            "label": node.label,
                            "type": node.type.value,
                            "confidence": node.confidence,
                            "data_class": node.data_class.value,
                            "metadata": json.dumps(node.metadata, ensure_ascii=False),
                        }
                        for node in diff.nodes
                    ],
                )
            for relation, edges in _group_by_relation(diff.edges).items():
                session.run(
                    f"""
                    UNWIND $rows AS row
                    MATCH (a:Entity {{id: row.source}}), (b:Entity {{id: row.target}})
                    MERGE (a)-[r:{relation} {{id: row.id}}]->(b)
                    ON CREATE SET r.created_by = $tx
                    SET r.confidence = row.confidence, r.data_class = row.data_class
                    """,
                    tx=tx_id,
                    rows=[
                        {
                            "id": edge.id,
                            "source": edge.source,
                            "target": edge.target,
                            "confidence": edge.confidence,
                            "data_class": edge.data_class.value,
                        }
                        for edge in edges
                    ],
                )
        self._invalidate_graph_cache()

    def ingest(self, document: DocumentRequest, extraction: ExtractionResult) -> DocumentReceipt:
        self._ensure_ready()
        checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        document_id = stable_uuid(checksum)
        with self._driver.session() as session:
            already = session.run(
                "MATCH (d:Entity {id: $id}) "
                "WHERE coalesce(d.semantic_extracted, false) RETURN d.id AS id",
                id=f"document-{document_id}",
            ).single()
        if already:
            return DocumentReceipt(
                document_id=document_id,
                checksum=checksum,
                status="duplicate",
                extracted_claims=0,
            )

        before_nodes = {node.id for node in self._seed.full_graph().nodes}
        before_edges = {edge.id for edge in self._seed.full_graph().edges}
        state = self._seed.snapshot_state()
        before_findings = set(state.findings)
        new_ids: list[str] = []
        tx_id = f"ingest-{stable_uuid(f'{checksum}:semantic')}"

        receipt = self._seed.ingest(document, extraction)
        if receipt.status == "duplicate":
            return receipt

        try:
            # Порядок «ES → Neo4j» выбран из-за отката: идентификаторы находок
            # известны заранее, поэтому компенсация удаляет ровно записанное
            # (delete по id). Для графа точный список созданных элементов даёт только
            # метка ``created_by`` — она и снимается в ``_roll_back_ingest``.
            current = {finding.id: finding for finding in self._seed.all_findings()}
            new_ids = [key for key in current if key not in before_findings]
            self._index_findings([current[key] for key in new_ids])
            self._search.indices.refresh(index=FINDING_INDEX)
            after = self._seed.full_graph()
            self._write_graph(
                _GraphDiff(
                    nodes=[node for node in after.nodes if node.id not in before_nodes],
                    edges=[edge for edge in after.edges if edge.id not in before_edges],
                ),
                tx_id=tx_id,
            )
            with self._driver.session() as session:
                session.run(
                    "MATCH (d:Entity {id: $id}) SET d.semantic_extracted = true",
                    id=f"document-{receipt.document_id}",
                )
            self._findings = current
        except Exception as error:
            # Атомарность импорта: прерванный импорт не вправе оставлять узлы графа,
            # находки каталога и половину ES-документов — иначе «есть ли у числа
            # источник» проверяется по неполному корпусу и отвечает неправду.
            # Остаток, который этот контур не гарантирует: процесс, убитый между
            # записью ES и записью графа, оставит находки без узлов (трассировка к
            # документу сохраняется, обход графа их не увидит).
            self._roll_back_ingest(receipt.document_id, state, new_ids, tx_id)
            logger.error("Импорт %s откачен: %s", receipt.document_id, error)
            raise
        return receipt

    def _roll_back_ingest(
        self,
        document_id: UUID,
        state: KnowledgeState,
        new_ids: Sequence[str],
        tx_id: str,
    ) -> None:
        """Откатывает каталог, seed-состояние, находки ES и созданные элементы графа."""
        self._seed.restore_state(state)
        self._findings = {finding.id: finding for finding in state.findings.values()}
        if new_ids:
            try:
                helpers.bulk(
                    self._search,
                    [
                        {"_index": FINDING_INDEX, "_id": finding_id, "_op_type": "delete"}
                        for finding_id in new_ids
                    ],
                    refresh=True,
                    raise_on_error=False,
                    raise_on_exception=False,
                )
            except Exception as error:  # noqa: BLE001 - вторичный сбой не перекрывает первый
                logger.error("Очистка %d находок после отказа не удалась: %s", len(new_ids), error)
        with self._driver.session() as session:
            session.run(
                "MATCH (d:Entity {id: $id}) REMOVE d.semantic_extracted",
                id=f"document-{document_id}",
            )
            # Только элементы этой записи: узлы, созданные прежними импортами,
            # остаются на месте вместе со своими ссылками.
            session.run(
                "MATCH ()-[r]->() WHERE r.created_by = $tx DELETE r",
                tx=tx_id,
            )
            session.run(
                "MATCH (n:Entity) WHERE n.created_by = $tx DETACH DELETE n",
                tx=tx_id,
            )
        self._invalidate_graph_cache()

    def index_document(
        self, document: DocumentRequest, source_path: str
    ) -> StructuralDocumentReceipt:
        """Структурный импорт: чанки попадают и в индекс, и в каталог находок.

        Без шага с каталогом ``all_findings()`` не видел ни одного чанка, поэтому
        ``supersede_finding`` по идентификатору ``chunk-<uuid>`` отдавал 409, а
        проверка версии проходила мимо скорректированного текста. Откат по ошибке
        обязателен: половина чанков в индексе при отсутствии узлов графа (или
        наоборот) делает число документов и число доказательств несводимыми.
        """
        self._ensure_ready()
        checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        document_id = stable_uuid(checksum)
        document_node_id = f"document-{document_id}"
        with self._driver.session() as session:
            existing = session.run(
                "MATCH (d:Entity {id: $id}) RETURN d.id AS id", id=document_node_id
            ).single()
        if existing:
            count = self._search.count(
                index=CHUNK_INDEX, query={"term": {"document_id": str(document_id)}}
            )["count"]
            return StructuralDocumentReceipt(
                document_id=document_id,
                checksum=checksum,
                status="duplicate",
                chunks=int(count),
            )

        pieces = chunk_document(document, document_id)
        findings = [chunk_finding(document, document_id, piece) for piece in pieces]
        actions = [
            {
                "_index": CHUNK_INDEX,
                "_id": finding.id,
                "_source": {
                    "text": finding.statement,
                    "title": document.title,
                    "data_class": finding.data_class.value,
                    "source_path": source_path,
                    "document_id": str(document_id),
                    "finding_json": finding.model_dump_json(),
                },
            }
            for finding in findings
        ]
        indexed = False
        try:
            if actions:
                helpers.bulk(self._search, actions, refresh=True)
                indexed = True
            self._write_chunks_to_graph(document, document_node_id, pieces)
        except Exception as error:
            if indexed:
                self._delete_chunk_docs([finding.id for finding in findings])
            self._delete_document_graph(document_node_id)
            self._invalidate_graph_cache()
            logger.error("Структурный импорт %s откачен: %s", document_node_id, error)
            raise
        # Каталог находок: чанки должны быть видны all_findings/supersede_finding.
        self._seed.register_findings(findings)
        for finding in findings:
            self._findings[finding.id] = finding
        self._invalidate_graph_cache()
        return StructuralDocumentReceipt(
            document_id=document_id,
            checksum=checksum,
            status="created",
            chunks=len(pieces),
            vectors_indexed=0,
        )

    def _write_chunks_to_graph(
        self, document: DocumentRequest, document_node_id: str, pieces: Sequence[ChunkPiece]
    ) -> None:
        document_metadata = json.dumps(
            {"source_path": document.title, "stage": "structural"}, ensure_ascii=False
        )
        with self._driver.session() as session:
            session.run(
                """
                MERGE (d:Entity {id: $id})
                SET d.label = $label, d.type = 'publication', d.confidence = 1.0,
                    d.data_class = $data_class, d.metadata = $metadata,
                    d.semantic_extracted = false
                WITH d
                UNWIND $rows AS chunk
                MERGE (c:Entity {id: chunk.id})
                SET c.label = chunk.label, c.type = 'chunk', c.confidence = 1.0,
                    c.data_class = chunk.data_class, c.metadata = chunk.metadata
                MERGE (d)-[r:HAS_CHUNK {id: chunk.edge_id}]->(c)
                SET r.confidence = 1.0, r.data_class = chunk.data_class
                """,
                id=document_node_id,
                label=document.title,
                data_class=document.data_class.value,
                metadata=document_metadata,
                rows=[
                    {
                        "id": piece.id,
                        "label": piece.text[:160],
                        "data_class": document.data_class.value,
                        "metadata": json.dumps(
                            {
                                key: value
                                for key, value in (
                                    ("page", piece.fragment.page),
                                    ("sheet", piece.fragment.sheet),
                                    ("cell_range", piece.fragment.cell_range),
                                    ("char_start", piece.char_start),
                                    ("char_end", piece.char_end),
                                )
                                if value is not None
                            },
                            ensure_ascii=False,
                        ),
                        "edge_id": f"{document_node_id}-has-{piece.id}",
                    }
                    for piece in pieces
                ],
            )

    def _delete_chunk_docs(self, finding_ids: Sequence[str]) -> None:
        if not finding_ids:
            return
        try:
            helpers.bulk(
                self._search,
                [
                    {"_index": CHUNK_INDEX, "_id": finding_id, "_op_type": "delete"}
                    for finding_id in finding_ids
                ],
                refresh=True,
                raise_on_error=False,
                raise_on_exception=False,
            )
        except Exception as error:  # noqa: BLE001 - откат не должен прятать причину сбоя
            logger.error("Не удалось снять %d документов чанков: %s", len(finding_ids), error)

    def _delete_document_graph(self, document_node_id: str) -> None:
        try:
            with self._driver.session() as session:
                session.run(
                    """
                    MATCH (d:Entity {id: $id})
                    OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Entity)
                    DETACH DELETE d, c
                    """,
                    id=document_node_id,
                )
        except Exception as error:  # noqa: BLE001 - откат не должен прятать причину сбоя
            logger.error("Не удалось снять узлы %s: %s", document_node_id, error)

    # ── Чтение ──────────────────────────────────────────────────────────────

    def retrieve(
        self,
        plan: QueryPlan,
        retrieval_plan: RetrievalPlan,
        allowed_data_classes: set[DataClass] | None = None,
    ) -> RetrievalContext:
        """Исполняет план retrieval теми же хелперами, что и memory-адаптер.

        Ранее ``numeric_filters`` здесь не применялись вообще, а observation
        рапортовала «доказательств после фильтрации» по нефильтрованному списку;
        ``countries``, ``year_from``/``year_to`` и ``use_global_context`` не
        читались нигде. Окно кандидатов шире потолка выдачи: иначе ограничение по
        году срезало бы выдачу в ноль вместо честного отчёта о покрытии.
        """
        self._ensure_ready()
        notes = [*self._startup_notes, *self._graph_window_notes()]
        window = candidate_window(plan, RETRIEVAL_TOP_K)
        candidates, leg_notes = self._rank(
            retrieval_plan.lexical_query,
            window,
            "hybrid",
            allowed_data_classes,
            semantic_query=retrieval_plan.semantic_query,
        )
        notes.extend(leg_notes)
        if not candidates:
            notes.append(
                "Лексика, чанки и векторы не дали ни одной находки: ограничения плана "
                "не к чему применять — доказательств по запросу нет."
            )
        if retrieval_plan.use_local_graph:
            graph, graph_notes = self._traverse(retrieval_plan, allowed_data_classes)
            notes.extend(graph_notes)
        else:
            graph = GraphSnapshot(nodes=[], edges=[], communities=[])
        wants_global = retrieval_plan.use_community_context or retrieval_plan.use_global_context
        briefs = (
            self._community_context(retrieval_plan, candidates, allowed_data_classes)
            if wants_global
            else []
        )
        if wants_global:
            notes.extend(global_context_notes(retrieval_plan.use_global_context, briefs))
        return finalize_retrieval(
            candidates,
            plan,
            graph=graph,
            community_summaries=briefs,
            vectors_used=self._embeddings is not None,
            notes=notes,
        )

    def _community_context(
        self,
        retrieval_plan: RetrievalPlan,
        findings: Sequence[Finding],
        allowed_data_classes: set[DataClass] | None,
    ) -> list[str]:
        """Содержательные сводки сообществ — та же функция, что в memory-контуре.

        Граф под сводки берётся уже срезанным по ACL: профили собираются из меток
        узлов, и без среза restricted-текст ушёл бы в промпт модели напрямую.
        """
        snapshot = self.full_graph(allowed_data_classes)
        return community_briefs(
            snapshot.nodes,
            snapshot.edges,
            findings,
            query_tokens(retrieval_plan.lexical_query),
            DEFAULT_PROFILE_LIMIT,
        )

    def _traverse(
        self,
        retrieval_plan: RetrievalPlan,
        allowed_data_classes: set[DataClass] | None,
    ) -> tuple[GraphSnapshot, list[str]]:
        """Ограниченный BFS по графу вместо перечисления всех путей длины ≤ hops.

        Прежний запрос ``MATCH path = (anchor)-[rels*1..d]-(n)`` перечисляет пути
        экспоненциально и на плотном графе корпуса сжигает CPU ради множества
        достижимых узлов — ровно того, что даёт BFS с посещёнными. Результат по
        путям длины ≤ max_hops совпадает, стоимость линейна по рёбрам, а число
        обращений к Neo4j фиксировано: якоря + один запрос на уровень (≤ 4) +
        сбор рёбер между посещёнными.
        """
        depth = min(max(retrieval_plan.max_hops, 1), 4)
        classes = _class_values(allowed_data_classes)
        entities = [name for name in retrieval_plan.entity_names if name.strip()] or [
            retrieval_plan.lexical_query
        ]
        notes: list[str] = []
        visited: dict[str, GraphNode] = {}
        with self._driver.session() as session:
            record = session.run(
                ANCHORS_CYPHER,
                entities=entities,
                classes=classes,
                anchors=MAX_ANCHORS,
            ).single()
            for item in ((record["nodes"] if record else []) or []):
                node = _parse_node(item)
                if node is not None:
                    visited[node.id] = node
            frontier = sorted(visited)
            level = 0
            while frontier and level < depth and len(visited) < MAX_TRAVERSAL_NODES:
                expanded = session.run(
                    EXPAND_CYPHER,
                    frontier=frontier,
                    visited=sorted(visited),
                    relations=retrieval_plan.relation_types,
                    classes=classes,
                    cap=MAX_TRAVERSAL_NODES - len(visited),
                ).single()
                next_frontier: list[str] = []
                for item in (expanded["nodes"] if expanded else []) or []:
                    node = _parse_node(item)
                    if node is None or node.id in visited:
                        continue
                    visited[node.id] = node
                    next_frontier.append(node.id)
                if len(visited) >= MAX_TRAVERSAL_NODES:
                    notes.append(
                        f"Обход графа остановлен на потолке {MAX_TRAVERSAL_NODES} узлов: "
                        "часть достижимых связей не показана."
                    )
                    break
                frontier = sorted(set(next_frontier))
                level += 1
            edge_record = (
                session.run(
                    EDGES_CYPHER,
                    ids=sorted(visited),
                    relations=retrieval_plan.relation_types,
                    classes=classes,
                ).single()
                if visited
                else None
            )
        edges = [
            edge
            for item in ((edge_record["edges"] if edge_record else []) or [])
            if (edge := _parse_edge(item)) is not None
        ]
        snapshot = GraphSnapshot(
            nodes=[node.model_copy(deep=True) for node in visited.values()],
            edges=edges,
            communities=self.full_graph().communities,
        )
        return snapshot, notes

    def rank_findings(
        self,
        query: str,
        top_k: int = RETRIEVAL_TOP_K,
        mode: str = "hybrid",
        allowed_data_classes: set[DataClass] | None = None,
        semantic_query: str | None = None,
    ) -> list[Finding]:
        findings, _ = self._rank(query, top_k, mode, allowed_data_classes, semantic_query)
        return findings

    def _rank(
        self,
        query: str,
        top_k: int,
        mode: str,
        allowed_data_classes: set[DataClass] | None,
        semantic_query: str | None = None,
    ) -> tuple[list[Finding], list[str]]:
        """Три ветки → RRF → один локальный re-rank на слитых кандидатах.

        ``_chunk_hits`` раньше возвращал находки, которые никто не брал, а те же
        id вторым запросом перечитывались через ``_load_findings``: лишний
        round-trip и окно ``top_k * 3`` внутри каждого вызова. Теперь размер окна
        задаёт вызывающий (``candidate_window``), а чанк-находка берётся прямо из
        ответного хита — её ``finding_json`` уже в ``_source``. Повторно за ней в
        ``FINDING_INDEX`` не идём: там её заведомо нет (чанки живут в своём
        индексе), и запрос на такие id только плодил пустые mget-слоты.
        """
        notes: list[str] = []
        size = max(top_k, 1)
        lexical_ids = self._lexical_ids(FINDING_INDEX, query, size, allowed_data_classes, notes)
        chunk_ids, chunk_findings = self._chunk_hits(query, size, allowed_data_classes, notes)
        wanted_semantic = (semantic_query or query).strip()
        semantic_ids: list[str] = []
        wants_semantic = mode in {"hybrid", "semantic"}
        if wants_semantic and self._embeddings is None:
            # Ветку просили, но она не может работать: молчать об этом — значит
            # рапортовать «гибридный поиск» там, где был только текст.
            notes.append(
                "Векторная ветка недоступна (эмбеддинги не настроены либо отключены из-за "
                "несовместимой размерности индекса): семантический запрос плана не исполнен, "
                "выдача собрана лексикой и структурными чанками."
            )
        elif wants_semantic:
            semantic_ids = self._semantic_ids(wanted_semantic, size, allowed_data_classes, notes)
        if mode == "semantic" and not semantic_ids and self._embeddings is not None:
            notes.append(
                "Семантическая ветка не дала результатов: векторов в индексе нет, "
                "выдача собрана лексическим сопоставлением."
            )
            semantic_ids = lexical_ids
        already_loaded = {finding.id for finding in chunk_findings}
        merged_ids = list(dict.fromkeys([*lexical_ids, *chunk_ids, *semantic_ids]))
        candidates = self._load_findings(
            [item for item in merged_ids if item not in already_loaded],
            allowed_data_classes,
            notes,
        )
        by_id = {finding.id: finding for finding in candidates}
        for finding in chunk_findings:
            by_id.setdefault(finding.id, finding)
        # Демонстрационный seed-контент — не источник предметной области: в
        # рабочем контуре он исключается до ранжирования, и число называется вслух.
        visible = exclude_demo(by_id.values())
        if len(visible) != len(by_id):
            notes.append(
                "Исключено из ранжирования демонстрационных находок (origin=demo): "
                f"{len(by_id) - len(visible)}."
            )
        fusion = reciprocal_rank_fusion(
            {
                "lexical": [(lexical_ids, 1.0), (chunk_ids, 0.3)],
                "semantic": [(semantic_ids, 1.0)],
            }.get(mode, [(lexical_ids, 1.0), (chunk_ids, 0.3), (semantic_ids, 0.6)])
        )
        communities = finding_communities(self.full_graph().nodes, visible)
        reranked = rerank_findings(
            query,
            sorted(visible, key=lambda finding: finding.id),
            top_k=top_k,
            fusion=fusion,
            communities=communities,
        )
        if reranked.degraded:
            notes.append(
                "Re-rank не нашёл содержательных совпадений с запросом: порядок выдачи "
                "задан согласием веток, а не текстом доказательств."
            )
        return list(reranked.findings), notes

    def _lexical_ids(
        self,
        index: str,
        query: str,
        size: int,
        allowed: set[DataClass] | None,
        notes: list[str],
    ) -> list[str]:
        fields = ["statement^3", "evidence"] if index == FINDING_INDEX else ["text^3", "title^2"]
        try:
            response = self._search.search(
                index=index,
                size=size,
                query={
                    "bool": {
                        "must": [
                            {
                                "multi_match": {
                                    "query": query,
                                    "fields": fields,
                                    "type": "best_fields",
                                }
                            }
                        ],
                        "filter": _class_filter(allowed),
                    }
                },
                source=False,
            )
        except Exception as error:  # noqa: BLE001 - без лексической ветки retrieval обязан деградировать вслух
            notes.append(str(self._note_degradation("lexical_leg", error)))
            return []
        return [str(hit["_id"]) for hit in response["hits"]["hits"]]

    def _chunk_hits(
        self,
        query: str,
        size: int,
        allowed: set[DataClass] | None,
        notes: list[str],
    ) -> tuple[list[str], list[Finding]]:
        try:
            response = self._search.search(
                index=CHUNK_INDEX,
                size=size,
                query={
                    "bool": {
                        "must": [
                            {
                                "multi_match": {
                                    "query": query,
                                    "fields": ["text^3", "title^2"],
                                    "type": "best_fields",
                                }
                            }
                        ],
                        "filter": _class_filter(allowed),
                    }
                },
            )
        except Exception as error:  # noqa: BLE001 - структурный индекс может быть не создан
            notes.append(str(self._note_degradation("chunk_leg", error)))
            return [], []
        findings: list[Finding] = []
        ids: list[str] = []
        for hit in response["hits"]["hits"]:
            payload = (hit.get("_source") or {}).get("finding_json")
            if not payload:
                continue
            finding = Finding.model_validate_json(payload)
            # Заменённая экспертом версия чанка не должна возвращаться в выдачу:
            # ``supersede_finding`` проставляет superseded_by в копию CHUNK_INDEX,
            # а до перестройки индекса это единственное место, где замена видна.
            if finding.superseded_by is not None:
                continue
            ids.append(finding.id)
            findings.append(finding)
        return ids, findings

    def _semantic_ids(
        self, query: str, size: int, allowed: set[DataClass] | None, notes: list[str]
    ) -> list[str]:
        if self._embeddings is None:
            return []
        try:
            vector = self._embeddings.query(query)
            if len(vector) != self._embedding_dimensions:
                # Разошедшаяся размерность — явная деградация: knn-запрос с таким
                # вектором отвалился бы на стороне Elasticsearch без объяснений.
                notes.append(
                    f"Векторная ветка пропущена: размерность запроса {len(vector)} не "
                    f"равна размерности индекса {self._embedding_dimensions}."
                )
                return []
            response = self._search.search(
                index=FINDING_INDEX,
                size=size,
                knn={
                    "field": "embedding",
                    "query_vector": vector,
                    "k": size,
                    "num_candidates": 200,
                    "filter": _class_filter(allowed),
                },
                source=False,
            )
        except Exception as error:  # noqa: BLE001 - лексическая ветка остаётся рабочей
            notes.append(str(self._note_degradation("semantic_leg", error)))
            return []
        return [str(hit["_id"]) for hit in response["hits"]["hits"]]

    def _load_findings(
        self,
        finding_ids: list[str],
        allowed_data_classes: set[DataClass] | None = None,
        notes: list[str] | None = None,
    ) -> list[Finding]:
        if not finding_ids:
            return []
        notes = notes if notes is not None else []
        try:
            response = self._search.mget(index=FINDING_INDEX, ids=finding_ids)
            documents: list[dict[str, Any]] = list(response["docs"])
        except Exception as error:  # noqa: BLE001
            notes.append(str(self._note_degradation("load_findings", error)))
            documents = []
        findings: list[Finding] = []
        seen: set[str] = set()
        for document in documents:
            payload = (document.get("_source") or {}).get("finding_json")
            finding_id = str(document.get("_id", ""))
            if not payload or finding_id in seen:
                continue
            finding = Finding.model_validate_json(payload)
            if finding.superseded_by is not None:
                continue
            findings.append(finding)
            seen.add(finding.id)
        # Локальный каталог закрывает и seed-документы, и те id, которые не
        # вернулись из-за отказа mget. Права проверяются и здесь: векторная ветка
        # отфильтрована в запросе, но fallback не должен их обходить.
        for candidate_id in finding_ids:
            local = self._findings.get(candidate_id)
            if local is None or local.id in seen or local.superseded_by is not None:
                continue
            if _visible(local.data_class, allowed_data_classes):
                findings.append(local)
                seen.add(local.id)
        return findings

    # ── Индексация доказательств ────────────────────────────────────────────

    def _index_findings(self, findings: list[Finding]) -> None:
        if not findings:
            return
        vectors: list[list[float] | None] = [None] * len(findings)
        if self._embeddings is not None:
            texts = [_embedding_text(finding) for finding in findings]
            try:
                vectors = list(self._embeddings.documents(texts))
            except Exception as error:  # noqa: BLE001 - индеемся без векторов, но вслух
                self._note_degradation("embedding_batch", error)
                self._vectors_missing += len(findings)
            # Вектор чужой размерности Elasticsearch отвергает всей партией, поэтому
            # такая запись снимается до bulk: остальное индексируется с вектором,
            # а не теряется вместе с ошибкой запроса.
            dimensions = self._embedding_dimensions
            mismatched = [
                index
                for index, vector in enumerate(vectors)
                if vector is not None and len(vector) != dimensions
            ]
            if mismatched:
                logger.error(
                    "Эмбеддинги не совпадают по размерности с индексом (%s): записей снято %d",
                    dimensions,
                    len(mismatched),
                )
                for index in mismatched:
                    vectors[index] = None
                    self._vectors_missing += 1
        actions = []
        for index, finding in enumerate(findings):
            document: dict[str, Any] = {
                "statement": finding.statement,
                "status": finding.status,
                "data_class": finding.data_class.value,
                "confidence": finding.confidence,
                "evidence": " ".join(item.quote for item in finding.evidence),
                "finding_json": finding.model_dump_json(),
            }
            vector = vectors[index] if index < len(vectors) else None
            if vector is not None:
                document["embedding"] = vector
                self._vectors_indexed += 1
            else:
                self._vectors_missing += 1
            actions.append({"_index": FINDING_INDEX, "_id": finding.id, "_source": document})
        helpers.bulk(self._search, actions, refresh=False)

    def _index_finding(self, finding: Finding) -> None:
        self._index_findings([finding])

    # ── Статистика и граф целиком ───────────────────────────────────────────

    def document_count(self) -> int:
        self._ensure_ready()
        with self._driver.session() as session:
            record = session.run(
                "MATCH (n:Entity {type: 'publication'}) RETURN count(n) AS count"
            ).single()
        return int(record["count"]) if record else 0

    def corpus_stats(self) -> CorpusStats:
        self._ensure_ready()
        with self._driver.session() as session:
            record = session.run(
                """
                MATCH (n:Entity)
                RETURN count(CASE WHEN n.type = 'publication' THEN 1 END) AS documents,
                       count(CASE WHEN n.type = 'chunk' THEN 1 END) AS chunks,
                       count(CASE WHEN n.type = 'claim' THEN 1 END) AS claims,
                       count(CASE WHEN NOT n.type IN
                         ['publication', 'chunk', 'claim'] THEN 1 END) AS entities,
                       count(CASE WHEN n.type = 'publication'
                         AND coalesce(n.semantic_extracted, false) THEN 1 END)
                         AS semantic_documents
                """
            ).single()
        if not record:
            return CorpusStats(documents=0, chunks=0, claims=0, entities=0, semantic_documents=0)
        stats = CorpusStats(
            **{
                key: int(record[key])
                for key in CorpusStats.model_fields
                if key != "vectors_indexed"
            },
            vectors_indexed=self._vectors_indexed,
        )
        return stats

    def full_graph(self, allowed_data_classes: set[DataClass] | None = None) -> GraphSnapshot:
        self._ensure_ready()
        cached = self._cached_graph()
        if allowed_data_classes is None:
            return cached.model_copy(deep=True)
        return AccessPolicyEngine().filter_graph(cached, allowed_data_classes)

    def _cached_graph(self) -> GraphSnapshot:
        """Кэш полного графа с инвалидацией на запись.

        Раньше каждый вызов перечитывал все сущности из Neo4j и заново запускал
        Leiden/Louvain; то же самое происходило при каждом retrieval в качестве
        fallback-а.
        """
        cached = self._graph_cache
        if (
            cached is not None
            and time.monotonic() - self._graph_cached_at < GRAPH_CACHE_TTL_SECONDS
        ):
            self._note_cache(True)
            return cached
        self._note_cache(False)
        snapshot = self._load_graph_from_neo4j()
        self._graph_cache = snapshot
        self._graph_cached_at = time.monotonic()
        return snapshot

    def _load_graph_from_neo4j(self) -> GraphSnapshot:
        with self._driver.session() as session:
            node_record = session.run(
                """
                MATCH (node:Entity)
                WHERE node.type <> 'chunk'
                WITH node
                ORDER BY coalesce(node.label, node.id), node.id
                WITH collect(properties(node)) AS nodes, count(node) AS total
                RETURN nodes[0..$limit] AS nodes, total
                """,
                limit=GRAPH_NODE_LIMIT,
            ).single()
            total = int((node_record["total"] if node_record else 0) or 0)
            edge_record = session.run(
                """
                MATCH (a:Entity)-[rel]->(b:Entity)
                WHERE a.type <> 'chunk' AND b.type <> 'chunk' AND type(rel) <> 'HAS_CHUNK'
                WITH a, rel, b
                ORDER BY rel.id, a.id, b.id
                RETURN collect(DISTINCT {
                    id: rel.id, source: a.id, target: b.id,
                    relation: type(rel), confidence: rel.confidence,
                    data_class: coalesce(rel.data_class, 'public')
                }) AS edges
                """
            ).single()

        raw_nodes = (node_record["nodes"] if node_record else []) or []
        raw_edges = (edge_record["edges"] if edge_record else []) or []
        nodes = [node for item in raw_nodes if (node := _parse_node(item)) is not None]
        edges = [edge for item in raw_edges if (edge := _parse_edge(item)) is not None]
        # Потолок GRAPH_NODE_LIMIT — не молчаливая потеря: пара (показано, всего)
        # раскрывается в degradation_reasons каждого retrieval на этом графе.
        self._graph_window = (len(nodes), max(total, len(nodes)))
        if not nodes:
            return self._seed.full_graph()
        connected = {edge.source for edge in edges} | {edge.target for edge in edges}
        keep = [
            node
            for node in nodes
            if node.id in connected or node.metadata.get("domain") or node.id.startswith("dom-")
        ]
        communities = detect_communities(keep, edges)
        return GraphSnapshot(nodes=keep, edges=edges, communities=communities)

    def _graph_window_notes(self) -> list[str]:
        window = self._graph_window
        if window is None or window[0] >= window[1]:
            return []
        return [
            f"Полный граф прочитан не целиком: узлов {window[0]} из {window[1]} "
            f"(потолок GRAPH_NODE_LIMIT={GRAPH_NODE_LIMIT}). Сообщества и обход считаются "
            "по этой части графа, а не по всему корпусу."
        ]

    def _invalidate_graph_cache(self) -> None:
        self._graph_cache = None
        self._graph_cached_at = 0.0

    def _note_cache(self, hit: bool) -> None:
        try:
            from scientific_tangle.services.agent_metrics import agent_metrics

            agent_metrics.observe_graph_cache(hit)
            agent_metrics.observe_cache_age(max(time.monotonic() - self._graph_cached_at, 0.0))
        except Exception:  # noqa: BLE001 - метрики не должны ронять retrieval
            return

    def _note_degradation(self, component: str, error: BaseException) -> str:
        """Фиксирует отказ ветки и возвращает формулировку для честного отчёта."""
        note = f"Ветка retrieval «{component}» завершилась ошибкой: {str(error)[:200]}."
        logger.warning("Деградация %s: %s", component, error)
        try:
            from scientific_tangle.services.agent_metrics import agent_metrics

            agent_metrics.observe_retrieval_failure(component)
        except Exception:  # noqa: BLE001 - метрики не должны ронять retrieval
            pass
        return note

    def all_findings(self, allowed_data_classes: set[DataClass] | None = None) -> list[Finding]:
        """Каталог доказательств рабочего контура: семантические тезисы и чанки.

        Seed-находки (``origin=demo``) в список не попадают: в рабочем контуре это
        не источники, а список находок интерфейса выдаёт их за документы корпуса.
        Структурные чанки, наоборот, обязаны быть видны — иначе версионирование по
        идентификатору ``chunk-<uuid>`` недоступно вовсе.
        """
        self._ensure_ready()
        findings = [
            finding.model_copy(deep=True)
            for finding in self._findings.values()
            if finding.superseded_by is None and not is_demo_finding(finding)
        ]
        if allowed_data_classes is None:
            return findings
        allowed = set(allowed_data_classes)
        return [finding for finding in findings if finding.data_class in allowed]

    def _stored_finding(self, finding_id: str) -> Finding | None:
        """Достаёт находку по id из индексов — так в каталог попадают чанки."""
        for index in (FINDING_INDEX, CHUNK_INDEX):
            try:
                response = self._search.mget(index=index, ids=[finding_id])
            except Exception as error:  # noqa: BLE001 - индекс мог быть ещё не создан
                logger.warning("Индекс %s не прочитан: %s", index, error)
                continue
            for document in response.get("docs") or []:
                if document.get("found") is False:
                    continue
                payload = (document.get("_source") or {}).get("finding_json")
                if payload:
                    return Finding.model_validate_json(payload)
        return None

    def _mark_chunk_superseded(self, old: Finding, new_id: str) -> None:
        """Проставляет ``superseded_by`` в копии чанка внутри CHUNK_INDEX.

        Чанк-ветка выдачи читает ``finding_json`` из этого индекса, а он не
        перестраивается при версионировании: без точечного обновления заменённый
        текст возвращался бы в ответе бессрочно, и правка эксперта обходила бы
        проверку версии. Отказ обновления не отменяет саму замену (она уже в
        каталоге, графе и FINDING_INDEX) — но остаётся в логе.
        """
        marked = old.model_copy(update={"superseded_by": new_id})
        try:
            helpers.bulk(
                self._search,
                [
                    {
                        "_index": CHUNK_INDEX,
                        "_id": old.id,
                        "_op_type": "update",
                        "doc": {"finding_json": marked.model_dump_json()},
                    }
                ],
                refresh=True,
                raise_on_error=False,
                raise_on_exception=False,
            )
        except Exception as error:  # noqa: BLE001 - вторичный сбой не перекрывает замену
            logger.error("Не удалось пометить чанк %s заменённым: %s", old.id, error)

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
        """Версионирование: старая версия остаётся в графе через SUPERSEDES.

        После замены новая версия индексируется, а старая исключается из выдачи
        за счёт поля ``superseded_by``. Работает и по идентификатору структурного
        чанка: если находки нет в каталоге процесса, она читается из индексов и
        регистрируется — иначе 409 на любой ``chunk-<uuid>``.

        Каталог seed-адаптера пополняется всегда, а не только для чанков: после
        перезапуска кэш процесса поднят из Elasticsearch, а seed пуст, и замена
        восстановленной находки падала на «finding not found» ровно тем же
        способом. ``register_findings`` идемпотентен, повтор безопасен.

        ``_ensure_ready`` вызывается первым: чтение индексов до инициализации
        падало на «индекс не создан», и первый запрос после старта контейнера
        терял находку, вместо того чтобы её заменить.
        """
        self._ensure_ready()
        before = self._findings.get(finding_id) or self._stored_finding(finding_id)
        if before is not None:
            self._findings.setdefault(finding_id, before)
            self._seed.register_findings([before])
        new_finding = self._seed.supersede_finding(
            finding_id,
            new_statement,
            new_confidence,
            observations,
            reviewer_id,
            review_date,
            review_reason,
        )
        self._findings = {finding.id: finding for finding in self._seed.all_findings()}
        self._index_findings([new_finding])
        if before is not None and before.id.startswith("chunk-"):
            self._mark_chunk_superseded(before, new_finding.id)
        # Концы SUPERSEDES — реальные id узлов графа (см. supersede_node_id):
        # прямое снятие префикса угадывал имя и MERGE концов молча не срабатывал.
        existing_ids = {node.id for node in self.full_graph().nodes}
        new_node_id = supersede_node_id(new_finding.id, existing_ids)
        old_node_id = supersede_node_id(finding_id, existing_ids)
        self._write_graph(
            _GraphDiff(
                nodes=[
                    GraphNode(
                        id=new_node_id,
                        label=new_finding.statement[:200],
                        type=NodeType.CLAIM,
                        confidence=new_finding.confidence,
                        data_class=new_finding.data_class,
                        metadata={
                            "knowledge_status": "superseding",
                            "version": new_finding.version,
                        },
                    )
                ],
                edges=[
                    GraphEdge(
                        id=f"supersedes-{new_finding.id}",
                        source=new_node_id,
                        target=old_node_id,
                        relation="SUPERSEDES",
                        confidence=new_finding.confidence,
                        data_class=new_finding.data_class,
                    )
                ],
            )
        )
        self._search.indices.refresh(index=FINDING_INDEX)
        return new_finding

    def claim_history(self, finding_id: str) -> list[Finding]:
        return self._seed.claim_history(finding_id)

    def rebuild_domain_graph(self) -> dict[str, int]:
        from scientific_tangle.services.graph_rebuilder import rebuild_semantic_graph

        self._ensure_ready()
        stats = rebuild_semantic_graph(self._driver)
        self._invalidate_graph_cache()
        return stats

    def close(self) -> None:
        self._driver.close()
        self._search.close()


def _class_values(allowed: set[DataClass] | None) -> list[str] | None:
    """``None`` — ограничений нет; пустой набор — не видно ничего.

    Ранее оба случая сливались в ``None`` через ``if allowed``, и роль без
    прав на классы получала весь корпус.
    """
    return None if allowed is None else sorted(item.value for item in allowed)


def _class_filter(allowed: set[DataClass] | None) -> list[dict[str, Any]]:
    if allowed is None:
        return []
    if not allowed:
        return [{"match_none": {}}]
    return [{"terms": {"data_class": [item.value for item in allowed]}}]


def _visible(data_class: DataClass, allowed: set[DataClass] | None) -> bool:
    return allowed is None or data_class in allowed


def _is_safe_relation(relation: str) -> bool:
    return bool(relation) and relation.replace("_", "").isalnum() and relation.upper() == relation


def _group_by_relation(edges: list[GraphEdge]) -> dict[str, list[GraphEdge]]:
    grouped: dict[str, list[GraphEdge]] = {}
    for edge in edges:
        grouped.setdefault(edge.relation, []).append(edge)
    return grouped


def _parse_node(item: dict[str, Any] | None) -> GraphNode | None:
    if not item or "id" not in item:
        return None
    try:
        node_type = NodeType(item.get("type", "material"))
    except ValueError:
        return None
    raw_metadata = item.get("metadata")
    try:
        metadata = (
            json.loads(raw_metadata) if isinstance(raw_metadata, str) else (raw_metadata or {})
        )
    except json.JSONDecodeError:
        metadata = {}
    try:
        data_class = DataClass(item.get("data_class") or "public")
    except ValueError:
        data_class = DataClass.PUBLIC
    return GraphNode(
        id=str(item["id"]),
        label=str(item.get("label") or item["id"]),
        type=node_type,
        confidence=float(item.get("confidence", 1)),
        data_class=data_class,
        metadata={
            key: value
            for key, value in metadata.items()
            if isinstance(value, str | int | float | bool)
        },
    )


def _parse_edge(item: dict[str, Any] | None) -> GraphEdge | None:
    if not item or not item.get("id"):
        return None
    try:
        data_class = DataClass(item.get("data_class") or "public")
    except ValueError:
        data_class = DataClass.PUBLIC
    return GraphEdge(
        id=str(item["id"]),
        source=str(item["source"]),
        target=str(item["target"]),
        relation=str(item["relation"]),
        confidence=float(item.get("confidence", 1)),
        data_class=data_class,
    )


def _embedding_text(finding: Finding) -> str:
    return f"{finding.statement} {' '.join(item.quote for item in finding.evidence)}"


def reciprocal_rank_fusion(
    ranked_lists: list[tuple[list[str], float]], k: int = 5
) -> dict[str, float]:
    """Взвешенный RRF: score = Σ weight / (k + rank)."""
    scores: dict[str, float] = {}
    for ranking, weight in ranked_lists:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + weight / (k + rank)
    return scores


def build_knowledge_base(settings: Settings) -> KnowledgeBase:
    if settings.knowledge_backend == "neo4j":
        return Neo4jElasticsearchKnowledgeBase(settings)
    return InMemoryKnowledgeBase()
