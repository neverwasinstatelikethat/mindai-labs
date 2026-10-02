import inspect
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

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
from scientific_tangle.evaluation.harness import (
    DEFAULT_PIPELINE_CASES,
    MIN_SAMPLES_FOR_P95,
    EvaluationHarness,
    _numbers_grounded,
)
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


@pytest.mark.asyncio
async def test_pipeline_p95_names_an_insufficient_sample() -> None:
    """Три кейса — это не p95: числом становится наихудший прогон, причина — в notes.

    Прежний замер клал `p95` из трёх точек и молча сравнивал его с
    ``PIPELINE_P95_LATENCY_MS_FLOOR``; вызывающий слой обязан видеть, что
    перцентиля не было.
    """
    registry = AgentMetricsRegistry()
    harness = EvaluationHarness(metrics=registry)
    cases = [
        GoldCase(
            id=f"case-{index}",
            language="ru",
            question=f"Вопрос {index}",
            source_path=f"doc-{index}",
            expected_source_titles=[f"doc-{index}"],
        )
        for index in range(3)
    ]
    notes: list[str] = []

    metrics, results = await harness.evaluate_workflow(
        _StubWorkflow(registry), cases, notes=notes
    )

    assert len(results) == 3
    # Наихудший наблюдаемый прогон вместо перцентиля: среднее не больше «хвоста».
    assert metrics.p95_latency_ms >= metrics.average_latency_ms
    assert any("недостаточная выборка" in note for note in notes)
    # Пустой ответ не проходит проверку не потому, что «метрики вакуумны», а потому
    # что трассировать нечего: ни один кейс не засчитан.
    assert [result.passed for result in results] == [False, False, False]


def test_pipeline_default_sample_is_ten_cases() -> None:
    """Потолок выборки сквозного прогона поднят с 3 до 10 gold-кейсов."""
    signature = inspect.signature(EvaluationHarness.benchmark_pipeline)
    assert signature.parameters["max_cases"].default == DEFAULT_PIPELINE_CASES == 10
    assert DEFAULT_PIPELINE_CASES >= MIN_SAMPLES_FOR_P95


# ── Метрика отмены: исход обращения, а не отказ модели ──────────────────


def test_cancelled_llm_call_is_its_own_outcome() -> None:
    """Отмена — отдельная серия Prometheus и не «failure» схемы.

    Провайдер зовёт ``observe_llm`` в ``finally`` с ``success=False`` и на отмене, и
    на таймауте: без явного исхода доля отказов модели зависела бы от поведения
    клиента. Именные счётчики при этом не удваиваются.
    """
    from prometheus_client import REGISTRY

    schema = "CancelledOutcomeProbe"
    registry = AgentMetricsRegistry()
    failures_before = (
        REGISTRY.get_sample_value("mindai_llm_calls_total", {"schema": schema, "status": "failure"})
        or 0.0
    )
    cancelled_before = (
        REGISTRY.get_sample_value(
            "mindai_llm_calls_total", {"schema": schema, "status": "cancelled"}
        )
        or 0.0
    )

    registry.observe_llm_cancelled(schema)
    registry.observe_llm(schema, 40.0, success=False, prompt_tokens=10, outcome="cancelled")

    snapshot = registry.snapshot()
    assert snapshot.llm[0].calls == 1
    assert snapshot.llm[0].failures == 0
    assert snapshot.llm[0].cancelled == 1
    assert (
        REGISTRY.get_sample_value(
            "mindai_llm_calls_total", {"schema": schema, "status": "cancelled"}
        )
        == cancelled_before + 1
    )
    assert (
        REGISTRY.get_sample_value(
            "mindai_llm_calls_total", {"schema": schema, "status": "failure"}
        )
        or 0.0
    ) == failures_before


def test_llm_run_usage_separates_cancellation_from_failure() -> None:
    """Учёт расхода запроса различает отказ и отмену: «failures» остаётся отказом."""
    from scientific_tangle.services.agent_metrics import begin_llm_usage

    registry = AgentMetricsRegistry()
    schema = "CancelledUsageProbe"
    usage = begin_llm_usage()
    registry.observe_llm(schema, 10.0, success=False, prompt_tokens=5, outcome="cancelled")
    registry.observe_llm(schema, 10.0, success=False, prompt_tokens=5, outcome="timeout")
    registry.observe_llm(schema, 10.0, success=False, prompt_tokens=5)

    assert (usage.calls, usage.cancelled, usage.timeouts, usage.failures) == (3, 1, 1, 1)


# ── Границы сигналов re-rank и единицы матрицы сравнения ────────────────


def _observation(
    value: float, unit: str, normalized_unit: str, normalized: float
) -> NumericObservation:
    return NumericObservation(
        property_name="recovery",
        operator="eq",
        value=value,
        unit=unit,
        normalized_value=normalized,
        normalized_unit=normalized_unit,
        raw_text=f"{value} {unit}",
    )


def _comparison_finding(
    index: int, value: float, unit: str, normalized_unit: str, normalized: float
) -> Finding:
    return Finding(
        id=f"cmp-{index}",
        statement="Извлечение меди",
        confidence=0.8,
        subject="медь",
        predicate="HAS_RECOVERY",
        observations=[_observation(value, unit, normalized_unit, normalized)],
        evidence=[
            EvidenceLocator(
                document_id=uuid.UUID(int=11), source_title="ОИП", page=1, quote="95 %"
            )
        ],
    )


def _ranked_finding(index: int, observations: list[NumericObservation]) -> Finding:
    return Finding(
        id=f"finding-{index}",
        statement=f"Извлечение меди {index}",
        confidence=0.9,
        subject="медь",
        observations=observations,
        evidence=[
            EvidenceLocator(
                document_id=uuid.UUID(int=9), source_title="ОИП", page=1, quote="извлечение 95 %"
            )
        ],
    )


def test_rerank_signals_stay_inside_the_contracted_unit_interval() -> None:
    """Ни один сигнал и суммарный скор не выше 1.0: шкала, на которой живёт порог.

    ``_numeric_support`` раньше возвращал до 1.1 (насыщение 1.0 плюс бонус за
    несколько единиц измерения), а веса в сумме давали 1.08 — «идеальная» находка
    получала скор выше возможного.
    """
    from scientific_tangle.services.reranking import rerank_findings

    observations = [
        _observation(90.0 + index, "%", "ratio", 0.9 + index / 100) for index in range(6)
    ]
    findings = [_ranked_finding(index, observations) for index in range(4)]
    result = rerank_findings(
        "извлечение меди",
        findings,
        top_k=4,
        fusion={finding.id: 1.0 for finding in findings},
        communities={findings[0].id: "c1", findings[1].id: "c1"},
    )

    for signals in result.signals.values():
        assert 0.0 <= signals.numeric <= 1.0
        assert 0.0 <= signals.total <= 1.0
    assert signals.numeric == 1.0  # насыщение, а не 1.1


def test_comparison_cell_refuses_a_single_unit_for_different_measures() -> None:
    """Ячейка с разными нормированными единицами не подписывается единицей первой.

    «70 %» (0.7 ratio) и «70000 ppm» — несопоставимые подписи в одной колонке:
    прежняя реализация клала единицу первого совпадения и прятала расхождение.
    """
    from scientific_tangle.domain.contracts import ComparisonRequest
    from scientific_tangle.services.comparison import INCOMPARABLE_MARKER, ComparisonService

    cell = ComparisonService().compare(
        [
            _comparison_finding(1, 70.0, "%", "ratio", 0.7),
            _comparison_finding(2, 70000.0, "ppm", "ppm", 70000.0),
        ],
        ComparisonRequest(question="сравнить", entities=["медь"], dimensions=["recovery"]),
    ).rows[0].cells["recovery"]
    assert cell.unit is None
    assert INCOMPARABLE_MARKER in (cell.value or "")
    text = cell.value or ""
    assert "70.0 %" in text and "0.7 ratio" in text
    assert "70000.0 ppm" in text

    same_unit = ComparisonService().compare(
        [
            _comparison_finding(3, 70.0, "%", "%", 70.0),
            _comparison_finding(4, 95.0, "%", "%", 95.0),
        ],
        ComparisonRequest(question="сравнить", entities=["медь"], dimensions=["recovery"]),
    ).rows[0].cells["recovery"]
    assert same_unit.unit == "%"
    # Знак оператора — часть утверждения: «=95» и «≥95» в таблице значат разное.
    assert same_unit.value == "=70.0 / =95.0"


# ── Потолок активной политики и неприменяемые предложения ───────────────


def _proposal(kind: str, change: str, minutes: int) -> Any:
    from scientific_tangle.domain.contracts import EvolutionProposal

    return EvolutionProposal(
        source_query_id=uuid4(),
        kind=kind,  # type: ignore[arg-type]
        title=kind,
        change=change,
        status="accepted",
        impact=[],
        created_at=datetime.now(UTC) + timedelta(minutes=minutes),
    )


def _evolution_service(proposals: list[Any]) -> Any:
    from scientific_tangle.services.evolution import EvolutionService

    service = EvolutionService(provider=None, policy_limit_chars=120)  # type: ignore[arg-type]
    service._proposals = {proposal.id: proposal for proposal in proposals}
    return service


def test_active_policy_is_capped_and_names_what_did_not_fit() -> None:
    """Принятые правки поверх потолка не исчезают молча: они перечислены как усечённые.

    Вызывающий слой (сборка промпта) обязан показать ``policy_truncated`` как
    деградацию ответа, иначе «принято экспертом» значило бы «действует», хотя текст
    в промпт не попал.
    """
    from scientific_tangle.services.evolution import POLICY_SEPARATOR

    blocks = [f"Правка {index}: " + "x" * 40 for index in range(6)]
    service = _evolution_service(
        [_proposal("rule", change, index) for index, change in enumerate(blocks)]
    )

    report = service.active_policy_report()
    assert len(report.text) <= 120
    assert report.text == service.active_policy()
    assert report.degraded is True
    assert report.truncated
    assert set(report.applied) & set(report.truncated) == set()
    assert "policy_truncated" in report.degradation_reasons()
    assert any("усечена" in note for note in report.notes())
    # Отсечка сохраняет порядок: в текст идут самые ранние решения эксперта.
    assert report.text.startswith(blocks[0])
    assert POLICY_SEPARATOR in report.text


def test_accepted_alias_and_gold_case_are_marked_not_applied() -> None:
    """Статус ``accepted`` у alias/gold_case не обещает применения — и это видно.

    Ни разбиратель сущностей, ни корпус эти предложения не читают; вместо
    выдуманного применения интерфейс получает явный список «принято, но не
    применено», а контракт статуса остаётся прежним.
    """
    alias = _proposal("alias", "Обратный осмос ← reverse osmosis", 0)
    gold = _proposal("gold_case", "Добавить кейс по сподумену", 1)
    rule = _proposal("rule", "Единицы обязательны.", 2)
    report = _evolution_service([alias, gold, rule]).active_policy_report()

    assert report.text == "Единицы обязательны."
    assert report.not_applied == [str(alias.id), str(gold.id)]
    assert "proposal_not_applied" in report.degradation_reasons()
    assert report.degraded is False  # усечения нет — деградация только по применению
