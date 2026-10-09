"""Видимость структурных находок и атомарность записи в production-контуре.

Живых Neo4j/Elasticsearch в тестовом контуре нет, поэтому контур проверяется на
подделках хранилищ: они записывают каждое обращение (текст Cypher, тело ES-запроса)
и отдают настроенные ответы. Это доказательство семантики, последовательности и
числа обращений к хранилищу, а не совместимости с реальным сервером — её проверяет
контейнерный контур.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    AnswerPayload,
    DocumentFragment,
    DocumentRequest,
    ExtractedClaim,
    ExtractedEntity,
    ExtractionResult,
    Finding,
    GraphSnapshot,
    NodeType,
    RetrievalPlan,
)
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import QueryPlan
from scientific_tangle.services import durable_state as state_module
from scientific_tangle.services import infrastructure
from scientific_tangle.services import knowledge as knowledge_module
from scientific_tangle.services.infrastructure import (
    CHUNK_INDEX,
    FINDING_DOC_SCHEMA,
    FINDING_INDEX,
    FINDING_INDICES,
    Neo4jElasticsearchKnowledgeBase,
    _subject_filter_value,
)
from scientific_tangle.services.knowledge import (
    CHUNK_MIN_CHARS,
    InMemoryKnowledgeBase,
    NoChunksError,
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
    # Сколько совпадений действительно нашёл бы запрос: потолок якорей обязан
    # называться вслух, и проверить это можно только по числу «всего», а не по
    # длине возвращённого списка.
    anchor_matched: int = 0
    substring_anchors: list[dict[str, Any]] = field(default_factory=list)
    substring_matched: int = 0
    edges_matched: int = 0
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
            return {
                "edges": self.graph_edges,
                "total": self.edges_matched or len(self.graph_edges),
            }
        # Точная ветка якорей проверяется раньше общей: оба запроса содержат
        # ``MATCH (anchor:Entity)``, а различаются они именно способом совпадения.
        if "anchor.label IN $labels" in query:
            return {"nodes": self.anchors, "matched": self.anchor_matched or len(self.anchors)}
        if "MATCH (anchor:Entity)" in query:
            return {
                "nodes": self.substring_anchors,
                "matched": self.substring_matched or len(self.substring_anchors),
            }
        if "UNWIND $frontier AS fid" in query:
            return {"nodes": self.expanded, "reached": len(self.expanded)}
        if "UNWIND $ids AS id" in query:
            return {"edges": self.traversed_edges, "matched": len(self.traversed_edges)}
        if "coalesce(d.semantic_extracted, false)" in query:
            if self.semantic_extracted:
                return {"id": params["id"]}
            return None
        if "MERGE (d:Entity {id: $id})" in query and "d.type = 'publication'" in query:
            # Как в реальном Neo4j: после структурного MERGE узел документа есть,
            # и следующий поток обязан получить «duplicate», а не писать заново.
            self.document_exists = True
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


# Индексы, которые продукт вообще называет. Подделка проверяет по этому списку:
# запрос с именем, собранным по одному символу из «a,b», на реальном кластере
# читал бы и правил чужие данные, а молча проходит сквозь заглушку.
KNOWN_INDICES = {FINDING_INDEX, CHUNK_INDEX, FINDING_INDICES}


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
    # ``hits.total`` отдаётся только когда проверющий задал полное число: иначе
    # старые проверки читают ровно то же, что читали до появления окна каталога.
    total: int | None = None
    # Имена индексов вне продуктового списка — сигнал о том, что код обратился не
    # туда. Не чистится вместе с журналом запросов: обращение видно и на прогреве.
    violations: list[str] = field(default_factory=list)

    def search(self, **kwargs: Any) -> dict[str, Any]:
        self.searches.append(kwargs)
        index = str(kwargs["index"])
        if index not in KNOWN_INDICES:
            # Ошибку ловит сам продукт (отказ индекса — штатная деградация), поэтому
            # нарушение ещё и записывается: проверка может требовать пустого списка.
            self.violations.append(index)
            raise AssertionError(f"Запрос к неизвестному индексу: {index!r}")
        hits: dict[str, Any] = {"hits": self.hits.get(index, [])}
        if self.total is not None:
            hits["total"] = {"value": self.total, "relation": "eq"}
        return {"hits": hits}

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
    violations: list[str] = field(default_factory=list)

    def bulk(self, client: Any, actions: Any, **kwargs: Any) -> tuple[int, list[Any]]:
        payload = list(actions)
        self.bulks.append(payload)
        for action in payload:
            index = str(action.get("_index"))
            if index not in KNOWN_INDICES:
                self.violations.append(index)
                raise AssertionError(f"Запись в неизвестный индекс: {index!r}")
            if index in self.fail_indexes:
                raise RuntimeError(f"Elasticsearch отверг запись в {index}")
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


def thin_document() -> DocumentRequest:
    """Файл, который парсер пропускает, а чанкинг не порождает: 30 символов текста.

    Порог разбора — 20 символов суммарного текста, порог куска — 40. Документ из
    одного короткого фрагмента проходит в ``index_document`` и раньше возвращал
    ``created`` при нуле чанков.
    """
    body = "Песок 0,2 мм по гранулометрии."
    assert CHUNK_MIN_CHARS > len(body) >= 20
    return DocumentRequest(
        title="Заметка лаборатории",
        text=body,
        year=2021,
        geography="Мурманская область",
        fragments=[DocumentFragment(text=body, page=1)],
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


def test_memory_structural_import_refuses_document_without_chunks() -> None:
    """Импорт без чанков — отказ с причиной, а не «создано».

    Иначе файл считается покрытым (статистика корпуса, ``created`` в отчёте
    компиляции), но в окне поиска его нет: аналитик получает «в корпусе такого
    нет» на собственный текст.
    """
    knowledge = InMemoryKnowledgeBase()
    # Каталог memory-бэкенда стартует с демо-сеятелем: сравнивать надо с собой
    # до импорта, а не с пустотой.
    before = {finding.id for finding in knowledge.all_findings()}

    with pytest.raises(NoChunksError):
        knowledge.index_document(thin_document(), "/data/sources/note.docx")

    assert {finding.id for finding in knowledge.all_findings()} == before
    # Отказ не должен отравлять хранилище: следующий нормальный документ импортируется.
    receipt = knowledge.index_document(structural_document(), "/data/sources/pilot.docx")
    assert receipt.status == "created"
    assert receipt.chunks > 0


def test_neo4j_structural_import_refuses_document_without_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_backend(monkeypatch)
    before = {finding.id for finding in harness.knowledge.all_findings()}

    with pytest.raises(NoChunksError):
        harness.knowledge.index_document(thin_document(), "/data/sources/note.docx")

    # Ни записей в индексе, ни узлов в графе: пустой импорт не оставляет узла
    # документа, который статистика посчитала бы покрытием. В драйвере остаётся
    # только чтение дедуп-проверки.
    written = "\n".join(
        str(item[0] if isinstance(item, tuple) else item)
        for item in harness.driver.queries
    )
    assert "MERGE" not in written.upper()
    assert harness.helpers.actions(CHUNK_INDEX) == []
    assert {finding.id for finding in harness.knowledge.all_findings()} == before


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


# ── Копии на границе выдачи и кэши production-контура ───────────────────────


def test_retrieval_findings_are_copies_at_the_boundary() -> None:
    """Выдача retrieval — копии: правка тезиса в ответе не переписывает каталог.

    Прежний порядок копировал находки дважды — на ранжировании и на границе
    выдачи. Копия остаётся ровно одна (в ``finalize_retrieval``), поэтому и
    публичный ``rank_findings`` обязан отдавать отдельные объекты: вызывающий вне
    каталога вправе менять результат.
    """
    knowledge = InMemoryKnowledgeBase()
    context = knowledge.retrieve(
        QueryPlan(question="обратный осмос", language="ru", mode="hybrid"),
        retrieval_plan("обратный осмос"),
    )
    assert context.findings

    emitted = context.findings[0]
    assert emitted is not knowledge._findings[emitted.id]
    emitted.statement = "переписанный в ответе тезис"
    assert knowledge._findings[emitted.id].statement != "переписанный в ответе тезис"

    ranked = knowledge.rank_findings("обратный осмос", 3, "hybrid")
    assert ranked
    assert ranked[0] is not knowledge._findings[ranked[0].id]


def test_neo4j_semantic_ingest_does_not_walk_the_whole_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Импорт документа не строит полный снимок графа ни разу.

    Дифф «что добавилось после записи» брался из ``full_graph()`` трижды на
    документ: снимок строит глубокую копию всего графа и поднимает кластеризацию,
    то есть на предзагрузке корпуса стоимость росла квадратично. Теперь достаточно
    одного снимка состояния каталога — сами новые элементы берёт снимок «после».
    """
    harness = build_backend(monkeypatch)
    calls: list[object] = []
    original = InMemoryKnowledgeBase.full_graph

    def counting(self: InMemoryKnowledgeBase, allowed: object = None) -> object:
        calls.append(allowed)
        return original(self, allowed)  # type: ignore[arg-type]

    monkeypatch.setattr(InMemoryKnowledgeBase, "full_graph", counting)

    receipt = harness.knowledge.ingest(
        structural_document("Текст семантического импорта."), extraction()
    )

    assert receipt.status == "created"
    assert calls == []
    # Дедуп и содержимое каталога не изменились: снимок состояния — не замена записи.
    assert harness.driver.issued_containing("MERGE (n:Entity {id: row.id})")
    indexed = {action["_id"] for action in harness.helpers.actions(FINDING_INDEX)}
    assert indexed and all(identifier.startswith("finding-claim-") for identifier in indexed)


def test_neo4j_brief_cache_is_reused_and_dropped_by_a_graph_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Профили сообществ считаются один раз на неизменившийся граф и сбрасываются при записи.

    Счётчик собирает именно пересчёты `build_community_profiles`: он и был
    квадратичной ценой исследовательского запроса (до 24 действий на одном графе).
    Молчание счётчика на повторном retrieval доказывает кэш, а единица после
    записи — что кэш не пережил мутацию корпуса.
    """
    harness = build_backend(
        monkeypatch,
        graph_nodes=[neo_node("n-1", "Обратный осмос"), neo_node("n-2", "Шахтная вода")],
        graph_edges=[
            {
                "id": "e-1",
                "source": "n-1",
                "target": "n-2",
                "relation": "TREATED_BY",
                "confidence": 0.9,
                "data_class": "public",
            }
        ],
    )
    builds: list[int] = []
    original = knowledge_module.build_community_profiles

    def counting(nodes: Any, edges: Any, findings: Any = ()) -> list[Any]:
        builds.append(len(nodes))
        return original(nodes, edges, findings)

    monkeypatch.setattr(knowledge_module, "build_community_profiles", counting)
    question = query_plan()
    global_plan = retrieval_plan("обратный осмос", use_global_context=True)

    first = harness.knowledge.retrieve(question, global_plan).community_summaries
    assert first and all("сущностей" in brief for brief in first)
    assert builds == [2]

    second = harness.knowledge.retrieve(question, global_plan)
    assert second.community_summaries == first
    assert builds == [2]

    harness.driver.graph_nodes.append(neo_node("n-3", "Хибинетт файнштейн"))
    harness.driver.graph_edges.append(
        {
            "id": "e-2",
            "source": "n-3",
            "target": "n-1",
            "relation": "CONTAINS",
            "confidence": 0.9,
            "data_class": "public",
        }
    )
    harness.knowledge.index_document(structural_document(), "/data/sources/pilot.docx")

    after = harness.knowledge.retrieve(question, global_plan).community_summaries

    assert "Хибинетт" in "".join(after)
    assert len(builds) == 2


def test_graph_write_drops_the_llm_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Мутация корпуса обязана снимать и кэш structured output, а не только граф.

    Промпты модели собираются из текста корпуса (секции FINDINGS/COMMUNITIES
    рабочего процесса), поэтому закэшированный ответ устаревает вместе со
    снимком графа — иначе правка источника не отразилась бы в ответе ровно с
    тем же промптом. Проверка идёт настоящим `invalidate_llm_cache`: словарь
    кэша подменён целиком, поэтому чужие записи тест не затирает.
    """
    from scientific_tangle.services import provider

    harness = build_backend(monkeypatch)
    spy: list[int] = []
    original = provider.invalidate_llm_cache

    def counted() -> None:
        spy.append(1)
        original()

    monkeypatch.setattr(provider, "invalidate_llm_cache", counted)
    monkeypatch.setattr(
        provider, "_llm_cache", OrderedDict({"тест-ключ": ('{"ok": true}', time.monotonic() + 600)})
    )

    harness.knowledge.index_document(structural_document(), "/data/sources/pilot.docx")

    assert spy and len(provider._llm_cache) == 0  # noqa: SLF001 - подменённый кэш самого модуля


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
    # Индекс по метке создаётся ровно потому, что точная ветка подбора якорей
    # (`anchor.label IN $labels`) его читает: «индекс, который никто не запрашивает»
    # — это расходы на запись и ложное впечатление настроенного поиска.
    assert any("CREATE INDEX entity_label" in query for query in schema)
    assert any("ON (n.label)" in query for query in schema)
    assert not [query for query in schema if query.strip().startswith("DROP")]


def test_anchor_lookup_prefers_indexed_exact_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """Точное совпадение метки закрывает якоря без поиска подстрокой.

    Подстрока (`CONTAINS`) не обслуживается ни одним индексом Neo4j, поэтому она
    идёт второй веткой и только когда точных совпадений не хватило до потолка.
    """
    harness = build_backend(monkeypatch)
    # Ровно потолок MAX_ANCHORS точных совпадений: только в этом случае запасная
    # ветка поиска подстрокой не нужна по построению обхода.
    harness.driver.anchors = [neo_node(f"n-{index}", f"Метка {index}") for index in range(5)]

    nodes, notes = harness.knowledge._traverse(
        retrieval_plan("обратный осмос", use_local_graph=True), None
    )

    exact = harness.driver.issued_containing("anchor.label IN $labels")
    substring = harness.driver.issued_containing("toLower(anchor.label) CONTAINS")
    assert exact and len(nodes.nodes) == 5
    assert substring == [], "поиск подстрокой не нужен, когда точных якорей хватило до потолка"
    # План не назвал сущностей, поэтому обход стартует от формулировки вопроса, и
    # происхождение якорей называется вслух даже здесь: читатель ответа обязан
    # отличать «сущности из графа» от «якорь = текст вопроса».
    assert notes == [
        "Якоря обхода взяты из формулировки вопроса: план не назвал сущностей."
    ]


def test_anchor_truncation_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Потолок MAX_ANCHORS называется вслух: часть якорей — потерянные связи."""
    harness = build_backend(monkeypatch)
    harness.driver.anchors = [neo_node("n-1", "Обратный осмос")]
    harness.driver.anchor_matched = 40

    _, notes = harness.knowledge._traverse(
        retrieval_plan("обратный осмос", use_local_graph=True), None
    )

    assert any("MAX_ANCHORS" in note and "40" in note for note in notes)


def test_full_graph_edge_ceiling_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Рёбра полного снимка ограничены потолком, и достижение его видно в ответе.

    Узлы были ограничены 600 с честной заметкой; рёбра читались «сколько есть».
    Проверка держит обе пары: потолок не должен отменить прежнюю честность.
    """
    harness = build_backend(monkeypatch, graph_nodes=[neo_node("n-1", "Обратный осмос")])
    harness.driver.graph_edges = [
        {
            "id": f"e-{index}",
            "source": "n-1",
            "target": "n-1",
            "relation": "CONTAINS",
            "confidence": 0.9,
            "data_class": "public",
        }
        for index in range(3)
    ]
    harness.driver.edges_matched = 5000

    harness.knowledge.full_graph()
    notes = harness.knowledge._graph_window_notes()

    assert any("GRAPH_EDGE_LIMIT" in note and "5000" in note for note in notes)
    context = harness.knowledge.retrieve(query_plan(), retrieval_plan("обратный осмос"))
    assert any("GRAPH_EDGE_LIMIT" in reason for reason in context.degradation_reasons)


def test_full_graph_note_names_nodes_the_traversal_actually_sees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Раскрытие обязано называть узлы обхода, а не размер сырого среза.

    Живой замер 2026-10-02 (`.agents/graph-ceiling-probe.py`, 1501 узел на
    одноразовом контуре): потолок срезал 600, а фильтр связности оставил 120 —
    заметка при этом говорила «600 из 1501», то есть недооценивала потерю в пять раз
    ровно там, где её надо назвать.
    """
    harness = build_backend(
        monkeypatch,
        graph_nodes=[
            neo_node("n-1", "Обратный осмос"),
            neo_node("n-2", "Шахтная вода"),
            neo_node("n-3", "Сульфаты"),
        ],
    )
    harness.driver.graph_edges = [
        {
            "id": "e-1",
            "source": "n-1",
            "target": "n-2",
            "relation": "CONTAINS",
            "confidence": 0.9,
            "data_class": "public",
        }
    ]

    snapshot = harness.knowledge.full_graph()
    notes = harness.knowledge._graph_window_notes()

    # Третий узел не связан — обход его не видит.
    assert {node.id for node in snapshot.nodes} == {"n-1", "n-2"}
    assert any("узлов 2 из 3" in note for note in notes), notes



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
    assert finding.evidence[0].char_end == len(finding.evidence[0].quote)
    assert finding.scope == {SCOPE_YEAR: "2019", SCOPE_GEOGRAPHY: "Мурманская область"}


def test_chunk_quote_supports_numbers_beyond_the_old_excerpt_limit() -> None:
    from scientific_tangle.agents.workflow import _ungrounded_answer_numbers, _ungrounded_numbers
    from scientific_tangle.domain.contracts import DocumentFragment, ReasoningResult

    text = "Описание процесса. " * 90 + "Температура сушки составляет 110 °C."
    document = structural_document().model_copy(
        update={"text": text, "fragments": [DocumentFragment(text=text, page=1)]}
    )
    piece = chunk_document(document, UUID(int=1))[0]
    finding = chunk_finding(document, UUID(int=1), piece)

    assert finding.evidence[0].quote == finding.statement
    assert finding.evidence[0].char_end == len(finding.statement)
    assert _ungrounded_numbers([finding]) == {}
    assert _ungrounded_answer_numbers(
        ReasoningResult(summary="Температура сушки — 110 °C."), [finding]
    ) == []
    assert _ungrounded_answer_numbers(
        ReasoningResult(summary="Температура сушки — 111 °C."), [finding]
    ) == ["111"]


# ─ 6. Каталог читается окном, а не целиком (узкое место 4) ─────────────────


def test_neo4j_findings_window_pushes_filters_into_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Фильтры списка уходят предикатами запроса, а не вырезаются из окна.

    Маршруты ``/findings`` и ``/conflicts`` читают окно хранилища, поэтому
    ``status`` и ``subject`` обязан исполнять индекс. Субъект ищется подстрокой без
    регистра — в точности как in-memory-отбор, иначе два контура начали бы отвечать
    на один и тот же фильтр по-разному.
    """
    harness = build_backend(monkeypatch)
    harness.es.hits = {FINDING_INDICES: []}

    harness.knowledge.findings_window(limit=5, offset=0, status="disputed", subject="обесс*")

    filters = harness.es.searches[-1]["query"]["bool"]["filter"]
    assert {"term": {"status": "disputed"}} in filters
    wildcard = next(item["wildcard"]["subject"] for item in filters if "wildcard" in item)
    # Маски из строки пользователя экранируются: «обесс*» — буквальные символы.
    assert wildcard["value"] == "*обесс\\**"
    assert wildcard["case_insensitive"] is True


def test_neo4j_facts_window_reads_only_structured_supported_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fact = make_finding("f-fact", "Обессоливание задерживает 97 процентов солей.").model_copy(
        update={
            "subject": "Обессоливание",
            "predicate": "HAS_PROPERTY",
            "object": "97 процентов",
        }
    )
    harness = build_backend(monkeypatch)
    harness.es.hits = {FINDING_INDEX: [{"_id": fact.id, "_source": as_source(fact)}]}
    harness.es.total = 1

    window = harness.knowledge.facts_window(limit=10, offset=0, subject="обесс")
    call = harness.es.searches[-1]
    filters = call["query"]["bool"]["filter"]

    assert call["index"] == FINDING_INDEX
    assert {"exists": {"field": "subject"}} in filters
    assert {"exists": {"field": "predicate"}} in filters
    assert {"exists": {"field": "object"}} in filters
    assert {"exists": {"field": "evidence"}} in filters
    assert [item.id for item in window.findings] == [fact.id]
    assert window.total == 1


def test_subject_filter_ignores_case_on_both_sides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Регистр строки отбора не должен решать, найдёт ли индекс русскую запись.

    `case_insensitive` у wildcard по keyword в Elasticsearch складывается по ASCII:
    на живом контуре «*Мембрана*» находило записи, а «*мемб*» — ноль, то есть
    фильтр аналитика на кириллице молча терял находки. Поэтому регистр снимается
    и в строке запроса, и в записываемом поле отбора (`_subject_filter_value`),
    а не оставляется анализатору.
    """
    harness = build_backend(monkeypatch)
    harness.es.hits = {FINDING_INDICES: []}

    harness.knowledge.findings_window(limit=5, subject="ОбЕсс")

    filters = harness.es.searches[-1]["query"]["bool"]["filter"]
    wildcard = next(item["wildcard"]["subject"] for item in filters if "wildcard" in item)
    assert wildcard["value"] == "*обесс*"
    assert _subject_filter_value("Мембрана обратного осмоса") == "мембрана обратного осмоса"
    assert _subject_filter_value(None) == ""


def test_memory_findings_window_applies_the_same_filters() -> None:
    """In-memory-контур отвечает на те же фильтры тем же набором находок."""
    knowledge = InMemoryKnowledgeBase()
    first = make_finding("f-a", "Задержание солей 97 процентов.").model_copy(
        update={"status": "disputed"}
    )
    second = make_finding("f-b", "Сухой остаток 800 мг/л.").model_copy(
        update={"subject": "Обессоливание"}
    )
    knowledge._findings.update({first.id: first, second.id: second})

    disputed = knowledge.findings_window(status="disputed")
    obess = knowledge.findings_window(subject="обесс")

    # Срез seed-каталога в in-memory контуре намеренно остаётся в выдаче, поэтому
    # проверяется предикат, а не состав списка целиком.
    assert first.id in [finding.id for finding in disputed.findings]
    assert second.id not in [finding.id for finding in disputed.findings]
    assert all(finding.status == "disputed" for finding in disputed.findings)
    assert second.id in [finding.id for finding in obess.findings]
    assert all("обесс" in (finding.subject or "").lower() for finding in obess.findings)
    both = knowledge.findings_window(status="disputed", subject="обесс")
    assert all(
        finding.status == "disputed" and "обесс" in (finding.subject or "").lower()
        for finding in both.findings
    )


def test_neo4j_findings_window_reads_the_page_from_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Страница каталога приходит из Elasticsearch запросом с окном и сортировкой.

    Прежний путь отдавал списку маршруту глубокие копии всего RAM-каталога, а сам
    каталог поднимался из индексов целиком. Здесь проверяется именно окно: смещение
    и размер уходят в запрос, порядок задан детерминированно, а наружу копируется
    страница.
    """
    first = make_finding("f-a", "Задержание солей 97 процентов.")
    harness = build_backend(monkeypatch)
    harness.es.hits = {FINDING_INDICES: [{"_id": first.id, "_source": as_source(first)}]}
    harness.es.total = 137

    window = harness.knowledge.findings_window(limit=1, offset=2)
    call = harness.es.searches[-1]

    assert call["index"] == FINDING_INDICES
    assert call["size"] == 1 and call["from_"] == 2
    assert call["sort"] == [{"_seq_no": {"order": "asc"}}]
    assert {"exists": {"field": "superseded_by"}} in call["query"]["bool"]["must_not"]
    assert {"term": {"scope.origin": "demo"}} in call["query"]["bool"]["must_not"]
    assert [finding.id for finding in window.findings] == [first.id]
    assert (window.offset, window.limit, window.total) == (2, 1, 137)
    assert window.findings[0] is not first


def test_neo4j_findings_window_applies_access_class_and_reports_short_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Окно не отдаёт то, что скрыто политикой, и честно говорит о длине страницы.

    Заменённая версия и демо-контент могут лежать в индексах, проиндексированных
    до появления фильтруемых полей, — тогда их снимает процесс, и страница
    обязана назвать причину, а не выглядеть молча короткой.
    """
    public = make_finding("f-public", "Задержание солей 97 процентов.")
    restricted = make_finding("f-secret", "Энергия в 3–5 раз выше.")
    restricted = restricted.model_copy(update={"data_class": DataClass.RESTRICTED})
    replaced = make_finding("f-old", "Прежняя версия тезиса.").model_copy(
        update={"superseded_by": "f-new"}
    )
    harness = build_backend(monkeypatch)
    harness.es.hits = {
        FINDING_INDICES: [
            {"_id": public.id, "_source": as_source(public)},
            {"_id": restricted.id, "_source": as_source(restricted)},
            {"_id": replaced.id, "_source": as_source(replaced)},
        ]
    }
    harness.es.total = 3

    window = harness.knowledge.findings_window(limit=3, offset=0)

    assert [finding.id for finding in window.findings] == [public.id, restricted.id]
    assert window.total == 3
    assert window.note and "limit" in window.note
    # Класс доступа проверяется и на стороне процесса: hidden-класс не имеет права
    # просочиться в окно, если индекс его не отфильтровал.
    hidden = harness.knowledge.findings_window(
        limit=3, offset=0, allowed_data_classes={DataClass.RESTRICTED}
    )
    assert [finding.id for finding in hidden.findings] == [restricted.id]
    empty = harness.knowledge.findings_window(limit=3, offset=0, allowed_data_classes=set())
    assert empty.findings == []


def test_legacy_index_docs_get_window_filters_backfilled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Старые записи volume добирают поля, которыми окно режет выдачу.

    Индексы, созданные до появления ``scope``/``superseded_by``, этих полей не
    содержат: запрос окна не может отсечь демо-запись, первая страница пустеет
    (запись снимается только на стороне процесса), а полное число считается по
    предикату индекса — завышенно. Правка точечная: служебные поля, без текста и
    без вектора, и только по продуктовым индексам.
    """
    demo = make_finding("f-demo", "Обратный осмос обеспечивает удаление солей.")
    demo = demo.model_copy(
        update={"scope": {**demo.scope, "origin": "demo"}, "subject": "Обессоливание шахтной воды"}
    )
    replaced = make_finding("f-old", "Прежняя версия тезиса.").model_copy(
        update={"superseded_by": "f-new"}
    )
    plain = make_finding("f-plain", "Сухой остаток 800 мг/л.").model_copy(update={"scope": {}})
    harness = build_backend(monkeypatch)
    harness.es.hits = {
        FINDING_INDEX: [as_hit(demo), as_hit(replaced), as_hit(plain)],
    }
    harness.helpers.reset()

    harness.knowledge._backfill_window_filters()

    actions = {action["_id"]: action for action in harness.helpers.actions(FINDING_INDEX)}
    # Метка формы документа ставится всем старым записям: иначе выборка «кого
    # чинить» на каждом старте совпадала бы с частично дописанными полями.
    assert set(actions) == {demo.id, replaced.id, plain.id}
    assert actions[demo.id]["_op_type"] == "update"
    assert actions[demo.id]["doc"]["scope"] == {
        "geography": "Мурманская область",
        "year": "2019",
        "origin": "demo",
    }
    assert actions[replaced.id]["doc"]["superseded_by"] == "f-new"
    # Поле отбора по субъекту дописывается уже в нижнем регистре: регистр
    # сохраняется в `finding_json`, а фильтр обязан находить русскую строку.
    assert actions[demo.id]["doc"]["subject"] == "обессоливание шахтной воды"
    # У записи без размеченных условий поля отсечения не появляются — но статус и
    # метка формы дописываются, и субъект фильтрации тоже.
    assert "scope" not in actions[plain.id]["doc"]
    assert actions[plain.id]["doc"]["status"] == plain.status
    assert all(
        action["doc"]["schema"] == infrastructure.FINDING_DOC_SCHEMA
        for action in actions.values()
    )
    # Выборка «кого чинить» — по метке формы, а не по её отсутствию: `term` не
    # совпадает и с записью без поля, поэтому одна клазура покрывает и совсем
    # старые записи, и записи предыдущей формы. Правка по `exists: schema` не
    # трогала ни одной записи со старой меткой, и поднятие `FINDING_DOC_SCHEMA`
    # не мигрировало ничего (проверено живым прогоном на одноразовом контуре:
    # окно по «мемб» = 0 против 4 в каталоге до правки предиката).
    assert [call["query"] for call in harness.es.searches] == [
        {"bool": {"must_not": [{"term": {"schema": FINDING_DOC_SCHEMA}}]}}
    ] * 2
    assert harness.es.violations == []
    assert harness.helpers.violations == []


def test_neo4j_findings_window_falls_back_to_catalog_on_index_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отказ индекса — деградация с причиной, а не пустой список находок."""
    harness = build_backend(monkeypatch)
    stored = make_finding("f-catalog", "Сухой остаток 800 мг/л.")
    harness.knowledge._merge_findings([stored])

    def broken(**kwargs: Any) -> dict[str, Any]:
        harness.es.searches.append(kwargs)
        raise ConnectionError("Elasticsearch недоступен")

    monkeypatch.setattr(harness.es, "search", broken)

    window = harness.knowledge.findings_window(limit=10, offset=0)

    assert window.note and "Elasticsearch недоступен" in window.note
    assert stored.id in [finding.id for finding in window.findings]


def test_memory_findings_window_pages_and_copies_only_the_page() -> None:
    """Окно memory-контура: тот же контракт, копия — только у возвращаемой страницы."""
    knowledge = InMemoryKnowledgeBase()
    knowledge.index_document(structural_document(), "/data/sources/pilot.xlsx")
    everything = sorted(finding.id for finding in knowledge.all_findings())

    window = knowledge.findings_window(limit=2, offset=1)

    assert window.total == len(everything) > 2
    assert [finding.id for finding in window.findings] == everything[1:3]
    for finding in window.findings:
        assert finding is not knowledge._findings[finding.id]
    finding = window.findings[0]
    finding.statement = "переписанный в окне тезис"
    assert knowledge._findings[finding.id].statement != "переписанный в окне тезис"

    # Ноль и отрицательное смещение не читают «сколько повезёт».
    clamped = knowledge.findings_window(limit=0, offset=-5)
    assert (clamped.limit, clamped.offset) == (1, 0)
    assert knowledge.findings_window(limit=10 ** 6).limit == knowledge_module.FINDINGS_WINDOW_MAX


# ─ 7. Гонки и производные кэши на обеих ветках ─────────────────────────────


def test_memory_write_drops_the_llm_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory-контур обязан снимать кэш structured output так же, как рабочий.

    Иначе в тестовом и локальном запуске закэшированный ответ модели переживает
    импорт документа, и проверка «ответ изменился после правки источника»
    расходится с поведением neo4j-контура.
    """
    from scientific_tangle.services import provider

    spy: list[int] = []
    original = provider.invalidate_llm_cache

    def counted() -> None:
        spy.append(1)
        original()

    monkeypatch.setattr(provider, "invalidate_llm_cache", counted)
    monkeypatch.setattr(
        provider, "_llm_cache", OrderedDict({"тест-ключ": ('{"ok": true}', time.monotonic() + 600)})
    )

    knowledge = InMemoryKnowledgeBase()
    knowledge.index_document(structural_document(), "/data/sources/pilot.docx")

    assert spy and len(provider._llm_cache) == 0  # noqa: SLF001 - подменённый кэш самого модуля


# ─ 8. Серверное состояние: потолок на аккаунт и очистка чекпоинтов ─────────


def stored_answer(seed: int) -> AnswerPayload:
    """Минимальный payload для проверок буферизации: содержимое не важно, важен id."""
    question = f"Вопрос номер {seed}"
    return AnswerPayload(
        query_id=UUID(int=seed + 1),
        question=question,
        summary="Проверка потолка серверных копий.",
        query_plan=QueryPlan(question=question, language="ru", mode="hybrid"),
        findings=[],
        conflicts=[],
        knowledge_gaps=[],
        recommendations=[],
        graph=GraphSnapshot(nodes=[], edges=[], communities=[]),
        trace=[],
        confidence=0.8,
        model_mode="scripted",
    )


def test_in_memory_answer_ceiling_is_per_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Чужая активность не съедает историю аналитика: потолок считается на владельца.

    Прежний общий лимит в 200 копий на весь сервис означал 404 на экспорте
    собственного ответа при ~20 активных пользователях.
    """
    monkeypatch.setattr(state_module, "MAX_STORED_ANSWERS", 2)
    state = state_module.InMemoryDurableState()

    async def scenario() -> None:
        for index in range(5):
            await state.put_answer(stored_answer(index), owner_id="analyst-a")
        for index in range(5, 9):
            await state.put_answer(stored_answer(index), owner_id="analyst-b")
        kept_a = [
            await state.get_answer(str(UUID(int=index + 1))) is not None for index in range(5)
        ]
        kept_b = [
            await state.get_answer(str(UUID(int=index + 1))) is not None for index in range(5, 9)
        ]
        return kept_a, kept_b

    kept_a, kept_b = asyncio.run(scenario())

    assert kept_a == [False, False, False, True, True]
    assert kept_b == [False, False, True, True]
    assert len(state._answers) == 4  # noqa: SLF001 - проверка размера журнала


@pytest.mark.anyio
async def test_recent_answers_are_paged_and_scoped_to_owner() -> None:
    state = state_module.InMemoryDurableState()
    for index, owner in enumerate(("analyst-a", "analyst-b", "analyst-a")):
        await state.put_answer(stored_answer(index), owner_id=owner)

    page = await state.recent_answers(owner_id="analyst-a", limit=1, offset=1)

    assert [item.answer.question for item in page] == ["Вопрос номер 0"]


def test_in_memory_total_ceiling_still_bounds_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Общий предохранитель остаётся: журнал не растётся числом учётных записей."""
    monkeypatch.setattr(state_module, "MAX_STORED_ANSWERS", 50)
    monkeypatch.setattr(state_module, "MAX_STORED_ANSWERS_TOTAL", 3)
    state = state_module.InMemoryDurableState()

    async def scenario() -> list[str]:
        for index in range(8):
            await state.put_answer(stored_answer(index), owner_id=f"analyst-{index}")
        return [item.owner_id for item in state._answers.values()]  # noqa: SLF001

    owners = asyncio.run(scenario())

    assert len(owners) == 3
    # Вытесняется самое давнее обращение — то есть страдают первые записи, а не
    # последние, которые читатель держит открытыми.
    assert owners == ["analyst-5", "analyst-6", "analyst-7"]


def test_recently_read_answer_survives_the_trim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ответ, перечитанный секунду назад, не вытесняется следующим же ответом.

    Проверка закрывает исходный дефект: аналитик жмёт «экспорт» по старому
    запросу, а trim снимает именно его копию как старейшую вставленную.
    """
    monkeypatch.setattr(state_module, "MAX_STORED_ANSWERS", 2)
    state = state_module.InMemoryDurableState()

    async def scenario() -> tuple[bool, bool]:
        await state.put_answer(stored_answer(1), owner_id="a")
        await state.put_answer(stored_answer(2), owner_id="a")
        # Перечитали самый старый: он перестаёт быть кандидатом на вытеснение.
        assert await state.get_answer(str(UUID(int=2))) is not None
        await state.put_answer(stored_answer(3), owner_id="a")
        return (
            await state.get_answer(str(UUID(int=2))) is not None,
            await state.get_answer(str(UUID(int=3))) is None,
        )

    survived, evicted = asyncio.run(scenario())

    assert survived, "перечитанный ответ обязан пережить trim"
    assert evicted, "вытеснить надо самое давнее обращение"


def test_postgres_answer_trim_is_scoped_to_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Рабочая ветка: SQL вытеснения идёт по владельцу, а не по таблице целиком."""
    state = state_module.PostgresDurableState(Settings(accounts_backend="memory"))
    statements: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_execute(query: str, params: Any = ()) -> int:
        statements.append(("write", query, tuple(params or ())))
        return 0

    async def fake_fetchone(query: str, params: Any = ()) -> dict[str, Any] | None:
        statements.append(("read", query, tuple(params or ())))
        if query.lstrip().startswith("SELECT query_id, owner_id"):
            now = datetime.now(UTC)
            return {
                "query_id": "q-1",
                "owner_id": "analyst-a",
                "answer_json": stored_answer(1).model_dump_json(),
                "created_at": now,
                "expires_at": now + timedelta(hours=1),
            }
        return None

    state._execute = fake_execute  # type: ignore[assignment]
    state._fetchone = fake_fetchone  # type: ignore[assignment]

    async def scenario() -> None:
        await state.put_answer(stored_answer(1), owner_id="analyst-a")
        await state.get_answer("q-1")

    asyncio.run(scenario())

    trims = [item for item in statements if "PARTITION BY owner_id" in item[1]]
    assert trims and trims[0][2][0] == "analyst-a"
    assert trims[0][2][1] == state_module.MAX_STORED_ANSWERS
    global_trims = [
        item
        for item in statements
        if "DELETE FROM nk_answers WHERE" in item[1]
        and "NOT IN" in item[1]
        and "ORDER BY created_at" in item[1]
    ]
    assert not global_trims, (
        "глобальный trim по дате вставки больше не снимает чужие ответы"
    )
    touches = [item for item in statements if "SET last_accessed_at" in item[1]]
    assert touches and touches[0][2][1] == "q-1"


def test_in_memory_checkpoint_purge_reports_nothing_to_clean() -> None:
    """Память честно говорит, что чекпоинтов у неё нет, вместо AttributeError."""
    state = state_module.InMemoryDurableState()

    async def scenario() -> state_module.CheckpointPurgeReport:
        return await state.purge_stale_checkpoint_threads()

    report = asyncio.run(scenario())

    assert report.removed_threads == 0 and report.skipped_reason


def test_postgres_checkpoint_purge_touches_only_stale_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Удаление по тредам, чей ПОСЛЕДНИЙ чекпоинт старше срока, и по дочерним таблицам.

    Свежий тред (в том числе упавший на середине прогона час назад) удалён быть не
    может: порог считан по ``max(ts)`` группы, а не по отдельной записи.
    """
    state = state_module.PostgresDurableState(Settings(accounts_backend="memory"))
    statements: list[tuple[str, tuple[Any, ...]]] = []

    async def fake_execute(query: str, params: Any = ()) -> int:
        statements.append((query, tuple(params or ())))
        return 2

    async def fake_fetchall(query: str, params: Any = ()) -> list[dict[str, Any]]:
        statements.append((query, tuple(params or ())))
        return [{"thread_id": "thread-stale", "last_seen": None}]

    async def fake_fetchone(query: str, params: Any = ()) -> dict[str, Any] | None:
        statements.append((query, tuple(params or ())))
        return {"table_name": "checkpoints"}

    state._execute = fake_execute  # type: ignore[assignment]
    state._fetchall = fake_fetchall  # type: ignore[assignment]
    state._fetchone = fake_fetchone  # type: ignore[assignment]

    async def scenario() -> state_module.CheckpointPurgeReport:
        return await state.purge_stale_checkpoint_threads(older_than=timedelta(days=7))

    report = asyncio.run(scenario())

    assert (report.removed_threads, report.removed_rows) == (1, 6)
    joined = " ".join(query for query, _ in statements)
    assert "max((checkpoint->>'ts')::timestamptz) < %s" in joined
    assert "DELETE FROM checkpoint_writes WHERE thread_id = ANY(%s)" in joined
    assert "DELETE FROM checkpoint_blobs WHERE thread_id = ANY(%s)" in joined
    assert "DELETE FROM checkpoints WHERE thread_id = ANY(%s)" in joined
    # Дочерние таблицы снимаются раньше самого треда.
    order = [index for index, (query, _) in enumerate(statements) if query.startswith("DELETE")]
    writes = next(
        index for index, (query, _) in enumerate(statements) if "checkpoint_writes" in query
    )
    deleted = next(
        index
        for index, (query, _) in enumerate(statements)
        if query.startswith("DELETE FROM checkpoints")
    )
    assert writes < deleted and len(order) == 3


def test_checkpoint_cleanup_loop_survives_a_failed_wave() -> None:
    """Волна очистки не должна ронять планировщик: следующий цикл по расписанию."""
    calls: list[timedelta] = []

    class FailingState:
        async def purge_stale_checkpoint_threads(
            self, *, older_than: timedelta, batch_threads: int
        ) -> state_module.CheckpointPurgeReport:
            del batch_threads
            calls.append(older_than)
            raise ConnectionError("база не ответила")

    async def scenario() -> bool:
        task = asyncio.create_task(
            state_module.run_checkpoint_cleanup(
                FailingState(),  # type: ignore[arg-type]
                interval_seconds=1.0,
                older_than=timedelta(days=3),
            )
        )
        await asyncio.sleep(1.3)
        alive = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return alive

    alive = asyncio.run(scenario())

    assert alive, "после сбоя цикл обязан остаться живым"

