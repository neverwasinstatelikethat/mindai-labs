from __future__ import annotations

from pathlib import Path

from scientific_tangle.domain.contracts import CorpusCompileReport
from scientific_tangle.services.document_parser import (
    SUPPORTED_EXTENSIONS,
    UnsupportedDocumentError,
    parse_document,
)
from scientific_tangle.services.knowledge import KnowledgeBase

# Единственный признак «в файле есть текст, но мы его не достали»: парсер ставит его,
# когда извлечено меньше 20 символов. Любая другая ошибка — отказ пайплайна, а не
# скан без текстового слоя, и в OCR-бэклог его записывать нельзя.
_OCR_NEEDED_MARK = "не удалось извлечь достаточно текста"


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
            except UnsupportedDocumentError as error:
                # OCR-бэклог — только про текстовый слой самого файла.
                if path.suffix.lower() == ".pdf" and _OCR_NEEDED_MARK in str(error).lower():
                    ocr_required += 1
                else:
                    failed += 1
                if len(errors) < 50:
                    errors.append(f"{relative}: {str(error)[:240]}")
            except Exception as error:
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
            # Знаменатель — файлы, которые пайплайн вообще мог обработать при текущем
            # лимите. Прежний len(all_files) считал неподдерживаемые форматы и всё,
            # что не вошло в лимит, как непокрытые: coverage не доходил до 1 даже
            # при полностью скомпилированном корпусе.
            coverage=round(processed / max(len(selected), 1), 4),
            errors=errors,
        )
