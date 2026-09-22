"""Видимость структурных находок и атомарность записи в production-контуре.

Живых Neo4j/Elasticsearch в тестовом контуре нет, поэтому контур проверяется на
подделках хранилищ: они записывают каждое обращение (текст Cypher, тело ES-запроса)
и отдают настроенные ответы. Это доказательство семантики, последовательности и
числа обращений к хранилищу, а не совместимости с реальным сервером — её проверяет
контейнерный контур.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    DocumentFragment,
    DocumentRequest,
    ExtractedClaim,
    ExtractedEntity,
    ExtractionResult,
    Finding,
    NodeType,
    RetrievalPlan,
)
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.services import infrastructure
from scientific_tangle.services.infrastructure import (
    CHUNK_INDEX,
    FINDING_INDEX,
    Neo4jElasticsearchKnowledgeBase,
)
from scientific_tangle.services.knowledge import (
    InMemoryKnowledgeBase,
    chunk_document,
    chunk_finding,
    stable_uuid,
)
from scientific_tangle.services.retrieval_semantics import SCOPE_GEOGRAPHY, SCOPE_YEAR

DOC_ID = uuid4()


# ── Подделки хранилищ ───────────────────────────────────────────────────────


def neo_node(
    node_id: str,
    label: str,
    node_type: str = "material",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Свойства узла ровно в том виде, который отдаёт ``properties(node)``."""
    return {
        "id": node_id,
        "label": label,
        "type": node_type,
        "confidence": 0.9,
        "data_class": "public",
        "metadata": json.dumps(metadata or {}, ensure_ascii=False),
    }


@dataclass
class FakeSession:
    driver: FakeDriver

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *exception: object) -> bool:
        return False

    def run(self, query: str, **params: Any) -> FakeResult:
        return FakeResult(self.driver.run(query, params))


@dataclass
class FakeResult:
    record: dict[str, Any] | None

    def single(self) -> dict[str, Any] | None:
        return self.record


@dataclass
class FakeDriver:
    """Диспетчер по маркерам запросов: реального Cypher-движка в тесте нет.

    Порядок проверок важен — «MERGE (n:Entity» встречается и в компенсации отката,
    поэтому маркеры взяты из уникальных подстрок конкретных запросов.
    """

    graph_nodes: list[dict[str, Any]] = field(default_factory=list)
    graph_edges: list[dict[str, Any]] = field(default_factory=list)
    anchors: list[dict[str, Any]] = field(default_factory=list)
    expanded: list[dict[str, Any]] = field(default_factory=list)
    traversed_edges: list[dict[str, Any]] = field(default_factory=list)
    document_exists: bool = False
    semantic_extracted: bool = False
    fail_on: tuple[str, ...] = ()
    queries: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    init_queries: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def session(self) -> FakeSession:
        return FakeSession(self)

    def close(self) -> None:
        return None

    def issued_containing(self, marker: str) -> list[tuple[str, dict[str, Any]]]:
        return [(query, params) for query, params in self.queries if marker in query]

    def run(self, query: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self.queries.append((query, params))
        for marker in self.fail_on:
            if marker in query:
                raise ConnectionError(f"Neo4j отверг запрос: {marker}")
        if "collect(properties(node))" in query:
            return {"nodes": self.graph_nodes, "total": len(self.graph_nodes)}
        if "type(rel) <> 'HAS_CHUNK'" in query:
            return {"edges": self.graph_edges}
        if "MATCH (anchor:Entity)" in query:
            return {"nodes": self.anchors}
        if "UNWIND $frontier AS fid" in query:
            return {"nodes": self.expanded, "reached": len(self.expanded)}
        if "UNWIND $ids AS id" in query:
            return {"edges": self.traversed_edges}
        if "coalesce(d.semantic_extracted, false)" in query:
            if self.semantic_extracted:
                return {"id": params["id"]}
            return None
        if "RETURN d.id AS id" in query:
            return {"id": params["id"]} if self.document_exists else None
        if "count(CASE WHEN n.type = 'publication'" in query:
            return {
                "documents": 0,
                "chunks": 0,
                "claims": 0,
                "entities": 0,
                "semantic_documents": 0,
            }
        if "RETURN count(n) AS count" in query:
            return {"count": 0}
        return None


@dataclass
class FakeIndices:
    existing: set[str] = field(default_factory=set)
    dims: int | None = 1024
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def exists(self, index: str) -> bool:
        return index in self.existing

    def create(self, index: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create", {"index": index, **kwargs}))
        self.existing.add(index)
        return {"acknowledged": True}

    def refresh(self, index: str | None = None) -> dict[str, Any]:
        self.calls.append(("refresh", {"index": index}))
        return {"_shards": {"total": 1}}

    def get_mapping(self, index: str) -> dict[str, Any]:
        self.calls.append(("get_mapping", {"index": index}))
        properties: dict[str, Any] = {}
        if self.dims is not None:
            properties["embedding"] = {"dims": self.dims}
        return {index: {"mappings": {"properties": properties}}}


@dataclass
class FakeES:
    indices: FakeIndices = field(default_factory=FakeIndices)
    hits: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    docs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    searches: list[dict[str, Any]] = field(default_factory=list)
    mgets: list[tuple[str, list[str]]] = field(default_factory=list)
    chunk_count: int = 0

    def search(self, **kwargs: Any) -> dict[str, Any]:
        self.searches.append(kwargs)
        index = str(kwargs["index"])
        return {"hits": {"hits": self.hits.get(index, [])}}

    def mget(self, index: str, ids: list[str]) -> dict[str, Any]:
        self.mgets.append((index, list(ids)))
        store = self.docs.get(index, {})
        return {
            "docs": [
                {
                    "_id": item,
                    "found": item in store,
                    "_source": store.get(item, {}),
                }
                for item in ids
            ]
        }

    def count(self, index: str | None = None, query: Any = None) -> dict[str, Any]:
        return {"count": self.chunk_count}

    def close(self) -> None:
        return None

    @property
    def request_count(self) -> int:
        return len(self.searches) + len(self.mgets)


@dataclass
class FakeHelpers:
    """``elasticsearch.helpers`` с журналом bulk-действий и отказом по индексу."""

    fail_indexes: set[str] = field(default_factory=set)
    scan_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    bulks: list[list[dict[str, Any]]] = field(default_factory=list)

    def bulk(self, client: Any, actions: Any, **kwargs: Any) -> tuple[int, list[Any]]:
        payload = list(actions)
        self.bulks.append(payload)
        for action in payload:
            if action.get("_index") in self.fail_indexes:
                raise RuntimeError(f"Elasticsearch отверг запись в {action['_index']}")
        return len(payload), []

    def scan(self, client: Any, index: str | None = None, **kwargs: Any) -> Any:
        yield from self.scan_rows.get(str(index), [])

    def actions(self, index: str) -> list[dict[str, Any]]:
        return [item for chunk in self.bulks for item in chunk if item.get("_index") == index]

    def reset(self) -> None:
        self.bulks.clear()


@dataclass
class FakeEmbeddings:
    dimensions: int = 1024
    queries: list[str] = field(default_factory=list)

    def query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [0.1] * self.dimensions

    def document(self, text: str) -> list[float]:
        return [0.1] * self.dimensions

    def documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self.dimensions for _ in texts]


class _GraphDatabaseStub:
    def __init__(self, driver: FakeDriver) -> None:
        self._driver = driver

    def driver(self, *args: Any, **kwargs: Any) -> FakeDriver:
        return self._driver


@dataclass
class Harness:
    knowledge: Neo4jElasticsearchKnowledgeBase
    es: FakeES
    driver: FakeDriver
    helpers: FakeHelpers


def build_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embeddings: FakeEmbeddings | None = None,
    dims: int | None = 1024,
    existing: tuple[str, ...] = (FINDING_INDEX, CHUNK_INDEX),
    fail_indexes: tuple[str, ...] = (),
    scan_rows: dict[str, list[dict[str, Any]]] | None = None,
    graph_nodes: list[dict[str, Any]] | None = None,
    graph_edges: list[dict[str, Any]] | None = None,
) -> Harness:
    """Собирает production-адаптер на подделках и прогревает его до первого запроса."""
    driver = FakeDriver(
        graph_nodes=graph_nodes or [],
        graph_edges=graph_edges or [],
    )
    es = FakeES(indices=FakeIndices(existing=set(existing), dims=dims))
    helpers = FakeHelpers(fail_indexes=set(fail_indexes), scan_rows=scan_rows or {})
    monkeypatch.setattr(infrastructure, "GraphDatabase", _GraphDatabaseStub(driver))
    monkeypatch.setattr(infrastructure, "Elasticsearch", lambda *args, **kwargs: es)
    monkeypatch.setattr(infrastructure, "helpers", helpers)
    knowledge = Neo4jElasticsearchKnowledgeBase(
        Settings(knowledge_backend="neo4j", gigachat_api_key=None)
    )
    if embeddings is not None:
        knowledge._embeddings = embeddings
    knowledge._ensure_ready()
    # Инициализация (схема, создание индексов, откат-запросы) видна отдельно от
    # запросов самого теста: иначе проверяющему не на что опереться.
    driver.init_queries = list(driver.queries)
    reset_logs(driver, es, helpers)
    return Harness(knowledge=knowledge, es=es, driver=driver, helpers=helpers)


def reset_logs(driver: FakeDriver, es: FakeES, helpers: FakeHelpers) -> None:
    driver.queries.clear()
    es.searches.clear()
    es.mgets.clear()
    helpers.reset()


def test_initialization_does_not_write_demo_seed_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Демо-сеятель остаётся каталогом процесса: его узлы не выдаются за знания корпуса."""
    harness = build_backend(monkeypatch)

    seed_markers = ("Шахтная вода", "Сульфаты", "Лаборатория водоподготовки")
    # FakeDriver хранит пары (запрос, параметры) — текст запроса первый элемент.
    written = "\n".join(
        str(item[0] if isinstance(item, tuple) else item)
        for item in harness.driver.init_queries
    )
    assert not any(marker in written for marker in seed_markers), (
        "узлы демо-сеятеля записываются в рабочий граф Neo4j"
    )


def make_finding(finding_id: str, statement: str) -> Finding:
    return Finding(
        id=finding_id,
        statement=statement,
        confidence=0.8,
        evidence=[
            {
                "document_id": DOC_ID,
                "source_title": "Отчёт по пилоту",
                "page": 3,
                "quote": statement[:40],
            }
        ],
        scope={"geography": "Мурманская область", "year": "2019"},
    )


def as_source(finding: Finding) -> dict[str, Any]:
    """Документ индекса: находка целиком лежит в ``finding_json``."""
    return {"finding_json": finding.model_dump_json()}


def as_hit(finding: Finding) -> dict[str, Any]:
    """Хит чанк-индекса вместе с находкой."""
    return {"_id": finding.id, "_source": as_source(finding)}


def chunk_findings(document: DocumentRequest) -> list[Finding]:
    # document_id выводится так же, как в продуктовом ingest: из checksum текста.
    checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
    document_id = stable_uuid(checksum)
    pieces = chunk_document(document, document_id)
    return [chunk_finding(document, document_id, piece) for piece in pieces]


def structural_document(text: str = "", title: str = "Отчёт по обессоливанию") -> DocumentRequest:
    body = text or (
        "Обратный осмос обеспечивает задержание солей 95–99 процентов при сухом остатке "
        "исходной воды до трёх тысяч миллиграммов на литр. Пилот на двух секциях подтвердил "
        "показатели на протяжении шестидесяти суток непрерывной работы модуля."
    )
    return DocumentRequest(
        title=title,
        text=body,
        year=2019,
        geography="Мурманская область",
        fragments=[DocumentFragment(text=body, page=7)],
    )


def query_plan(question: str = "обессоливание шахтных вод") -> QueryPlan:
    return QueryPlan(question=question, language="ru", mode="hybrid")


def retrieval_plan(
    query: str = "обратный осмос",
    *,
    semantic_query: str | None = None,
    use_global_context: bool = False,
    use_local_graph: bool = False,
) -> RetrievalPlan:
    return RetrievalPlan(
        lexical_query=query,
        semantic_query=semantic_query or query,
        entity_names=[],
        relation_types=["HAS_CHUNK", "SUPPORTED_BY"],
        max_hops=2,
        use_global_context=use_global_context,
        use_local_graph=use_local_graph,
    )


# ── 1. Чанки видимы в каталоге и версионируются (memory) ────────────────────


def test_memory_catalog_returns_chunks_and_supersedes_by_chunk_id() -> None:
    knowledge = InMemoryKnowledgeBase()
    document = structural_document()
    expected = [finding.id for finding in chunk_findings(document)]

    receipt = knowledge.index_document(document, "/data/sources/pilot.xlsx")
    listed = {finding.id for finding in knowledge.all_findings()}

    assert receipt.status == "created"
    assert receipt.chunks == len(expected) > 0
    assert all(identifier.startswith("chunk-") for identifier in expected)
    # Прежний дефект: чанков в каталоге нет, и версионирование по его id невозможно.
    assert set(expected) <= listed

    updated = knowledge.supersede_finding(expected[0], "Уточнённые условия пилота.", 0.9)
    after = {finding.id: finding for finding in knowledge.all_findings()}

    assert updated.id == f"{expected[0]}-v2"
    assert expected[0] not in after
    assert after[updated.id].statement == "Уточнённые условия пилота."
    assert [item.id for item in knowledge.claim_history(expected[0])][:2] == [
        expected[0],
        updated.id,
    ]


# ── 1. Чанки видимы в каталоге и версионируются (neo4j) ─────────────────────


def test_neo4j_index_document_publishes_chunks_to_index_and_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_backend(monkeypatch)
    document = structural_document()
    expected = [finding.id for finding in chunk_findings(document)]

    receipt = harness.knowledge.index_document(document, "/data/sources/pilot.docx")
    listed = {finding.id: finding for finding in harness.knowledge.all_findings()}

    assert receipt.status == "created"
    written = {action["_id"] for action in harness.helpers.actions(CHUNK_INDEX)}
    assert written == set(expected)
    assert set(expected) <= set(listed)
    assert listed[expected[0]].evidence[0].page == 7
    # Чанк живёт в CHUNK_INDEX: в индекс находок он не пишется и не должен там
    # появляться пустой копией.
    assert harness.helpers.actions(FINDING_INDEX) == []


def test_neo4j_restores_chunks_from_index_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Перезапуск контейнера: каталог пустой, данные — в индексах.

    ``all_findings()`` обязан видеть чанки и там, иначе версионирование структурных
    находок в интерфейсе исчезает без всякой ошибки.
    """
    stored = chunk_findings(structural_document())
    rows = {
        FINDING_INDEX: [{"_source": as_source(make_finding("f-1", "Тезис."))}],
        CHUNK_INDEX: [{"_source": as_source(finding)} for finding in stored],
    }

    harness = build_backend(monkeypatch, scan_rows=rows)
    listed = {finding.id for finding in harness.knowledge.all_findings()}

    assert {finding.id for finding in stored} <= listed
    assert "f-1" in listed

    # Тот же дефект задевал и восстановленные семантические находки: кэш процесса
    # их знает, каталог seed-адаптера — нет, и замена отвечала 409.
    updated = harness.knowledge.supersede_finding("f-1", "Уточнённый тезис.", 0.9)

    assert updated.id == "f-1-v2"
    assert "f-1-v2" in {finding.id for finding in harness.knowledge.all_findings()}


def test_neo4j_supersede_accepts_chunk_id_missing_from_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Регрессия прежнего дефекта: 409 на любой ``chunk-<uuid>``.

    Находка читается из CHUNK_INDEX, регистрируется в каталоге и только потом
    заменяется; заменённая копия помечается в том же индексе, иначе чанк-ветка
    выдачи возвращала бы отредактированный экспертом текст бессрочно.
    """
    stored = chunk_findings(structural_document())[0]
    harness = build_backend(monkeypatch)
    # Каталог процесса о чанке ничего не знает — только индекс.
    harness.es.docs = {
        CHUNK_INDEX: {stored.id: as_source(stored)},
        FINDING_INDEX: {},
    }

    updated = harness.knowledge.supersede_finding(stored.id, "Исправленный фрагмент.", 0.85)

    assert updated.id == f"{stored.id}-v2"
    assert [query for query, _ in harness.driver.issued_containing("SUPERSEDES")]
    marked = [
        action
        for action in harness.helpers.actions(CHUNK_INDEX)
        if action.get("_op_type") == "update"
    ]
    assert [action["_id"] for action in marked] == [stored.id]
    payload = Finding.model_validate_json(marked[0]["doc"]["finding_json"])
    assert payload.superseded_by == updated.id
    # Заменённая копия больше не попадает в выдачу чанк-ветки.
    harness.es.hits = {CHUNK_INDEX: [as_hit(payload)]}
    assert harness.knowledge.rank_findings("обратный осмос", 5, "lexical") == []


# ── 2. Каждый кандидат читается один раз ────────────────────────────────────


def test_retrieval_reads_every_candidate_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = chunk_findings(structural_document())[:2]
    harness = build_backend(monkeypatch)
    harness.es.hits = {
        FINDING_INDEX: [{"_id": "f-a"}, {"_id": "f-b"}],
        CHUNK_INDEX: [as_hit(finding) for finding in stored],
    }
    harness.es.docs = {
        FINDING_INDEX: {
            "f-a": as_source(make_finding("f-a", "Задержание солей 97 процентов.")),
            "f-b": as_source(make_finding("f-b", "Сухой остаток 800 мг/л.")),
        }
    }

    context = harness.knowledge.retrieve(query_plan(), retrieval_plan("обратный осмос"))

    chunk_ids = {finding.id for finding in stored}
    requested = {item for _, ids in harness.es.mgets for item in ids}
    # Лексика + чанки + один mget за остальными: за чанк-находками повторного
    # обращения к индексу находок быть не должно.
    assert [call["index"] for call in harness.es.searches] == [FINDING_INDEX, CHUNK_INDEX]
    assert harness.es.mgets == [(FINDING_INDEX, ["f-a", "f-b"])]
    assert harness.es.request_count == 3
    assert chunk_ids.isdisjoint(requested)
    assert {finding.id for finding in context.findings} == chunk_ids | {"f-a", "f-b"}


# ── 3. Атомарность импорта ──────────────────────────────────────────────────


def extraction() -> ExtractionResult:
    return ExtractionResult(
        entities=[
            ExtractedEntity(name="Мембрана", canonical_name="Мембрана", type=NodeType.MATERIAL)
        ],
        claims=[
            ExtractedClaim(
                subject="Мембрана",
                predicate="HAS_PROPERTY",
                object="Мембрана",
                statement="Мембрана задерживает 97% солей.",
                confidence=0.7,
                evidence_quote="задерживает 97% солей",
            )
        ],
    )


def test_neo4j_ingest_does_not_touch_graph_when_indexing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_backend(monkeypatch, fail_indexes=(FINDING_INDEX,))
    before_findings = {finding.id for finding in harness.knowledge.all_findings()}

    with pytest.raises(Exception, match="Elasticsearch отверг"):
        harness.knowledge.ingest(structural_document("Текст семантического импорта."), extraction())

    # Ни одного обращения к записи графа: узлы не пишутся, пока находки не в индексе.
    assert harness.driver.issued_containing("MERGE (n:Entity {id: row.id})") == []
    assert harness.driver.issued_containing("MERGE (a)-[r:") == []
    assert not harness.driver.issued_containing("SET d.semantic_extracted = true")
    assert {finding.id for finding in harness.knowledge.all_findings()} == before_findings


def test_neo4j_ingest_rolls_back_indexed_findings_when_graph_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_backend(monkeypatch)
    before_findings = {finding.id for finding in harness.knowledge.all_findings()}
    harness.driver.fail_on = ("MERGE (n:Entity {id: row.id})",)

    with pytest.raises(ConnectionError):
        harness.knowledge.ingest(structural_document("Текст семантического импорта."), extraction())

    deletes = [
        action
        for action in harness.helpers.actions(FINDING_INDEX)
        if action.get("_op_type") == "delete"
    ]
    assert deletes, "записанные до отказа находки обязаны быть сняты"
    assert all(action["_id"].startswith("finding-claim-") for action in deletes)
    # Созданные этим импортом элементы снимаются по метке записи, а не по id из
    # диффа: совпадающие сущности других документов остаются на месте.
    assert harness.driver.issued_containing("r.created_by = $tx DELETE r")
    assert harness.driver.issued_containing("n.created_by = $tx DETACH DELETE n")
    assert harness.driver.issued_containing("REMOVE d.semantic_extracted")
    assert {finding.id for finding in harness.knowledge.all_findings()} == before_findings


def test_neo4j_structural_import_rolls_back_on_either_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    es_only = build_backend(monkeypatch, fail_indexes=(CHUNK_INDEX,))
    with pytest.raises(Exception, match="Elasticsearch отверг"):
        es_only.knowledge.index_document(structural_document(), "/data/sources/a.docx")

    assert es_only.driver.issued_containing("MERGE (c:Entity {id: chunk.id})") == []
    chunks_left = [
        finding.id for finding in es_only.knowledge.all_findings() if "chunk-" in finding.id
    ]
    assert chunks_left == []

    graph_only = build_backend(monkeypatch)
    stored = chunk_findings(structural_document())
    graph_only.driver.fail_on = ("MERGE (c:Entity {id: chunk.id})",)

    with pytest.raises(ConnectionError):
        graph_only.knowledge.index_document(structural_document(), "/data/sources/a.docx")

    removed = {
        action["_id"]
        for action in graph_only.helpers.actions(CHUNK_INDEX)
        if action.get("_op_type") == "delete"
    }
    assert removed == {finding.id for finding in stored}
    assert graph_only.driver.issued_containing("DETACH DELETE d, c")
    assert {finding.id for finding in graph_only.knowledge.all_findings()}.isdisjoint(removed)


def test_memory_import_rollback_leaves_no_half_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Каталог и граф памяти: прерванное извлечение не оставляет следов.

    Половина тезисов в каталоге при отсутствии второй половины выглядит как
    состоявшийся импорт, и повторная попытка после отказа обязана пройти — иначе
    отказ превращается в «документ уже есть», а не в «документ не дописан».
    """
    knowledge = InMemoryKnowledgeBase()
    before_findings = {finding.id for finding in knowledge.all_findings()}
    before_nodes = [node.id for node in knowledge._graph.nodes]
    before_edges = [edge.id for edge in knowledge._graph.edges]
    before_documents = knowledge.document_count()
    original = InMemoryKnowledgeBase._locate_evidence
    calls: list[str] = []

    def broken(document: DocumentRequest, document_id: UUID, quote: str) -> Any:
        calls.append(quote)
        if len(calls) > 1:
            raise RuntimeError("извлечение прервано")
        return original(document, document_id, quote)

    monkeypatch.setattr(InMemoryKnowledgeBase, "_locate_evidence", staticmethod(broken))
    document = structural_document()
    extraction_result = ExtractionResult(
        entities=[
            ExtractedEntity(
                name="Мембрана", canonical_name="Мембрана", type=NodeType.MATERIAL
            )
        ],
        claims=[
            ExtractedClaim(
                subject="Мембрана",
                predicate="HAS_PROPERTY",
                object="Мембрана",
                statement="Первая находка записана.",
                confidence=0.7,
                evidence_quote="первая",
            ),
            ExtractedClaim(
                subject="Мембрана",
                predicate="HAS_PROPERTY",
                object="Мембрана",
                statement="Вторая находка прервана.",
                confidence=0.7,
                evidence_quote="вторая",
            ),
        ],
    )

    with pytest.raises(RuntimeError, match="извлечение прервано"):
        knowledge.ingest(document, extraction_result)

    assert len(calls) == 2
    assert {finding.id for finding in knowledge.all_findings()} == before_findings
    assert [node.id for node in knowledge._graph.nodes] == before_nodes
    assert [edge.id for edge in knowledge._graph.edges] == before_edges
    assert knowledge.document_count() == before_documents

    monkeypatch.undo()
    assert knowledge.ingest(document, extraction_result).status == "created"


# ── 3. Размерность эмбеддингов: явная деградация, а не тихий промах ─────────


def test_embedding_dimension_mismatch_disables_semantic_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_backend(monkeypatch, embeddings=FakeEmbeddings(dimensions=512), dims=1024)
    notes = harness.knowledge._startup_notes

    assert harness.knowledge._embeddings is None
    assert any(
        "Векторная ветка отключена" in note and "512" in note and "1024" in note
        for note in notes
    )
    # Деградация обязана называть, что именно перестроить: без этого оператор
    # читает «векторной ветки нет» как отсутствие настроенных эмбеддингов.
    assert any(FINDING_INDEX in note for note in notes)

    context = harness.knowledge.retrieve(
        query_plan(),
        retrieval_plan(semantic_query="мембранное обессоливание"),
    )
    assert any("Векторная ветка отключена" in reason for reason in context.degradation_reasons)
    assert context.vectors_used is False
    assert harness.es.searches and all("knn" not in call for call in harness.es.searches)


def test_missing_embedding_field_is_reported_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_backend(monkeypatch, dims=None)
    harness.knowledge._embeddings = FakeEmbeddings(dimensions=1024)
    harness.knowledge._reconcile_embedding_dimensions()

    assert harness.knowledge._embeddings is None
    assert any("поля embedding нет" in note for note in harness.knowledge._startup_notes)


# ── 4. Мёртвого fulltext-индекса в схеме нет ────────────────────────────────


def test_schema_creates_only_indexes_used_by_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_backend(monkeypatch)
    schema = [
        query
        for query, _ in harness.driver.init_queries
        if query.strip().startswith(("CREATE", "DROP"))
    ]

    assert len(schema) == 3
    assert not [query for query in schema if "FULLTEXT" in query.upper()]
    assert any("CREATE CONSTRAINT entity_id" in query for query in schema)
    assert any("REQUIRE n.id IS UNIQUE" in query for query in schema)
    assert any("CREATE INDEX entity_type" in query for query in schema)
    # Создание убрано, но в уже поднятой базе индекс остаётся: он снимается явно.
    assert any("DROP INDEX entity_label IF EXISTS" in query for query in schema)


# ── 5. ``use_global_context`` и ``semantic_query`` влияют на результат ──────


def test_neo4j_semantic_query_drives_the_vector_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embeddings = FakeEmbeddings(dimensions=1024)
    stored = chunk_findings(structural_document())[:1]
    harness = build_backend(monkeypatch, embeddings=embeddings)
    harness.es.hits = {
        FINDING_INDEX: [{"_id": "f-a"}],
        CHUNK_INDEX: [as_hit(finding) for finding in stored],
    }
    harness.es.docs = {
        FINDING_INDEX: {"f-a": as_source(make_finding("f-a", "Задержание солей мембраной."))}
    }

    context = harness.knowledge.retrieve(
        query_plan(),
        retrieval_plan(
            "осмос шахтная вода",
            semantic_query="мембранное обессоливание рассола",
        ),
    )

    assert embeddings.queries == ["мембранное обессоливание рассола"]
    knn_calls = [call for call in harness.es.searches if "knn" in call]
    assert len(knn_calls) == 1
    assert knn_calls[0]["knn"]["field"] == "embedding"
    assert context.vectors_used is True
    assert not [reason for reason in context.degradation_reasons if "Векторная ветка" in reason]


def test_neo4j_global_context_yields_briefs_and_names_the_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked = [neo_node("n-1", "Обратный осмос"), neo_node("n-2", "Шахтная вода")]
    edges = [
        {
            "id": "e-1",
            "source": "n-1",
            "target": "n-2",
            "relation": "TREATED_BY",
            "confidence": 0.9,
            "data_class": "public",
        }
    ]
    built = build_backend(monkeypatch, graph_nodes=linked, graph_edges=edges)

    context = built.knowledge.retrieve(
        query_plan(),
        retrieval_plan("обратный осмос", use_global_context=True),
    )

    assert context.community_summaries
    assert all("сущностей" in summary for summary in context.community_summaries)

    # Узлы без рёбер в кэш графа не попадают: сообществ нет — причина в ответе.
    isolated = build_backend(monkeypatch, graph_nodes=[neo_node("n-9", "Одиночная сущность")])
    silent = isolated.knowledge.retrieve(
        query_plan(),
        retrieval_plan("обратный осмос", use_global_context=True),
    )

    assert silent.community_summaries == []
    assert any(
        "глобальный контекст" in reason.lower() for reason in silent.degradation_reasons
    )

    skipped = built.knowledge.retrieve(
        query_plan(),
        retrieval_plan("обратный осмос", use_global_context=False),
    )
    assert skipped.community_summaries == []
    reasons = [reason.lower() for reason in skipped.degradation_reasons]
    assert not any("глобальный контекст" in reason for reason in reasons)


def test_neo4j_vector_leg_reports_request_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WrongDim(FakeEmbeddings):
        def query(self, text: str) -> list[float]:
            self.queries.append(text)
            return [0.5] * 7

    harness = build_backend(monkeypatch, embeddings=_WrongDim(dimensions=1024))

    context = harness.knowledge.retrieve(
        query_plan(),
        retrieval_plan("обратный осмос", semantic_query="обессоливание"),
    )

    assert [call for call in harness.es.searches if "knn" in call] == []
    assert any("размерность запроса 7" in reason for reason in context.degradation_reasons)


# ── Идентификатор чанка одинаков на обоих бэкендах ──────────────────────────


def test_chunk_id_and_locator_are_computed_once_for_both_backends() -> None:
    """Один и тот же документ даёт один и тот же ``chunk-<uuid>`` и смещения.

    Прод-контур строит узлы графа по этим id, каталог находок — по тем же, а
    смещения попадают ещё и в metadata узла. Расхождение сделало бы
    версионирование непроверяемым: id из ответа не совпал бы с id запроса.
    """
    document = structural_document()
    pieces = chunk_document(document, UUID(int=1))
    finding = chunk_finding(document, UUID(int=1), pieces[0])

    assert finding.id == pieces[0].id
    assert finding.evidence[0].char_start == 0
    assert finding.evidence[0].char_end == len(pieces[0].text[:1200])
    assert finding.scope == {SCOPE_YEAR: "2019", SCOPE_GEOGRAPHY: "Мурманская область"}
