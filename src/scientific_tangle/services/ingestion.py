from __future__ import annotations

import asyncio
import hashlib
from uuid import UUID

from scientific_tangle.domain.contracts import (
    DocumentReceipt,
    DocumentRequest,
    ExtractedEntity,
    ExtractionResult,
    IngestionBundle,
    NodeType,
)
from scientific_tangle.domain.relations import (
    RelationContractError,
    relations_for_extraction,
    spec_for,
)
from scientific_tangle.services.knowledge import KnowledgeBase, quote_offsets
from scientific_tangle.services.ontology import OntologyValidator
from scientific_tangle.services.provider import ModelProvider
from scientific_tangle.services.resolution import EntityResolutionWorkbench

# Словарь предикатов приходит из реестра отношений — из того же источника, который
# проверяет ``ontology/shapes.ttl``. Примеры вместо закрытого списка обходятся
# дорого: на реальном прогоне GigaChat додумывал STARTED_WITH и EQUIVALENT_TO,
# импорт уходил в repair-раунд и падал уже после него.
EXTRACTION_SYSTEM = f"""Ты Extractor Agent платформы MindAI.
Каждый claim — короткий атомарный факт с субъектом, отношением, значением,
общим fact_kind и контекстом (условия, даты, единицы и ограничения).
Извлекай определения, свойства, числа, отношения, события, процессы, ограничения
и решения. Пропускай заголовки, оглавления, учебные цели, биографии, списки людей,
рекламу и фрагменты без проверяемого знания. Мнения, рекомендации и предположения
не выдавай за факты. evidence_quote — дословная непрерывная цитата из источника.
Извлеки только явно поддержанные текстом сущности и утверждения.
Сохрани исходную формулировку evidence_quote. Не додумывай факты.
Типы сущностей ограничены JSON Schema. Confidence оценивает качество evidence, а не красоту текста.
Predicate — ровно одно имя из закрытого словаря отношений графа, заглавными
буквами через подчёркивание, без склонений и пояснений. Отношение вне словаря
отклоняется проверкой, и импорт не состоится:
{relations_for_extraction()}
У каждого claim.subject и claim.object обязана быть сущность в entities — по name,
canonical_name или alias. Иначе утверждение считается неподдержанным.
Формируй resolution proposals только для явных синонимов или алиасов; не создавай
proposal типа create для каждой сущности. Если явных alias нет, верни пустой список resolutions.
Русские и английские синонимы своди к одному canonical_name.
"""

EXTRACTION_REPAIR_SYSTEM = f"""{EXTRACTION_SYSTEM}
Ты исправляешь schema/ontology violation предыдущего extraction result.
Верни полный исправленный IngestionBundle. Не удаляй корректные evidence-backed claims.
У каждого claim.subject и claim.object должна быть соответствующая entity.
Используй только типы из JSON Schema.
"""


def _complete_claim_endpoints(extraction: ExtractionResult) -> ExtractionResult:
    """Добавляет пропущенные endpoints с типом, разрешённым подписью отношения."""
    entities = list(extraction.entities)
    known = {
        value.casefold()
        for entity in entities
        for value in (entity.name, entity.canonical_name, *entity.aliases)
    }
    for claim in extraction.claims:
        try:
            relation = spec_for(claim.predicate)
        except RelationContractError:
            continue
        for term, allowed_types in (
            (claim.subject, relation.source_types),
            (claim.object, relation.target_types),
        ):
            key = term.casefold()
            if key in known:
                continue
            # If a relation permits several endpoint types, prefer material when
            # allowed; otherwise use a stable allowed type (e.g. LOCATED_IN → location).
            choices = allowed_types or frozenset({NodeType.MATERIAL})
            entity_type = (
                NodeType.MATERIAL
                if NodeType.MATERIAL in choices
                else min(choices, key=lambda value: value.value)
            )
            entities.append(
                ExtractedEntity(name=term, canonical_name=term, type=entity_type)
            )
            known.add(key)
    return extraction.model_copy(update={"entities": entities})


class IngestionService:
    def __init__(
        self,
        knowledge: KnowledgeBase,
        provider: ModelProvider,
        resolution: EntityResolutionWorkbench | None = None,
        model: str | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._provider = provider
        self._ontology = OntologyValidator()
        self._resolution = resolution
        self._model = model

    async def ingest(
        self,
        document: DocumentRequest,
        *,
        source_document_id: UUID | None = None,
        semantic_part_id: str | None = None,
        semantic_part_total: int | None = None,
    ) -> DocumentReceipt:
        """Извлечение пакета — LLM, всё остальное убрано из event loop.

        pyshacl-валидация строит RDF-граф и прогоняет shapes: на реальном
        документе это секунды чистого CPU, и держать ими цикл обработки событий
        нельзя (импорт идёт параллельно с запросами аналитиков).
        """
        part_mode = source_document_id is not None
        if part_mode != (semantic_part_id is not None and semantic_part_total is not None):
            raise ValueError("Для частичного импорта нужны все идентификаторы semantic part")
        if source_document_id is not None:
            completed = await self.semantic_parts_completed(
                source_document_id, semantic_part_total or 0
            )
        else:
            completed = set()
        if source_document_id is not None and semantic_part_id in completed:
            return DocumentReceipt(
                document_id=source_document_id,
                checksum=hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
                status="duplicate",
                extracted_claims=0,
            )
        user_text = (
            f"Название: {document.title}\nЯзык: {document.language}\n"
            f"География: {document.geography}\nГод: {document.year}\n\n{document.text}"
        )
        bundle = await self._complete(EXTRACTION_SYSTEM, user_text)
        extraction = self._apply_resolutions(bundle)
        source_texts = [document.text, *(item.text for item in document.fragments)]
        supported_claims = [
            claim
            for claim in extraction.claims
            if any(
                quote_offsets(source, claim.evidence_quote) is not None
                for source in source_texts
            )
        ]
        if len(supported_claims) != len(extraction.claims):
            supported_entities = {
                term.casefold()
                for claim in supported_claims
                for term in (claim.subject, claim.object)
            }
            extraction = extraction.model_copy(
                update={
                    "claims": supported_claims,
                    "entities": [
                        entity
                        for entity in extraction.entities
                        if entity.name.casefold() in supported_entities
                        or entity.canonical_name.casefold() in supported_entities
                        or any(alias.casefold() in supported_entities for alias in entity.aliases)
                    ],
                }
            )
        extraction = _complete_claim_endpoints(extraction)
        try:
            await asyncio.to_thread(self._ontology.validate, extraction)
        except ValueError as error:
            bundle = await self._complete(
                EXTRACTION_REPAIR_SYSTEM,
                (
                    f"DOCUMENT:\n{user_text}\n\n"
                    f"PREVIOUS RESULT:\n{bundle.model_dump_json()}\n\n"
                    f"VALIDATION ERROR:\n{error}"
                ),
            )
            extraction = self._apply_resolutions(bundle)
            extraction = _complete_claim_endpoints(extraction)
            await asyncio.to_thread(self._ontology.validate, extraction)
        receipt = await asyncio.to_thread(
            self._knowledge.ingest,
            document,
            extraction,
            source_document_id=source_document_id,
            semantic_part_id=semantic_part_id,
            semantic_part_total=semantic_part_total,
        )
        if receipt.status == "created" and self._resolution:
            await asyncio.to_thread(self._resolution.register, bundle.resolutions)
        return receipt

    async def semantic_parts_completed(
        self, source_document_id: UUID, part_total: int
    ) -> set[str]:
        return await asyncio.to_thread(
            self._knowledge.semantic_parts_completed, source_document_id, part_total
        )

    async def _complete(self, system: str, user: str) -> IngestionBundle:
        """Импорт документов наполняет GraphRAG выделенной моделью."""
        if self._model is None:
            return await self._provider.complete_model(system, user, IngestionBundle)
        result = await self._provider.complete_model(
            system, user, IngestionBundle, model=self._model
        )
        return IngestionBundle.model_validate(result)

    def _apply_resolutions(self, bundle: IngestionBundle) -> ExtractionResult:
        resolved = {
            item.mention.lower(): item.canonical_name
            for item in bundle.resolutions
            if item.confidence >= 0.7
        }
        # Принятые ранее склейки имеют приоритет над новым решением модели:
        # эксперт уже выбрал канон, и второй импорт не вправе его переопределить.
        resolved.update(self._approved_aliases())
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

    def _approved_aliases(self) -> dict[str, str]:
        """Карта «алиас → канон» принятых экспертом склеек (регистронезависимый ключ)."""
        if self._resolution is None:
            return {}
        try:
            return {
                alias.lower(): canonical
                for alias, canonical in self._resolution.alias_map().items()
            }
        except Exception:  # noqa: BLE001 — без карты продолжаем по решению модели
            return {}
