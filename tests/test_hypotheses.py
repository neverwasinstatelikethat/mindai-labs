from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from scientific_tangle.domain.contracts import Finding, GraphSnapshot, ModelMode
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import EvidenceLocator, NumericObservation
from scientific_tangle.services.hypotheses import HypothesisDraft, HypothesisGenerator
from scientific_tangle.services.research_intelligence import (
    ResearchIntelligenceService,
    to_research_claims,
)

_Output = TypeVar("_Output", bound=BaseModel)


def _finding(
    statement: str,
    *,
    finding_id: str = "finding-1",
    document_id: UUID | None = None,
    observations: list[NumericObservation] | None = None,
    subject: str | None = None,
) -> Finding:
    return Finding(
        id=finding_id,
        statement=statement,
        confidence=0.9,
        evidence=[
            EvidenceLocator(
                document_id=document_id or uuid4(),
                source_title="Производственный отчет",
                page=3,
                quote=statement,
            )
        ],
        observations=observations or [],
        subject=subject,
        data_class=DataClass.INTERNAL,
    )


def test_hypothesis_requires_evidence() -> None:
    from scientific_tangle.domain.hypotheses import HypothesisSignal

    with pytest.raises(ValueError):
        HypothesisSignal(
            id=uuid4(),
            kind="improvement_opportunity",
            statement="Проверить повторный контроль операции.",
            confidence=0.7,
            data_class=DataClass.INTERNAL,
            evidence=[],
        )


class _DraftBatch(BaseModel):
    hypotheses: list[HypothesisDraft]


class _Provider:
    mode: ModelMode = "scripted"

    def __init__(self, drafts: Sequence[HypothesisDraft]) -> None:
        self.drafts = drafts
        self.calls = 0

    async def complete_model(
        self,
        system: str,
        user: str,
        schema: type[_Output],
        *,
        model: str | None = None,
    ) -> _Output:
        self.calls += 1
        return schema.model_validate(
            {"hypotheses": [draft.model_dump() for draft in self.drafts]}
        )


def _graph() -> GraphSnapshot:
    return GraphSnapshot(nodes=[], edges=[])


@pytest.mark.asyncio
async def test_generator_discards_unknown_evidence_ids() -> None:
    finding = _finding("Линия работает 16 часов за смену.")
    provider = _Provider(
        [
            HypothesisDraft(
                kind="bottleneck",
                statement="Проверить график работы линии.",
                evidence_ids=["invented-evidence"],
            )
        ]
    )

    signals = await HypothesisGenerator(provider).generate(_graph(), [finding], [])

    assert signals == []


@pytest.mark.asyncio
async def test_generator_discards_model_numeric_conflict_without_detector_candidate() -> None:
    first = _finding("Производительность составляет 10 т/ч.", finding_id="finding-a")
    second = _finding("Производительность составляет 15 т/ч.", finding_id="finding-b")
    provider = _Provider(
        [
            HypothesisDraft(
                kind="numeric_discrepancy",
                statement="Показатели производительности не совпадают.",
                evidence_ids=["finding-a:0", "finding-b:0"],
            )
        ]
    )

    signals = await HypothesisGenerator(provider).generate(
        _graph(), [first, second], []
    )

    assert signals == []


@pytest.mark.asyncio
async def test_generator_keeps_unlisted_kind() -> None:
    finding = _finding("Время ожидания оборудования составляет 40 минут.")
    provider = _Provider(
        [
            HypothesisDraft(
                kind="maintenance_window_risk",
                statement="Проверить, совпадает ли ожидание с окном обслуживания.",
                proposal="Сопоставить журнал ожиданий с графиком обслуживания.",
                evidence_ids=["finding-1:0"],
            )
        ]
    )

    signals = await HypothesisGenerator(provider).generate(_graph(), [finding], [])

    assert len(signals) == 1
    assert signals[0].kind == "maintenance_window_risk"
    assert signals[0].evidence == finding.evidence
    assert signals[0].data_class == DataClass.INTERNAL


@pytest.mark.asyncio
async def test_signal_id_includes_full_evidence_locator() -> None:
    original = _finding("Ожидание оборудования составляет 40 минут.")
    another_page = original.model_copy(
        deep=True,
        update={
            "evidence": [original.evidence[0].model_copy(update={"page": 4})],
        },
    )
    draft = HypothesisDraft(
        kind="bottleneck",
        statement="Проверить причины ожидания оборудования.",
        evidence_ids=["finding-1:0"],
    )

    first = await HypothesisGenerator(_Provider([draft])).generate(_graph(), [original], [])
    repeated = await HypothesisGenerator(_Provider([draft])).generate(_graph(), [original], [])
    second = await HypothesisGenerator(_Provider([draft])).generate(_graph(), [another_page], [])

    assert first[0].id == repeated[0].id
    assert first[0].id != second[0].id


@pytest.mark.asyncio
async def test_generator_ignores_retrieval_chunks() -> None:
    chunk = _finding("Длинный фрагмент текста из документа.", finding_id="chunk-123")
    provider = _Provider(
        [
            HypothesisDraft(
                kind="information_gap",
                statement="Нужно сопоставить источники.",
                evidence_ids=["chunk-123:0"],
            )
        ]
    )

    signals = await HypothesisGenerator(provider).generate(_graph(), [chunk], [])

    assert signals == []
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_generator_rejects_evidence_from_another_batch() -> None:
    first = _finding("Первый документ описывает отдельный процесс.", finding_id="finding-first")
    second = _finding("Второй документ описывает другой процесс.", finding_id="finding-second")
    class CrossBatchProvider(_Provider):
        async def complete_model(
            self,
            system: str,
            user: str,
            schema: type[_Output],
            *,
            model: str | None = None,
        ) -> _Output:
            self.calls += 1
            drafts = self.drafts if self.calls == 1 else []
            return schema.model_validate(
                {"hypotheses": [draft.model_dump() for draft in drafts]}
            )

    provider = CrossBatchProvider(
        [
            HypothesisDraft(
                kind="bottleneck",
                statement="Сопоставить процессы между источниками.",
                evidence_ids=["finding-second:0"],
            )
        ]
    )

    signals = await HypothesisGenerator(provider).generate(_graph(), [first, second], [])

    assert signals == []
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_generator_does_not_mix_data_classes_in_one_prompt() -> None:
    public = _finding("Публичный процесс длится 15 минут.", finding_id="finding-claim-public")
    restricted = _finding(
        "Закрытый процесс задерживается на секретном этапе.",
        finding_id="finding-claim-restricted",
    ).model_copy(update={"data_class": DataClass.RESTRICTED})
    graph = GraphSnapshot(
        nodes=[
            {"id": "claim-public", "label": "Публичное утверждение", "type": "claim"},
            {"id": "claim-restricted", "label": "Закрытое утверждение", "type": "claim"},
            {"id": "shared", "label": "Процесс", "type": "material"},
        ],
        edges=[
            {
                "id": "public-link",
                "source": "claim-public",
                "target": "shared",
                "relation": "RELATED_TO",
            },
            {
                "id": "restricted-link",
                "source": "claim-restricted",
                "target": "shared",
                "relation": "RELATED_TO",
            },
        ],
    )

    class PromptRecorder(_Provider):
        def __init__(self) -> None:
            super().__init__([])
            self.prompts: list[str] = []

        async def complete_model(
            self,
            system: str,
            user: str,
            schema: type[_Output],
            *,
            model: str | None = None,
        ) -> _Output:
            self.calls += 1
            self.prompts.append(user)
            return schema.model_validate({"hypotheses": []})

    provider = PromptRecorder()

    await HypothesisGenerator(provider).generate(graph, [public, restricted], [])

    assert provider.calls == 2
    assert all(
        "Публичный процесс" not in prompt or "Закрытый процесс" not in prompt
        for prompt in provider.prompts
    )


@pytest.mark.asyncio
async def test_numeric_hypothesis_uses_only_comparable_conflicts() -> None:
    document_a = uuid4()
    document_b = uuid4()
    observations = [
        NumericObservation(
            property_name="часовая производительность",
            operator="eq",
            value=10,
            normalized_value=10,
            unit="т/ч",
            normalized_unit="т/ч",
            raw_text="10 т/ч",
        )
    ]
    left = _finding(
        "Участок А обрабатывает 10 т/ч.",
        finding_id="finding-a",
        document_id=document_a,
        observations=observations,
        subject="Участок А",
    )
    right = _finding(
        "Участок А обрабатывает 15 т/ч.",
        finding_id="finding-b",
        document_id=document_b,
        observations=[observations[0].model_copy(update={"value": 15, "normalized_value": 15})],
        subject="Участок А",
    )
    incomparable = _finding(
        "Участок А обрабатывает 18 кг/ч.",
        finding_id="finding-c",
        document_id=uuid4(),
        observations=[
            observations[0].model_copy(
                update={
                    "value": 18,
                    "normalized_value": 18,
                    "unit": "кг/ч",
                    "normalized_unit": "кг/ч",
                }
            )
        ],
        subject="Участок А",
    )
    provider = _Provider([])

    corpus = [left, right, incomparable]
    conflicts = ResearchIntelligenceService().detect_conflicts(to_research_claims(corpus))
    signals = await HypothesisGenerator(provider).generate(_graph(), corpus, conflicts)

    numeric = [signal for signal in signals if signal.kind == "numeric_discrepancy"]
    assert len(numeric) == 1
    assert {item.document_id for item in numeric[0].evidence} == {document_a, document_b}


@pytest.mark.asyncio
async def test_model_cannot_publish_numeric_conflict_under_another_kind() -> None:
    observations = [
        NumericObservation(
            property_name="производительность",
            operator="eq",
            value=10,
            normalized_value=10,
            unit="т/ч",
            normalized_unit="т/ч",
            raw_text="10 т/ч",
        )
    ]
    left = _finding(
        "Линия обрабатывает 10 т/ч.",
        finding_id="finding-a",
        subject="Линия",
        observations=observations,
    )
    right = _finding(
        "Линия обрабатывает 10 кг/ч.",
        finding_id="finding-b",
        subject="Линия",
        observations=[
            observations[0].model_copy(update={"unit": "кг/ч", "normalized_unit": "кг/ч"})
        ],
    )
    provider = _Provider(
        [
            HypothesisDraft(
                kind="cross_source_conflict",
                statement="Источники противоречат друг другу по производительности.",
                evidence_ids=["finding-a:0", "finding-b:0"],
            )
        ]
    )

    signals = await HypothesisGenerator(provider).generate(_graph(), [left, right], [])

    assert signals == []


@pytest.mark.asyncio
async def test_numeric_signal_statement_stays_within_domain_limit() -> None:
    observation = NumericObservation(
        property_name="Показатель " + "длинное название " * 12,
        operator="eq",
        value=10,
        normalized_value=10,
        unit="т/ч",
        normalized_unit="т/ч",
        raw_text="10 т/ч",
    )
    findings = [
        _finding(
            "Линия обрабатывает значение " + "десять " * 24,
            finding_id="finding-a",
            subject="Линия",
            observations=[observation],
        ),
        _finding(
            "Линия обрабатывает значение " + "пятнадцать " * 20,
            finding_id="finding-b",
            subject="Линия",
            observations=[observation.model_copy(update={"value": 15, "normalized_value": 15})],
        ),
    ]
    conflicts = ResearchIntelligenceService().detect_conflicts(to_research_claims(findings))

    signals = await HypothesisGenerator(_Provider([])).generate(_graph(), findings, conflicts)

    numeric = [signal for signal in signals if signal.kind == "numeric_discrepancy"]
    assert len(numeric) == 1
    assert len(numeric[0].statement) <= 600


@pytest.mark.asyncio
async def test_numeric_signal_id_does_not_depend_on_candidate_side_order() -> None:
    observation = NumericObservation(
        property_name="производительность",
        operator="eq",
        value=10,
        normalized_value=10,
        unit="т/ч",
        normalized_unit="т/ч",
        raw_text="10 т/ч",
    )
    first = _finding(
        "Линия обрабатывает 10 т/ч.",
        finding_id="finding-a",
        subject="Линия",
        observations=[observation],
    )
    second = _finding(
        "Линия обрабатывает 15 т/ч.",
        finding_id="finding-b",
        subject="Линия",
        observations=[observation.model_copy(update={"value": 15, "normalized_value": 15})],
    )
    service = ResearchIntelligenceService()
    forward = service.detect_conflicts(to_research_claims([first, second]))
    reversed_candidates = service.detect_conflicts(to_research_claims([second, first]))

    left = await HypothesisGenerator(_Provider([])).generate(_graph(), [first, second], forward)
    right = await HypothesisGenerator(_Provider([])).generate(
        _graph(), [second, first], reversed_candidates
    )

    assert [signal.id for signal in left] == [signal.id for signal in right]


def test_hypotheses_window_applies_acl_and_pagination() -> None:
    from scientific_tangle.domain.hypotheses import HypothesisSignal, HypothesisWindow

    public = HypothesisSignal(
        id=uuid4(),
        kind="bottleneck",
        statement="Проверить ожидания оборудования.",
        confidence=0.7,
        evidence=[_finding("Ожидание составляет 40 минут.").evidence[0]],
        data_class=DataClass.PUBLIC,
    )
    public_second = HypothesisSignal(
        id=uuid4(),
        kind="information_gap",
        statement="Сопоставить периоды измерений.",
        confidence=0.7,
        evidence=[_finding("Период измерений не указан.").evidence[0]],
        data_class=DataClass.PUBLIC,
    )
    restricted = HypothesisSignal(
        id=uuid4(),
        kind="information_gap",
        statement="Сопоставить закрытые показатели.",
        confidence=0.7,
        evidence=[_finding("Закрытые показатели не приведены.").evidence[0]],
        data_class=DataClass.RESTRICTED,
    )

    window = HypothesisWindow.from_signals(
        [restricted, public, public_second],
        allowed_data_classes={DataClass.PUBLIC},
        limit=1,
        offset=1,
    )

    assert window.total == 2
    assert window.signals == [public_second]
    assert window.limit == 1
    assert window.offset == 1


def test_upsert_hypotheses_is_idempotent_and_enforces_window_acl() -> None:
    from scientific_tangle.domain.hypotheses import HypothesisSignal
    from scientific_tangle.services.knowledge import InMemoryKnowledgeBase

    signal = HypothesisSignal(
        id=uuid4(),
        kind="bottleneck",
        statement="Проверить ожидание оборудования.",
        confidence=0.7,
        evidence=[_finding("Ожидание длится 40 минут.").evidence[0]],
        data_class=DataClass.INTERNAL,
    )
    knowledge = InMemoryKnowledgeBase()

    knowledge.upsert_hypotheses([signal])
    revised = signal.model_copy(
        update={"statement": "Проверить ожидание оборудования и причины."}
    )
    knowledge.upsert_hypotheses([revised])

    visible = knowledge.hypotheses_window(
        limit=10, offset=0, allowed_data_classes={DataClass.INTERNAL}
    )
    hidden = knowledge.hypotheses_window(
        limit=10, offset=0, allowed_data_classes={DataClass.PUBLIC}
    )
    assert visible.total == 1
    assert visible.signals[0].statement.endswith("и причины.")
    assert hidden.total == 0
