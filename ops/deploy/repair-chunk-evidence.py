"""Расширить цитаты сохранённых чанков до их исходного текста без вызовов LLM.

Запуск: docker compose exec -T backend python - < ops/deploy/repair-chunk-evidence.py
Повторный запуск безопасен; изменённые или заменённые экспертом чанки пропускаются.
"""

import json
import logging
from itertools import batched

from elasticsearch import Elasticsearch, helpers
from neo4j import GraphDatabase

from scientific_tangle.config import get_settings
from scientific_tangle.domain.contracts import Finding
from scientific_tangle.services.infrastructure import CHUNK_INDEX

logging.basicConfig(level=logging.INFO)
logging.getLogger("elastic_transport.transport").setLevel(logging.WARNING)
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)
settings = get_settings()
updated = skipped = 0
with (
    Elasticsearch(settings.elasticsearch_url) as search,
    GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password)
    ) as driver,
    driver.session() as session,
):
    hits = helpers.scan(
        search,
        index=CHUNK_INDEX,
        query={"query": {"match_all": {}}},
        _source=["text", "finding_json"],
        size=500,
    )
    for batch in batched(hits, 500):
        actions = []
        ranges = {}
        for hit in batch:
            source = hit["_source"]
            finding = Finding.model_validate_json(source["finding_json"])
            text = source.get("text")
            if (
                finding.superseded_by is not None
                or not finding.id.startswith("chunk-")
                or text != finding.statement
                or len(finding.evidence) != 1
                or not text.startswith(finding.evidence[0].quote)
            ):
                skipped += 1
                continue
            locator = finding.evidence[0]
            end = locator.char_start + len(text) if locator.char_start is not None else None
            if locator.quote != text or locator.char_end != end:
                finding.evidence = [locator.model_copy(update={"quote": text, "char_end": end})]
                actions.append(
                    {
                        "_op_type": "update",
                        "_index": CHUNK_INDEX,
                        "_id": hit["_id"],
                        "doc": {"finding_json": finding.model_dump_json()},
                    }
                )
            if end is not None:
                ranges[finding.id] = end
        if actions:
            helpers.bulk(search, actions)
            updated += len(actions)
        if ranges:
            nodes = session.run(
                "UNWIND $ids AS id MATCH (c:Entity {id: id}) "
                "RETURN c.id AS id, c.metadata AS metadata",
                ids=list(ranges),
            ).data()
            rows = [
                {
                    "id": node["id"],
                    "metadata": json.dumps(
                        {**json.loads(node["metadata"] or "{}"), "char_end": ranges[node["id"]]},
                        ensure_ascii=False,
                    ),
                }
                for node in nodes
            ]
            session.run(
                "UNWIND $rows AS row MATCH (c:Entity {id: row.id}) SET c.metadata = row.metadata",
                rows=rows,
            ).consume()
    search.indices.refresh(index=CHUNK_INDEX)
logger.info(
    "chunk_evidence_repair_complete updated=%d skipped=%d",
    updated,
    skipped,
    extra={"updated": updated, "skipped": skipped},
)
