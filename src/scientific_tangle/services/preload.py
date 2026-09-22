from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from scientific_tangle.domain.contracts import (
    DocumentRequest,
    PreloadDocumentResult,
    PreloadReport,
)
from scientific_tangle.services.document_parser import parse_document
from scientific_tangle.services.ingestion import IngestionService


class PreloadService:
    def __init__(self, ingestion: IngestionService, source_root: Path) -> None:
        self._ingestion = ingestion
        self._source_root = source_root.resolve()

    async def run(self, manifest_path: Path, max_fragments: int = 8) -> PreloadReport:
        started_at = datetime.now(UTC)
        manifest = json.loads(manifest_path.read_text("utf-8"))
        results: list[PreloadDocumentResult] = []
        for item in manifest:
            relative_path = str(item["path"])
            path = (self._source_root / relative_path).resolve()
            if self._source_root not in path.parents:
                raise ValueError(f"Путь выходит за пределы корпуса: {relative_path}")
            try:
                parsed = parse_document(
                    path.name,
                    path.read_bytes(),
                    language=item.get("language", "ru"),
                    geography=item.get("geography"),
                    year=item.get("year"),
                )
                document = self._sample_document(parsed, max_fragments)
                receipt = await self._ingestion.ingest(document)
                results.append(
                    PreloadDocumentResult(
                        path=relative_path,
                        title=document.title,
                        status=receipt.status,
                        extracted_claims=receipt.extracted_claims,
                    )
                )
            except Exception as error:
                results.append(
                    PreloadDocumentResult(
                        path=relative_path,
                        title=path.stem,
                        status="failed",
                        error=str(error)[:500],
                    )
                )
        return PreloadReport(
            started_at=started_at,
            finished_at=datetime.now(UTC),
            total=len(results),
            created=sum(item.status == "created" for item in results),
            duplicates=sum(item.status == "duplicate" for item in results),
            failed=sum(item.status == "failed" for item in results),
            claims=sum(item.extracted_claims for item in results),
            documents=results,
        )

    @staticmethod
    def _sample_document(document: DocumentRequest, max_fragments: int) -> DocumentRequest:
        fragments = document.fragments
        limit = max(max_fragments, 1)
        # Равномерная выборка требует минимум двух опорных точек: при limit=1
        # знаменатель (limit - 1) обнулялся и падать должен был на ноль.
        if len(fragments) <= limit or limit < 2:
            selected = fragments[:limit]
        else:
            positions = {
                round(index * (len(fragments) - 1) / (limit - 1))
                for index in range(limit)
            }
            selected = [fragments[index] for index in sorted(positions)]
        text = "\n\n".join(fragment.text for fragment in selected)
        return document.model_copy(update={"text": text, "fragments": selected})
