"""Поведение tool-исполнителя: объединённый пул, ярусы связи и честные срезы.

Здесь проверяется не текст observation, а обещания, которые продукт даёт аналитику:
conflict/gap считаются по всем доказательствам запроса, размер пула называется,
срез выглядит как срез, а отсутствие research space — как отсутствие, а не как
«ноль пробелов».
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import product
from uuid import UUID

import pytest

from scientific_tangle.agents.tools import (
    MAX_CONFLICT_FACTS,
    ResearchToolExecutor,
    ToolExecutionResult,
)
from scientific_tangle.domain.contracts import (
    AgentActionPlan,
    Finding,
    GraphSnapshot,
    ToolAction,
    ToolObservation,
)
from scientific_tangle.domain.models import EvidenceLocator, NumericObservation, QueryPlan
from scientific_tangle.services.knowledge import RetrievalContext
from scientific_tangle.services.research_intelligence import (
    ResearchIntelligenceService,
    comparable_groups,
    to_research_claims,
)

DOC_ID = UUID(int=11)
# Потолок глубины обхода задаёт схема ToolAction/RetrievalPlan (le=4).
HOP_CEILING = 4


class _StubKnowledge:
    """Отдаёт заданные находки: пул conflict/gap формирует исполнитель, а не корпус."""

    def __init__(self, findings: Sequence[Finding] = ()) -> None:
        self._findings = list(findings)
        self.plans: list[list[str]] = []

    def retrieve(
        self,
        query_plan: QueryPlan,
        retrieval_plan: object,
        allowed_data_classes: object = None,
    ) -> RetrievalContext:
        self.plans.append(list(getattr(retrieval_plan, "relation_types", [])))
        return RetrievalContext(
            findings=list(self._findings),
            graph=GraphSnapshot(nodes=[], edges=[], communities=[]),
            community_summaries=[],
            no_evidence=not self._findings,
        )


def observation(prop: str, low: float, high: float, unit: str = "%") -> NumericObservation:
    return NumericObservation(
        property_name=prop,
        operator="between",
        min_value=low,
        max_value=high,
        normalized_min=low,
        normalized_max=high,
        unit=unit,
        normalized_unit=unit,
        raw_text=f"{low:g}–{high:g} {unit}",
    )


def finding(
    identifier: str,
    subject: str,
    prop: str,
    low: float,
    high: float,
    scope: dict[str, str] | None = None,
    unit: str = "%",
) -> Finding:
    return Finding(
        id=f"f-{identifier}",
        statement=f"{subject}: {prop} {low}–{high} {unit}",
        confidence=0.8,
        subject=subject,
        predicate="HAS_EFFICIENCY",
        scope={"geography": "RU"} if scope is None else scope,
        observations=[observation(prop, low, high, unit)],
        evidence=[EvidenceLocator(document_id=DOC_ID, source_title="Отчёт", page=1, quote="число")],
    )


def action(tool: str, identifier: str, **kwargs: object) -> ToolAction:
    return ToolAction(
        id=identifier,
        tool=tool,  # type: ignore[arg-type]
        purpose=f"Действие {tool}",
        query="эффективность метода очистки",
        **kwargs,  # type: ignore[arg-type]
    )


def plan_for(tool: str, **kwargs: object) -> AgentActionPlan:
    return AgentActionPlan(
        rationale="Проверка пула",
        actions=[action(tool, "a1", **kwargs)],
        completion_criteria=["Числа сравнены"],
    )


QUERY_PLAN = QueryPlan(question="Сравнить методы?", language="ru", mode="local", max_hops=2)


def _only(result: ToolExecutionResult) -> ToolObservation:
    assert len(result.observations) == 1
    return result.observations[0]


# ── Конфликты по объединённому пулу (п. 4) ─────────────────────────────────


@pytest.mark.asyncio
async def test_conflict_pair_from_different_rounds_is_found() -> None:
    """Пара из предыдущего раунда и из текущего обязана сойтись в одном анализе."""
    executor = ResearchToolExecutor(_StubKnowledge([finding("b", "ro", "rejection", 40, 70)]))

    result = await executor.execute(
        plan_for("conflict_scan"),
        QUERY_PLAN,
        None,
        prior_findings=[finding("a", "ro", "rejection", 92, 98)],
    )

    assert len(result.conflicts) == 1
    assert "f-a" in result.conflicts[0] and "f-b" in result.conflicts[0]
    assert "из 2 доказательств" in _only(result).summary


@pytest.mark.asyncio
async def test_same_finding_twice_does_not_self_conflict() -> None:
    """Дедуп пула: без него одна находка дала бы пару «тезис сам с собой»."""
    shared = finding("a", "ro", "rejection", 92, 98)
    executor = ResearchToolExecutor(_StubKnowledge([shared]))

    result = await executor.execute(
        plan_for("conflict_scan"), QUERY_PLAN, None, prior_findings=[shared]
    )

    assert result.conflicts == []
    assert "из 1 доказательств" in _only(result).summary


@pytest.mark.asyncio
async def test_values_in_different_units_are_not_a_contradiction() -> None:
    """0,2–0,3 г/л против 200–300 мг/л — одна величина в разных шкалах, не спор."""
    executor = ResearchToolExecutor(
        _StubKnowledge([finding("b", "ro", "rejection", 200, 300, unit="mg/L")])
    )

    result = await executor.execute(
        plan_for("conflict_scan"),
        QUERY_PLAN,
        None,
        prior_findings=[finding("a", "ro", "rejection", 0.2, 0.3, unit="g/L")],
    )

    assert result.conflicts == []


@pytest.mark.asyncio
async def test_conflict_cut_is_reported_as_a_cut() -> None:
    """Срез потолка видим: «не показано N», а не «это все конфликты»."""
    overflow = MAX_CONFLICT_FACTS + 3
    pool = [finding(f"hi{i}", f"subj{i}", "rejection", 90, 98) for i in range(overflow)] + [
        finding(f"lo{i}", f"subj{i}", "rejection", 10, 20) for i in range(overflow)
    ]
    executor = ResearchToolExecutor(_StubKnowledge(pool))

    result = await executor.execute(plan_for("conflict_scan"), QUERY_PLAN, None)

    cut = [item for item in result.conflicts if item.startswith("Не показано")]
    assert len(result.conflicts) - len(cut) == MAX_CONFLICT_FACTS
    assert cut and f"{overflow - MAX_CONFLICT_FACTS} пар противоречий" in cut[0]


@pytest.mark.asyncio
async def test_no_conflict_on_small_pool_is_not_corpus_consistency() -> None:
    executor = ResearchToolExecutor(_StubKnowledge([finding("a", "ro", "rejection", 92, 98)]))

    result = await executor.execute(plan_for("conflict_scan"), QUERY_PLAN, None)

    observed = _only(result)
    assert observed.status == "warning"
    assert "не доказательство согласованности" in observed.summary


def test_conflict_scan_scales_with_groups_not_with_the_pool() -> None:
    """Сравнение ограничено группами «субъект × предикат × свойство × единица».

    Пул всех раундов — это сотни тезисов: квадрат по нему сравнивает заведомо
    несопоставимые пары и упирается в агентский дедлайн.
    """
    pool = [finding(str(index), f"subj{index}", "rejection", 90, 95) for index in range(400)]
    claims = to_research_claims(pool)

    groups = comparable_groups(claims)
    compared = sum(len(group) * (len(group) - 1) // 2 for group in groups.values())

    assert len(claims) == 400
    # Квадрат по всему пулу дал бы 79 800 сравнений вместо нуля несопоставимых пар.
    assert compared == 0
    assert len(claims) * (len(claims) - 1) // 2 == 79_800
    assert ResearchIntelligenceService().detect_conflicts(claims) == []


def test_conflict_comparison_never_crosses_group_keys() -> None:
    """Каждая найденная пара сопоставима по ключу — группировка ничего не теряет."""
    claims = to_research_claims(
        [
            finding("a", "ro", "rejection", 92, 98),
            finding("b", "ro", "rejection", 40, 70),
            finding("c", "ix", "rejection", 10, 20),
            finding("d", "ro", "energy", 3, 5),
        ]
    )
    by_id = {claim.id: claim for claim in claims}

    conflicts = ResearchIntelligenceService().detect_conflicts(claims)

    assert [(item.left_claim_id, item.right_claim_id) for item in conflicts] == [
        (claims[0].id, claims[1].id)
    ]
    for conflict in conflicts:
        left, right = by_id[conflict.left_claim_id], by_id[conflict.right_claim_id]
        assert (left.subject_id, left.predicate, left.value.property_name, left.value.unit) == (
            right.subject_id,
            right.predicate,
            right.value.property_name,
            right.value.unit,
        )


# ── Пробелы по реальным данным (п. 5) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_gap_dimensions_come_from_pool_not_from_entity_mentions() -> None:
    """Синтетический space из mentions удалён: 5 упоминаний не дают 5 измерений."""
    executor = ResearchToolExecutor(_StubKnowledge([finding("a", "ro", "rejection", 92, 98)]))
    query_plan = QUERY_PLAN.model_copy(update={"entity_mentions": ["a", "b", "c", "d", "e"]})

    result = await executor.execute(plan_for("gap_scan"), query_plan, None)

    observed = _only(result)
    assert "geography" in observed.summary
    assert "subject:" not in observed.summary
    assert result.gaps == []


@pytest.mark.asyncio
async def test_gap_scan_without_evidence_warns_instead_of_reporting_zero() -> None:
    executor = ResearchToolExecutor(_StubKnowledge([]))

    result = await executor.execute(plan_for("gap_scan"), QUERY_PLAN, None)

    observed = _only(result)
    assert observed.status == "warning"
    assert "Research space не определён" in observed.summary
    assert "непокрытых" not in observed.summary
    assert result.gaps == []
    assert result.gaps_omitted == 0


@pytest.mark.asyncio
async def test_gap_matrix_is_bounded_and_omission_reaches_the_answer() -> None:
    """Сверх лимита: gaps_omitted nonzero, и строка среза едет в ответ пользователю."""
    combos = [
        ("ro", "rejection", "RU", "cold", "acid"),
        ("ix", "energy", "KZ", "warm", "base"),
        ("nf", "rejection", "CL", "arid", "acid"),
        ("ev", "energy", "AU", "cold", "base"),
    ]
    pool = [
        finding(
            f"{subject}-{prop}-{geo}-{regime}-{reagent}",
            subject,
            prop,
            90,
            95,
            scope={"geography": geo, "regime": regime, "reagent": reagent},
        )
        for subject, prop, geo, regime, reagent in combos
    ]
    dimensions = {
        "geography": {item[2] for item in combos},
        "regime": {item[3] for item in combos},
        "reagent": {item[4] for item in combos},
    }
    total = len(dimensions["geography"]) * len(dimensions["regime"]) * len(dimensions["reagent"])
    executor = ResearchToolExecutor(_StubKnowledge(pool))

    result = await executor.execute(plan_for("gap_scan"), QUERY_PLAN, None)

    shown = [item for item in result.gaps if item.startswith("Пробел:")]
    cut = [item for item in result.gaps if item.startswith("Не показано")]
    assert total - len(combos) > MAX_CONFLICT_FACTS
    assert len(shown) <= 12 and cut
    assert result.gaps_omitted == total - len(combos) - len(shown)
    assert f"{result.gaps_omitted}" in cut[0]


@pytest.mark.asyncio
async def test_wide_dimension_is_named_not_blurred() -> None:
    """Широкое измерение называется: иначе «непокрытые» описывают размер матрицы."""
    geographies = ["RU", "KZ", "CN", "CL", "US", "ZA", "PE", "AU"]
    pool = [
        finding(str(index), f"subj{index}", "rejection", 90, 95, scope={"geography": geo})
        for index, geo in enumerate(geographies * 2)
    ]
    executor = ResearchToolExecutor(_StubKnowledge(pool))

    result = await executor.execute(plan_for("gap_scan"), QUERY_PLAN, None)

    assert "широкие измерения: geography" in _only(result).summary


@pytest.mark.asyncio
async def test_pool_coverage_is_not_presented_as_corpus_completeness() -> None:
    subjects = ("ro", "ix")
    props = ("rejection", "energy")
    geographies = ("RU", "KZ")
    pool = [
        finding(
            f"{subject}-{prop}-{geo}",
            subject,
            prop,
            90,
            95,
            scope={"geography": geo},
        )
        for subject, prop, geo in product(subjects, props, geographies)
    ]
    executor = ResearchToolExecutor(_StubKnowledge(pool))

    result = await executor.execute(plan_for("gap_scan"), QUERY_PLAN, None)

    summary = _only(result).summary
    assert "Полнота корпуса не заявляется" in summary
    assert f"в {len(pool)} доказательств" in summary


# ── Отклонённые имена связи видны в observation (п. 2) ─────────────────────


@pytest.mark.asyncio
async def test_rejected_relation_is_reported_in_observation() -> None:
    executor = ResearchToolExecutor(_StubKnowledge([finding("a", "ro", "rejection", 9, 9)]))

    result = await executor.execute(
        plan_for("graph_traverse", relation_types=["CONTRADICTS"]), QUERY_PLAN, None
    )

    observed = _only(result)
    assert observed.status == "warning"
    assert "CONTRADICTS" in observed.summary
    assert any("CONTRADICTS" in item for item in observed.next_actions)
    assert any("CONTRADICTS" in item for item in result.degradation_reasons)


@pytest.mark.asyncio
async def test_partial_provenance_request_traverses_accepted_names_only() -> None:
    """Принятые имена уходят в план retrieval, отклонённые остаются в observation."""
    knowledge = _StubKnowledge([finding("a", "ro", "rejection", 9, 9)])
    executor = ResearchToolExecutor(knowledge)

    result = await executor.execute(
        plan_for("graph_traverse", relation_types=["PRODUCES", "NO_SUCH_REL"]),
        QUERY_PLAN,
        None,
    )

    assert knowledge.plans == [["PRODUCES"]]
    assert "NO_SUCH_REL" in _only(result).summary


@pytest.mark.asyncio
async def test_explicit_provenance_request_reaches_the_retrieval_plan() -> None:
    """Ярус по явному запросу доезжает до фильтра, но не трогает глубину плана."""
    knowledge = _StubKnowledge([finding("a", "ro", "rejection", 9, 9)])
    executor = ResearchToolExecutor(knowledge)

    await executor.execute(
        plan_for("graph_traverse", relation_types=["PRODUCES", "PROVENANCE"], max_hops=HOP_CEILING),
        QUERY_PLAN.model_copy(update={"max_hops": 1}),
        None,
    )

    assert "HAS_CHUNK" in knowledge.plans[0]
    assert "PRODUCES" in knowledge.plans[0]


@pytest.mark.asyncio
async def test_identical_actions_execute_retrieval_once() -> None:
    """Дубль в плане не удваивает retrieval; наблюдения возвращаются по action_id."""
    knowledge = _StubKnowledge()
    executor = ResearchToolExecutor(knowledge)
    duplicate = AgentActionPlan(
        rationale="Дубль в плане не должен удваивать retrieval",
        actions=[action("hybrid_search", "a1"), action("hybrid_search", "a2")],
        completion_criteria=["Доказательства собраны"],
    )

    result = await executor.execute(duplicate, QUERY_PLAN)

    assert len(knowledge.plans) == 1
    assert [item.action_id for item in result.observations] == ["a1", "a2"]
