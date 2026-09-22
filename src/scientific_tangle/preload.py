from __future__ import annotations

import asyncio
import json
from pathlib import Path

from scientific_tangle.config import get_settings
from scientific_tangle.services.corpus import CorpusCompiler
from scientific_tangle.services.infrastructure import (
    Neo4jElasticsearchKnowledgeBase,
    build_knowledge_base,
)
from scientific_tangle.services.ingestion import IngestionService
from scientific_tangle.services.preload import PreloadService
from scientific_tangle.services.provider import build_provider
from scientific_tangle.services.resolution import EntityResolutionWorkbench


async def main() -> None:
    settings = get_settings()
    knowledge = build_knowledge_base(settings)
    provider = build_provider(settings)
    resolution = EntityResolutionWorkbench(settings)
    ingestion = IngestionService(knowledge, provider, resolution)
    source_root = Path(settings.source_root)
    structural = None
    if settings.preload_mode in {"full", "structural"}:
        structural = CorpusCompiler(
            knowledge,
            source_root,
            max_file_bytes=settings.corpus_max_file_bytes,
        ).compile(settings.corpus_preload_limit)
    manifest = Path(__file__).with_name("preload_manifest.json")
    semantic = None
    if settings.preload_mode in {"full", "semantic"}:
        semantic = await PreloadService(ingestion, source_root).run(manifest)

    # Перестройка доменного графа знаний: сущности, рёбра, утверждения
    domain_stats = None
    if isinstance(knowledge, Neo4jElasticsearchKnowledgeBase):
        domain_stats = knowledge.rebuild_domain_graph()
        print(f"Domain graph rebuilt: {domain_stats}")

    print(
        json.dumps(
            {
                "structural": json.loads(structural.model_dump_json()) if structural else None,
                "semantic": json.loads(semantic.model_dump_json()) if semantic else None,
                "domain_graph": domain_stats,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if semantic and semantic.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
