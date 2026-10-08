import io

import pytest
from openpyxl import Workbook

from scientific_tangle.domain.contracts import (
    DocumentRequest,
    ExtractedClaim,
    ExtractedEntity,
    ExtractionResult,
    IngestionBundle,
    NodeType,
)
from scientific_tangle.services.document_parser import parse_document
from scientific_tangle.services.ingestion import IngestionService
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from tests.fakes import ScriptedProvider


def extraction() -> ExtractionResult:
    return ExtractionResult(
        entities=[
            ExtractedEntity(
                name="шахтная вода",
                canonical_name="Шахтная вода",
                type=NodeType.MATERIAL,
            ),
            ExtractedEntity(
                name="обратный осмос",
                canonical_name="Обратный осмос",
                type=NodeType.PROCESS,
            ),
        ],
        claims=[
            ExtractedClaim(
                subject="шахтная вода",
                predicate="TREATED_BY",
                object="обратный осмос",
                statement="Шахтная вода очищается обратным осмосом.",
                confidence=0.91,
                evidence_quote="Обратный осмос применён для очистки шахтной воды.",
            )
        ],
    )


@pytest.mark.asyncio
async def test_ingestion_writes_extracted_claim_and_provenance_to_graph() -> None:
    knowledge = InMemoryKnowledgeBase()
    service = IngestionService(
        knowledge,
        ScriptedProvider(IngestionBundle(extraction=extraction())),
    )
    document = DocumentRequest(
        title="Пилот",
        text="Обратный осмос применён для очистки шахтной воды.",
    )

    receipt = await service.ingest(document)
    graph = knowledge.full_graph()

    assert receipt.extracted_claims == 1
    assert any(node.label == "Пилот" for node in graph.nodes)
    assert any(node.metadata.get("knowledge_status") == "extracted" for node in graph.nodes)
    assert any(edge.relation == "SUPPORTED_BY" for edge in graph.edges)
    assert any(finding.evidence[0].source_title == "Пилот" for finding in knowledge.all_findings())


@pytest.mark.asyncio
async def test_semantic_parts_share_document_id_and_resume_idempotently() -> None:
    knowledge = InMemoryKnowledgeBase()
    bundle = IngestionBundle(extraction=extraction())
    provider = ScriptedProvider(bundle, bundle, bundle)
    service = IngestionService(knowledge, provider)
    first = DocumentRequest(
        title="Пилот",
        text="Обратный осмос применён для очистки шахтной воды. Часть первая.",
        fragments=[{"text": "Обратный осмос применён для очистки шахтной воды.", "page": 3}],
    )
    second = DocumentRequest(
        title="Пилот",
        text="Обратный осмос применён для очистки шахтной воды. Часть вторая.",
        fragments=[{"text": "Обратный осмос применён для очистки шахтной воды.", "page": 4}],
    )
    source_document = DocumentRequest(
        title="Пилот",
        text=f"{first.text}\n\n{second.text}",
        fragments=[*first.fragments, *second.fragments],
    )
    source_document_id = knowledge.index_document(source_document, "book.txt").document_id

    first_receipt = await service.ingest(
        first,
        source_document_id=source_document_id,
        semantic_part_id="part-00001",
        semantic_part_total=2,
    )
    assert await service.semantic_parts_completed(source_document_id, 2) == {"part-00001"}
    second_receipt = await service.ingest(
        second,
        source_document_id=source_document_id,
        semantic_part_id="part-00002",
        semantic_part_total=2,
    )
    duplicate_receipt = await service.ingest(
        second,
        source_document_id=source_document_id,
        semantic_part_id="part-00002",
        semantic_part_total=2,
    )

    assert first_receipt.document_id == source_document_id
    assert second_receipt.document_id == source_document_id
    assert duplicate_receipt.status == "duplicate"
    assert await service.semantic_parts_completed(source_document_id, 2) == {
        "part-00001",
        "part-00002",
    }
    evidence = [
        item.evidence[0]
        for item in knowledge.all_findings()
        if item.evidence and item.evidence[0].document_id == source_document_id
    ]
    assert {item.page for item in evidence} == {3, 4}


@pytest.mark.asyncio
async def test_document_ingestion_uses_configured_graphrag_model() -> None:
    provider = ScriptedProvider(IngestionBundle(extraction=extraction()))
    service = IngestionService(
        InMemoryKnowledgeBase(), provider, model="GigaChat-Pro"
    )

    await service.ingest(
        DocumentRequest(
            title="Пилот",
            text="Обратный осмос применён для очистки шахтной воды.",
        )
    )

    assert provider.models == ["GigaChat-Pro"]


def test_json_and_xlsx_parsers_preserve_source_fragments() -> None:
    json_document = parse_document("sample.json", b'{"material":"ore","value":42}')

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Experiment"
    sheet.append(["material", "value"])
    sheet.append(["ore", 42])
    stream = io.BytesIO()
    workbook.save(stream)
    xlsx_document = parse_document("sample.xlsx", stream.getvalue())

    assert json_document.fragments[0].page == 1
    assert xlsx_document.fragments[0].sheet == "Experiment"
    assert xlsx_document.fragments[0].cell_range == "A1:B1"
