from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from scientific_tangle.domain.contracts import (
    DocumentReceipt,
    DocumentRequest,
    PreloadReport,
)
from scientific_tangle.services import preload as preload_module
from scientific_tangle.services.document_parser import UnsupportedDocumentError
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from scientific_tangle.services.preload import PreloadService, _semantic_parts


class _Ingestion:
    def __init__(self) -> None:
        self.completed: dict[UUID, set[str]] = {}
        self.calls: list[tuple[DocumentRequest, UUID, str, int]] = []
        self.fail_titles: set[str] = set()

    async def semantic_parts_completed(self, document_id: UUID, total: int) -> set[str]:
        return self.completed.get(document_id, set())

    async def ingest(
        self,
        document: DocumentRequest,
        *,
        source_document_id: UUID,
        semantic_part_id: str,
        semantic_part_total: int,
    ) -> DocumentReceipt:
        self.calls.append((document, source_document_id, semantic_part_id, semantic_part_total))
        if document.title in self.fail_titles:
            raise RuntimeError("extraction failed")
        self.completed.setdefault(source_document_id, set()).add(semantic_part_id)
        return DocumentReceipt(
            document_id=source_document_id,
            checksum="checksum",
            status="created",
            extracted_claims=1,
        )


def _parsed(filename: str, content: bytes, **kwargs: object) -> DocumentRequest:
    return DocumentRequest(title=Path(filename).stem, text=content.decode(), fragments=[])


@pytest.mark.asyncio
async def test_run_corpus_processes_supported_files_in_stable_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "z.txt").write_text("z" * 30)
    (tmp_path / "a.txt").write_text("a" * 30)
    (tmp_path / "ignored.csv").write_text("i" * 30)
    monkeypatch.setattr(preload_module, "parse_document", _parsed)
    ingestion = _Ingestion()

    report = await PreloadService(ingestion, tmp_path, max_file_bytes=100).run_corpus()

    assert isinstance(report, PreloadReport)
    assert [call[0].title for call in ingestion.calls] == ["a", "z"]
    assert report.total == 2
    assert report.created == 2


@pytest.mark.asyncio
async def test_run_corpus_splits_all_fragments_into_bounded_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "book.txt").write_text("x" * 50)
    monkeypatch.setattr(
        preload_module,
        "parse_document",
        lambda *args, **kwargs: DocumentRequest(
            title="book",
            text="x" * 70_000,
            fragments=[
                {"text": "a" * 40_000, "page": 1},
                {"text": "b" * 30_000, "page": 2},
            ],
        ),
    )
    ingestion = _Ingestion()

    report = await PreloadService(ingestion, tmp_path, max_file_bytes=100).run_corpus()

    assert len(ingestion.calls) >= 3
    assert all(len(call[0].text) <= 32_000 for call in ingestion.calls)
    assert all(len(call[0].text) >= 20 for call in ingestion.calls)
    assert sum(len(call[0].text) for call in ingestion.calls) == 70_000
    assert all(call[3] == len(ingestion.calls) for call in ingestion.calls)
    assert report.claims == len(ingestion.calls)


def test_semantic_fragment_splits_keep_original_character_offsets() -> None:
    document = DocumentRequest(
        title="book",
        text="x" * 40_000,
        fragments=[{"text": "x" * 40_000, "page": 3}],
    )

    parts = _semantic_parts(document)

    assert [part.fragments[0].source_char_start for part in parts] == [0, 32_000]
    evidence = InMemoryKnowledgeBase._locate_evidence(
        parts[1], UUID(int=1), "xxxxx"
    )
    assert (evidence.char_start, evidence.char_end) == (32_000, 32_005)


@pytest.mark.asyncio
async def test_run_corpus_skips_oversize_and_reports_parse_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "large.txt").write_text("x" * 40)
    (tmp_path / "bad.txt").write_text("y" * 30)
    (tmp_path / "scan.pdf").write_bytes(b"pdf")
    (tmp_path / "stat.txt").write_text("s" * 30)
    original_is_file = Path.is_file
    original_stat = Path.stat

    def is_file(path: Path) -> bool:
        return True if path.name == "stat.txt" else original_is_file(path)

    def stat(path: Path, *args: object, **kwargs: object) -> object:
        if path.name == "stat.txt":
            raise PermissionError("stat denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(Path, "stat", stat)

    def parse(filename: str, content: bytes, **kwargs: object) -> DocumentRequest:
        if filename == "bad.txt":
            raise ValueError("parse failed")
        raise UnsupportedDocumentError(preload_module._OCR_NEEDED_MARK)

    monkeypatch.setattr(preload_module, "parse_document", parse)
    ingestion = _Ingestion()

    report = await PreloadService(ingestion, tmp_path, max_file_bytes=35).run_corpus()

    assert report.skipped_oversize == 1
    assert report.ocr_required == 1
    assert report.failed == 2
    assert {item.path for item in report.documents} == {
        "bad.txt",
        "scan.pdf",
        "large.txt",
        "stat.txt",
    }


@pytest.mark.asyncio
async def test_run_corpus_continues_when_file_discovery_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "denied.txt").write_text("x" * 30)
    (tmp_path / "readable.txt").write_text("y" * 30)
    original_is_file = Path.is_file

    def is_file(path: Path) -> bool:
        if path.name == "denied.txt":
            raise PermissionError("stat denied")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.setattr(preload_module, "parse_document", _parsed)
    ingestion = _Ingestion()

    report = await PreloadService(ingestion, tmp_path, max_file_bytes=100).run_corpus()

    assert [call[0].title for call in ingestion.calls] == ["readable"]
    assert report.failed == 1
    assert report.documents[0].path == "denied.txt"


@pytest.mark.asyncio
async def test_run_corpus_resumes_already_semantic_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "book.txt").write_text("x" * 40)
    monkeypatch.setattr(
        preload_module,
        "parse_document",
        lambda *args, **kwargs: DocumentRequest(
            title="book",
            text="x" * 40,
            fragments=[{"text": "x" * 40, "page": 1}],
        ),
    )
    ingestion = _Ingestion()
    service = PreloadService(ingestion, tmp_path, max_file_bytes=100)

    first = await service.run_corpus()
    calls_after_first = len(ingestion.calls)
    second = await service.run_corpus()

    assert calls_after_first == 1
    assert len(ingestion.calls) == calls_after_first
    assert first.created == 1
    assert second.duplicates == 1
