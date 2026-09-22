"""SHACL-подпись импорта: словарь реестра в shapes.ttl и один повторный вызов модели.

Словари в ``ontology/shapes.ttl`` — вторая копия подписи (первая в
``domain/relations.py``), поэтому здесь они сверяются с реестром и с ``NodeType``:
рассогласование означает, что импорт либо принимает предикат/тип, которого агент
никогда не сможет пересечь, либо отвергает валидный документ.
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import Any

import pytest
from rdflib import Graph, Namespace
from rdflib.collection import Collection

from scientific_tangle.domain.contracts import (
    DocumentRequest,
    ExtractedClaim,
    ExtractedEntity,
    ExtractionResult,
    IngestionBundle,
    NodeType,
)
from scientific_tangle.domain.relations import PROVENANCE_RELATIONS, SEMANTIC_RELATIONS
from scientific_tangle.services.ingestion import EXTRACTION_SYSTEM, IngestionService
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from scientific_tangle.services.ontology import OntologyValidationError

MIND = Namespace("https://mindai.local/ontology/")
SH = Namespace("http://www.w3.org/ns/shacl#")
SHAPES = Path(__file__).parents[1] / "src" / "scientific_tangle" / "ontology" / "shapes.ttl"


def _vocabulary(graph: Graph, path: Any) -> set[str]:
    """Значения sh:in, объявленные для свойства в форме SHACL."""
    for shape in graph.subjects(SH.path, path):
        listed = graph.value(shape, SH["in"])
        if listed is None:
            continue
        return {str(item) for item in Collection(graph, listed)}
    raise AssertionError(f"В shapes.ttl нет словаря для {path}")


def bundle(extraction: ExtractionResult) -> IngestionBundle:
    return IngestionBundle(extraction=extraction)


def extraction(predicate: str = "TREATED_BY") -> ExtractionResult:
    return ExtractionResult(
        entities=[
            ExtractedEntity(
                name="шахтная вода", canonical_name="Шахтная вода", type=NodeType.MATERIAL
            ),
            ExtractedEntity(name="осмос", canonical_name="Обратный осмос", type=NodeType.PROCESS),
        ],
        claims=[
            ExtractedClaim(
                subject="шахтная вода",
                predicate=predicate,
                object="осмос",
                statement="Шахтная вода очищается обратным осмосом.",
                confidence=0.91,
                evidence_quote="Обратный осмос применён для очистки шахтной воды.",
            )
        ],
    )


class _RecordingProvider:
    """Провайдер, который помнит промпты: повтор обязан нести текст нарушения."""

    mode = "scripted"

    def __init__(self, *outputs: IngestionBundle) -> None:
        self._outputs: deque[IngestionBundle] = deque(outputs)
        self.prompts: list[tuple[str, str]] = []

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[Any],
    ) -> Any:
        self.prompts.append((system, user))
        return self._outputs.popleft()

    async def complete_text(self, system: str, user: str) -> str:
        self.prompts.append((system, user))
        return ""


DOCUMENT = DocumentRequest(
    title="Пилот",
    text="Обратный осмос применён для очистки шахтной воды.",
)


# ── Словари shapes.ttl ⇔ реестр/NodeType (п. 1, п. 6) ──────────────────────


def test_shapes_predicate_vocabulary_matches_the_registry() -> None:
    graph = Graph().parse(SHAPES, format="turtle")

    assert _vocabulary(graph, MIND.predicate) == set(SEMANTIC_RELATIONS)


def test_shapes_predicate_vocabulary_excludes_provenance() -> None:
    """Модели недоступен служебный ярус: ASSERTS/SUPPORTED_BY заводит система."""
    graph = Graph().parse(SHAPES, format="turtle")

    assert _vocabulary(graph, MIND.predicate).isdisjoint(PROVENANCE_RELATIONS)


def test_shapes_entity_types_match_node_type() -> None:
    graph = Graph().parse(SHAPES, format="turtle")

    assert _vocabulary(graph, MIND.entityType) == {item.value for item in NodeType}


def test_validator_accepts_registry_signature() -> None:
    from scientific_tangle.services.ontology import OntologyValidator

    OntologyValidator().validate(extraction())


def test_extraction_prompt_offers_exactly_the_accepted_vocabulary() -> None:
    """Промпт и проверка обязаны называть один и тот же словарь предикатов.

    Живой прогон на GigaChat ловил обратное: промпт показывал четыре примера,
    модель предлагала STARTED_WITH, и импорт отбивался проверкой уже после
    повторной сборки — то есть сорванным документом, а не исправлением.
    """
    accepted = _vocabulary(Graph().parse(SHAPES, format="turtle"), MIND.predicate)
    mentioned = {token for token in re.findall(r"\b[A-Z][A-Z_]+\b", EXTRACTION_SYSTEM)}

    assert accepted <= mentioned
    # Заглавных слов в промпте два вида: имена отношений и ссылка на «JSON Schema».
    assert mentioned - accepted == {"JSON"}


def test_validator_rejects_predicate_outside_the_signature() -> None:
    from scientific_tangle.services.ontology import OntologyValidator

    with pytest.raises(OntologyValidationError) as error:
        OntologyValidator().validate(extraction("HAS_SALT_REJECTION"))

    text = str(error.value)
    assert "HAS_SALT_REJECTION" in text
    assert "Допустимые предикаты" in text
    # Нарушение объяснено коротко и по-русски: текст читает модель в повторе.
    assert "значения нет в словаре подписи" in text


def test_validator_rejects_unknown_subject() -> None:
    from scientific_tangle.services.ontology import OntologyValidator

    claim = extraction().claims[0].model_copy(update={"subject": "марсианская руда"})
    broken = extraction().model_copy(update={"claims": [claim]})

    with pytest.raises(OntologyValidationError, match="Неизвестный subject"):
        OntologyValidator().validate(broken)


# ── Ровно один повторный вызов, затем ошибка импорта (п. 6) ────────────────


@pytest.mark.asyncio
async def test_import_retries_once_with_the_violation_text() -> None:
    knowledge = InMemoryKnowledgeBase()
    # Два «плохих» ответа: первый вызов и ровно один повтор с текстом нарушения.
    provider = _RecordingProvider(
        *[bundle(extraction("HAS_SALT_REJECTION")) for _ in range(2)]
    )

    with pytest.raises(OntologyValidationError):
        await IngestionService(knowledge, provider).ingest(DOCUMENT)  # type: ignore[arg-type]

    assert len(provider.prompts) == 2
    retry = provider.prompts[1][1]
    assert "HAS_SALT_REJECTION" in retry and "Допустимые предикаты" in retry
    assert "VALIDATION ERROR" in retry


@pytest.mark.asyncio
async def test_import_does_not_retry_more_than_once() -> None:
    """Второе нарушение — ошибка импорта, а не тихое принятие и не цикл повторов."""
    knowledge = InMemoryKnowledgeBase()
    before_findings = knowledge.all_findings()
    before_edges = knowledge.full_graph().edges
    provider = _RecordingProvider(
        *[bundle(extraction("HAS_SALT_REJECTION")) for _ in range(4)]
    )

    with pytest.raises(OntologyValidationError):
        await IngestionService(knowledge, provider).ingest(DOCUMENT)  # type: ignore[arg-type]

    assert len(provider.prompts) == 2
    # Импорт не должен что-либо оставить в графе: тихого принятия нет.
    assert knowledge.all_findings() == before_findings
    assert knowledge.full_graph().edges == before_edges


@pytest.mark.asyncio
async def test_import_succeeds_after_a_single_repair() -> None:
    knowledge = InMemoryKnowledgeBase()
    provider = _RecordingProvider(
        bundle(extraction("HAS_SALT_REJECTION")),
        bundle(extraction()),
    )

    receipt = await IngestionService(knowledge, provider).ingest(DOCUMENT)  # type: ignore[arg-type]

    assert receipt.status == "created"
    assert receipt.extracted_claims == 1
    assert len(provider.prompts) == 2


@pytest.mark.asyncio
async def test_valid_document_does_not_trigger_a_second_call() -> None:
    knowledge = InMemoryKnowledgeBase()
    provider = _RecordingProvider(bundle(extraction()))

    await IngestionService(knowledge, provider).ingest(DOCUMENT)  # type: ignore[arg-type]

    assert len(provider.prompts) == 1
