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
