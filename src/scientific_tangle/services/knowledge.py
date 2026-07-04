from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid5

from scientific_tangle.domain.contracts import (
    CorpusStats,
    DocumentReceipt,
    DocumentRequest,
    ExtractionResult,
    Finding,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    NodeType,
    RetrievalPlan,
    StructuralDocumentReceipt,
)
from scientific_tangle.domain.models import (
    EvidenceLocator,
    NumericFilter,
    NumericObservation,
    QueryPlan,
)

NAMESPACE = UUID("f83e9ec0-5094-4ae5-b2ba-178777d09443")


def stable_uuid(value: str) -> UUID:
    return uuid5(NAMESPACE, value)


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    findings: list[Finding]
    graph: GraphSnapshot
    community_summaries: list[str]


class KnowledgeBase(Protocol):
    def ingest(
        self, document: DocumentRequest, extraction: ExtractionResult
    ) -> DocumentReceipt: ...

    def retrieve(
        self,
        plan: QueryPlan,
        retrieval_plan: RetrievalPlan,
        allowed_data_classes: set[str] | None = None,
    ) -> RetrievalContext: ...

    def full_graph(self) -> GraphSnapshot: ...

    def rank_findings(self, query: str, top_k: int, mode: str = "hybrid") -> list[Finding]: ...

    def document_count(self) -> int: ...

    def index_document(
        self, document: DocumentRequest, source_path: str
    ) -> StructuralDocumentReceipt: ...

    def corpus_stats(self) -> CorpusStats: ...


class InMemoryKnowledgeBase:
    """Тестовый KG-adapter с теми же контрактами, что и production repositories."""

    def __init__(self) -> None:
        self._documents: dict[str, DocumentRequest] = {}
        self._findings = self._seed_findings()
        self._graph = self._seed_graph()
        self._version_chains: dict[str, list[str]] = {}

    def ingest(self, document: DocumentRequest, extraction: ExtractionResult) -> DocumentReceipt:
        checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        document_id = stable_uuid(checksum)
        if checksum in self._documents:
            return DocumentReceipt(
                document_id=document_id,
                checksum=checksum,
                status="duplicate",
                extracted_claims=0,
            )

        self._documents[checksum] = document
        self._add_extraction(document, document_id, extraction)
        return DocumentReceipt(
            document_id=document_id,
            checksum=checksum,
            status="created",
            extracted_claims=len(extraction.claims),
        )

    def _add_extraction(
        self,
        document: DocumentRequest,
        document_id: UUID,
        extraction: ExtractionResult,
    ) -> None:
        existing_nodes = {node.id for node in self._graph.nodes}
        document_node_id = f"document-{document_id}"
        if document_node_id not in existing_nodes:
            self._graph.nodes.append(
                GraphNode(
                    id=document_node_id,
                    label=document.title,
                    type=NodeType.PUBLICATION,
                    metadata={"year": document.year or 0, "geography": document.geography or ""},
                )
            )

        entity_ids: dict[str, str] = {}
        for entity in extraction.entities:
            entity_id = f"entity-{stable_uuid(entity.canonical_name.lower())}"
            entity_ids[entity.name.lower()] = entity_id
            entity_ids[entity.canonical_name.lower()] = entity_id
            if entity_id not in existing_nodes:
                self._graph.nodes.append(
                    GraphNode(
                        id=entity_id,
                        label=entity.canonical_name,
                        type=entity.type,
                        metadata={"aliases": ", ".join(entity.aliases)},
                    )
                )
                existing_nodes.add(entity_id)

        for index, claim in enumerate(extraction.claims):
            claim_id = f"claim-{stable_uuid(f'{document_id}:{index}:{claim.statement}')}"
            subject_id = entity_ids.get(claim.subject.lower())
            object_id = entity_ids.get(claim.object.lower())
            if not subject_id:
                subject_id = f"entity-{stable_uuid(claim.subject.lower())}"
                if subject_id not in existing_nodes:
                    self._graph.nodes.append(
                        GraphNode(id=subject_id, label=claim.subject, type=NodeType.MATERIAL)
                    )
                    existing_nodes.add(subject_id)
            self._graph.nodes.append(
                GraphNode(
                    id=claim_id,
                    label=claim.statement,
                    type=NodeType.CLAIM,
                    confidence=claim.confidence,
                    metadata={
                        "knowledge_status": "extracted",
                        "created_at": datetime.now(UTC).isoformat(),
                        "observations": json.dumps(
                            [item.model_dump(mode="json") for item in claim.observations],
                            ensure_ascii=False,
                        ),
                    },
                )
            )
            self._graph.edges.extend(
                [
                    GraphEdge(
                        id=f"{claim_id}-asserts",
                        source=subject_id,
                        target=claim_id,
                        relation="ASSERTS",
                        confidence=claim.confidence,
                    ),
                    GraphEdge(
                        id=f"{claim_id}-evidence",
                        source=claim_id,
                        target=document_node_id,
                        relation="SUPPORTED_BY",
                        confidence=claim.confidence,
                    ),
                ]
            )
            if object_id:
                self._graph.edges.append(
                    GraphEdge(
                        id=f"{claim_id}-object",
                        source=claim_id,
                        target=object_id,
                        relation=claim.predicate,
                        confidence=claim.confidence,
                    )
                )
            self._findings.append(
                Finding(
                    id=f"finding-{claim_id}",
                    statement=claim.statement,
                    confidence=claim.confidence,
                    evidence=[self._locate_evidence(document, document_id, claim.evidence_quote)],
                    status="hypothesis",
                    observations=claim.observations,
                    subject=claim.subject,
                    predicate=claim.predicate,
                )
            )

    @staticmethod
    def _locate_evidence(
        document: DocumentRequest,
        document_id: UUID,
        quote: str,
    ) -> EvidenceLocator:
        fragment = next(
            (item for item in document.fragments if quote in item.text),
            document.fragments[0] if document.fragments else None,
        )
        return EvidenceLocator(
            document_id=document_id,
            source_title=document.title,
            page=fragment.page if fragment else 1,
            sheet=fragment.sheet if fragment else None,
            cell_range=fragment.cell_range if fragment else None,
            quote=quote,
        )

    def retrieve(
        self,
        plan: QueryPlan,
        retrieval_plan: RetrievalPlan,
        allowed_data_classes: set[str] | None = None,
    ) -> RetrievalContext:
        tokens = {
            token.lower() for token in retrieval_plan.lexical_query.split() if len(token) >= 4
        }

        def score(finding: Finding) -> int:
            return sum(token in finding.statement.lower() for token in tokens)

        # Исключаем superseded-версии: возвращаем только актуальные
        active = [f for f in self._findings if f.superseded_by is None]
        # Pre-retrieval ACL: отсекаем запрещённые data_class до scoring/ranking
        if allowed_data_classes is not None:
            active = [f for f in active if f.data_class in allowed_data_classes]
        ranked = sorted(
            active, key=lambda item: (score(item), item.confidence), reverse=True
        )
        findings = ranked[:4]
        findings = self._apply_numeric_filters(findings, plan.numeric_filters)
        visible_ids = {"water", "sulfates", "chlorides", "reverse-osmosis", "ion-exchange"}
        visible_ids.update(
            edge.target
            for edge in self._graph.edges
            if edge.source in visible_ids and edge.target.startswith("claim")
        )
        graph = GraphSnapshot(
            nodes=[node for node in self._graph.nodes if node.id in visible_ids],
            edges=[
                edge
                for edge in self._graph.edges
                if edge.source in visible_ids and edge.target in visible_ids
            ],
            communities=self._graph.communities,
        )
        return RetrievalContext(
            findings=findings,
            graph=graph,
            community_summaries=[
                "Мембранные методы дают глубокое обессоливание, но требуют pretreatment.",
                "Ионный обмен эффективен для селективного удаления и полировки потока.",
            ],
        )

    @staticmethod
    def _apply_numeric_filters(
        findings: list[Finding], filters: list[NumericFilter]
    ) -> list[Finding]:
        """Оставляет только findings, удовлетворяющие всем числовым ограничениям.

        Finding без observation для данного property сохраняется — фильтр применяется
        только к findings, у которых есть соответствующее numeric observation.
        """
        if not filters:
            return findings
        result: list[Finding] = []
        for finding in findings:
            keep = True
            for flt in filters:
                obs = [
                    o
                    for o in finding.observations
                    if o.property_name == flt.property_name
                    and (o.normalized_unit == flt.unit or o.unit == flt.unit)
                ]
                if not obs:
                    continue
                if not any(InMemoryKnowledgeBase._obs_matches(o, flt) for o in obs):
                    keep = False
                    break
            if keep:
                result.append(finding)
        return result

    @staticmethod
    def _obs_matches(obs: NumericObservation, flt: NumericFilter) -> bool:
        """Проверяет, удовлетворяет ли observation числовому фильтру.

        Сравнение идёт в единицах фильтра: если raw-единицы совпадают —
        используются raw-значения, иначе normalised.
        """
        op = flt.operator
        if obs.unit == flt.unit:
            val = obs.value
            lo_obs = obs.min_value
            hi_obs = obs.max_value
        elif obs.normalized_unit == flt.unit:
            val = obs.normalized_value
            lo_obs = obs.normalized_min
            hi_obs = obs.normalized_max
        else:
            return False  # Units incompatible — conservatively exclude
        if op in {"between", "range"}:
            if flt.min_value is None or flt.max_value is None:
                return True
            if val is not None:
                return flt.min_value <= val <= flt.max_value
            if lo_obs is not None and hi_obs is not None:
                return lo_obs <= flt.max_value and hi_obs >= flt.min_value
            return True
        threshold = flt.value
        if threshold is None:
            return True
        if val is None and lo_obs is not None and hi_obs is not None:
            val = (lo_obs + hi_obs) / 2
        if val is None:
            return True
        if op == "eq":
            return abs(val - threshold) < 1e-9
        if op == "lt":
            return val < threshold
        if op == "lte":
            return val <= threshold
        if op == "gt":
            return val > threshold
        if op == "gte":
            return val >= threshold
        return True

    def rank_findings(self, query: str, top_k: int, mode: str = "hybrid") -> list[Finding]:
        """Ранжирует findings по релевантности к запросу.

        Режимы:
          * ``lexical`` — только лексическое совпадение токенов в statement (baseline).
          * ``hybrid``  — лексический + семантический (evidence quote) + графовый
                           (заголовок источника, subject) boost.

        Гибридный режим имитирует три компонента полноценного GraphRAG:
        1. Лексический — совпадение токенов в statement (как в baseline).
        2. Семантический — совпадение токенов в evidence quote (контекст документа).
        3. Графовый — boost по совпадению заголовка источника и subject-сущности.
        """
        tokens = {token.lower() for token in query.split() if len(token) >= 4}
        active = [f for f in self._findings if f.superseded_by is None]

        def lexical_score(finding: Finding) -> tuple[int, float]:
            # Только совпадение по statement — чистый лексический baseline
            stmt_hits = sum(token in finding.statement.lower() for token in tokens)
            return (stmt_hits, finding.confidence)

        def hybrid_score(finding: Finding) -> tuple[float, int, float]:
            # 1) Лексический: совпадение токенов в statement
            stmt_hits = sum(token in finding.statement.lower() for token in tokens)

            # 2) Семантический: совпадение токенов в evidence quote (контекст фрагмента)
            ev_text = " ".join(
                ev.quote for ev in finding.evidence
            ).lower() if finding.evidence else ""
            ev_hits = sum(token in ev_text for token in tokens) if ev_text else 0

            # 3) Графовый: совпадение токенов в заголовке источника
            source_title = ""
            if finding.evidence:
                source_title = finding.evidence[0].source_title.lower()
            source_hits = sum(token in source_title for token in tokens) if source_title else 0

            # 4) Графовый: совпадение subject-сущности с запросом
            subject_hits = (
                1 if finding.subject and finding.subject.lower() in query.lower() else 0
            )

            # Взвешенная сумма: лексический (1.0) + семантический (0.3)
            #   + графовый-источник (0.5) + графовый-subject (0.5)
            total = (
                stmt_hits
                + 0.3 * ev_hits
                + 0.5 * source_hits
                + 0.5 * subject_hits
            )
            return (total, stmt_hits, finding.confidence)

        if mode == "lexical":
            ranked = sorted(active, key=lexical_score, reverse=True)
        else:
            ranked = sorted(active, key=hybrid_score, reverse=True)

        return [
            finding.model_copy(deep=True)
            for finding in ranked[:top_k]
        ]

    def document_count(self) -> int:
        document_ids = {
            str(evidence.document_id) for finding in self._findings for evidence in finding.evidence
        }
        return len(document_ids)

    def index_document(
        self, document: DocumentRequest, source_path: str
    ) -> StructuralDocumentReceipt:
        checksum = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        return StructuralDocumentReceipt(
            document_id=stable_uuid(checksum),
            checksum=checksum,
            status="created",
            chunks=len(document.fragments),
        )

    def corpus_stats(self) -> CorpusStats:
        return CorpusStats(
            documents=self.document_count(),
            chunks=0,
            claims=sum(node.type == NodeType.CLAIM for node in self._graph.nodes),
            entities=sum(node.type != NodeType.CLAIM for node in self._graph.nodes),
            semantic_documents=len(self._documents),
        )

    def full_graph(self) -> GraphSnapshot:
        return self._graph.model_copy(deep=True)

    def all_findings(self) -> list[Finding]:
        return [
            finding.model_copy(deep=True)
            for finding in self._findings
            if finding.superseded_by is None
        ]

    def supersede_finding(
        self,
        finding_id: str,
        new_statement: str,
        new_confidence: float,
        observations: list[NumericObservation] | None = None,
        reviewer_id: str | None = None,
        review_date: str | None = None,
        review_reason: str | None = None,
    ) -> Finding:
        """Создаёт новую версию утверждения и связывает старую через SUPERSEDES."""
        old = next((f for f in self._findings if f.id == finding_id), None)
        if old is None:
            raise KeyError(f"Finding not found: {finding_id}")
        if old.superseded_by is not None:
            raise ValueError(
                f"Finding {finding_id} уже заменён на {old.superseded_by}"
            )
        new_id = f"{finding_id}-v{old.version + 1}"
        updated_old = old.model_copy(update={"superseded_by": new_id})
        idx = self._findings.index(old)
        self._findings[idx] = updated_old
        new_finding = Finding(
            id=new_id,
            statement=new_statement,
            confidence=new_confidence,
            evidence=old.evidence,
            status=old.status,
            observations=observations or old.observations,
            version=old.version + 1,
            subject=old.subject,
            predicate=old.predicate,
            scope=old.scope,
            data_class=old.data_class,
            reviewer_id=reviewer_id,
            review_date=review_date,
            review_reason=review_reason,
        )
        self._findings.append(new_finding)
        root_id = next(
            (k for k, chain in self._version_chains.items() if finding_id in chain),
            finding_id,
        )
        chain = self._version_chains.setdefault(root_id, [root_id])
        chain.append(new_id)
        self._graph.edges.append(
            GraphEdge(
                id=f"supersedes-{new_id}",
                source=new_id,
                target=finding_id,
                relation="SUPERSEDES",
                confidence=new_confidence,
            )
        )
        return new_finding

    def claim_history(self, finding_id: str) -> list[Finding]:
        """Возвращает все версии утверждения, включая исходную."""
        chain = self._version_chains.get(finding_id, [finding_id])
        by_id = {f.id: f for f in self._findings}
        return [by_id[fid] for fid in chain if fid in by_id]

    @staticmethod
    def _evidence(slug: str, title: str, page: int, quote: str) -> EvidenceLocator:
        return EvidenceLocator(
            document_id=stable_uuid(slug),
            source_title=title,
            page=page,
            quote=quote,
        )

    @staticmethod
    def _point_obs(
        property_name: str,
        value: float,
        unit: str,
        normalized_unit: str,
        raw_text: str,
    ) -> NumericObservation:
        return NumericObservation(
            property_name=property_name,
            operator="eq",
            value=value,
            unit=unit,
            normalized_value=value,
            normalized_unit=normalized_unit,
            raw_text=raw_text,
        )

    @staticmethod
    def _range_obs(
        property_name: str,
        min_value: float,
        max_value: float,
        unit: str,
        normalized_min: float,
        normalized_max: float,
        normalized_unit: str,
        raw_text: str,
    ) -> NumericObservation:
        return NumericObservation(
            property_name=property_name,
            operator="between",
            min_value=min_value,
            max_value=max_value,
            unit=unit,
            normalized_min=normalized_min,
            normalized_max=normalized_max,
            normalized_unit=normalized_unit,
            raw_text=raw_text,
        )

    def _seed_findings(self) -> list[Finding]:
        return [
            Finding(
                id="finding-ro",
                statement=(
                    "Обратный осмос обеспечивает удаление 95–99% растворённых солей и подходит "
                    "для достижения сухого остатка ≤1000 мг/л после предварительной очистки."
                ),
                confidence=0.92,
                evidence=[
                    self._evidence(
                        "water-treatment-review",
                        "Обзор методов обессоливания шахтных вод",
                        14,
                        (
                            "Задержание растворённых солей мембраной обратного осмоса "
                            "составляет 95–99%."
                        ),
                    )
                ],
                subject="reverse-osmosis",
                predicate="HAS_SALT_REJECTION",
                scope={"water_type": "mine_water"},
                observations=[
                    self._range_obs(
                        "salt_rejection", 95, 99, "%", 95, 99, "%",
                        "95–99%",
                    ),
                    NumericObservation(
                        property_name="dry_residue",
                        operator="lte",
                        value=1000,
                        unit="mg/L",
                        normalized_value=1000,
                        normalized_unit="mg/L",
                        raw_text="сухой остаток ≤1000 мг/л",
                    ),
                ],
            ),
            Finding(
                id="finding-ro-pilot",
                statement=(
                    "Пилотные испытания обратного осмоса показали задержание солей "
                    "на уровне 80–85% при пониженном давлении."
                ),
                confidence=0.70,
                evidence=[
                    self._evidence(
                        "ro-pilot-study",
                        "Пилот обратного осмоса",
                        5,
                        "Задержание солей составило 80–85%.",
                    )
                ],
                subject="reverse-osmosis",
                predicate="HAS_SALT_REJECTION",
                scope={"water_type": "mine_water"},
                observations=[
                    self._range_obs(
                        "salt_rejection", 80, 85, "%", 80, 85, "%",
                        "80–85%",
                    ),
                ],
            ),
            Finding(
                id="finding-ion",
                statement=(
                    "Ионный обмен целесообразен как селективная ступень для Ca и Mg, "
                    "но регенерационные стоки ограничивают применение в одиночку."
                ),
                confidence=0.84,
                evidence=[
                    self._evidence(
                        "ion-exchange-protocol",
                        "Протокол пилотных испытаний ионного обмена",
                        7,
                        "Снижение Ca и Mg достигало 82–91%; требовалась регенерация смолы.",
                    )
                ],
                subject="ion-exchange",
                predicate="HAS_REMOVAL_EFFICIENCY",
                scope={"water_type": "mine_water"},
                observations=[
                    self._range_obs(
                        "ca_mg_removal", 82, 91, "%", 82, 91, "%",
                        "82–91%",
                    ),
                ],
            ),
            Finding(
                id="finding-thermal",
                statement=(
                    "Термическое выпаривание устойчиво к широкому составу воды, "
                    "но требует в 3–5 раз больше энергии, чем мембранная схема."
                ),
                confidence=0.78,
                status="disputed",
                data_class="restricted",
                evidence=[
                    self._evidence(
                        "thermal-comparison",
                        "Сравнение технологий концентрирования",
                        22,
                        (
                            "Удельные энергозатраты выпаривания превышали мембранный "
                            "вариант в 3–5 раз."
                        ),
                    )
                ],
                subject="evaporation",
                predicate="HAS_ENERGY_RATIO",
                scope={"water_type": "mine_water"},
                observations=[
                    self._range_obs(
                        "energy_ratio", 3, 5, "ratio", 3, 5, "ratio",
                        "3–5 раз",
                    ),
                ],
            ),
            Finding(
                id="finding-climate",
                statement=(
                    "Для холодного климата мембранный блок требует утепления и поддержания "
                    "температуры сырья выше 8 °C."
                ),
                confidence=0.81,
                evidence=[
                    self._evidence(
                        "cold-climate-membranes",
                        "Эксплуатация мембран в холодном климате",
                        9,
                        (
                            "Стабильная производительность наблюдалась при температуре "
                            "питания выше 8 °C."
                        ),
                    )
                ],
                subject="reverse-osmosis",
                predicate="REQUIRES_MIN_TEMPERATURE",
                scope={"climate": "cold"},
                observations=[
                    self._point_obs(
                        "min_temperature", 8, "°C", "°C",
                        "температуры сырья выше 8 °C",
                    ),
                ],
            ),
        ]

    @staticmethod
    def _seed_graph() -> GraphSnapshot:
        nodes = [
            GraphNode(id="water", label="Шахтная вода", type=NodeType.MATERIAL),
            GraphNode(id="sulfates", label="Сульфаты 200–300 мг/л", type=NodeType.CONDITION),
            GraphNode(id="chlorides", label="Хлориды 200–300 мг/л", type=NodeType.CONDITION),
            GraphNode(id="reverse-osmosis", label="Обратный осмос", type=NodeType.PROCESS),
            GraphNode(id="ion-exchange", label="Ионный обмен", type=NodeType.PROCESS),
            GraphNode(id="evaporation", label="Выпаривание", type=NodeType.PROCESS),
            GraphNode(
                id="claim-ro",
                label="Сухой остаток ≤1000 мг/л",
                type=NodeType.CLAIM,
                confidence=0.92,
            ),
            GraphNode(
                id="claim-energy",
                label="Энергия в 3–5 раз выше",
                type=NodeType.CLAIM,
                confidence=0.78,
            ),
            GraphNode(id="expert", label="Лаборатория водоподготовки", type=NodeType.EXPERT),
        ]
        edges = [
            GraphEdge(id="e1", source="water", target="sulfates", relation="CONTAINS"),
            GraphEdge(id="e2", source="water", target="chlorides", relation="CONTAINS"),
            GraphEdge(id="e3", source="water", target="reverse-osmosis", relation="TREATED_BY"),
            GraphEdge(id="e4", source="water", target="ion-exchange", relation="TREATED_BY"),
            GraphEdge(id="e5", source="reverse-osmosis", target="claim-ro", relation="PRODUCES"),
            GraphEdge(id="e6", source="evaporation", target="claim-energy", relation="REQUIRES"),
            GraphEdge(id="e7", source="expert", target="reverse-osmosis", relation="EXPERT_IN"),
        ]
        return GraphSnapshot(
            nodes=nodes,
            edges=edges,
            communities=["Мембранное обессоливание", "Селективная очистка", "Термические методы"],
        )
