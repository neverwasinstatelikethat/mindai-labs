from __future__ import annotations

from pathlib import Path

from scientific_tangle.domain.contracts import CorpusCompileReport
from scientific_tangle.services.document_parser import SUPPORTED_EXTENSIONS, parse_document
from scientific_tangle.services.knowledge import KnowledgeBase


class CorpusCompiler:
    def __init__(
        self,
        knowledge: KnowledgeBase,
        source_root: Path,
        *,
        max_file_bytes: int,
    ) -> None:
        self._knowledge = knowledge
        self._source_root = source_root.resolve()
        self._max_file_bytes = max_file_bytes

    def compile(self, limit: int) -> CorpusCompileReport:
        all_files = sorted(
            (path for path in self._source_root.rglob("*") if path.is_file()),
            key=lambda path: str(path.relative_to(self._source_root)).lower(),
        )
        supported = [path for path in all_files if path.suffix.lower() in SUPPORTED_EXTENSIONS]
        eligible = [path for path in supported if path.stat().st_size <= self._max_file_bytes]
        selected = eligible[:limit]
        created = 0
        duplicates = 0
        failed = 0
        chunks = 0
        ocr_required = 0
        errors: list[str] = []
        for path in selected:
            relative = str(path.relative_to(self._source_root)).replace("\\", "/")
            try:
                document = parse_document(path.name, path.read_bytes(), ocr=False)
                receipt = self._knowledge.index_document(document, relative)
                created += receipt.status == "created"
                duplicates += receipt.status == "duplicate"
                chunks += receipt.chunks
            except Exception as error:
                if path.suffix.lower() == ".pdf":
                    ocr_required += 1
                else:
                    failed += 1
                if len(errors) < 50:
                    errors.append(f"{relative}: {str(error)[:240]}")
        processed = created + duplicates
        return CorpusCompileReport(
            discovered=len(all_files),
            eligible=len(eligible),
            processed=processed,
            created=created,
            duplicates=duplicates,
            failed=failed,
            chunks=chunks,
            skipped_unsupported=len(all_files) - len(supported),
            skipped_oversize=len(supported) - len(eligible),
            ocr_required=ocr_required,
            coverage=round(processed / max(len(all_files), 1), 4),
            errors=errors,
        )
