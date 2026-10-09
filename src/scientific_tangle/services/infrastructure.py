from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
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
from scientific_tangle.domain.hypotheses import HypothesisSignal, HypothesisWindow
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import NumericObservation, QueryPlan
from scientific_tangle.services.communities import (
    DEFAULT_PROFILE_LIMIT,
    detect_communities,
)
from scientific_tangle.services.embeddings import EmbeddingClient
from scientific_tangle.services.governance import AccessPolicyEngine
from scientific_tangle.services.knowledge import (
    CHUNK_MIN_CHARS,
    MAX_ANCHORS,
    ChunkPiece,
    CommunityBriefCache,
    FindingWindow,
    InMemoryKnowledgeBase,
    KnowledgeBase,
    KnowledgeState,
    NoChunksError,
    RetrievalContext,
    brief_key,
    cached_community_briefs,
    chunk_document,
    chunk_finding,
    finalize_retrieval,
    finding_communities,
    graph_element_ids,
    invalidate_derived_llm_cache,
    normalize_window,
    stable_uuid,
    supersede_node_id,
)
from scientific_tangle.services.provider import redact_provider_error
from scientific_tangle.services.reranking import query_tokens, rerank_findings
from scientific_tangle.services.retrieval_semantics import (
    DEMO_ORIGIN,
    RETRIEVAL_TOP_K,
    SCOPE_GEOGRAPHY,
    SCOPE_ORIGIN,
    SCOPE_YEAR,
    AbortCheck,
    abort_note,
    candidate_window,
    exclude_demo,
    global_context_notes,
    is_aborted,
    is_demo_finding,
)

logger = logging.getLogger(__name__)

FINDING_INDEX = "mindai-findings-v2"
CHUNK_INDEX = "mindai-chunks-v2"
HYPOTHESIS_INDEX = "mindai-hypotheses-v1"
# Потолок одноразовой правки полей отсечения окна (см. _backfill_window_filters):
# за один процесс чинится ограниченное число старых записей, остальное допишется
# путём обычной записи в индекс.
WINDOW_FILTER_BACKFILL_LIMIT = 2000
# Ключи условий применимости, которые живут отдельными полями индекса: ими режет
# окно каталога и retrieval-фильтры. Один набор на запись и на починку старых
# записей — расхождение этих двух мест и пустовало аналитику первую страницу.
WINDOW_SCOPE_KEYS = {SCOPE_GEOGRAPHY, SCOPE_YEAR, SCOPE_ORIGIN}
# Форма документа индекса. Починка старых записей ищет тех, у кого метки нет, и
# дописывает все выводимые из ``finding_json`` поля разом: без метки «кого чинить»
# пришлось бы угадывать по отдельным полям, и выборка снова разошлась бы с тем,
# что читает окно.
FINDING_DOC_SCHEMA = 3
# Подстрока субъекта ищется по keyword-полю: значения длиннее потолка Elasticsearch
# в индекс не кладёт, и фильтр их не увидит. Субъекты — имена сущностей, а не
# абзацы текста; потолок назван, чтобы это было видно в коде, а не в тикетах.
SUBJECT_FILTER_KEYWORD_MAX = 1024
GRAPH_CACHE_TTL_SECONDS = 30.0
GRAPH_NODE_LIMIT = 600
# Рёбра полного снимка графа раньше читались без потолка: узлов ограничивали 600,
# а рёбер — сколько есть. На плотном корпусе это самая дорогая часть загрузки
# снимка, и её достижение раскрывается так же честно, как потолок узлов.
GRAPH_EDGE_LIMIT = 3000
MAX_TRAVERSAL_EDGES = 2000
# RAM-каталог процесса нужен только там, где без цельного списка не обойтись:
# резервная ветка ``_load_findings`` и версионирование. Списочные маршруты читают
# окно из Elasticsearch (``findings_window``), поэтому восстановление каталога
# ограничено, а не «сколько лежало в индексе».
CATALOG_RESTORE_LIMIT = 20000
# Шаг окна каталога по умолчанию — тот же порядок, что у списочных маршрутов API.
FINDINGS_WINDOW_DEFAULT = 50
FINDINGS_WINDOW_MAX = 500
# Окна чтения находок идут через оба индекса одним запросом: чанки живут в своём,
# и список находок интерфейса обязан показывать и те и другие.
FINDING_INDICES = f"{FINDING_INDEX},{CHUNK_INDEX}"
# Анализатор живёт под ключом `analysis`: без него Elasticsearch читает настройки
# как `index.analyzer.ru.type` и отклоняет создание индекса с
# illegal_argument_exception — на memory-бэкенде это не видно вообще.
RUS_ANALYSIS = {
    "analysis": {
        "analyzer": {"ru": {"type": "russian", "stopwords": "_russian_"}},
    },
}


# ── Cypher обхода графа ─────────────────────────────────────────────────────
#
# Каждый запрос содержит ORDER BY до LIMIT/cut: без него «первые N» выбираются в
# порядке обхода хранилища, и один и тот же вопрос на тех же данных возвращает
# разные подграфы от запуска к запуску. Потолки параметризуются, а достижение
# потолка раскрывается в degradation_reasons — скрытой потери данных нет.
MAX_TRAVERSAL_NODES = 200

# Якорный поиск разделён на две ветки. Точное совпадение метки — единственный
# случай, который обслуживает индекс по ``label`` (range-индекс Neo4j не ускоряет
# ``CONTAINS``), и он идёт первым: на корпусе из сотен документов это seek по
# индексу вместо полного скана всех узлов. Поиск по подстроке остаётся запасной
# веткой — честная поддержка «части имени» дороже молчаливой потери якорей.
ANCHOR_EXACT_CYPHER = """
MATCH (anchor:Entity)
WHERE anchor.label IN $labels
  AND anchor.type <> 'chunk'
  AND ($classes IS NULL OR coalesce(anchor.data_class, 'public') IN $classes)
WITH anchor
ORDER BY coalesce(anchor.data_class, 'public'), anchor.label, anchor.id
WITH collect(anchor)[0..$anchors] AS picked, count(anchor) AS matched
RETURN [a IN picked | properties(a)] AS nodes, matched
"""

ANCHOR_PREDICATES = """anchor.type <> 'chunk'
  AND any(
    name IN $entities
    WHERE toLower(anchor.label) CONTAINS toLower(name)
      OR toLower(name) CONTAINS toLower(anchor.label)
  )
  AND ($classes IS NULL OR coalesce(anchor.data_class, 'public') IN $classes)"""

# Ограничение обязано быть на стороне движка (`LIMIT`), а не срезом над `collect()`:
# срез собирает в память все совпадения и только потом выбрасывает лишнее, а окно
# обхода от этого не становится меньше. `matched` считается отдельным проходом,
# иначе усечение нельзя честно назвать вслух.
ANCHORS_CYPHER = f"""
MATCH (anchor:Entity)
WHERE {ANCHOR_PREDICATES}
WITH count(anchor) AS matched
MATCH (anchor:Entity)
WHERE {ANCHOR_PREDICATES}
WITH anchor, matched
ORDER BY coalesce(anchor.data_class, 'public'), anchor.label, anchor.id
LIMIT $anchors
WITH collect(anchor) AS picked, max(matched) AS matched
RETURN [a IN picked | properties(a)] AS nodes, matched
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
WITH collect({id: rel.id, source: a.id, target: b.id, relation: type(rel),
              confidence: rel.confidence,
              data_class: coalesce(rel.data_class, 'public')})[0..$edges] AS edges,
     count(*) AS matched
RETURN edges, matched
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
        # Каталог seed-адаптера отдаётся без глубокого копирования: инициализация
        # процесса не должна оплачивать копию каждой находки.
        self._findings: dict[str, Finding] = dict(self._seed.finding_catalog())
        self._driver: Driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
        )
        self._search = Elasticsearch(settings.elasticsearch_url)
        self._embeddings: EmbeddingClient | None = None
        self._initialized = False
        # RLock: ``_ensure_ready`` вызывается из методов, которые уже держат замок
        # состояний (``all_findings`` → ``_ensure_ready`` → ``_restore_findings``),
        # и обычный Lock умер бы на повторном входе того же потока.
        # Дисциплина замка: ``self._lock`` охраняет только поля этого процесса
        # (каталог, кэш графа, поколения, окна, счётчики) и никогда не удерживается
        # сквозь Neo4j, Elasticsearch или эмбеддинги — иначе паралельный импорт
        # одного документа заблокировал бы чтение всего контура на секунды сети.
        self._lock = threading.RLock()
        # Замок на документ: дедуп импорта проверяется до записи, а пишет после,
        # и без повторной проверки под этим замком два аналитика, загрузившие один
        # файл, проходили дедуп вместе и удваивали работу по записи графа.
        # Ключей ровно столько, сколько документов завел процесс (потолок корпуса —
        # ``corpus_preload_limit``), поэтому словари не чистят: вытеснение замка
        # под чужим ожиданием дало бы ровно ту гонку, от которой он спасает.
        self._doc_locks: dict[str, threading.Lock] = {}
        self._graph_cache: GraphSnapshot | None = None
        self._graph_cached_at = 0.0
        # Поколение графа: растёт в той же точке, где сбрасывается кэш полного
        # графа, и входит в ключ кэша сводок сообществ.
        self._graph_epoch = 0
        self._brief_cache = CommunityBriefCache()
        self._vectors_indexed = 0
        self._vectors_missing = 0
        # Потолок выборки графа — не скрытая потеря данных: (показано, всего).
        self._graph_window: tuple[int, int] | None = None
        self._graph_edge_window: tuple[int, int] | None = None
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
            self._ensure_hypothesis_index()
            self._restore_findings()
            self._backfill_window_filters()
            # Демо-сеятель остаётся каталогом процесса (supersede, история версий):
            # его узлы не пишутся в Neo4j — фиктивные сущности не должны выдаваться
            # за знания корпуса в якорях, сообществах и графе интерфейса.
            self._initialized = True

    def warmup(self) -> None:
        """Схема Neo4j, индексы Elasticsearch, каталог и починка старых записей.

        Всё это лениво поднималось на первом обращении, и первый аналитик платил
        за него секундами ожидания при полностью исправном контуре. Здесь тот же
        путь, только вызванный стартом процесса.
        """
        self._ensure_ready()

    def _ensure_neo4j_schema(self) -> None:
        """Индексы, которые исполнители запросов действительно используют.

        ``entity_label`` нужен точной ветке подбора якорей (``anchor.label IN
        $labels``): единственный случай якорного поиска, который обслуживается
        индексом. ``CONTAINS`` по подстроке range-индекс не ускоряет, поэтому
        точная ветка и вынесена отдельно: она перестаёт платить полным сканом
        всех узлов, а поиск подстрокой остаётся запасным.
        Существующий индекс того же имени пересоздавать нечем: ``IF NOT EXISTS``
        идемпотентен, а ``DROP`` снял бы индекс у поднятой до этого базы.
        """
        with self._driver.session() as session:
            session.run(
                "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE"
            )
            session.run("CREATE INDEX entity_type IF NOT EXISTS FOR (n:Entity) ON (n.type)")
            session.run("CREATE INDEX entity_label IF NOT EXISTS FOR (n:Entity) ON (n.label)")

    def _document_lock(self, key: str) -> threading.Lock:
        """Замок на один документ (или на пару «документ + вид импорта»)."""
        with self._lock:
            return self._doc_locks.setdefault(key, threading.Lock())

    def _snapshot_findings(self) -> dict[str, Finding]:
        """Снимок каталога процесса (ссылки на модели, не копии)."""
        with self._lock:
            return dict(self._findings)

    def _publish_findings(self, findings: Mapping[str, Finding]) -> None:
        with self._lock:
            self._findings = dict(findings)

    def _merge_findings(self, findings: Iterable[Finding]) -> None:
        with self._lock:
            for finding in findings:
                self._findings[finding.id] = finding

    def _note_startup(self, note: str) -> None:
        with self._lock:
            self._startup_notes.append(note)
        logger.error(note)

    def _count_vectors(self, *, indexed: int = 0, missing: int = 0) -> None:
        with self._lock:
            self._vectors_indexed += indexed
            self._vectors_missing += missing

    def _ensure_es_indices(self) -> None:
        """Создаёт индексы, если их нет; существующие не удаляются — preload-данные
        должны переживать перезапуск контейнера."""
        finding_mappings = {
            "properties": {
                "statement": {"type": "text", "analyzer": "ru"},
                "status": {"type": "keyword"},
                # Субъект ищется подстрокой (так работает отбор списка), поэтому
                # keyword, а не текст с анализатором: поиск по токенам потерял бы
                # «осмос» в «обратный осмос-М». Потолок — ignore_above: значение
                # длиннее потолка в фильтр не попадёт.
                "subject": {"type": "keyword", "ignore_above": SUBJECT_FILTER_KEYWORD_MAX},
                "predicate": {"type": "keyword"},
                "object": {"type": "keyword", "ignore_above": SUBJECT_FILTER_KEYWORD_MAX},
                # Версия формы документа: по ней починка старых записей понимает,
                # что доводить нечего, и не перебирает индекс на каждом старте.
                "schema": {"type": "short"},
                "data_class": {"type": "keyword"},
                "confidence": {"type": "float"},
                "evidence": {"type": "text", "analyzer": "ru"},
                "scope.geography": {"type": "keyword"},
                "scope.year": {"type": "keyword"},
                "scope.origin": {"type": "keyword"},
                # ``superseded_by`` отдельным полем: оконное чтение каталога обязано
                # отсечь заменённые версии на стороне индекса, иначе страница выдачи
                # зависела бы от того, что случайно лежит в RAM-каталоге процесса.
                "superseded_by": {"type": "keyword"},
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
                # Те же поля отсечения и отбора, что у семантических тезисов:
                # окно читается по обоим индексам одним запросом, и поле, которого
                # нет в одном из них, молча исключает его записи из выдачи.
                "status": {"type": "keyword"},
                "subject": {"type": "keyword", "ignore_above": SUBJECT_FILTER_KEYWORD_MAX},
                "schema": {"type": "short"},
                "data_class": {"type": "keyword"},
                "source_path": {"type": "keyword"},
                "document_id": {"type": "keyword"},
                "scope.geography": {"type": "keyword"},
                "scope.year": {"type": "keyword"},
                "scope.origin": {"type": "keyword"},
                "superseded_by": {"type": "keyword"},
                "finding_json": {"type": "keyword", "index": False},
            }
        }
        if self._search.indices.exists(index=FINDING_INDEX):
            # Размерность векторов сверяется до первого knn-запроса: поиск вектором
            # другого размера Elasticsearch отклоняет на каждой ветке, и retrieval
            # тихо терял бы семантическую ветку вместо явной деградации.
            self._reconcile_embedding_dimensions()
            self._extend_index_mapping(FINDING_INDEX, finding_mappings)
        else:
            self._search.indices.create(
                index=FINDING_INDEX, settings=RUS_ANALYSIS, mappings=finding_mappings
            )
            for finding in self._snapshot_findings().values():
                self._index_finding(finding)
        if not self._search.indices.exists(index=CHUNK_INDEX):
            self._search.indices.create(
                index=CHUNK_INDEX, settings=RUS_ANALYSIS, mappings=chunk_mappings
            )
        else:
            self._extend_index_mapping(CHUNK_INDEX, chunk_mappings)

    def _ensure_hypothesis_index(self) -> None:
        if self._search.indices.exists(index=HYPOTHESIS_INDEX):
            return
        self._search.indices.create(
            index=HYPOTHESIS_INDEX,
            settings=RUS_ANALYSIS,
            mappings={
                "properties": {
                    "id": {"type": "keyword"},
                    "kind": {"type": "keyword"},
                    "statement": {"type": "text", "analyzer": "ru"},
                    "statement_sort": {"type": "keyword", "ignore_above": 2048},
                    "data_class": {"type": "keyword"},
                    "signal_json": {"type": "keyword", "index": False},
                }
            },
        )

    def _extend_index_mapping(self, index: str, mappings: dict[str, Any]) -> None:
        """Дописывает новые поля в уже созданный индекс.

        Поднятие сервиса не чинит чужой volume молча: поле, объявленное в коде,
        но отсутствующее в поднятом индексе, Elasticsearch вывел бы динамической
        схемой (``text`` + ``.keyword``), и фильтр по субъекту читал бы не то поле.
        Добавление полей — операция аддитивная, существующие значения не трогаются,
        а отказ не считается деградацией: окно продолжит работать по старым полям.
        """
        try:
            self._search.indices.put_mapping(
                index=index,
                properties={
                    key: value
                    for key, value in mappings["properties"].items()
                    if key in {"status", "subject", "schema", "scope.geography", "scope.year"}
                },
            )
        except Exception as error:  # noqa: BLE001 - старые поля не теряются из-за новых
            logger.error("Индекс %s не принял дополнительные поля: %s", index, error)

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
        self._embeddings = None
        self._note_startup(note)

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

        Каталог процесса ограничен ``CATALOG_RESTORE_LIMIT``. Он нужен ровно двум
        вещам — резервной ветке ``_load_findings`` (seed-находки и id, которые не
        вернулись из mget) и сверке версионирования; постраничный список читается
        окном из Elasticsearch (``findings_window``). Держать в RAM весь корпус
        ради этого — платить памятью за данные, которые уже лежат в индексе, а
        недогрузку каталога контур обязан назвать вслух, а не выдавать за пустоту.
        """
        restored: dict[str, Finding] = {}
        budget = max(CATALOG_RESTORE_LIMIT, 0)
        outside_catalog = 0
        for index in (FINDING_INDEX, CHUNK_INDEX):
            if not self._search.indices.exists(index=index):
                continue
            try:
                stored = int(self._search.count(index=index)["count"])
            except Exception as error:  # noqa: BLE001 - размер индекса узнаётся по возможности
                logger.warning("Размер индекса %s не получен: %s", index, error)
                stored = 0
            if budget <= 0:
                outside_catalog += stored
                continue
            read = 0
            try:
                for hit in helpers.scan(
                    self._search,
                    index=index,
                    query={"query": {"match_all": {}}},
                    _source=["finding_json"],
                ):
                    read += 1
                    if read > budget:
                        break
                    payload = (hit.get("_source") or {}).get("finding_json")
                    if not payload:
                        continue
                    finding = Finding.model_validate_json(payload)
                    if finding.id not in self._findings and finding.id not in restored:
                        restored[finding.id] = finding
            except Exception as error:  # noqa: BLE001 - старт не должен падать на чтении
                self._note_degradation(f"restore_findings:{index}", error)
                continue
            budget -= read
            outside_catalog += max(stored - read, 0)
        if restored:
            self._merge_findings(restored.values())
        if outside_catalog:
            note = (
                f"Каталог процесса поднят не полностью: записей вне RAM-каталога "
                f"{outside_catalog} (потолок CATALOG_RESTORE_LIMIT={CATALOG_RESTORE_LIMIT}). "
                "Список находок и его окно читаются напрямую из Elasticsearch, а резервная "
                "ветка загрузки находок по id видит только поднятую часть корпуса."
            )
            logger.warning(note)
            with self._lock:
                self._startup_notes.append(note)
        logger.info("Восстановлено findings из ES: %d", len(restored))

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

    def ingest(
        self,
        document: DocumentRequest,
        extraction: ExtractionResult,
        *,
        source_document_id: UUID | None = None,
        semantic_part_id: str | None = None,
        semantic_part_total: int | None = None,
    ) -> DocumentReceipt:
        self._ensure_ready()
        checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        part_mode = source_document_id is not None
        if part_mode != (semantic_part_id is not None and semantic_part_total is not None):
            raise ValueError("Для частичного импорта нужны все идентификаторы semantic part")
        document_id = source_document_id or stable_uuid(checksum)
        # TOCTOU дедупа закрывает замок на документ: проверка ``semantic_extracted``
        # и запись исполняются в одной критической секции. Второй аналитик,
        # загрузивший тот же файл, встаёт в ожидание и получает «duplicate» от уже
        # записанного узла вместо второй половины графа и отката чужого импорта.
        # Сетевые вызовы остаются под ЭТИМ замком: сериализуются импорты одного
        # документа, а не весь контур — общий ``self._lock`` при этом держится
        # только на мутациях RAM-состояния.
        with self._document_lock(f"semantic:{document_id}"):
            return self._ingest_locked(
                document,
                extraction,
                checksum,
                document_id,
                semantic_part_id=semantic_part_id,
                semantic_part_total=semantic_part_total,
            )

    def semantic_parts_completed(self, source_document_id: UUID, part_total: int) -> set[str]:
        self._ensure_ready()
        with self._driver.session() as session:
            record = session.run(
                "MATCH (d:Entity {id: $id}) "
                "RETURN d.semantic_part_total AS total, "
                "d.semantic_parts_completed AS parts",
                id=f"document-{source_document_id}",
            ).single()
        if not record:
            return set()
        stored_total = record.get("total")
        if stored_total is not None and int(stored_total) != part_total:
            raise ValueError("Состав semantic parts документа изменился между запусками")
        return {str(item) for item in (record.get("parts") or [])}

    def _ingest_locked(
        self,
        document: DocumentRequest,
        extraction: ExtractionResult,
        checksum: str,
        document_id: UUID,
        *,
        semantic_part_id: str | None = None,
        semantic_part_total: int | None = None,
    ) -> DocumentReceipt:
        with self._driver.session() as session:
            already = session.run(
                "MATCH (d:Entity {id: $id}) "
                "WHERE coalesce(d.semantic_extracted, false) RETURN d.id AS id",
                id=f"document-{document_id}",
            ).single()
        if already and semantic_part_id is None:
            return DocumentReceipt(
                document_id=document_id,
                checksum=checksum,
                status="duplicate",
                extracted_claims=0,
            )
        if semantic_part_id is not None:
            completed = self.semantic_parts_completed(document_id, semantic_part_total or 0)
            if semantic_part_id in completed:
                return DocumentReceipt(
                    document_id=document_id,
                    checksum=checksum,
                    status="duplicate",
                    extracted_claims=0,
                )

            # Убирает незавершённую попытку той же части и старый полный preload
            # перед миграцией на part IDs. Это закрывает окно падения после записи
            # графа, но до durable-маркера, а также повтор старой выборочной загрузки.
            part_tx = f"ingest-{stable_uuid(f'{document_id}:semantic:{semantic_part_id}')}"
            self._clear_ingest_attempt(part_tx)
            if already and not completed:
                legacy_tx = f"ingest-{stable_uuid(f'{document_id}:semantic:full')}"
                self._clear_ingest_attempt(legacy_tx)

        # Один снимок каталога до записи вместо трёх проходов по графу:
        # ``full_graph()`` здесь использовался только ради множеств id, но он строит
        # копию всего графа и поднимает кластеризацию — на предзагрузке корпуса из
        # сотен документов это квадратичная стоимость. Метка ``community`` в
        # metadata узла при этом не теряется: её пересчитывает
        # ``_load_graph_from_neo4j`` на каждом чтении графа, а не импорт.
        state = self._seed.snapshot_state()
        before_nodes, before_edges = graph_element_ids(state.nodes, state.edges)
        before_findings = set(state.findings)
        new_ids: list[str] = []
        part_key = semantic_part_id or "full"
        tx_id = f"ingest-{stable_uuid(f'{document_id}:semantic:{part_key}')}"

        receipt = self._seed.ingest(
            document,
            extraction,
            source_document_id=document_id if semantic_part_id is not None else None,
            semantic_part_id=semantic_part_id,
            semantic_part_total=semantic_part_total,
        )
        if receipt.status == "duplicate":
            return receipt

        try:
            # Порядок «ES → Neo4j» выбран из-за отката: идентификаторы находок
            # известны заранее, поэтому компенсация удаляет ровно записанное
            # (delete по id). Для графа точный список созданных элементов даёт только
            # метка ``created_by`` — она и снимается в ``_roll_back_ingest``.
            current = dict(self._seed.finding_catalog())
            new_ids = [key for key in current if key not in before_findings]
            self._index_findings([current[key] for key in new_ids])
            self._search.indices.refresh(index=FINDING_INDEX)
            after = self._seed.snapshot_state()
            self._write_graph(
                _GraphDiff(
                    nodes=[node for node in after.nodes if node.id not in before_nodes],
                    edges=[edge for edge in after.edges if edge.id not in before_edges],
                ),
                tx_id=tx_id,
            )
            self._merge_findings([current[key] for key in new_ids])
            if semantic_part_id is None:
                with self._driver.session() as session:
                    session.run(
                        "MATCH (d:Entity {id: $id}) SET d.semantic_extracted = true",
                        id=f"document-{receipt.document_id}",
                    )
            else:
                with self._driver.session() as session:
                    session.run(
                        "MATCH (d:Entity {id: $id}) "
                        "WITH d, coalesce(d.semantic_parts_completed, []) AS previous "
                        "WITH d, CASE WHEN $part IN previous THEN previous "
                        "ELSE previous + $part END AS completed "
                        "SET d.semantic_parts_completed = completed, "
                        "d.semantic_part_total = $total, "
                        "d.semantic_extracted = size(completed) >= $total",
                        id=f"document-{receipt.document_id}",
                        part=semantic_part_id,
                        total=semantic_part_total,
                    )
        except Exception as error:
            # Атомарность импорта: прерванный импорт не вправе оставлять узлы графа,
            # находки каталога и половину ES-документов — иначе «есть ли у числа
            # источник» проверяется по неполному корпусу и отвечает неправду.
            # Остаток, который этот контур не гарантирует: процесс, убитый между
            # записью ES и записью графа, оставит находки без узлов (трассировка к
            # документу сохраняется, обход графа их не увидит).
            self._roll_back_ingest(
                receipt.document_id,
                state,
                new_ids,
                tx_id,
                semantic_part_id=semantic_part_id,
            )
            logger.error("Импорт %s откачен: %s", receipt.document_id, error)
            raise
        return receipt

    def _clear_ingest_attempt(self, tx_id: str) -> None:
        with self._driver.session() as session:
            record = session.run(
                "MATCH (n:Entity {created_by: $tx, type: 'claim'}) "
                "RETURN collect(n.id) AS claim_ids",
                tx=tx_id,
            ).single()
            raw_claim_ids = record.get("claim_ids") if record is not None else []
            claim_ids = [str(item) for item in (raw_claim_ids or [])]
            session.run(
                "MATCH ()-[r]->() WHERE r.created_by = $tx DELETE r",
                tx=tx_id,
            )
            session.run(
                "MATCH (n:Entity) WHERE n.created_by = $tx DETACH DELETE n",
                tx=tx_id,
            )
        if claim_ids:
            finding_ids = [f"finding-{claim_id}" for claim_id in claim_ids]
            helpers.bulk(
                self._search,
                [
                    {"_index": FINDING_INDEX, "_id": item, "_op_type": "delete"}
                    for item in finding_ids
                ],
                refresh=True,
                raise_on_error=False,
                raise_on_exception=False,
            )
            with self._lock:
                for finding_id in finding_ids:
                    self._findings.pop(finding_id, None)

    def _roll_back_ingest(
        self,
        document_id: UUID,
        state: KnowledgeState,
        new_ids: Sequence[str],
        tx_id: str,
        *,
        semantic_part_id: str | None = None,
    ) -> None:
        """Откатывает каталог, seed-состояние, находки ES и созданные элементы графа."""
        self._seed.restore_state(state)
        # Снимаются ровно id этого импорта, а не весь каталог присваиванием снимка
        # «до»: параллельный импорт другого документа уже добавил свои находки, и
        # присваивание потеряло бы их из каталога процесса при целых данных в ES.
        with self._lock:
            for finding_id in new_ids:
                self._findings.pop(finding_id, None)
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
            if semantic_part_id is None:
                session.run(
                    "MATCH (d:Entity {id: $id}) REMOVE d.semantic_extracted",
                    id=f"document-{document_id}",
                )
            else:
                session.run(
                    "MATCH (d:Entity {id: $id}) "
                    "SET d.semantic_parts_completed = [part IN "
                    "coalesce(d.semantic_parts_completed, []) WHERE part <> $part], "
                    "d.semantic_extracted = false",
                    id=f"document-{document_id}",
                    part=semantic_part_id,
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
        # Тот же дедуп-замок, что и у семантического импорта: «узла документа нет»
        # проверяется до записи, а пишет после, и два параллельных пропуска одного
        # файла удваивали и чанки в индексе, и обход графа с откатом.
        with self._document_lock(f"structural:{document_id}"):
            return self._index_document_locked(document, source_path, checksum, document_id)

    def _index_document_locked(
        self, document: DocumentRequest, source_path: str, checksum: str, document_id: UUID
    ) -> StructuralDocumentReceipt:
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
        if not pieces:
            raise NoChunksError(
                f"фрагменты короче порога чанкинга ({CHUNK_MIN_CHARS} символов): "
                "в индекс не лёг ни один кусок текста"
            )
        findings = [chunk_finding(document, document_id, piece) for piece in pieces]
        actions = [
            {
                "_index": CHUNK_INDEX,
                "_id": finding.id,
                "_source": {
                    "text": finding.statement,
                    "title": document.title,
                    # Те же поля отсечения и отбора, что у семантического индекса:
                    # окно читается одним запросом по обоим, и поле, не записанное
                    # здесь, исключает чанки из выдачи по фильтру молча.
                    "status": finding.status,
                    "subject": _subject_filter_value(finding.subject),
                    "schema": FINDING_DOC_SCHEMA,
                    "scope": {
                        key: value
                        for key, value in finding.scope.items()
                        if key in WINDOW_SCOPE_KEYS
                    },
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
        self._merge_findings(findings)
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
        *,
        abort: AbortCheck | None = None,
    ) -> RetrievalContext:
        """Исполняет план retrieval теми же хелперами, что и memory-адаптер.

        Ранее ``numeric_filters`` здесь не применялись вообще, а observation
        рапортовала «доказательств после фильтрации» по нефильтрованному списку;
        ``countries``, ``year_from``/``year_to`` и ``use_global_context`` не
        читались нигде. Окно кандидатов шире потолка выдачи: иначе ограничение по
        году срезало бы выдачу в ноль вместо честного отчёта о покрытии.

        ``abort`` — кооперативная отмена: на границе каждой платной фазы
        проверяется, есть ли у прогона остаток. Драйвер синхронный, поток выдернуть
        нельзя, поэтому снятие с прогона означает «не начинать следующую фазу», а не
        «оборвать текущую».
        """
        self._ensure_ready()
        with self._lock:
            notes = [*self._startup_notes]
        notes.extend(self._graph_window_notes())
        window = candidate_window(plan, RETRIEVAL_TOP_K)
        candidates, leg_notes = self._rank(
            retrieval_plan.lexical_query,
            window,
            "hybrid",
            allowed_data_classes,
            semantic_query=retrieval_plan.semantic_query,
            abort=abort,
        )
        notes.extend(leg_notes)
        if not candidates:
            notes.append(
                "Лексика, чанки и векторы не дали ни одной находки: ограничения плана "
                "не к чему применять — доказательств по запросу нет."
            )
        if not retrieval_plan.use_local_graph:
            graph = GraphSnapshot(nodes=[], edges=[], communities=[])
        elif is_aborted(abort):
            notes.append(abort_note("обход графа"))
            graph = GraphSnapshot(nodes=[], edges=[], communities=[])
        else:
            graph, graph_notes = self._traverse(retrieval_plan, allowed_data_classes, abort=abort)
            notes.extend(graph_notes)
        wants_global = retrieval_plan.use_community_context or retrieval_plan.use_global_context
        briefs: list[str] = []
        if wants_global and is_aborted(abort):
            notes.append(abort_note("сводки сообществ"))
        elif wants_global:
            briefs = self._community_context(retrieval_plan, candidates, allowed_data_classes)
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

        Профили кэшируются тем же коротким сроком, что и полный граф, и по тому же
        поколению записи: ``community_briefs`` пересобирает их по всему графу на
        каждое действие, а действие в исследовательском запросе до 24 штук подряд на
        неизменившемся графе. Ключ содержит срез доступа и момент загрузки
        закэшированного графа, поэтому сводки не переживают ни смену прав, ни
        обновление снимка по TTL.
        """
        cached = self._cached_graph()
        # Stamp снимка берётся под тем же замком, что и его публикация: иначе ключ
        # кэша сводок собирался бы из поколения одного графа и момента загрузки
        # другого, и устаревший профиль мог пережить импорт.
        with self._lock:
            stamp = (self._graph_epoch, self._graph_cached_at)
        key = brief_key(stamp, allowed_data_classes, cached.communities)
        return cached_community_briefs(
            self._brief_cache,
            key,
            lambda: self.full_graph(allowed_data_classes),
            findings,
            query_tokens(retrieval_plan.lexical_query),
            DEFAULT_PROFILE_LIMIT,
        )

    def _traverse(
        self,
        retrieval_plan: RetrievalPlan,
        allowed_data_classes: set[DataClass] | None,
        *,
        abort: AbortCheck | None = None,
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
        planned = [name for name in retrieval_plan.entity_names if name.strip()]
        entities = planned or [retrieval_plan.lexical_query]
        notes: list[str] = []
        if not planned:
            # Приёмка 4 октября на живом GigaChat: из четырёх вопросов план не назвал
            # сущностей ни в одном, и обход держался только на формулировке. Путь
            # рабочий, но он обязан быть назван: «граф по якорям плана» и «граф по
            # тексту вопроса» — разное доверие к ответу, а не два
            # способа одного и того же запроса.
            notes.append(
                "Якоря обхода взяты из формулировки вопроса: план не назвал сущностей."
            )
        visited: dict[str, GraphNode] = {}
        with self._driver.session() as session:
            # Первая ветка — точное совпадение метки (seek по индексу `label`),
            # вторая — поиск подстрокой только для недостающих якорей. Достижение
            # потолка MAX_ANCHORS называется в ответе: «якорей было больше» — это
            # потеря покрытия обхода, а не деталь плана.
            exact = session.run(
                ANCHOR_EXACT_CYPHER,
                labels=entities,
                classes=classes,
                anchors=MAX_ANCHORS,
            ).single()
            for item in ((exact["nodes"] if exact else []) or []):
                node = _parse_node(item)
                if node is not None:
                    visited[node.id] = node
            exact_matched = _record_int(exact, "matched")
            if planned and exact_matched == 0:
                # Имена план назвал, но ни одно не совпало с меткой: покрытие обхода
                # держится на подстроке и на формулировке, и это тот случай, где
                # «id узлов в плане» ничего бы не спасло (замер 4 октября: 1 имя из
                # 4 вопросов, совпадений 0).
                notes.append(
                    "Ни одно имя из плана не совпало с меткой графа: "
                    "якоря добираются подстрокой и формулировкой вопроса."
                )
            if len(visited) < MAX_ANCHORS:
                record = session.run(
                    ANCHORS_CYPHER,
                    entities=entities,
                    classes=classes,
                    anchors=MAX_ANCHORS - len(visited),
                ).single()
                for item in ((record["nodes"] if record else []) or []):
                    node = _parse_node(item)
                    if node is not None:
                        visited.setdefault(node.id, node)
                substring_matched = _record_int(record, "matched")
                if exact_matched + substring_matched > MAX_ANCHORS:
                    notes.append(
                        f"Якорей обхода найдено больше, чем взято: совпадений "
                        f"{exact_matched + substring_matched}, потолок MAX_ANCHORS="
                        f"{MAX_ANCHORS}. Связи остальных якорей в этот подграф не попали."
                    )
            elif exact_matched > MAX_ANCHORS:
                notes.append(
                    f"Якорей обхода найдено больше, чем взято: совпадений {exact_matched}, "
                    f"потолок MAX_ANCHORS={MAX_ANCHORS}."
                )
            frontier = sorted(visited)
            level = 0
            while frontier and level < depth and len(visited) < MAX_TRAVERSAL_NODES:
                if is_aborted(abort):
                    # Каждый уровень — отдельный запрос к драйверу. Снятие с прогона
                    # обрывает обход на границе уровня: начатый Cypher уже не отменить,
                    # но следующий уровень прогон не оплачивает.
                    notes.append(abort_note(f"обход графа, уровень {level + 1} из {depth}"))
                    break
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
                    edges=MAX_TRAVERSAL_EDGES,
                ).single()
                if visited
                else None
            )
        edges = [
            edge
            for item in ((edge_record["edges"] if edge_record else []) or [])
            if (edge := _parse_edge(item)) is not None
        ]
        if _record_int(edge_record, "matched") > len(edges):
            notes.append(
                f"Рёбер между посещёнными узлами больше, чем взято: потолок "
                f"MAX_TRAVERSAL_EDGES={MAX_TRAVERSAL_EDGES}, часть связей не показана."
            )
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
        *,
        abort: AbortCheck | None = None,
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
        if wants_semantic and is_aborted(abort):
            # Ветку просили, но она стоит оплаченного round-trip в модель: отменённый
            # прогон не имеет права его совершать.
            notes.append(abort_note("векторная ветка"))
        elif wants_semantic and self._embeddings is None:
            # Ветку просили, но она не может работать: молчать об этом — значит
            # рапортовать «гибридный поиск» там, где был только текст.
            notes.append(
                "Векторная ветка недоступна (эмбеддинги не настроены либо отключены из-за "
                "несовместимой размерности индекса): семантический запрос плана не исполнен, "
                "выдача собрана лексикой и структурными чанками."
            )
        elif wants_semantic:
            semantic_ids = self._semantic_ids(wanted_semantic, size, allowed_data_classes, notes)
        if (
            mode == "semantic"
            and not semantic_ids
            and self._embeddings is not None
            and not is_aborted(abort)
        ):
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
        if is_aborted(abort):
            # Полный граф под сигнал сообществ — самая дорогая часть ранжирования:
            # отменённому прогону он не оплачивается, порядок задают ветки и RRF.
            notes.append(abort_note("сигнал сообществ в ранжировании"))
            communities: dict[str, str] = {}
        else:
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
        catalog = self._snapshot_findings()
        for candidate_id in finding_ids:
            local = catalog.get(candidate_id)
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
        # Счётчики копятся локально и публикуются одним входом в замок: при
        # параллельном предзагрузе индексируют несколько рабочих потоков, и
        # `+=` без блокировки терял бы части прироста (чтение статистики корпуса
        # обязано сходиться с числом реально записанных векторов).
        indexed_vectors = 0
        missing_vectors = 0
        vectors: list[list[float] | None] = [None] * len(findings)
        if self._embeddings is not None:
            texts = [_embedding_text(finding) for finding in findings]
            try:
                vectors = list(self._embeddings.documents(texts))
            except Exception as error:  # noqa: BLE001 - индеемся без векторов, но вслух
                self._note_degradation("embedding_batch", error)
                missing_vectors += len(findings)
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
                    missing_vectors += 1
        actions = []
        for index, finding in enumerate(findings):
            document: dict[str, Any] = {
                "statement": finding.statement,
                "status": finding.status,
                "subject": _subject_filter_value(finding.subject),
                "predicate": finding.predicate,
                "object": finding.object,
                "schema": FINDING_DOC_SCHEMA,
                "data_class": finding.data_class.value,
                "confidence": finding.confidence,
                "evidence": " ".join(item.quote for item in finding.evidence),
                # Год и территория источника лежат отдельными полями: plan-фильтры
                # и оконное чтение каталога иначе нечем исполнять, а маппинс эти
                # поля уже объявлял — раньше в них не писали ничего.
                "scope": {
                    key: value
                    for key, value in finding.scope.items()
                    if key in WINDOW_SCOPE_KEYS
                },
                "finding_json": finding.model_dump_json(),
            }
            if finding.superseded_by is not None:
                document["superseded_by"] = finding.superseded_by
            vector = vectors[index] if index < len(vectors) else None
            if vector is not None:
                document["embedding"] = vector
                indexed_vectors += 1
            else:
                missing_vectors += 1
            actions.append({"_index": FINDING_INDEX, "_id": finding.id, "_source": document})
        self._count_vectors(indexed=indexed_vectors, missing=missing_vectors)
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
            vectors_indexed=self._vector_counters()[0],
        )
        return stats

    def full_graph(self, allowed_data_classes: set[DataClass] | None = None) -> GraphSnapshot:
        self._ensure_ready()
        cached = self._cached_graph()
        if allowed_data_classes is None:
            # Новый список-контейнер вместо глубокой копии: узлы и рёбра снимка
            # только читают (профили сообществ, каталог, обход), а правка
            # возвращённого списка не должна доходить до кэша полного графа.
            return GraphSnapshot(
                nodes=list(cached.nodes),
                edges=list(cached.edges),
                communities=list(cached.communities),
            )
        return AccessPolicyEngine().filter_graph(cached, allowed_data_classes)

    def _cached_graph(self) -> GraphSnapshot:
        """Кэш полного графа с инвалидацией на запись.

        Раньше каждый вызов перечитывал все сущности из Neo4j и заново запускал
        Leiden/Louvain; то же самое происходило при каждом retrieval в качестве
        fallback-а.

        Под замок берётся только чтение и публикация снимка: запрос к Neo4j и
        кластеризация исполняются без блокировок. Два потока на холодном кэше
        могут прочитать граф дважды — это повторная работа, а не испорченное
        состояние, зато чтение корпуса не ждёт чужой обход графа.
        """
        with self._lock:
            cached = self._graph_cache
            fresh = cached is not None and time.monotonic() - self._graph_cached_at < (
                GRAPH_CACHE_TTL_SECONDS
            )
            if fresh and cached is not None:
                hit_age = max(time.monotonic() - self._graph_cached_at, 0.0)
            else:
                hit_age = None
        if hit_age is not None and cached is not None:
            self._note_cache(True, hit_age)
            return cached
        snapshot = self._load_graph_from_neo4j()
        with self._lock:
            self._graph_cache = snapshot
            self._graph_cached_at = time.monotonic()
            age = 0.0
        self._note_cache(False, age)
        return snapshot

    def _load_graph_from_neo4j(self, *, complete: bool = False) -> GraphSnapshot:
        # Срез обязана быть backbone'ом, а не алфавитным префиксом. До этой правки
        # узлы резались `ORDER BY label`, а рёбра — независимо, по `rel.id`: на
        # живом замере (1501 узел) пересечение двух посторонних срезов оставляло
        # обходу 120 узлов из 600, и в снимке доживали до сообщества только те
        # сущности, чьи имена случайно оказались в начале алфавита и чьи рёбра
        # попали в начало списка. Теперь узлы берутся по убывающей степени
        # связности, а рёбра читаются только между уже выбранными узлами:
        # снимок становится настоящим подграфом, а не пересечением двух лимитов.
        with self._driver.session() as session:
            node_query = (
                """
                MATCH (node:Entity)
                WHERE node.type <> 'chunk'
                WITH node, COUNT {
                    (node)-[relation]-(neighbor:Entity)
                    WHERE neighbor.type <> 'chunk' AND type(relation) <> 'HAS_CHUNK'
                } AS degree
                ORDER BY degree DESC, coalesce(node.label, node.id), node.id
                WITH collect(properties(node)) AS nodes, count(node) AS total
                RETURN nodes, total
                """
                if complete
                else
                """
                MATCH (node:Entity)
                WHERE node.type <> 'chunk'
                WITH node, COUNT {
                    (node)-[relation]-(neighbor:Entity)
                    WHERE neighbor.type <> 'chunk' AND type(relation) <> 'HAS_CHUNK'
                } AS degree
                ORDER BY degree DESC, coalesce(node.label, node.id), node.id
                WITH collect(properties(node)) AS nodes, count(node) AS total
                RETURN nodes[0..$limit] AS nodes, total
                """
            )
            node_record = (
                session.run(node_query).single()
                if complete
                else session.run(node_query, limit=GRAPH_NODE_LIMIT).single()
            )
            total = int((node_record["total"] if node_record else 0) or 0)
            raw_nodes = (node_record["nodes"] if node_record else []) or []
            selected = [
                node["id"] for node in raw_nodes if isinstance(node.get("id"), str)
            ]
            edge_query = (
                """
                UNWIND $ids AS id
                MATCH (a:Entity {id: id})-[rel]->(b:Entity)
                WHERE a.type <> 'chunk' AND b.type <> 'chunk'
                  AND type(rel) <> 'HAS_CHUNK' AND b.id IN $ids
                WITH a, rel, b
                ORDER BY rel.id, a.id, b.id
                WITH collect(DISTINCT {
                    id: rel.id, source: a.id, target: b.id,
                    relation: type(rel), confidence: rel.confidence,
                    data_class: coalesce(rel.data_class, 'public')
                }) AS edges, count(*) AS shown
                RETURN edges, shown
                """
                if complete
                else
                """
                UNWIND $ids AS id
                MATCH (a:Entity {id: id})-[rel]->(b:Entity)
                WHERE a.type <> 'chunk' AND b.type <> 'chunk'
                  AND type(rel) <> 'HAS_CHUNK' AND b.id IN $ids
                WITH a, rel, b
                ORDER BY rel.id, a.id, b.id
                WITH collect(DISTINCT {
                    id: rel.id, source: a.id, target: b.id,
                    relation: type(rel), confidence: rel.confidence,
                    data_class: coalesce(rel.data_class, 'public')
                }) AS edges, count(*) AS shown
                RETURN edges[0..$limit] AS edges, shown
                """
            )
            if complete:
                edge_record = (
                    session.run(edge_query, ids=selected).single() if selected else None
                )
            else:
                edge_record = (
                    session.run(
                        edge_query,
                        ids=selected,
                        limit=GRAPH_EDGE_LIMIT,
                    ).single()
                    if selected
                    else None
                )
            # Полное число рёбёр корпуса считается отдельно: ограничение сверху
            # (показано, всего) обязано называть весь корпус, а не подграф среза,
            # иначе «3000 из 3000» читалось бы как «потолка не было».
            edges_total_record = session.run(
                """
                MATCH (a:Entity)-[rel]->(b:Entity)
                WHERE a.type <> 'chunk' AND b.type <> 'chunk' AND type(rel) <> 'HAS_CHUNK'
                RETURN count(rel) AS total
                """
            ).single()

        raw_edges = (edge_record["edges"] if edge_record else []) or []
        edges_total = _record_int(edges_total_record, "total")
        nodes = [node for item in raw_nodes if (node := _parse_node(item)) is not None]
        edges = [edge for item in raw_edges if (edge := _parse_edge(item)) is not None]
        # Потолки GRAPH_NODE_LIMIT и GRAPH_EDGE_LIMIT — не молчаливая потеря: пара
        # (показано, всего) раскрывается в degradation_reasons каждого retrieval.
        if not nodes:
            with self._lock:
                self._graph_window = (0, max(total, 0))
                self._graph_edge_window = (len(edges), max(edges_total, len(edges)))
            return self._seed.full_graph()
        connected = {edge.source for edge in edges} | {edge.target for edge in edges}
        keep = [
            node
            for node in nodes
            if node.id in connected or node.metadata.get("domain") or node.id.startswith("dom-")
        ]
        # Раскрытие называет то число узлов, на котором действительно считаются
        # сообщества и обход: потолок среза (600) — ещё не видимый корпус, потому
        # что фильтр связности выбрасывает из него узлы без рёбер внутри среза.
        # Числиться 600 значило бы недооценить потерю ровно в тот момент, когда
        # её надо назвать; на старом алфавитном порядке эта же потеря и была
        # причиной 120 узлов вместо 600 на замере 2 октября.
        if not complete:
            with self._lock:
                self._graph_window = (len(keep), max(total, len(keep)))
                self._graph_edge_window = (len(edges), max(edges_total, len(edges)))
        communities = detect_communities(keep, edges)
        return GraphSnapshot(nodes=keep, edges=edges, communities=communities)

    def _graph_window_notes(self) -> list[str]:
        with self._lock:
            window = self._graph_window
            edge_window = self._graph_edge_window
        notes: list[str] = []
        if window is not None and window[0] < window[1]:
            notes.append(
                f"Полный граф прочитан не целиком: узлов {window[0]} из {window[1]} "
                f"(потолок GRAPH_NODE_LIMIT={GRAPH_NODE_LIMIT}). Сообщества и обход считаются "
                "по этой части графа, а не по всему корпусу."
            )
        if edge_window is not None and edge_window[0] < edge_window[1]:
            notes.append(
                f"Рёбра полного графа прочитаны не целиком: {edge_window[0]} из "
                f"{edge_window[1]} (потолок GRAPH_EDGE_LIMIT={GRAPH_EDGE_LIMIT}). "
                "Сообщества посчитаны по этой части связей."
            )
        return notes

    @property
    def graph_epoch(self) -> int:
        """Поколение графа: меняется на любую запись в корпус."""
        with self._lock:
            return self._graph_epoch

    def _vector_counters(self) -> tuple[int, int]:
        with self._lock:
            return self._vectors_indexed, self._vectors_missing

    def _invalidate_graph_cache(self) -> None:
        """Сбрасывает всё производное от графа: снимок, сводки и кэш структурированных ответов.

        LLM-кэш снимается здесь, а не в пяти местах вызова, по той же причине, по
        какой сбрасывается кэш полного графа: промпты structured output собираются
        из текста корпуса (секции FINDINGS и COMMUNITIES в agents/workflow.py),
        поэтому мутация источников устаревляет и закэшированный ответ модели — даже
        когда промпт буквально совпадает.

        Поля процесса мутируются под замком одним блоком: ``_graph_epoch`` обязан
        расти строго последовательно (он входит в ключ кэша сводок), а параллельная
        запись двух документов без замка давала бы потерянное поколение — сводки
        прежнего графа переживали бы импорт.
        """
        with self._lock:
            self._graph_cache = None
            self._graph_cached_at = 0.0
            self._graph_epoch += 1
            self._graph_window = None
            self._graph_edge_window = None
        self._brief_cache.invalidate()
        invalidate_derived_llm_cache("граф корпуса")

    def _note_cache(self, hit: bool, age_seconds: float) -> None:
        try:
            from scientific_tangle.services.agent_metrics import agent_metrics

            agent_metrics.observe_graph_cache(hit)
            agent_metrics.observe_cache_age(age_seconds)
        except Exception:  # noqa: BLE001 - метрики не должны ронять retrieval
            return

    def _note_degradation(self, component: str, error: BaseException) -> str:
        """Фиксирует отказ ветки и возвращает формулировку для честного отчёта.

        Формулировку читает аналитик (``degradation_reasons``) и следом видит
        модель в промпте, поэтому наружу уходит отредактированный текст:
        ``ResponseError`` и ``ConnectionError`` приводят к строке с телом ответа,
        заголовками и строкой подключения (``bolt://``, URL Elasticsearch).
        Полный текст — в журнале процесса.
        """
        note = (
            f"Ветка retrieval «{component}» завершилась ошибкой: "
            f"{redact_provider_error(error, context='хранилище')}."
        )
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

        Полный список с копией каждой находки — дорогое чтение: он нужен только
        потребителям, которые действительно обходят корпус целиком (оценка качества,
        поиск пробелов). Постраничным маршрутам интерфейса служит
        ``findings_window``, который читает окно из Elasticsearch и копирует ровно
        его. Отбор (ACL, замены, демо) выполняется до копирования, а глубокое
        копирование — вне замка, чтобы отрисовка чужого ответа не блокировала импорт.
        """
        self._ensure_ready()
        allowed = None if allowed_data_classes is None else set(allowed_data_classes)
        with self._lock:
            selected = [
                finding
                for finding in self._findings.values()
                if finding.superseded_by is None
                and not is_demo_finding(finding)
                and (allowed is None or finding.data_class in allowed)
            ]
        return [finding.model_copy(deep=True) for finding in selected]

    def semantic_findings(self) -> list[Finding]:
        self._ensure_ready()
        findings: list[Finding] = []
        for hit in helpers.scan(
            self._search,
            index=FINDING_INDEX,
            query={"query": {"match_all": {}}},
            _source=["finding_json"],
            size=1000,
        ):
            source = hit.get("_source", {})
            raw = source.get("finding_json")
            if not isinstance(raw, str):
                continue
            finding = Finding.model_validate_json(raw)
            if finding.superseded_by is None and not is_demo_finding(finding):
                findings.append(finding)
        return findings

    def semantic_graph(self) -> GraphSnapshot:
        self._ensure_ready()
        return self._load_graph_from_neo4j(complete=True)

    def upsert_hypotheses(self, signals: Sequence[HypothesisSignal]) -> None:
        self._ensure_ready()
        if not signals:
            return
        actions = [
            {
                "_index": HYPOTHESIS_INDEX,
                "_id": str(signal.id),
                "_source": {
                    "id": str(signal.id),
                    "kind": signal.kind,
                    "statement": signal.statement,
                    "statement_sort": signal.statement,
                    "data_class": signal.data_class.value,
                    "signal_json": signal.model_dump_json(),
                },
            }
            for signal in signals
        ]
        helpers.bulk(self._search, actions, refresh=True)

    def hypotheses_window(
        self,
        *,
        limit: int,
        offset: int,
        allowed_data_classes: set[DataClass] | None = None,
    ) -> HypothesisWindow:
        self._ensure_ready()
        filters: list[dict[str, object]] = []
        if allowed_data_classes is not None:
            filters.append(
                {"terms": {"data_class": [item.value for item in allowed_data_classes]}}
            )
        response = self._search.search(
            index=HYPOTHESIS_INDEX,
            query={"bool": {"filter": filters}},
            from_=offset,
            size=limit,
            sort=[{"kind": "asc"}, {"statement_sort": "asc"}, {"id": "asc"}],
            track_total_hits=True,
            source_includes=["signal_json"],
        )
        hits = response.get("hits", {})
        total_data = hits.get("total", 0)
        total = int(total_data.get("value", 0)) if isinstance(total_data, dict) else int(total_data)
        signals = [
            HypothesisSignal.model_validate_json(hit["_source"]["signal_json"])
            for hit in hits.get("hits", [])
        ]
        return HypothesisWindow(
            signals=signals,
            total=total,
            limit=limit,
            offset=offset,
        )

    def hypothesis_by_id(
        self, signal_id: UUID, allowed_data_classes: set[DataClass] | None = None
    ) -> HypothesisSignal | None:
        self._ensure_ready()
        filters: list[dict[str, object]] = [{"term": {"id": str(signal_id)}}]
        if allowed_data_classes is not None:
            filters.append(
                {"terms": {"data_class": [item.value for item in allowed_data_classes]}}
            )
        response = self._search.search(
            index=HYPOTHESIS_INDEX,
            query={"bool": {"filter": filters}},
            size=1,
            source_includes=["signal_json"],
        )
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            return None
        return HypothesisSignal.model_validate_json(hits[0]["_source"]["signal_json"])

    def findings_window(
        self,
        *,
        limit: int = FINDINGS_WINDOW_DEFAULT,
        offset: int = 0,
        allowed_data_classes: set[DataClass] | None = None,
        status: str | None = None,
        subject: str | None = None,
    ) -> FindingWindow:
        """Окно каталога находок, прочитанное из Elasticsearch.

        Каталог больше не обязан жить в RAM целиком: списку интерфейса нужны
        ``limit`` записей со смещением, и они берутся из индексов одним запросом
        (оба индекса — семантические тезисы и структурные чанки). Глубокая копия
        делается только для возвращаемого окна.

        Порядок — ``_seq_no``: сортировка по ``_id`` в Elasticsearch 8 запрещена
        (fielddata на ``_id`` выключен на уровне кластера), а ``_seq_no`` даёт
        детерминированную нумерацию, которая не сдвигает уже отданные страницы при
        дописывании корпуса — новые записи получают старшие номера.

        Класс доступа, заменённые версии, демо-сеятель, статус и субъект отсечены
        запросом; то же проверяется и на стороне процесса, потому что индексы,
        созданные до появления этих полей, их не содержат. Из-за этого страница
        может быть короче ``limit`` — оговорка лежит в ``note``, а не в молчании.
        """
        self._ensure_ready()
        size, start = normalize_window(limit, offset, max_limit=FINDINGS_WINDOW_MAX)
        filters: list[dict[str, Any]] = list(_class_filter(allowed_data_classes))
        if status:
            filters.append({"term": {"status": status}})
        if subject:
            # Подстрока без регистра — как в отборе по каталогу процесса: поле
            # записывается уже в нижнем регистре (`_subject_filter_value`), потому
            # что `case_insensitive` на кириллице не срабатывает. Маски
            # Elasticsearch из строки пользователя экранируются: его «осмос*» должен
            # остаться буквальным «осмос*», а не шаблоном на весь индекс.
            escaped = _subject_filter_value(subject).replace("\\", "\\\\").replace(
                "*", "\\*"
            ).replace("?", "\\?")
            filters.append(
                {
                    "wildcard": {
                        "subject": {"value": f"*{escaped}*", "case_insensitive": True}
                    }
                }
            )
        query = {
            "bool": {
                "filter": filters,
                "must_not": [
                    {"exists": {"field": "superseded_by"}},
                    {"term": {"scope.origin": DEMO_ORIGIN}},
                ],
            }
        }
        try:
            response = self._search.search(
                index=FINDING_INDICES,
                size=size,
                from_=start,
                query=query,
                sort=[{"_seq_no": {"order": "asc"}}],
                source={"includes": ["finding_json"]},
                track_total_hits=True,
            )
        except Exception as error:  # noqa: BLE001 - окно читается из каталога процесса
            failure = str(self._note_degradation("findings_window", error))
            window = self._findings_window_from_catalog(
                size, start, allowed_data_classes, status, subject
            )
            return FindingWindow(
                findings=window.findings,
                total=window.total,
                offset=start,
                limit=size,
                note=f"{failure} Окно каталога взято из RAM-каталога процесса и может "
                "не содержать находок сверх его потолка.",
            )
        hits = response["hits"]
        findings: list[Finding] = []
        dropped = 0
        for hit in hits.get("hits") or []:
            payload = (hit.get("_source") or {}).get("finding_json")
            if not payload:
                dropped += 1
                continue
            finding = Finding.model_validate_json(payload)
            if finding.superseded_by is not None or is_demo_finding(finding):
                dropped += 1
                continue
            if allowed_data_classes is not None and finding.data_class not in allowed_data_classes:
                dropped += 1
                continue
            if status and finding.status != status:
                dropped += 1
                continue
            if subject and subject.lower() not in (finding.subject or "").lower():
                dropped += 1
                continue
            findings.append(finding.model_copy(deep=True))
        total = hits.get("total")
        total_value = int(total.get("value", 0)) if isinstance(total, dict) else int(total or 0)
        note = None
        if dropped:
            note = (
                f"Из окна каталога снято записей на стороне процесса: {dropped} "
                "(заменённые версии, демо-контент или записи индексов, созданных до "
                "появления фильтруемых полей) — страница короче limit."
            )
        return FindingWindow(
            findings=findings,
            total=max(total_value, start + len(findings)),
            offset=start,
            limit=size,
            note=note,
        )

    def facts_window(
        self,
        *,
        limit: int = FINDINGS_WINDOW_DEFAULT,
        offset: int = 0,
        allowed_data_classes: set[DataClass] | None = None,
        status: str | None = None,
        subject: str | None = None,
    ) -> FindingWindow:
        """Окно только для подтверждённых источниками структурированных фактов."""
        self._ensure_ready()
        size, start = normalize_window(limit, offset, max_limit=FINDINGS_WINDOW_MAX)
        filters: list[dict[str, Any]] = list(_class_filter(allowed_data_classes))
        filters.extend(
            [
                {"exists": {"field": "subject"}},
                {"exists": {"field": "predicate"}},
                {"exists": {"field": "object"}},
                {"exists": {"field": "evidence"}},
            ]
        )
        if status:
            filters.append({"term": {"status": status}})
        if subject:
            escaped = _subject_filter_value(subject).replace("\\", "\\\\").replace(
                "*", "\\*"
            ).replace("?", "\\?")
            filters.append(
                {"wildcard": {"subject": {"value": f"*{escaped}*", "case_insensitive": True}}}
            )
        query = {
            "bool": {
                "filter": filters,
                "must_not": [
                    {"exists": {"field": "superseded_by"}},
                    {"term": {"scope.origin": DEMO_ORIGIN}},
                ],
            }
        }
        try:
            response = self._search.search(
                index=FINDING_INDEX,
                size=size,
                from_=start,
                query=query,
                sort=[{"_seq_no": {"order": "asc"}}],
                source={"includes": ["finding_json"]},
                track_total_hits=True,
            )
        except Exception as error:  # noqa: BLE001 - сохраняем доступность каталога при деградации поиска
            failure = str(self._note_degradation("facts_window", error))
            allowed = None if allowed_data_classes is None else set(allowed_data_classes)
            selected = sorted(
                (
                    item
                    for item in self._snapshot_findings().values()
                    if item.superseded_by is None
                    and not is_demo_finding(item)
                    and item.subject
                    and item.predicate
                    and item.object
                    and item.evidence
                    and (allowed is None or item.data_class in allowed)
                    and (status is None or item.status == status)
                    and (subject is None or subject.casefold() in item.subject.casefold())
                ),
                key=lambda item: item.id,
            )
            return FindingWindow(
                findings=[item.model_copy(deep=True) for item in selected[start : start + size]],
                total=len(selected),
                offset=start,
                limit=size,
                note=f"{failure} Окно фактов взято из RAM-каталога процесса.",
            )
        hits = response.get("hits", {})
        findings: list[Finding] = []
        for hit in hits.get("hits", []):
            payload = (hit.get("_source") or {}).get("finding_json")
            if not payload:
                continue
            finding = Finding.model_validate_json(payload)
            if not (
                finding.subject and finding.predicate and finding.object and finding.evidence
                and finding.superseded_by is None
                and (allowed_data_classes is None or finding.data_class in allowed_data_classes)
                and (status is None or finding.status == status)
                and (subject is None or subject.casefold() in finding.subject.casefold())
                and not is_demo_finding(finding)
            ):
                continue
            findings.append(finding)
        total = hits.get("total", 0)
        total_value = int(total.get("value", 0)) if isinstance(total, dict) else int(total)
        return FindingWindow(
            findings=findings,
            total=total_value,
            offset=start,
            limit=size,
            note=None,
        )

    def _findings_window_from_catalog(
        self,
        size: int,
        start: int,
        allowed_data_classes: set[DataClass] | None,
        status: str | None = None,
        subject: str | None = None,
    ) -> FindingWindow:
        """Окно из RAM-каталога — резервная ветка на отказе Elasticsearch."""
        allowed = None if allowed_data_classes is None else set(allowed_data_classes)
        needle = subject.lower() if subject else None
        with self._lock:
            ordered = sorted(
                (
                    finding
                    for finding in self._findings.values()
                    if finding.superseded_by is None
                    and not is_demo_finding(finding)
                    and (allowed is None or finding.data_class in allowed)
                    and (status is None or finding.status == status)
                    and (needle is None or needle in (finding.subject or "").lower())
                ),
                key=lambda finding: finding.id,
            )
        window = ordered[start : start + size]
        return FindingWindow(
            findings=[finding.model_copy(deep=True) for finding in window],
            total=len(ordered),
            offset=start,
            limit=size,
            note=None,
        )

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
                        # Отдельным полем, а не только внутри ``finding_json``:
                        # оконное чтение каталога фильтрует замены запросом, а не
                        # постфактум в выдаче чанк-ветки.
                        "doc": {
                            "finding_json": marked.model_dump_json(),
                            "superseded_by": new_id,
                        },
                    }
                ],
                refresh=True,
                raise_on_error=False,
                raise_on_exception=False,
            )
        except Exception as error:  # noqa: BLE001 - вторичный сбой не перекрывает замену
            logger.error("Не удалось пометить чанк %s заменённым: %s", old.id, error)

    def set_finding_status(self, finding_id: str, status: str) -> Finding:
        """Степень консенсуса находки без создания версии.

        Каталог процесса — источник для ``all_findings`` (экран «Расхождения»,
        сводка дашборда), индекс находок — источник для окна выдачи и retrieval.
        Поэтому правятся оба, точечно и без переэмбеддинга: ``status`` не входит в
        текст, по которому считается вектор, и перечувать его было бы платным
        обращением к `/embeddings` ради одного поля.

        Узел графа не трогается: свойства узлов несут утверждение, класс доступа и
        версию, а консенсус читают из находок.
        """
        self._ensure_ready()
        with self._document_lock(f"status:{finding_id}"):
            current = self._snapshot_findings().get(finding_id) or self._stored_finding(finding_id)
            if current is None:
                raise KeyError(f"Finding not found: {finding_id}")
            updated = current.model_copy(update={"status": status})
            # Каталог seed-адаптера: replacement-цепочки читают его при следующей
            # замене, и устаревший статус там вернулся бы наружу после перезапуска.
            self._seed.register_findings([updated])
            self._merge_findings([updated])
            self._update_stored_status(updated)
            return updated.model_copy(deep=True)

    def _update_stored_status(self, finding: Finding) -> None:
        """Точечное обновление ``status`` в индексах находок и чанков.

        Семантический тезис живёт только в индексе находок, структурный чанк — в
        индексе чанков, поэтому «документа нет» на втором индексе это норма, а не
        сбой: ошибкой считается только отсутствие обоих. Статус не перечувствуют
        через переэмбеддинг — у записи меняются два поля.
        """
        payload = finding.model_dump_json()
        accepted: list[str] = []
        for index in (FINDING_INDEX, CHUNK_INDEX):
            action = {
                "_index": index,
                "_id": finding.id,
                "_op_type": "update",
                "doc": {"status": finding.status, "finding_json": payload},
            }
            try:
                _, errors = cast(
                    "tuple[int, list[dict[str, Any]]]",
                    helpers.bulk(
                        self._search,
                        [action],
                        refresh=True,
                        raise_on_error=False,
                        raise_on_exception=False,
                    ),
                )
            except Exception as error:  # noqa: BLE001 - индекс мог быть ещё не создан
                logger.error(
                    "Индекс %s не ответил на обновление статуса находки %s: %s",
                    index,
                    finding.id,
                    error,
                )
                continue
            if errors:
                logger.info("Индекс %s не содержит находку %s", index, finding.id)
                continue
            accepted.append(index)
        if not accepted:
            logger.error("Статус находки %s не принят ни одним индексом", finding.id)

    def _backfill_window_filters(self) -> None:
        """Дописывает в индексы поля отсечения и условий применимости.

        Записи, проиндексированные до появления этих полей, их не содержат, и
        ``must_not exists superseded_by`` / ``must_not term scope.origin`` такие
        документы не видит. На рабочем контуре это стоило аналитику пустой первой
        страницы ``/findings``: окно резалось демо-записями, снятие происходило
        только на стороне процесса, а ``X-Total-Count`` считал по предикату индекса,
        то есть завышенно. Без ``scope`` та же старая запись не участвует и в
        фильтрах retrieval-плана по территории и году.

        Правятся исключительно служебные поля: текст тезиса и вектор не меняются,
        поэтому переэмбеддинга нет. Операция — на процесс одна, с потолком: остальное
        допишется обычным путём записи.
        """
        actions: list[dict[str, Any]] = []
        for index in (FINDING_INDEX, CHUNK_INDEX):
            try:
                response = self._search.search(
                    index=index,
                    size=WINDOW_FILTER_BACKFILL_LIMIT,
                    # «Нет поля schema» ИЛИ «schema не текущей формы»: term-запрос
                    # не совпадает и с отсутствующим полем, поэтому один must_not
                    # покрывает обе группы. Отбирать только записи без метки было
                    # ошибкой: поднятие `FINDING_DOC_SCHEMA` при этом ничего не
                    # мигрировало, и старый volume после обновления сервиса
                    # продолжал терять кириллический фильтр по субъекту — проверено
                    # живым прогоном на одноразовом контуре.
                    query={"bool": {"must_not": [{"term": {"schema": FINDING_DOC_SCHEMA}}]}},
                    source={"includes": ["finding_json"]},
                )
            except Exception as error:  # noqa: BLE001 - индекс мог быть ещё не создан
                logger.warning("Индекс %s не проверен на поля окна: %s", index, error)
                continue
            for hit in response["hits"].get("hits") or []:
                payload = (hit.get("_source") or {}).get("finding_json")
                if not payload:
                    continue
                finding = Finding.model_validate_json(payload)
                # Те же поля и из того же набора ключей, что пишет обычная индексация:
                # расхождение предиката записи и предиката чтения — и есть этот дефект.
                doc: dict[str, Any] = {
                    "status": finding.status,
                    "schema": FINDING_DOC_SCHEMA,
                }
                if finding.subject:
                    doc["subject"] = _subject_filter_value(finding.subject)
                scope = {
                    key: value
                    for key, value in finding.scope.items()
                    if key in WINDOW_SCOPE_KEYS
                }
                if scope:
                    doc["scope"] = scope
                if finding.superseded_by is not None:
                    doc["superseded_by"] = finding.superseded_by
                actions.append(
                    {
                        "_index": index,
                        "_id": finding.id,
                        "_op_type": "update",
                        "doc": doc,
                    }
                )
        if not actions:
            return
        try:
            accepted, errors = cast(
                "tuple[int, list[dict[str, Any]]]",
                helpers.bulk(
                    self._search,
                    actions,
                    refresh=True,
                    raise_on_error=False,
                    raise_on_exception=False,
                ),
            )
        except Exception as error:  # noqa: BLE001 - без полей окно останется неполным
            logger.error("Поля отсечения окна не дописаны (%d записей): %s", len(actions), error)
            return
        # Оператор должен видеть, что volume читается старой записью и что именно
        # исправили: молча менять чужие индексы нельзя. Это не деградация — данные
        # целые, правятся только поля отсечения. Отклонённые позиции не считаются
        # исправленными: «запрос ушёл» ≠ «индекс принял».
        rejected = len(errors or [])
        logger.info(
            "Индексы находок: поля отсечения окна дописаны для %d записей "
            "(в выборке %d, отклонено %d).",
            accepted,
            len(actions),
            rejected,
        )
        if rejected:
            logger.error(
                "Поля отсечения окна приняты не полностью: окно каталога по-прежнему "
                "может резать страницу иначе, чем считать полное число.",
            )

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
        # Замок на находку: «заменена ли она» проверяется seed-адаптером, а запись
        # в индексы и граф идёт после — два эксперта, открывшие одну карточку,
        # иначе создадут две версии одного тезиса и два ребра SUPERSEDES.
        with self._document_lock(f"supersede:{finding_id}"):
            return self._supersede_finding_locked(
                finding_id,
                new_statement,
                new_confidence,
                observations,
                reviewer_id,
                review_date,
                review_reason,
            )

    def _supersede_finding_locked(
        self,
        finding_id: str,
        new_statement: str,
        new_confidence: float,
        observations: list[NumericObservation] | None,
        reviewer_id: str | None,
        review_date: str | None,
        review_reason: str | None,
    ) -> Finding:
        before = self._snapshot_findings().get(finding_id) or self._stored_finding(finding_id)
        if before is not None:
            self._merge_findings([before])
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
        # Каталог процесса — снимок seed-адаптера (без глубокого копирования каждой
        # находки): замена уже оформлена там, а заменённые версии наружу не отдаются.
        self._publish_findings(self._seed.finding_catalog())
        if before is not None and before.id.startswith("chunk-"):
            self._index_findings([new_finding])
            self._mark_chunk_superseded(before, new_finding.id)
        else:
            # Старая версия семантического тезиса живёт в FINDING_INDEX, и оконное
            # чтение каталога отсекает её полем ``superseded_by`` — без перезаписи
            # старой копии фильтр видел бы в окне обе версии одного утверждения.
            marked = (
                [before.model_copy(update={"superseded_by": new_finding.id}), new_finding]
                if before is not None
                else [new_finding]
            )
            self._index_findings(marked)
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


def _record_int(record: Any, key: str) -> int:
    """Целое из записи результата Neo4j (или из тестовой подделки-словаря).

    ``matched``/``total`` могут отсутствовать, если запрос отдаёт только список
    элементов: отсутствие числа читается как «потолок не достигнут», а не как сбой.
    """
    if record is None:
        return 0
    try:
        if key not in record:
            return 0
        return int(record[key] or 0)
    except (KeyError, TypeError, ValueError):
        return 0


def _class_values(allowed: set[DataClass] | None) -> list[str] | None:
    """``None`` — ограничений нет; пустой набор — не видно ничего.

    Ранее оба случая сливались в ``None`` через ``if allowed``, и роль без
    прав на классы получала весь корпус.
    """
    return None if allowed is None else sorted(item.value for item in allowed)


def _subject_filter_value(value: str | None) -> str:
    """Поле отбора по субъекту — в нижнем регистре, снятом при записи.

    ``case_insensitive`` у wildcard по keyword в Elasticsearch складывается по
    верхнему регистру ASCII: на живом контуре «*Мембрана*» находило 4 записи,
    а «*мемб*» — ноль. Для русскоязычного фильтра аналитика это молча
    «находок нет», поэтому регистр снимается здесь, одним предикатом с чтением,
    а не надеждой на анализатор. Отображаемый субъект живёт в ``finding_json``
    и от этого не меняется.
    """
    return (value or "").lower()


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
