from __future__ import annotations

from scientific_tangle.domain.contracts import (
    DocumentReceipt,
    DocumentRequest,
    ExtractionResult,
    IngestionBundle,
)
from scientific_tangle.services.knowledge import KnowledgeBase
from scientific_tangle.services.ontology import OntologyValidator
from scientific_tangle.services.provider import ModelProvider
from scientific_tangle.services.resolution import EntityResolutionWorkbench

EXTRACTION_SYSTEM = """Ты Extractor Agent платформы MindAI.
Извлеки только явно поддержанные текстом сущности и утверждения.
Сохрани исходную формулировку evidence_quote. Не додумывай факты.
Типы сущностей ограничены JSON Schema. Confidence оценивает качество evidence, а не красоту текста.
Predicate записывай в UPPER_SNAKE_CASE, например TREATED_BY, PRODUCES, OPERATES_AT, USES.
Для каждой сущности сформируй resolution proposal: link только при высокой уверенности,
иначе create. Русские и английские синонимы своди к одному canonical_name.
"""

EXTRACTION_REPAIR_SYSTEM = f"""{EXTRACTION_SYSTEM}
Ты исправляешь schema/ontology violation предыдущего extraction result.
Верни полный исправленный IngestionBundle. Не удаляй корректные evidence-backed claims.
У каждого claim.subject должна быть соответствующая entity. Используй только типы из JSON Schema.
"""


class IngestionService:
    def __init__(
        self,
        knowledge: KnowledgeBase,
        provider: ModelProvider,
        resolution: EntityResolutionWorkbench | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._provider = provider
        self._ontology = OntologyValidator()
        self._resolution = resolution

    async def ingest(self, document: DocumentRequest) -> DocumentReceipt:
        user_text = (
            f"Название: {document.title}\nЯзык: {document.language}\n"
            f"География: {document.geography}\nГод: {document.year}\n\n{document.text}"
        )
        bundle = await self._provider.complete_model(
            EXTRACTION_SYSTEM,
            user_text,
            IngestionBundle,
        )
        extraction = self._apply_resolutions(bundle)
        try:
            self._ontology.validate(extraction)
        except ValueError as error:
            bundle = await self._provider.complete_model(
                EXTRACTION_REPAIR_SYSTEM,
                (
                    f"DOCUMENT:\n{user_text}\n\n"
                    f"PREVIOUS RESULT:\n{bundle.model_dump_json()}\n\n"
                    f"VALIDATION ERROR:\n{error}"
                ),
                IngestionBundle,
            )
            extraction = self._apply_resolutions(bundle)
            self._ontology.validate(extraction)
        receipt = self._knowledge.ingest(document, extraction)
        if receipt.status == "created" and self._resolution:
            self._resolution.register(bundle.resolutions)
        return receipt

    @staticmethod
    def _apply_resolutions(bundle: IngestionBundle) -> ExtractionResult:
        resolved = {
            item.mention.lower(): item.canonical_name
            for item in bundle.resolutions
            if item.confidence >= 0.7
        }
        entities = [
            entity.model_copy(
                update={"canonical_name": resolved.get(entity.name.lower(), entity.canonical_name)}
            )
            for entity in bundle.extraction.entities
        ]
        canonical = {
            alias.lower(): entity.canonical_name
            for entity in entities
            for alias in [entity.name, entity.canonical_name, *entity.aliases]
        }
        canonical.update(resolved)
        claims = [
            claim.model_copy(
                update={
                    "subject": canonical.get(claim.subject.lower(), claim.subject),
                    "object": canonical.get(claim.object.lower(), claim.object),
                }
            )
            for claim in bundle.extraction.claims
        ]
        return bundle.extraction.model_copy(update={"entities": entities, "claims": claims})
