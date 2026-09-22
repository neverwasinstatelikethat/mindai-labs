import uuid

import pytest

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    AnswerPayload,
    EntityResolutionProposal,
    EvidenceLocator,
    Finding,
    GoldCase,
)
from scientific_tangle.domain.models import NumericObservation
from scientific_tangle.evaluation.harness import EvaluationHarness, _numbers_grounded
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from scientific_tangle.services.resolution import EntityResolutionWorkbench


def test_multilingual_gold_retrieval_recall() -> None:
    benchmark = EvaluationHarness().benchmark_retrieval(InMemoryKnowledgeBase())

    assert benchmark.gold_cases == 10
    assert benchmark.top_k == 3
    assert benchmark.hybrid.recall_at_3 < 1
    # Регрессия V-4: методика замера отделена от качества. На in-memory
    # корпусе gold-источников нет, и benchmark обязан называть это отсутствием
    # покрытия, а не провалом поиска по несбываемому условию.
    assert benchmark.scored_cases == 0
    assert benchmark.validity_checks["expected_sources_present_in_corpus"] is False
    assert benchmark.validity_checks["corpus_larger_than_top_k"] is True
    assert benchmark.validity_checks["queries_do_not_copy_source_titles"] is True
    assert not benchmark.passed


def test_entity_merge_is_reviewable_and_reversible() -> None:
    workbench = EntityResolutionWorkbench(Settings(knowledge_backend="memory"))
    workbench.register(
        [
            EntityResolutionProposal(
                mention="reverse osmosis",
                canonical_name="Обратный осмос",
                action="link",
                confidence=0.96,
                rationale="Переводной эквивалент.",
            )
        ]
    )
    proposal = workbench.list_proposals()[0]

    assert workbench.review(proposal.id, "accept").status == "accepted"
    assert workbench.review(proposal.id, "revert").status == "reverted"


def _finding(statement: str, quote: str, observations: list[NumericObservation]) -> Finding:
    return Finding(
        id="f-1",
        statement=statement,
        confidence=0.9,
        evidence=[EvidenceLocator(document_id=uuid.UUID(int=1), quote=quote, page=1)],
        observations=observations,
    )


def test_numbers_grounded_counts_thousands_and_observation_bounds() -> None:
    """Семантика метрики совпадает с guardrail workflow: тысячи и границы наблюдений."""
    thousands = _finding(
        "Расход составляет 10 000 м3/ч.",
        "расход раствора 10000 м3/ч",
        [],
    )
    bounds = _finding(
        "Концентрация 5 г/л.",
        "концентрация железа на входе",
        [
            NumericObservation(
                property_name="концентрация",
                operator="between",
                min_value=5.0,
                max_value=8.0,
                unit="г/л",
                normalized_min=5.0,
                normalized_max=8.0,
                normalized_unit="г/л",
                raw_text="от 5 до 8 г/л",
            )
        ],
    )
    unsupported = _finding("Температура 95 °C.", "температура среды", [])

    assert _numbers_grounded(thousands)
    assert _numbers_grounded(bounds)
    assert not _numbers_grounded(unsupported)


class _StubWorkflow:
    """Отвечает пустым ответом и тратит токены/повторы своего кейса в реестре."""

    def __init__(self, registry: AgentMetricsRegistry) -> None:
        self._registry = registry

    async def run(self, request: object) -> AnswerPayload:
        self._registry.observe_llm(
            "ReasoningResult",
            10.0,
            success=True,
            prompt_tokens=100,
            completion_tokens=50,
        )
        self._registry.observe_llm_retry("ReasoningResult")
        return AnswerPayload.model_construct(findings=[], degradation_reasons=[])


@pytest.mark.asyncio
async def test_workflow_metrics_attributed_per_case() -> None:
    """Токены и повторы варианта не включают чужой трафик реестра (A/B-сравнение)."""
    registry = AgentMetricsRegistry()
    registry.observe_llm(
        "ReasoningResult", 10.0, success=True, prompt_tokens=1000, completion_tokens=500
    )
    harness = EvaluationHarness(metrics=registry)
    cases = [
        GoldCase(
            id="case-1",
            language="ru",
            question="Вопрос 1",
            source_path="doc-1",
            expected_source_titles=["doc-1"],
        ),
        GoldCase(
            id="case-2",
            language="ru",
            question="Вопрос 2",
            source_path="doc-2",
            expected_source_titles=["doc-2"],
        ),
    ]

    metrics, results = await harness.evaluate_workflow(_StubWorkflow(registry), cases)

    # 1500 токенов «чужого» трафика до прогона не попадают в метрику варианта:
    # два кейса × (100 prompt + 50 completion) = 300.
    assert metrics.total_tokens == 300
    assert metrics.retries_per_case == pytest.approx(1.0)
    assert [result.case_id for result in results] == ["case-1", "case-2"]
