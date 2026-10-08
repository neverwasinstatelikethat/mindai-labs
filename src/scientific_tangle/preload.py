from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from scientific_tangle.config import get_settings
from scientific_tangle.services.corpus import CorpusCompiler
from scientific_tangle.services.hypotheses import HypothesisGenerator
from scientific_tangle.services.infrastructure import (
    build_knowledge_base,
)
from scientific_tangle.services.ingestion import IngestionService
from scientific_tangle.services.preload import PreloadService
from scientific_tangle.services.provider import build_provider
from scientific_tangle.services.research_intelligence import (
    ResearchIntelligenceService,
    to_research_claims,
)
from scientific_tangle.services.resolution import EntityResolutionWorkbench


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--semantic-corpus",
        action="store_true",
        help="обработать семантически весь поддерживаемый корпус",
    )
    semantic_corpus = parser.parse_args().semantic_corpus
    settings = get_settings()
    knowledge = build_knowledge_base(settings)
    provider = build_provider(settings)
    resolution = EntityResolutionWorkbench(settings)
    ingestion = IngestionService(
        knowledge, provider, resolution, model=settings.gigachat_graphrag_model
    )
    source_root = Path(settings.source_root)
    structural = None
    if not semantic_corpus and settings.preload_mode in {"full", "structural"}:
        structural = CorpusCompiler(
            knowledge,
            source_root,
            max_file_bytes=settings.corpus_max_file_bytes,
        ).compile(settings.corpus_preload_limit)
    manifest = Path(__file__).with_name("preload_manifest.json")
    semantic = None
    hypothesis_count = 0
    preload = PreloadService(
        ingestion,
        source_root,
        max_file_bytes=settings.corpus_max_file_bytes,
    )
    if semantic_corpus:
        semantic = await preload.run_corpus()
        findings = await asyncio.to_thread(knowledge.semantic_findings)
        graph = await asyncio.to_thread(knowledge.semantic_graph)
        conflicts = ResearchIntelligenceService().detect_conflicts(
            to_research_claims(findings)
        )
        signals = await HypothesisGenerator(provider).generate(graph, findings, conflicts)
        await asyncio.to_thread(knowledge.upsert_hypotheses, signals)
        hypothesis_count = len(signals)
    elif settings.preload_mode in {"full", "semantic"}:
        semantic = await preload.run(manifest)

    print(
        json.dumps(
            {
                "structural": json.loads(structural.model_dump_json()) if structural else None,
                "semantic": json.loads(semantic.model_dump_json()) if semantic else None,
                "hypotheses": hypothesis_count if semantic_corpus else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if semantic and semantic.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
