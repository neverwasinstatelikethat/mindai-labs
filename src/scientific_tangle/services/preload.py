from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from scientific_tangle.domain.contracts import (
    DocumentFragment,
    DocumentRequest,
    PreloadDocumentResult,
    PreloadReport,
)
from scientific_tangle.services.document_parser import (
    SUPPORTED_EXTENSIONS,
    UnsupportedDocumentError,
    parse_document,
)
from scientific_tangle.services.ingestion import IngestionService
from scientific_tangle.services.knowledge import stable_uuid

SEMANTIC_PART_CHAR_LIMIT = 32_000
_OCR_NEEDED_MARK = "не удалось извлечь достаточно текста"


class PreloadService:
    def __init__(
        self,
        ingestion: IngestionService,
        source_root: Path,
        *,
        max_file_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        self._ingestion = ingestion
        self._source_root = source_root.resolve()
        self._max_file_bytes = max_file_bytes

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

    async def run_corpus(self) -> PreloadReport:
        """Идемпотентно извлекает смысл из всех поддерживаемых файлов корпуса."""
        started_at = datetime.now(UTC)
        candidates = sorted(
            self._source_root.rglob("*"),
            key=lambda path: (
                str(path.relative_to(self._source_root)).casefold(),
                str(path.relative_to(self._source_root)),
            ),
        )
        files: list[Path] = []
        results: list[PreloadDocumentResult] = []
        for path in candidates:
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            relative = str(path.relative_to(self._source_root)).replace("\\", "/")
            try:
                if path.is_file():
                    files.append(path)
            except Exception as error:
                results.append(
                    PreloadDocumentResult(
                        path=relative,
                        title=path.stem,
                        status="failed",
                        error=f"Не удалось проверить файл: {str(error)[:400]}",
                    )
                )
        skipped_oversize = 0
        ocr_required = 0
        for path in files:
            relative = str(path.relative_to(self._source_root)).replace("\\", "/")
            try:
                file_size = path.stat().st_size
            except Exception as error:
                results.append(
                    PreloadDocumentResult(
                        path=relative,
                        title=path.stem,
                        status="failed",
                        error=f"Не удалось прочитать метаданные файла: {str(error)[:400]}",
                    )
                )
                continue
            if file_size > self._max_file_bytes:
                skipped_oversize += 1
                results.append(
                    PreloadDocumentResult(
                        path=relative,
                        title=path.stem,
                        status="failed",
                        error=f"Размер файла превышает лимит {self._max_file_bytes} байт",
                    )
                )
                continue
            try:
                document = parse_document(path.name, path.read_bytes(), ocr=False)
                parts = _semantic_parts(document)
                document_id = stable_uuid(
                    hashlib.sha256(document.text.encode("utf-8")).hexdigest()
                )
                completed = await self._ingestion.semantic_parts_completed(
                    document_id, len(parts)
                )
                extracted_claims = 0
                newly_ingested = 0
                part_errors: list[str] = []
                for index, part in enumerate(parts):
                    part_id = f"part-{index + 1:05d}"
                    if part_id in completed:
                        continue
                    try:
                        receipt = await self._ingestion.ingest(
                            part,
                            source_document_id=document_id,
                            semantic_part_id=part_id,
                            semantic_part_total=len(parts),
                        )
                        extracted_claims += receipt.extracted_claims
                        newly_ingested += receipt.status == "created"
                    except Exception as error:
                        part_errors.append(f"{part_id}: {str(error)[:240]}")
                # The store marks completion after each successful part. Re-read to
                # avoid treating a partial document as complete after an interrupted run.
                complete_parts = await self._ingestion.semantic_parts_completed(
                    document_id, len(parts)
                )
                status: Literal["created", "duplicate", "failed"] = (
                    "created" if newly_ingested else "duplicate"
                )
                if len(complete_parts) != len(parts) or part_errors:
                    status = "failed"
                results.append(
                    PreloadDocumentResult(
                        path=relative,
                        title=document.title,
                        status=status,
                        extracted_claims=extracted_claims,
                        error=(
                            None
                            if status != "failed"
                            else "; ".join(part_errors)
                            if part_errors
                            else (
                                f"Семантически обработано частей: {len(complete_parts)} "
                                f"из {len(parts)}"
                            )
                        ),
                    )
                )
            except UnsupportedDocumentError as error:
                if path.suffix.lower() == ".pdf" and _OCR_NEEDED_MARK in str(error).lower():
                    ocr_required += 1
                results.append(
                    PreloadDocumentResult(
                        path=relative,
                        title=path.stem,
                        status="failed",
                        error=str(error)[:500],
                    )
                )
            except Exception as error:
                results.append(
                    PreloadDocumentResult(
                        path=relative,
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
            failed=max(
                sum(item.status == "failed" for item in results)
                - skipped_oversize
                - ocr_required,
                0,
            ),
            claims=sum(item.extracted_claims for item in results),
            documents=results,
            skipped_oversize=skipped_oversize,
            ocr_required=ocr_required,
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


def _semantic_parts(document: DocumentRequest) -> list[DocumentRequest]:
    """Разбивает все фрагменты без потерь, сохраняя их исходные локаторы."""
    fragments = document.fragments or [DocumentFragment(text=document.text)]
    pieces: list[DocumentFragment] = []
    for fragment in fragments:
        text = fragment.text
        pieces.extend(
            fragment.model_copy(
                update={
                    "text": text[start : start + SEMANTIC_PART_CHAR_LIMIT],
                    "source_char_start": fragment.source_char_start + start,
                }
            )
            for start in range(0, len(text), SEMANTIC_PART_CHAR_LIMIT)
        )
    groups: list[list[DocumentFragment]] = []
    current: list[DocumentFragment] = []
    current_size = 0
    for fragment in pieces:
        separator_size = 2 if current else 0
        if (
            current
            and current_size + separator_size + len(fragment.text)
            > SEMANTIC_PART_CHAR_LIMIT
        ):
            groups.append(current)
            current = []
            current_size = 0
            separator_size = 0
        current.append(fragment)
        current_size += separator_size + len(fragment.text)
    if current:
        groups.append(current)
    while len(groups) > 1 and _group_char_count(groups[-1]) < 20:
        previous = groups[-2]
        short = groups[-1]
        needed = 20 - _group_char_count(short)
        tail = previous[-1]
        if len(tail.text) > needed:
            previous[-1] = tail.model_copy(update={"text": tail.text[:-needed]})
            short.insert(
                0,
                tail.model_copy(
                    update={
                        "text": tail.text[-needed:],
                        "source_char_start": tail.source_char_start + len(tail.text) - needed,
                    }
                ),
            )
        else:
            previous_size_after_move = _group_char_count(previous[:-1])
            if previous_size_after_move >= 20:
                short.insert(0, previous.pop())
            else:
                # The preceding group has no single long fragment to split. Move
                # just enough text from its tail, preserving each original locator.
                take = 20 - _group_char_count(short)
                short.insert(
                    0,
                    tail.model_copy(
                        update={
                            "text": tail.text[-take:],
                            "source_char_start": tail.source_char_start + len(tail.text) - take,
                        }
                    ),
                )
                previous[-1] = tail.model_copy(update={"text": tail.text[:-take]})
        if _group_char_count(previous) < 20:
            groups[-2:] = [previous, short]
            break
    return [
        document.model_copy(
            update={
                "text": "\n\n".join(fragment.text for fragment in group),
                "fragments": group,
            }
        )
        for group in groups
    ]


def _group_char_count(fragments: list[DocumentFragment]) -> int:
    return sum(len(fragment.text) for fragment in fragments) + 2 * max(len(fragments) - 1, 0)
