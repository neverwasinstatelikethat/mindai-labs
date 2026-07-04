from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from typing import TypedDict

import networkx as nx
from elasticsearch import Elasticsearch, helpers
from neo4j import GraphDatabase, Record

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
from scientific_tangle.domain.models import EvidenceLocator, NumericObservation, QueryPlan
from scientific_tangle.services.embeddings import YandexEmbeddingClient
from scientific_tangle.services.knowledge import (
    InMemoryKnowledgeBase,
    KnowledgeBase,
    RetrievalContext,
    stable_uuid,
)

FINDING_INDEX = "mindai-findings-v1"
CHUNK_INDEX = "mindai-chunks-v1"

# Ключевые слова для автоматического именования сообществ
_COMMUNITY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("Водоподготовка", [
        "вод", "осмос", "мембран", "фильтрац", "обессолив", "ионн",
        "выпарив", "пермеат", "рассол", "сухой остаток", "смола",
    ]),
    ("Гидрометаллургия", [
        "выщелачивани", "кучн", "хлорн", "цианидн", "экстракц",
        "продуктивн", "руд", "малахит", "азурит", "кислот", "раствор",
    ]),
    ("Пирометаллургия", [
        "плавк", "конверт", "обжиг", "шлак", "штейн", "матт",
        "возгон", "печь", "концентрат", "so2",
    ]),
    ("Электролиз", [
        "электролиз", "электровыскан", "электрорафинир", "катод", "анод",
        "ток", "напряжен", "выпрям", "шлам", "плотность",
    ]),
    ("Очистка растворов", [
        "осаждени", "цементац", "сорбц", "очистк", "желез", "свинец",
        "мышьяк", "сурьма", "цинк", "гётит", "ярозит", "силикагел",
    ]),
    ("Получение солей", [
        "сульфат", "кобальт", "литий", "кристаллиз", "сподумен",
        "гидроксид", "никель класс", "карбонат",
    ]),
    ("Переработка штейнов", [
        "файнштейн", "хибинетт", "cesl", "никкельвер", "niihama",
        "sandouville", "штейн", "медно-никел",
    ]),
]

_TYPE_NAMES: dict[str, str] = {
    "material": "Материалы",
    "process": "Технологические процессы",
    "equipment": "Оборудование",
    "condition": "Условия процесса",
    "claim": "Утверждения",
    "expert": "Эксперты",
    "publication": "Публикации",
    "location": "Локации",
    "organization": "Организации",
}


class StructuralChunk(TypedDict):
    id: str
    text: str
    page: int | None
    sheet: str | None
    cell_range: str | None


class Neo4jElasticsearchKnowledgeBase:
    """Production GraphRAG executor for LLM-generated, validated retrieval plans."""

    def __init__(self, settings: Settings) -> None:
        self._seed = InMemoryKnowledgeBase()
        self._findings = {finding.id: finding for finding in self._seed.all_findings()}
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
        )
        self._search = Elasticsearch(settings.elasticsearch_url)
        self._settings = settings
        self._embeddings = YandexEmbeddingClient(settings) if settings.use_yandex else None
        self._embedding_dimensions = settings.embedding_dimensions
        self._initialized = False
        self._lock = threading.Lock()

    def _ensure_seed(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            graph = self._seed.full_graph()
            with self._driver.session() as session:
                session.run(
                    "CREATE CONSTRAINT entity_id IF NOT EXISTS "
                    "FOR (n:Entity) REQUIRE n.id IS UNIQUE"
                )
                for node in graph.nodes:
                    session.run(
                        """
                        MERGE (n:Entity {id: $id})
                        SET n.label = $label, n.type = $type, n.confidence = $confidence,
                            n.metadata = $metadata
                        """,
                        id=node.id,
                        label=node.label,
                        type=node.type.value,
                        confidence=node.confidence,
                        metadata=json.dumps(node.metadata, ensure_ascii=False),
                    )
                for edge in graph.edges:
                    relation = edge.relation
                    if not relation.replace("_", "").isalnum() or relation.upper() != relation:
                        raise ValueError(f"Unsafe seed relation: {relation}")
                    session.run(
                        f"""
                        MATCH (a:Entity {{id: $source}}), (b:Entity {{id: $target}})
                        MERGE (a)-[r:{relation} {{id: $id}}]->(b)
                        SET r.confidence = $confidence
                        """,
                        source=edge.source,
                        target=edge.target,
                        id=edge.id,
                        confidence=edge.confidence,
                    )

            # Настраиваем индекс с русским анализатором для корректной обработки кириллицы
            _ru_analyzer = {
                "analysis": {
                    "analyzer": {
                        "ru": {"type": "russian", "stopwords": "_russian_"}
                    }
                }
            }

            # Загружаем существующие findings из ES в in-memory кэш
            preserved_findings: list[Finding] = []
            preserved_chunks: list[dict] = []
            if self._search.indices.exists(index=FINDING_INDEX):
                try:
                    for hit in helpers.scan(
                        self._search,
                        index=FINDING_INDEX,
                        query={"query": {"match_all": {}}},
                        _source=["finding_json"],
                    ):
                        payload = (hit.get("_source") or {}).get("finding_json")
                        if payload:
                            preserved_findings.append(Finding.model_validate_json(payload))
                except Exception:
                    pass
            if self._search.indices.exists(index=CHUNK_INDEX):
                try:
                    for hit in helpers.scan(
                        self._search,
                        index=CHUNK_INDEX,
                        query={"query": {"match_all": {}}},
                        _source=True,
                    ):
                        preserved_chunks.append(hit.get("_source", {}))
                except Exception:
                    pass

            # Объединяем сохранённые findings с seed-данными
            for finding in preserved_findings:
                if finding.id not in self._findings:
                    self._findings[finding.id] = finding

            # Создаём индексы только если они не существуют — не удаляем существующие,
            # чтобы сохранить preloaded данные между перезапусками контейнера
            if not self._search.indices.exists(index=FINDING_INDEX):
                self._search.indices.create(
                    index=FINDING_INDEX,
                    settings=_ru_analyzer,
                    mappings={
                        "properties": {
                            "statement": {"type": "text", "analyzer": "ru"},
                            "status": {"type": "keyword"},
                            "confidence": {"type": "float"},
                            "evidence": {"type": "text", "analyzer": "ru"},
                            "finding_json": {"type": "keyword", "index": False},
                            "embedding": {
                                "type": "dense_vector",
                                "dims": self._embedding_dimensions,
                                "index": True,
                                "similarity": "cosine",
                            },
                        }
                    },
                )
                # Индекс создан впервые — индексируем все seed findings
                for finding in self._findings.values():
                    self._index_finding(finding)
            if not self._search.indices.exists(index=CHUNK_INDEX):
                self._search.indices.create(
                    index=CHUNK_INDEX,
                    settings=_ru_analyzer,
                    mappings={
                        "properties": {
                            "text": {"type": "text", "analyzer": "ru"},
                            "title": {"type": "text", "analyzer": "ru", "fields": {"keyword": {"type": "keyword"}}},
                            "source_path": {"type": "keyword"},
                            "document_id": {"type": "keyword"},
                            "finding_json": {"type": "keyword", "index": False},
                        }
                    },
                )
                # Переиндексируем сохранённые chunks, если они были
                if preserved_chunks:
                    chunk_actions = [
                        {"_index": CHUNK_INDEX, "_id": hit.get("_id"), "_source": hit}
                        for hit in preserved_chunks
                        if hit
                    ]
                    helpers.bulk(self._search, chunk_actions, refresh=False)
            self._search.indices.refresh(index=FINDING_INDEX)
            self._search.indices.refresh(index=CHUNK_INDEX)
            self._initialized = True

    def ingest(self, document: DocumentRequest, extraction: ExtractionResult) -> DocumentReceipt:
        self._ensure_seed()
        checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        document_id = stable_uuid(checksum)
        with self._driver.session() as session:
            if session.run(
                "MATCH (d:Entity {id: $id}) "
                "WHERE coalesce(d.semantic_extracted, false) "
                "RETURN d.id AS id",
                id=f"document-{document_id}",
            ).single():
                return DocumentReceipt(
                    document_id=document_id,
                    checksum=checksum,
                    status="duplicate",
                    extracted_claims=0,
                )

        previous_finding_ids = set(self._findings)
        receipt = self._seed.ingest(document, extraction)
        if receipt.status == "duplicate":
            return receipt

        graph = self._seed.full_graph()
        with self._driver.session() as session:
            for node in graph.nodes:
                session.run(
                    """
                    MERGE (n:Entity {id: $id})
                    SET n.label = $label, n.type = $type, n.confidence = $confidence,
                        n.metadata = $metadata
                    """,
                    id=node.id,
                    label=node.label,
                    type=node.type.value,
                    confidence=node.confidence,
                    metadata=json.dumps(node.metadata, ensure_ascii=False),
                )
            for edge in graph.edges:
                relation = edge.relation
                if not relation.replace("_", "").isalnum() or relation.upper() != relation:
                    raise ValueError(f"Unsafe extracted relation: {relation}")
                session.run(
                    f"""
                    MATCH (a:Entity {{id: $source}}), (b:Entity {{id: $target}})
                    MERGE (a)-[r:{relation} {{id: $id}}]->(b)
                    SET r.confidence = $confidence
                    """,
                    source=edge.source,
                    target=edge.target,
                    id=edge.id,
                    confidence=edge.confidence,
                )
            session.run(
                "MATCH (d:Entity {id: $id}) SET d.semantic_extracted = true",
                id=f"document-{receipt.document_id}",
            )

        self._findings = {finding.id: finding for finding in self._seed.all_findings()}
        for finding_id, finding in self._findings.items():
            if finding_id not in previous_finding_ids:
                self._index_finding(finding)
        self._search.indices.refresh(index=FINDING_INDEX)
        return receipt

    def retrieve(
        self,
        plan: QueryPlan,
        retrieval_plan: RetrievalPlan,
        allowed_data_classes: set[str] | None = None,
    ) -> RetrievalContext:
        self._ensure_seed()
        findings = self.rank_findings(retrieval_plan.lexical_query, 6, "hybrid")
        finding_ids = [finding.id for finding in findings]
        if retrieval_plan.semantic_query != retrieval_plan.lexical_query:
            semantic_findings = self.rank_findings(retrieval_plan.semantic_query, 6, "semantic")
            findings_by_id = {finding.id: finding for finding in findings}
            findings_by_id.update({finding.id: finding for finding in semantic_findings})
            findings = [findings_by_id[item] for item in finding_ids if item in findings_by_id]

        if not findings:
            findings = self._seed.all_findings()[:4]

        # Исключаем superseded и применяем числовые фильтры из QueryPlan
        findings = [f for f in findings if f.superseded_by is None]
        # Pre-retrieval ACL: фильтруем по data_class до возврата вызывающему
        if allowed_data_classes is not None:
            findings = [f for f in findings if f.data_class in allowed_data_classes]
        findings = InMemoryKnowledgeBase._apply_numeric_filters(findings, plan.numeric_filters)

        depth = min(max(retrieval_plan.max_hops, 1), 4)
        cypher = f"""
        MATCH (anchor:Entity)
        WHERE any(name IN $entities WHERE toLower(anchor.label) CONTAINS toLower(name))
        MATCH path=(anchor)-[*1..{depth}]-(neighbor:Entity)
        WHERE all(rel IN relationships(path) WHERE type(rel) IN $relations)
        WITH collect(DISTINCT path)[0..20] AS paths
        UNWIND paths AS path
        UNWIND nodes(path) AS node
        WITH paths, collect(DISTINCT node) AS nodes
        UNWIND paths AS path
        UNWIND relationships(path) AS rel
        RETURN [node IN nodes | properties(node)] AS nodes,
               collect(DISTINCT {{id: rel.id, source: startNode(rel).id,
                                  target: endNode(rel).id, relation: type(rel),
                                  confidence: rel.confidence}}) AS edges
        """
        with self._driver.session() as session:
            record = session.run(
                cypher,
                entities=retrieval_plan.entity_names,
                relations=retrieval_plan.relation_types,
            ).single()
        graph = self._record_to_graph(record) if record else self.full_graph()
        return RetrievalContext(
            findings=[finding.model_copy(deep=True) for finding in findings],
            graph=graph,
            community_summaries=(
                self._seed.full_graph().communities if retrieval_plan.use_global_context else []
            ),
        )

    def rank_findings(self, query: str, top_k: int, mode: str = "hybrid") -> list[Finding]:
        self._ensure_seed()
        lexical = self._search.search(
            index=FINDING_INDEX,
            size=top_k * 3,
            query={
                "multi_match": {
                    "query": query,
                    "fields": ["statement^3", "evidence"],
                    "type": "best_fields",
                }
            },
        )
        lexical_ids = [str(hit["_id"]) for hit in lexical["hits"]["hits"]]
        lexical_findings = self._load_findings(lexical_ids)
        chunks = self._search.search(
            index=CHUNK_INDEX,
            size=top_k * 3,
            query={
                "multi_match": {
                    "query": query,
                    "fields": ["text^3", "title^2"],
                    "type": "best_fields",
                }
            },
        )
        chunk_findings = [
            Finding.model_validate_json(hit["_source"]["finding_json"])
            for hit in chunks["hits"]["hits"]
            if hit.get("_source", {}).get("finding_json")
        ]
        chunk_ids = [finding.id for finding in chunk_findings]
        semantic_ids: list[str] = []
        if self._embeddings and mode in {"hybrid", "semantic"}:
            try:
                vector = self._embeddings.query(query)
                semantic = self._search.search(
                    index=FINDING_INDEX,
                    size=top_k * 3,
                    knn={
                        "field": "embedding",
                        "query_vector": vector,
                        "k": top_k * 3,
                        "num_candidates": 400,
                    },
                    source=False,
                )
                semantic_ids = [str(hit["_id"]) for hit in semantic["hits"]["hits"]]
            except Exception:
                semantic_ids = []
        semantic_findings = self._load_findings(semantic_ids)
        candidates = {
            finding.id: finding
            for finding in [*lexical_findings, *chunk_findings, *semantic_findings]
        }
        # Взвешенный RRF: findings (1.0), chunks (0.3), semantic (0.6)
        if mode == "lexical":
            ranked_lists = [(lexical_ids, 1.0), (chunk_ids, 0.3)]
        elif mode == "semantic":
            ranked_lists = [(semantic_ids, 1.0)]
        else:
            ranked_lists = [(lexical_ids, 1.0), (chunk_ids, 0.3), (semantic_ids, 0.6)]
        scores: dict[str, float] = {}
        for ranking, weight in ranked_lists:
            for rank, finding_id in enumerate(ranking, start=1):
                scores[finding_id] = scores.get(finding_id, 0) + weight / (5 + rank)
        # Получаем больше кандидатов для LLM reranking
        finding_ids = sorted(scores, key=scores.__getitem__, reverse=True)[:max(top_k * 3, 10)]
        rerank_candidates = [candidates[item] for item in finding_ids if item in candidates]

        # LLM reranking для hybrid режима — батч-запрос к Yandex GPT
        if mode == "hybrid" and self._settings and self._settings.use_yandex and len(rerank_candidates) > top_k:
            try:
                import httpx
                statements_block = "\n".join(
                    f"{i + 1}. {f.statement[:300]}" for i, f in enumerate(rerank_candidates)
                )
                resp = httpx.post(
                    f"{self._settings.yandex_base_url}/chat/completions",
                    headers={
                        "Authorization": f"Api-Key {self._settings.yandex_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._settings.resolved_yandex_model_uri,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Ты эксперт по металлургии. Оцени релевантность каждого "
                                    "утверждения запросу от 0 до 10. Верни JSON-объект с "
                                    "числовыми ключами (1, 2, 3, ...) и значениями-оценками."
                                ),
                            },
                            {
                                "role": "user",
                                "content": (
                                    f"Запрос: {query}\n\nУтверждения:\n{statements_block}\n\n"
                                    'Верни JSON: {"1": score1, "2": score2, ...}'
                                ),
                            },
                        ],
                        "temperature": 0.0,
                        "response_format": {"type": "json_object"},
                    },
                    timeout=60,
                )
                content = resp.json()["choices"][0]["message"]["content"].strip()
                # Очищаем от markdown-обёртки
                if content.startswith("```"):
                    content = content.split("\n", 1)[-1] if content.startswith("```json") else content[3:]
                if content.endswith("```"):
                    content = content[:-3].strip()
                llm_scores = json.loads(content)
                scored = []
                for i, finding in enumerate(rerank_candidates):
                    raw = llm_scores.get(str(i + 1), llm_scores.get(i + 1, 0))
                    scored.append((finding, float(raw)))
                scored.sort(key=lambda x: x[1], reverse=True)
                return [f for f, _ in scored[:top_k]]
            except Exception:
                pass  # Fallback на RRF при ошибке LLM

        return rerank_candidates[:top_k]

    def document_count(self) -> int:
        self._ensure_seed()
        with self._driver.session() as session:
            record = session.run(
                "MATCH (n:Entity {type: 'publication'}) RETURN count(n) AS count"
            ).single()
        return int(record["count"]) if record else 0

    def index_document(
        self, document: DocumentRequest, source_path: str
    ) -> StructuralDocumentReceipt:
        self._ensure_seed()
        checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        document_id = stable_uuid(checksum)
        document_node_id = f"document-{document_id}"
        with self._driver.session() as session:
            existing = session.run(
                "MATCH (d:Entity {id: $id}) RETURN d.id AS id",
                id=document_node_id,
            ).single()
            if existing:
                count = self._search.count(
                    index=CHUNK_INDEX,
                    query={"term": {"document_id": str(document_id)}},
                )["count"]
                return StructuralDocumentReceipt(
                    document_id=document_id,
                    checksum=checksum,
                    status="duplicate",
                    chunks=int(count),
                )

        chunks = self._chunks(document)
        document_metadata = json.dumps(
            {"source_path": source_path, "checksum": checksum, "stage": "structural"},
            ensure_ascii=False,
        )
        with self._driver.session() as session:
            session.run(
                """
                MERGE (d:Entity {id: $id})
                SET d.label = $label, d.type = 'publication', d.confidence = 1.0,
                    d.metadata = $metadata, d.semantic_extracted = false
                WITH d
                UNWIND $chunks AS chunk
                MERGE (c:Entity {id: chunk.id})
                SET c.label = chunk.label, c.type = 'chunk', c.confidence = 1.0,
                    c.metadata = chunk.metadata
                MERGE (d)-[r:HAS_CHUNK {id: chunk.edge_id}]->(c)
                SET r.confidence = 1.0
                """,
                id=document_node_id,
                label=document.title,
                metadata=document_metadata,
                chunks=[
                    {
                        "id": item["id"],
                        "label": str(item["text"])[:160],
                        "metadata": json.dumps(
                            {
                                "page": item["page"],
                                "sheet": item["sheet"],
                                "cell_range": item["cell_range"],
                            },
                            ensure_ascii=False,
                        ),
                        "edge_id": f"{document_node_id}-has-{item['id']}",
                    }
                    for item in chunks
                ],
            )
        actions = []
        for item in chunks:
            evidence = EvidenceLocator(
                document_id=document_id,
                source_title=document.title,
                page=item["page"],
                sheet=item["sheet"],
                cell_range=item["cell_range"],
                quote=str(item["text"])[:1200],
            )
            finding = Finding(
                id=str(item["id"]),
                statement=str(item["text"])[:4000],
                confidence=0.7,
                evidence=[evidence],
                status="hypothesis",
            )
            actions.append(
                {
                    "_index": CHUNK_INDEX,
                    "_id": item["id"],
                    "_source": {
                        "text": item["text"],
                        "title": document.title,
                        "source_path": source_path,
                        "document_id": str(document_id),
                        "finding_json": finding.model_dump_json(),
                    },
                }
            )
        if actions:
            helpers.bulk(self._search, actions, refresh=False)
        return StructuralDocumentReceipt(
            document_id=document_id,
            checksum=checksum,
            status="created",
            chunks=len(chunks),
        )

    def corpus_stats(self) -> CorpusStats:
        self._ensure_seed()
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
                         AND coalesce(n.semantic_extracted, false)
                         THEN 1 END) AS semantic_documents
                """
            ).single()
        if not record:
            return CorpusStats(documents=0, chunks=0, claims=0, entities=0, semantic_documents=0)
        return CorpusStats(**{key: int(record[key]) for key in CorpusStats.model_fields})

    @staticmethod
    def _chunks(document: DocumentRequest) -> list[StructuralChunk]:
        chunks: list[StructuralChunk] = []
        for fragment_index, fragment in enumerate(document.fragments):
            text = fragment.text.strip()
            for offset in range(0, len(text), 3700):
                part = text[offset : offset + 4000].strip()
                if len(part) < 40:
                    continue
                stable_key = f"{document.title}:{fragment_index}:{offset}:{part[:80]}"
                chunk_id = f"chunk-{stable_uuid(stable_key)}"
                chunks.append(
                    {
                        "id": chunk_id,
                        "text": part,
                        "page": fragment.page,
                        "sheet": fragment.sheet,
                        "cell_range": fragment.cell_range,
                    }
                )
        return chunks

    def _index_finding(self, finding: Finding) -> None:
        indexed_document: dict[str, object] = {
            "statement": finding.statement,
            "status": finding.status,
            "confidence": finding.confidence,
            "evidence": " ".join(item.quote for item in finding.evidence),
            "finding_json": finding.model_dump_json(),
        }
        if self._embeddings:
            try:
                embed_text = f"{finding.statement} {' '.join(ev.quote for ev in finding.evidence)}"
                indexed_document["embedding"] = self._embeddings.document(embed_text)
            except Exception:
                pass
        self._search.index(
            index=FINDING_INDEX,
            id=finding.id,
            document=indexed_document,
            refresh=False,
        )

    def _load_findings(self, finding_ids: list[str]) -> list[Finding]:
        if not finding_ids:
            return []
        response = self._search.mget(index=FINDING_INDEX, ids=finding_ids)
        findings: list[Finding] = []
        for document in response["docs"]:
            source = document.get("_source") or {}
            payload = source.get("finding_json")
            if payload:
                findings.append(Finding.model_validate_json(payload))
                continue
            finding_id = str(document.get("_id", ""))
            if finding_id in self._findings:
                findings.append(self._findings[finding_id].model_copy(deep=True))
        return findings

    def all_findings(self) -> list[Finding]:
        """Возвращает активные findings (не superseded) из in-memory кэша."""
        self._ensure_seed()
        return [
            finding.model_copy(deep=True)
            for finding in self._findings.values()
            if finding.superseded_by is None
        ]

    def full_graph(self) -> GraphSnapshot:
        """Возвращает граф знаний с алгоритмической кластеризацией (Louvain)."""
        self._ensure_seed()
        with self._driver.session() as session:
            # Получаем все не-chunk узлы
            node_record = session.run(
                """
                MATCH (node:Entity)
                WHERE node.type <> 'chunk'
                RETURN collect(DISTINCT properties(node)) AS nodes
                """
            ).single()
            # Получаем все семантические рёбра (исключаем HAS_CHUNK)
            edge_record = session.run(
                """
                MATCH (a:Entity)-[rel]->(b:Entity)
                WHERE a.type <> 'chunk' AND b.type <> 'chunk'
                  AND type(rel) <> 'HAS_CHUNK'
                RETURN collect(DISTINCT {
                    id: rel.id, source: a.id, target: b.id,
                    relation: type(rel), confidence: rel.confidence
                }) AS edges
                """
            ).single()

        if not node_record or not node_record["nodes"]:
            return self._seed.full_graph()

        # Парсим узлы
        nodes: list[GraphNode] = []
        for item in node_record["nodes"]:
            if not item:
                continue
            try:
                nodes.append(
                    GraphNode(
                        id=item["id"],
                        label=item.get("label", item["id"]),
                        type=NodeType(item["type"]),
                        confidence=float(item.get("confidence", 1)),
                        metadata=json.loads(item.get("metadata") or "{}"),
                    )
                )
            except (KeyError, ValueError):
                continue

        # Парсим рёбра
        raw_edges = edge_record["edges"] if edge_record else []
        edges: list[GraphEdge] = []
        for item in raw_edges:
            if not item or not item.get("id"):
                continue
            edges.append(
                GraphEdge(
                    id=item["id"],
                    source=item["source"],
                    target=item["target"],
                    relation=item["relation"],
                    confidence=float(item.get("confidence", 1)),
                )
            )

        # Фильтруем: оставляем только узлы, участвующие в рёбрах,
        # либо доменные сущности (имеющие metadata.domain)
        connected_ids: set[str] = set()
        for edge in edges:
            connected_ids.add(edge.source)
            connected_ids.add(edge.target)

        filtered_nodes = [
            node for node in nodes
            if node.id in connected_ids
            or node.metadata.get("domain")
            or node.id.startswith("dom-")
        ]
        # Ограничиваем сверху, чтобы не перегружать фронтенд
        if len(filtered_nodes) > 600:
            # Приоритет: узлы с рёбрами, затем доменные сущности
            prioritized = sorted(
                filtered_nodes,
                key=lambda n: (
                    n.id in connected_ids,
                    bool(n.metadata.get("domain")),
                    n.id.startswith("dom-"),
                ),
                reverse=True,
            )
            filtered_nodes = prioritized[:600]

        filtered_node_ids = {n.id for n in filtered_nodes}
        filtered_edges = [
            edge for edge in edges
            if edge.source in filtered_node_ids and edge.target in filtered_node_ids
        ]

        # Обнаружение сообществ (Louvain / greedy modularity)
        communities = self._detect_communities(filtered_nodes, filtered_edges)

        return GraphSnapshot(
            nodes=filtered_nodes,
            edges=filtered_edges,
            communities=communities,
        )

    def _detect_communities(
        self, nodes: list[GraphNode], edges: list[GraphEdge]
    ) -> list[str]:
        """Обнаруживает сообщества алгоритмом Louvain и именует их по ключевым словам.

        Назначает metadata.community на каждый узел.
        Возвращает список имён сообществ.
        """
        if not nodes:
            return []

        node_by_id = {n.id: n for n in nodes}

        if not edges:
            # Нет рёбер — все узлы в одном сообществе
            for node in nodes:
                node.metadata["community"] = "База знаний"
            return ["База знаний"]

        # Строим ненаправленный граф для community detection
        graph = nx.Graph()
        for node in nodes:
            graph.add_node(node.id)
        for edge in edges:
            if edge.source in node_by_id and edge.target in node_by_id:
                graph.add_edge(edge.source, edge.target, weight=edge.confidence)

        # Запускаем Leiden (предпочтительно) или Louvain для обнаружения сообществ
        communities_sets: list[set[str]] = []
        try:
            import igraph as ig
            import leidenalg
            node_list = list(graph.nodes())
            node_idx = {node: i for i, node in enumerate(node_list)}
            ig_edges = [(node_idx[u], node_idx[v]) for u, v in graph.edges()]
            ig_graph = ig.Graph(n=len(node_list), edges=ig_edges, directed=False)
            for u, v, d in graph.edges(data=True):
                ig_graph.es[ig_graph.get_eid(node_idx[u], node_idx[v])]["weight"] = d.get("weight", 1.0)
            partition = leidenalg.find_partition(
                ig_graph, leidenalg.ModularityVertexPartition,
                seed=42,
            )
            comm_dict: dict[int, set[str]] = {}
            for i, c in enumerate(partition.membership):
                comm_dict.setdefault(c, set()).add(node_list[i])
            communities_sets = list(comm_dict.values())
            if not (3 <= len(communities_sets) <= 8):
                communities_sets = []
        except ImportError:
            pass
        if not communities_sets:
            for res in (0.2, 0.3, 0.4, 0.6, 0.8, 1.0):
                try:
                    communities_sets = nx.algorithms.community.louvain_communities(
                        graph, seed=42, resolution=res
                    )
                    if 3 <= len(communities_sets) <= 8:
                        break
                except Exception:
                    continue
        if not communities_sets:
            try:
                communities_sets = list(
                    nx.algorithms.community.greedy_modularity_communities(graph)
                )
            except Exception:
                for node in nodes:
                    node.metadata["community"] = "База знаний"
                return ["База знаний"]

        # Сортируем сообщества по размеру (от больших к малым)
        communities_sorted = sorted(communities_sets, key=len, reverse=True)
        community_names: list[str] = []
        used_names: set[str] = set()

        # Малые сообщества (< 15 узлов) объединяем в «Прочие»
        small_comm_nodes: set[str] = set()
        main_communities: list[set[str]] = []
        for comm_node_ids in communities_sorted:
            if len(comm_node_ids) >= 15:
                main_communities.append(comm_node_ids)
            else:
                small_comm_nodes.update(comm_node_ids)

        for comm_node_ids in main_communities:
            labels = [
                node_by_id[nid].label.lower()
                for nid in comm_node_ids
                if nid in node_by_id
            ]
            types = [
                node_by_id[nid].type.value
                for nid in comm_node_ids
                if nid in node_by_id
            ]
            name = self._name_community(labels, types, used_names)
            used_names.add(name)
            community_names.append(name)
            for nid in comm_node_ids:
                if nid in node_by_id:
                    node_by_id[nid].metadata["community"] = name

        # Узлы из малых сообществ → в «Прочие сущности»
        if small_comm_nodes:
            other_name = "Прочие сущности"
            if other_name in used_names:
                other_name = f"Сообщество {len(used_names) + 1}"
            used_names.add(other_name)
            community_names.append(other_name)
            for nid in small_comm_nodes:
                if nid in node_by_id:
                    node_by_id[nid].metadata["community"] = other_name

        return community_names

    @staticmethod
    def _name_community(
        labels: list[str],
        types: list[str],
        used_names: set[str],
    ) -> str:
        """Подбирает имя сообщества по ключевым словам в метках узлов."""
        best_match = None
        best_score = 0
        for comm_name, keywords in _COMMUNITY_KEYWORDS:
            score = sum(
                1
                for label in labels
                for kw in keywords
                if kw in label
            )
            if score > best_score and comm_name not in used_names:
                best_score = score
                best_match = comm_name
        if best_match:
            return best_match
        # Если нет совпадений — используем доминирующий тип сущностей
        if types:
            type_counts = Counter(types)
            dominant_type = type_counts.most_common(1)[0][0]
            base_name = _TYPE_NAMES.get(dominant_type, "Сообщество")
            if base_name not in used_names:
                return base_name
        return f"Сообщество {len(used_names) + 1}"

    def _record_to_graph(self, record: Record) -> GraphSnapshot:
        data = record.data()
        nodes = [
            GraphNode(
                id=item["id"],
                label=item["label"],
                type=NodeType(item["type"]),
                confidence=float(item.get("confidence", 1)),
                metadata=json.loads(item.get("metadata") or "{}"),
            )
            for item in data.get("nodes", [])
            if item
        ]
        edges = [
            GraphEdge(
                id=item["id"],
                source=item["source"],
                target=item["target"],
                relation=item["relation"],
                confidence=float(item.get("confidence", 1)),
            )
            for item in data.get("edges", [])
            if item and item.get("id")
        ]
        return GraphSnapshot(
            nodes=nodes,
            edges=edges,
            communities=self._seed.full_graph().communities,
        )

    def rebuild_domain_graph(self) -> dict[str, int]:
        """Создаёт доменный граф знаний в Neo4j (сущности, рёбра, утверждения)."""
        self._ensure_seed()
        from scientific_tangle.services.graph_rebuilder import rebuild_semantic_graph

        return rebuild_semantic_graph(self._driver)

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
        """Делегирует версионирование в InMemoryKnowledgeBase (seed)."""
        return self._seed.supersede_finding(
            finding_id,
            new_statement,
            new_confidence,
            observations,
            reviewer_id,
            review_date,
            review_reason,
        )

    def claim_history(self, finding_id: str) -> list[Finding]:
        return self._seed.claim_history(finding_id)


def build_knowledge_base(settings: Settings) -> KnowledgeBase:
    if settings.knowledge_backend == "neo4j":
        return Neo4jElasticsearchKnowledgeBase(settings)
    return InMemoryKnowledgeBase()
