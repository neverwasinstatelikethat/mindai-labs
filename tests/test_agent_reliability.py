"""Ненадёжные ветки рабочего процесса: ревизии, бюджеты, чекпоинтер, стрим.

Регрессионные тесты на находки H-3 (состояние просачивалось между запусками на
одном thread), H-4 (несколько слоёв retry перемножались) и M-серию (не были
проверены error-ветки, цикл critic→improver, лимит tool-раундов). Сюда же —
деградация SSE, перенасыщение бюджета контекста и числовой guardrail.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from time import monotonic
from typing import Any
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ValidationError

from scientific_tangle.agents.tools import ResearchToolExecutor, RunBudget
from scientific_tangle.agents.workflow import (
    _CONFIDENCE_PENALTY,
    _DEGRADABLE,
    _UNTRACED_CONFIDENCE_CAP,
    ModelFailureError,
    ResearchWorkflow,
    _degradation_code,
    _describe_failure,
    _language_mismatch,
    _numbers,
    _unit_conflicts,
    _unit_unmatched,
)
from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    AgentControlDecision,
    CritiqueResult,
    Finding,
    GraphSnapshot,
    PlanningBundle,
    QueryRequest,
    ReasoningResult,
)
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import EvidenceLocator, NumericObservation
from scientific_tangle.services.admission import (
    MIN_RETRY_AFTER_SECONDS,
    AdmissionRefusedError,
    AgentRunAdmission,
    agent_run_limit,
)
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry
from scientific_tangle.services.governance import AccessPolicyEngine
from scientific_tangle.services.knowledge import InMemoryKnowledgeBase
from scientific_tangle.services.provider import ModelUnavailableError
from tests.fakes import ScriptedProvider
from tests.test_workflow import planning_bundle

QUESTION = "Какие методы обессоливания подходят для шахтной воды?"
VISIBLE = {DataClass.PUBLIC, DataClass.INTERNAL}


class RepeatProvider:
    """Двойник провайдера: одно значение на схему, без исчерпания сценария."""

    mode = "scripted"

    def __init__(
        self,
        responses: dict[type[BaseModel], Callable[[], Awaitable[Any]] | BaseModel],
    ) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def complete_model(self, system: str, user: str, schema: type[Any]) -> Any:
        self.calls.append(schema.__name__)
        response = self._responses.get(schema)
        if response is None:
            raise ModelUnavailableError(f"Нет заготовки для {schema.__name__}")
        if callable(response):
            return await response()
        return response.model_copy(deep=True)


def reasoning(summary: str) -> ReasoningResult:
    return ReasoningResult(
        summary=summary,
        finding_ids=["finding-ro", "finding-ro-pilot"],
        conflicts=[],
        knowledge_gaps=[],
        recommendations=[],
    )


def rejected(issue: str) -> CritiqueResult:
    return CritiqueResult(
        approved=False, issues=[issue], revision_instructions=["Уточнить условия."]
    )


ENOUGH = AgentControlDecision(decision="reason", rationale="Доказательств достаточно.")
HOLD_ON = AgentControlDecision(
    decision="continue_tools",
    rationale="Не закрыт сравнительный контекст.",
    missing_evidence=["community coverage"],
)
OK = CritiqueResult(approved=True, issues=[], revision_instructions=[])


@pytest.mark.asyncio
async def test_critic_rejection_runs_improver_and_second_critique() -> None:
    provider = ScriptedProvider(
        planning_bundle(),
        ENOUGH,
        reasoning("Черновик без нормализации условий."),
        rejected("Условия применимости не согласованы."),
        reasoning("Ответ после ревизии: 95–99% задержания после pretreatment."),
        OK,
    )

    answer = await ResearchWorkflow(provider=provider).run(QueryRequest(question=QUESTION))

    assert [event.agent for event in answer.trace] == [
        "planning_agent",
        "tool_executor",
        "controller",
        "reasoner",
        "critic",
        "improver",
        "critic",
        "synthesizer",
    ]
    assert answer.summary.startswith("Ответ после ревизии")
    assert provider.calls == [
        "PlanningBundle",
        "AgentControlDecision",
        "ReasoningResult",
        "CritiqueResult",
        "ReasoningResult",
        "CritiqueResult",
    ]


@pytest.mark.asyncio
async def test_revision_loop_is_capped_by_budget() -> None:
    provider = ScriptedProvider(
        planning_bundle(),
        ENOUGH,
        reasoning("Первый черновик."),
        rejected("Не хватает данных по пилоту."),
        reasoning("Второй черновик."),
        rejected("Данных по пилоту всё ещё не хватает."),
    )
    settings = Settings(knowledge_backend="memory", agent_max_revisions=1)

    answer = await ResearchWorkflow(provider=provider, settings=settings).run(
        QueryRequest(question=QUESTION)
    )

    agents = [event.agent for event in answer.trace]
    assert agents.count("improver") == 1
    assert agents.count("critic") == 2
    assert answer.summary == "Второй черновик."


@pytest.mark.asyncio
async def test_tool_round_budget_forces_reasoning() -> None:
    """Контроллер не вправе зациклить tools: по исчерпании лимита он уходит в reason."""
    provider = RepeatProvider(
        {
            PlanningBundle: planning_bundle(),
            AgentControlDecision: HOLD_ON,
            ReasoningResult: reasoning("Добрано сколько успели."),
            CritiqueResult: OK,
        }
    )
    settings = Settings(knowledge_backend="memory", agent_max_tool_rounds=1)

    answer = await ResearchWorkflow(provider=provider, settings=settings).run(
        QueryRequest(question=QUESTION)
    )

    agents = [event.agent for event in answer.trace]
    assert agents.count("tool_executor") == 1
    assert "reasoner" in agents
    controller = [event for event in answer.trace if event.agent == "controller"]
    assert [event.status for event in controller] == ["revised"]


@pytest.mark.asyncio
async def test_deadline_salvages_evidence_instead_of_hanging() -> None:
    async def slow() -> ReasoningResult:
        await asyncio.sleep(5)
        return reasoning("Этот ответ достигнут не был.")

    provider = RepeatProvider(
        {
            PlanningBundle: planning_bundle(),
            AgentControlDecision: ENOUGH,
            ReasoningResult: slow,
        }
    )
    settings = Settings(knowledge_backend="memory", agent_deadline_seconds=0.5)

    answer = await ResearchWorkflow(provider=provider, settings=settings).run(
        QueryRequest(question=QUESTION)
    )

    assert any("бюджет времени" in item for item in answer.degradation_reasons)
    assert answer.query_plan is not None
    assert answer.findings, "таймаут обязан сохранить уже собранное доказательство"
    assert answer.tool_observations
    assert answer.graph.nodes
    assert [event.status for event in answer.trace][-1] == "failed"


@pytest.mark.asyncio
async def test_failed_node_degrades_with_explicit_reason() -> None:
    async def broken() -> ReasoningResult:
        raise RuntimeError("внутренняя ошибка узла")

    provider = RepeatProvider(
        {
            PlanningBundle: planning_bundle(),
            AgentControlDecision: ENOUGH,
            ReasoningResult: broken,
        }
    )

    answer = await ResearchWorkflow(provider=provider).run(QueryRequest(question=QUESTION))

    assert any("reasoner завершился ошибкой" in item for item in answer.degradation_reasons)
    assert [event.status for event in answer.trace][-1] == "failed"


@pytest.mark.asyncio
async def test_missing_provider_at_planning_surfaces_as_unavailable_not_empty_answer() -> None:
    """Недоступная модель до первого доказательства — 503, а не «успешный» пустой ответ.

    Терпеть здесь нечего: собрано ни одного факта, и «неполный ответ» был бы
    выдумкой, а не деградацией.
    """
    provider = RepeatProvider({AgentControlDecision: ENOUGH})

    with pytest.raises(ModelUnavailableError):
        await ResearchWorkflow(provider=provider).run(QueryRequest(question=QUESTION))


@pytest.mark.asyncio
async def test_late_model_failure_keeps_findings_and_returns_degraded_answer() -> None:
    """Отказ модели на позднем узле обязан сохранить находки, а не отдать 503.

    «Сервис занят» после потолка очереди провайдера приходил, когда retrieval уже
    сделал свою работу: ответ терял и доказательства, и trace. Дальше идёт тот же
    ``_degraded``-механизм, что и по дедлайну, а internals провайдера режутся.
    """
    async def refuse() -> ReasoningResult:
        raise ModelUnavailableError(
            "402 https://gigachat.internal/api/v1: b'{\"status\":402}', Headers(...)"
        )

    provider = RepeatProvider(
        {
            PlanningBundle: planning_bundle(),
            AgentControlDecision: ENOUGH,
            ReasoningResult: refuse,
        }
    )

    answer = await ResearchWorkflow(provider=provider).run(QueryRequest(question=QUESTION))

    assert answer.findings, "отказ модели не имеет права выбрасывать уже собранное"
    assert answer.tool_observations
    assert any("модель недоступна на узле reasoner" in item for item in answer.degradation_reasons)
    assert any(
        "тариф провайдера не оплачен (HTTP 402)" in item
        for item in answer.degradation_reasons
    )
    assert "Headers" not in " ".join(answer.degradation_reasons)
    assert [event.status for event in answer.trace][-1] == "failed"


@pytest.mark.asyncio
async def test_checkpointer_does_not_leak_state_between_runs() -> None:
    """Два запуска в одном диалоге не накапливают состояние предыдущего."""
    checkpointer = InMemorySaver()
    request = QueryRequest(question=QUESTION)

    async def run_once() -> Any:
        provider = ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Одинаковый."), OK)
        workflow = ResearchWorkflow(provider=provider, checkpointer=checkpointer)
        return await workflow.run(request)

    first = await run_once()
    second = await run_once()

    assert first.query_id != second.query_id
    assert len(second.trace) == len(first.trace)
    assert len(second.findings) == len(first.findings)
    assert len(second.tool_observations) == len(first.tool_observations)


@pytest.mark.asyncio
async def test_stream_reports_the_same_nodes_as_run() -> None:
    """JSON- и SSE-обработчики идут из одного стрима, а не из двух прогонов."""
    provider = ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Ответ из стрима."), OK)

    chunks = [
        name async for name, _ in ResearchWorkflow(provider=provider).stream(
            QueryRequest(question=QUESTION)
        )
    ]

    assert chunks == [
        "planning_agent",
        "tool_executor",
        "controller",
        "reasoner",
        "critic",
        "finalize",
    ]


@pytest.mark.asyncio
async def test_stream_degradation_keeps_the_evidence_run_would_keep() -> None:
    """Дедлайн в стриме отдаёт тот же минимально допустимый ответ, что и ``run``.

    Пока стрим шёл только по ``stream_mode="updates"``, деградация собиралась по
    пустому состоянию: аналитик в SSE получал ответ без доказательств там, где
    JSON-эндпоинт их сохранял.
    """
    async def slow() -> ReasoningResult:
        await asyncio.sleep(5)
        return reasoning("Этот ответ достигнут не был.")

    def workflow() -> ResearchWorkflow:
        settings = Settings(knowledge_backend="memory", agent_deadline_seconds=0.5)
        return ResearchWorkflow(
            provider=RepeatProvider(
                {
                    PlanningBundle: planning_bundle(),
                    AgentControlDecision: ENOUGH,
                    ReasoningResult: slow,
                }
            ),
            settings=settings,
        )

    request = QueryRequest(question=QUESTION)
    streamed = [update async for _, update in workflow().stream(request)]
    unary = await workflow().run(request)

    assert streamed, "стрим обязан завершиться ответом, а не молча оборваться"
    answer = streamed[-1]["answer"]
    assert {item.id for item in answer.findings} == {item.id for item in unary.findings}
    assert len(answer.tool_observations) == len(unary.tool_observations)
    assert len(answer.graph.nodes) == len(unary.graph.nodes)
    assert answer.findings and answer.tool_observations and answer.graph.nodes
    assert any("бюджет времени" in item for item in answer.degradation_reasons)
    assert [event.status for event in answer.trace][-1] == "failed"


@pytest.mark.asyncio
async def test_synthesis_is_not_paid_for_when_evidence_does_not_fit() -> None:
    """Доказательства не влезли — синтез не вызывается: три обращения за вымыслом.

    Раньше при перенасыщении бюджета FINDINGS отбрасывались целиком, а
    Reasoner/Critic/Improver всё равно шли и обязаны были выдумывать ``finding_ids``.
    Теперь деградация называется честно, а уже найденное доказательство сохраняется.
    """
    provider = RepeatProvider(
        {
            PlanningBundle: planning_bundle(),
            AgentControlDecision: ENOUGH,
        }
    )
    settings = Settings(knowledge_backend="memory", context_token_budget=500)

    answer = await ResearchWorkflow(provider=provider, settings=settings).run(
        QueryRequest(question=QUESTION)
    )

    assert "ReasoningResult" not in provider.calls, "синтез вслепую запрещён"
    notes = " ".join(answer.degradation_reasons)
    assert "не поместились в бюджет контекста" in notes
    assert "модель недоступна" not in notes, "причина — бюджет, а не отказ провайдера"
    assert answer.findings, "деградация обязана сохранить то, что retrieval уже нашёл"


@pytest.mark.asyncio
async def test_reasoner_prompt_keeps_at_least_one_finding_or_the_run_degrades() -> None:
    """Детерминированное сжатие доказательной базы не смеет быть молчаливым.

    Половина инварианта: если синтез вызван — в промпте есть хотя бы одно real
    доказательство и названо число отброшенного. Вторая половина покрыта предыдущим
    тестом: не влезает ни одного — обращения к модели не происходит вовсе.
    """
    provider = ScriptedProvider(
        planning_bundle(),
        ENOUGH,
        reasoning("Обратный осмос даёт 95–99% задержания солей."),
        OK,
    )
    settings = Settings(knowledge_backend="memory", context_token_budget=3400)

    answer = await ResearchWorkflow(provider=provider, settings=settings).run(
        QueryRequest(question=QUESTION)
    )

    if "ReasoningResult" in provider.calls:
        prompt = provider.prompt_for("ReasoningResult")
        assert "finding-ro" in prompt.split("FINDINGS")[1], "секция FINDINGS не вправе быть пустой"
        assert all("исключены секции FINDINGS" not in item for item in answer.degradation_reasons)
    else:
        assert any(
            "не поместились в бюджет контекста" in item for item in answer.degradation_reasons
        )


@pytest.mark.asyncio
async def test_decimal_spelling_of_a_grounded_number_is_not_rejected() -> None:
    """«95,0» и «95» — одно число. Пока сравнение было строковым, та же самая
    находка считалась неподтверждённой: guardrail сжигал ревизию впустую, а на
    исчерпанном бюджете ревизий ещё и ронял ложную причину в деградацию ответа."""
    provider = ScriptedProvider(
        planning_bundle(),
        ENOUGH,
        reasoning("Обратный осмос даёт 95,0–99,0% задержания при сухом остатке ≤1000,0 мг/л."),
        OK,
    )

    answer = await ResearchWorkflow(provider=provider).run(QueryRequest(question=QUESTION))

    assert [event.agent for event in answer.trace].count("improver") == 0
    assert not any("без поддержки" in item for item in answer.degradation_reasons)


@pytest.mark.asyncio
async def test_ungrounded_number_in_answer_is_rejected_by_the_guardrail() -> None:
    """Число без поддержки в наблюдениях находок — отклонённый черновик, а не Opinion."""
    provider = ScriptedProvider(
        planning_bundle(),
        ENOUGH,
        reasoning("Обессоливание даёт 97.5% задержания солей."),
        OK,
        reasoning("Обессоливание даёт 95–99% задержания солей."),
        OK,
    )

    answer = await ResearchWorkflow(provider=provider).run(QueryRequest(question=QUESTION))

    critics = [event for event in answer.trace if event.agent == "critic"]
    assert [event.status for event in critics] == ["revised", "completed"]
    assert "без поддержки" in critics[0].message
    assert [event.agent for event in answer.trace].count("improver") == 1
    assert answer.summary.startswith("Обессоливание даёт 95")
    assert not any("без поддержки" in item for item in answer.degradation_reasons)


@pytest.mark.asyncio
async def test_grounded_numbers_do_not_trigger_the_guardrail() -> None:
    """Позитивный кейс: числа из цитат и границ наблюдений не считаются вымыслом."""
    provider = ScriptedProvider(
        planning_bundle(),
        ENOUGH,
        reasoning("Обратный осмос даёт 95–99% задержания при сухом остатке ≤1000 мг/л."),
        OK,
    )

    answer = await ResearchWorkflow(provider=provider).run(QueryRequest(question=QUESTION))

    assert [event.agent for event in answer.trace].count("improver") == 0
    assert not any("без поддержки" in item for item in answer.degradation_reasons)


@pytest.mark.asyncio
async def test_ungrounded_number_survives_as_explicit_degradation() -> None:
    """Если ревизий не осталось — unsupported число уходит в degradation_reasons."""
    provider = ScriptedProvider(
        planning_bundle(),
        ENOUGH,
        reasoning("Обессоливание даёт 97.5% задержания солей."),
        OK,
    )
    settings = Settings(knowledge_backend="memory", agent_max_revisions=0)

    answer = await ResearchWorkflow(provider=provider, settings=settings).run(
        QueryRequest(question=QUESTION)
    )

    assert [event.agent for event in answer.trace].count("improver") == 0
    assert any("97.5" in item for item in answer.degradation_reasons)


@pytest.mark.asyncio
async def test_same_thread_carries_compact_history_into_next_run() -> None:
    """Ветка — не write-only: тот же thread_id передаёт след последних ходов."""
    checkpointer = InMemorySaver()
    request = QueryRequest(question=QUESTION)
    first = ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Первый вывод."), OK)
    await ResearchWorkflow(provider=first, checkpointer=checkpointer).run(request)

    second = ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Второй вывод."), OK)
    await ResearchWorkflow(provider=second, checkpointer=checkpointer).run(request)

    planning_prompt = second.prompt_for("PlanningBundle")
    assert "ИСТОРИЯ ВЕТКИ" in planning_prompt
    assert "Первый вывод." in planning_prompt, "вывод прошлого прогона обязан дойти до Planner"


@pytest.mark.asyncio
async def test_thread_keeps_only_the_compact_tail() -> None:
    """Сырые шаги прогона не остаются в чекпоинтере: ветка = сжатый след + права."""
    checkpointer = InMemorySaver()
    request = QueryRequest(question=QUESTION)
    workflow = ResearchWorkflow(
        provider=ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Вывод."), OK),
        checkpointer=checkpointer,
    )

    await workflow.run(request)

    config = {"configurable": {"thread_id": str(request.thread_id)}}
    values = (await workflow.graph.aget_state(config)).values
    assert [turn.summary for turn in values["research_history"]] == ["Вывод."]
    assert values["acl_scope"] == ""
    assert values["findings"] == [] and values["observations"] == []
    assert len(list(checkpointer.list(config))) == 1, "один компактный чекпоинт на ветку"


@pytest.mark.asyncio
async def test_acl_cut_hides_restricted_layers_from_the_answer() -> None:
    """Скрытый класс данных не проходит ни в тезисах, ни в графе, ни в слоях разведки."""
    provider = ScriptedProvider(
        planning_bundle(), ENOUGH, reasoning("Ответ с ограниченным доказательством."), OK
    )
    answer = await ResearchWorkflow(provider=provider).run(QueryRequest(question=QUESTION))
    restricted = [
        item.model_copy(update={"data_class": DataClass.RESTRICTED}) for item in answer.findings
    ]
    nodes = [
        node.model_copy(update={"data_class": DataClass.RESTRICTED}) for node in answer.graph.nodes
    ]
    graph = answer.graph.model_copy(update={"nodes": nodes})
    answer = answer.model_copy(update={"findings": restricted, "graph": graph})

    cut = AccessPolicyEngine().apply_acl(answer, VISIBLE)

    assert cut.findings == []
    assert cut.graph.nodes == []
    assert cut.graph.edges == []
    assert cut.conflicts == []
    assert cut.knowledge_gaps == []
    assert any("классу данных" in item for item in cut.degradation_reasons)


class BrokenStorageCheckpointer(InMemorySaver):
    """Чекпоинтер с недоступным хранилищем: ветку не открыть, но прогон возможен."""

    async def aget_state(self, config: Any, *, subgraphs: bool = False) -> Any:
        raise RuntimeError("checkpoint storage недоступна")


@pytest.mark.asyncio
async def test_broken_checkpointer_runs_without_history() -> None:
    """Сбой чекпоинтера на входе не роняет запрос: чекпоинтер опционален на обоих концах."""
    provider = ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Ответ без истории."), OK)
    workflow = ResearchWorkflow(provider=provider, checkpointer=BrokenStorageCheckpointer())

    answer = await workflow.run(QueryRequest(question=QUESTION))

    assert answer.summary == "Ответ без истории."
    failure_markers = (
        "превышен бюджет времени",
        "предел шагов",
        "завершился ошибкой",
        "не вернул ответ",
    )
    assert not any(
        marker in reason for reason in answer.degradation_reasons for marker in failure_markers
    ), "сбой чекпоинтера не должен выглядеть как сбой прогона"


@pytest.mark.asyncio
async def test_controller_skips_llm_when_rounds_exhausted() -> None:
    """Исход при исчерпанных раундах детерминирован: LLM не тратит дедлайн зря."""
    provider = RepeatProvider(
        {
            PlanningBundle: planning_bundle(),
            AgentControlDecision: HOLD_ON,
            ReasoningResult: reasoning("Собранное достаточно."),
            CritiqueResult: OK,
        }
    )
    settings = Settings(knowledge_backend="memory", agent_max_tool_rounds=1)

    answer = await ResearchWorkflow(provider=provider, settings=settings).run(
        QueryRequest(question=QUESTION)
    )

    assert "AgentControlDecision" not in provider.calls
    controller = [event for event in answer.trace if event.agent == "controller"]
    assert [event.status for event in controller] == ["revised"]
    assert answer.summary == "Собранное достаточно."


def _finding_with_observation(value: float, unit: str) -> Finding:
    return Finding(
        id="f-units",
        statement=f"Показатель {value:g} {unit}.",
        confidence=0.9,
        evidence=[EvidenceLocator(document_id=UUID(int=1), quote=f"{value:g} {unit}", page=1)],
        observations=[
            NumericObservation(
                property_name="показатель",
                operator="eq",
                value=value,
                normalized_value=value,
                unit=unit,
                normalized_unit=unit,
                raw_text=f"{value:g} {unit}",
            )
        ],
    )


def test_numbers_normalizes_thousands_separator() -> None:
    """«1 000 мг/л» — одно число 1000, а не «1» и «0»; короткие числа не склеиваются."""
    assert _numbers("1 000 мг/л") == {"1000"}
    assert _numbers("1 000 000 т") == {"1000000"}
    assert _numbers("70,5 %") == {"70.5"}
    assert _numbers("5 30") == {"5", "30"}


def test_unit_conflicts_flag_scale_mismatch_and_only_it() -> None:
    """«70 ГПа» против доказательства в МПа — пометка; совпадающая шкала — тишина."""
    evidence_finding = _finding_with_observation(70.0, "МПа")
    mismatch = ReasoningResult(
        summary="Прочность 70 ГПа.",
        finding_ids=["f-units"],
        conflicts=[],
        knowledge_gaps=[],
        recommendations=[],
    )
    assert _unit_conflicts(mismatch, [evidence_finding]) == [
        "число 70: ГПа в ответе против МПа в доказательстве"
    ]

    match = mismatch.model_copy(update={"summary": "Прочность 70 МПа."})
    assert _unit_conflicts(match, [evidence_finding]) == []


def _answer_with(summary: str) -> ReasoningResult:
    return ReasoningResult(
        summary=summary,
        finding_ids=["f-units"],
        conflicts=[],
        knowledge_gaps=[],
        recommendations=[],
    )


def test_unit_conflicts_names_unmatched_units_instead_of_ignoring_them() -> None:
    """Единица вне словаря — не тишина, но и не штраф: пометка отдельно от шкалы.

    «70 баррелей» против «70 т/м³» аналитик обязан увидеть, однако масштаб
    неизвестной единицы ошибку шкалы не доказывает, поэтому такие пары считает
    ``_unit_unmatched`` и не засчитывает в уверенность. Русское слово в падеже
    («95 процентов» против «95 %») — то же написание, а не расхождение.
    """
    barrels = _unit_unmatched(_answer_with("Плотность 70 баррелей."), [
        _finding_with_observation(70.0, "т/м³")
    ])
    assert barrels == [
        "число 70: баррелей в ответе против т/м³ в доказательстве — единицы не сопоставлены"
    ]
    assert _unit_conflicts(_answer_with("Плотность 70 баррелей."), [
        _finding_with_observation(70.0, "т/м³")
    ]) == []

    # Слово вместо символа и падежное окончание — не разные единицы.
    for spelled in ("Задержание 95 процентов.", "Задержание 95 процентом."):
        assert _unit_conflicts(_answer_with(spelled), [_finding_with_observation(95.0, "%")]) == []
        assert _unit_unmatched(_answer_with(spelled), [_finding_with_observation(95.0, "%")]) == []

    # Разное написание одной единицы — корпус англоязычный, ответ русскоязычный.
    latin_spelling = _finding_with_observation(70.0, "mg/L")
    assert _unit_conflicts(_answer_with("Доза 70 мг/л."), [latin_spelling]) == []
    assert _unit_unmatched(_answer_with("Доза 70 мг/л."), [latin_spelling]) == []
    assert _unit_conflicts(_answer_with("Задержание 70 percent."), [
        _finding_with_observation(70.0, "%")
    ]) == []
    # Латинское написание не прячет доказуемое расхождение масштабов.
    assert _unit_conflicts(_answer_with("Concentration 70 g/L."), [latin_spelling]) == [
        "число 70: g/L в ответе против mg/L в доказательстве"
    ]

    # Число доказательства вовсе без единицы остаётся территорией «число без единиц».
    plain = Finding(
        id="f-plain",
        statement="Порог 5.",
        confidence=0.8,
        evidence=[EvidenceLocator(document_id=UUID(int=1), quote="порог 5", page=1)],
    )
    assert _unit_conflicts(_answer_with("Порог 5 баррелей."), [plain]) == []
    assert _unit_unmatched(_answer_with("Порог 5 баррелей."), [plain]) == []


def test_language_check_compares_script_of_the_summary() -> None:
    """Проверка языка детерминирована: большинство букв — алфавитом запроса."""
    assert _language_mismatch("Reverse osmosis rejects 95% of dissolved salts.", "ru")
    assert _language_mismatch("Обратный осмос даёт задержание растворённых солей.", "en")
    assert not _language_mismatch("Обратный осмос даёт 95% солей.", "ru")
    assert not _language_mismatch("Membrane rejects 95% of salts.", "en")
    assert not _language_mismatch("95–99%", "ru"), "без букв сравнивать нечего"


@pytest.mark.asyncio
async def test_acl_scope_change_discards_thread_history() -> None:
    """Права доступа изменились — прошлый вывод ветки не наследуется.

    Ветка живёт по thread_id, а не по правам: если между прогонами аналитику
    расширили или сузили классы данных, история прошлых ходов (собранных под
    другой подписью ACL) в контекст нового прогона попадать не должна.
    """
    checkpointer = InMemorySaver()
    request = QueryRequest(question=QUESTION)

    async def run_once(allowed: set[DataClass] | None) -> Any:
        provider = ScriptedProvider(planning_bundle(), ENOUGH, reasoning(f"Прогон {allowed}."), OK)
        workflow = ResearchWorkflow(provider=provider, checkpointer=checkpointer)
        answer = await workflow.run(request, allowed_data_classes=allowed)
        snapshot = await workflow.graph.aget_state(workflow._config(request))
        return answer, list(snapshot.values.get("research_history", []))

    _, first_history = await run_once(None)
    _, same_scope_history = await run_once(None)
    answer, changed_scope_history = await run_once(VISIBLE)

    assert len(first_history) == 1
    # Тот же подпись прав: ветка наследует прошлый ход.
    assert len(same_scope_history) == 2
    # Подпись прав сменилась: остался только ход нового прогона.
    assert len(changed_scope_history) == 1
    assert changed_scope_history[0].summary == answer.summary


@pytest.mark.asyncio
async def test_unapproved_answer_is_marked_when_revisions_are_exhausted() -> None:
    """На исчерпанном бюджете ревизий finalize получает отклонённый черновик.

    Без отметки аналитик не отличил бы ответ, который Critic забраковал, от
    одобренного: незакрытые замечания обязаны дойти до degradation_reasons, а сам
    ответ при этом остаётся показанным.
    """
    provider = ScriptedProvider(
        planning_bundle(),
        ENOUGH,
        reasoning("Черновик без условий применимости."),
        rejected("Не названа граница по сухому остатку."),
    )
    settings = Settings(knowledge_backend="memory", agent_max_revisions=0)

    answer = await ResearchWorkflow(provider=provider, settings=settings).run(
        QueryRequest(question=QUESTION)
    )

    assert [event.agent for event in answer.trace].count("improver") == 0
    assert answer.summary == "Черновик без условий применимости."
    notes = [item for item in answer.degradation_reasons if "без одобрения Critic'а" in item]
    assert len(notes) == 1
    assert "Не названа граница по сухому остатку." in notes[0]
    assert "даже после ревизии" not in notes[0]


@pytest.mark.asyncio
async def test_critic_note_separates_revised_answer_from_approved_one() -> None:
    """Ревизия, после которой Critic всё ещё не согласен, помечена; одобрение — нет."""
    still_rejected = await ResearchWorkflow(
        provider=ScriptedProvider(
            planning_bundle(),
            ENOUGH,
            reasoning("Первый черновик."),
            rejected("Не хватает данных по пилоту."),
            reasoning("Второй черновик."),
            rejected("Данных по пилоту всё ещё не хватает."),
        ),
        settings=Settings(knowledge_backend="memory", agent_max_revisions=1),
    ).run(QueryRequest(question=QUESTION))

    notes = [
        item for item in still_rejected.degradation_reasons if "без одобрения Critic'а" in item
    ]
    assert len(notes) == 1 and "даже после ревизии" in notes[0]
    assert "Данных по пилоту всё ещё не хватает." in notes[0]
    assert still_rejected.summary == "Второй черновик."

    approved = await ResearchWorkflow(
        provider=ScriptedProvider(
            planning_bundle(),
            ENOUGH,
            reasoning("Черновик без нормализации условий."),
            rejected("Условия применимости не согласованы."),
            reasoning("Ответ после ревизии: 95–99% задержания солей."),
            OK,
        )
    ).run(QueryRequest(question=QUESTION))

    assert not any("без одобрения Critic'а" in item for item in approved.degradation_reasons)


@pytest.mark.asyncio
async def test_summary_in_the_wrong_script_is_marked_as_degradation() -> None:
    """Язык запроса — факт входящего вопроса: summary не тем алфавитом видно аналитику."""
    def provider() -> ScriptedProvider:
        return ScriptedProvider(
            planning_bundle(),
            ENOUGH,
            reasoning("Reverse osmosis rejects 95–99% of dissolved salts."),
            OK,
        )

    russian = await ResearchWorkflow(provider=provider()).run(QueryRequest(question=QUESTION))
    assert any("Язык ответа не совпадает" in item for item in russian.degradation_reasons)

    english = await ResearchWorkflow(provider=provider()).run(
        QueryRequest(question=QUESTION, language="en")
    )
    assert not any("Язык ответа не совпадает" in item for item in english.degradation_reasons)


@pytest.mark.asyncio
async def test_confidence_pays_for_guardrail_facts_instead_of_self_report() -> None:
    """Уверенность ответа отражает проваленные guardrail-проверки finalize.

    Среднее ``finding.confidence`` — самооценка модели; без штрафов за детерминированно
    обнаруженные нестыковки и без потолка при ответе по всему пулу она не отличалась бы
    от проверяемого вывода.
    """
    clean = await ResearchWorkflow(
        provider=ScriptedProvider(
            planning_bundle(),
            ENOUGH,
            reasoning("Обратный осмос даёт 95–99% задержания солей."),
            OK,
        )
    ).run(QueryRequest(question=QUESTION))
    assert clean.confidence == pytest.approx(0.81), "среднее 0,92 и 0,70 без штрафов"

    no_revisions = Settings(knowledge_backend="memory", agent_max_revisions=0)
    numbers_hit = await ResearchWorkflow(
        provider=ScriptedProvider(
            planning_bundle(),
            ENOUGH,
            reasoning("Обессоливание даёт 97.5% задержания солей."),
            OK,
        ),
        settings=no_revisions,
    ).run(QueryRequest(question=QUESTION))
    assert any("без поддержки" in item for item in numbers_hit.degradation_reasons)
    assert numbers_hit.confidence == pytest.approx(0.81 * _CONFIDENCE_PENALTY, abs=0.001)

    wrong_citations = await ResearchWorkflow(
        provider=ScriptedProvider(
            planning_bundle(),
            ENOUGH,
            reasoning("Обратный осмос даёт 95–99% задержания солей.").model_copy(
                update={"finding_ids": ["finding-вне-пула"]}
            ),
            rejected("Ответ ссылается на неизвестные finding IDs."),
        ),
        settings=no_revisions,
    ).run(QueryRequest(question=QUESTION))
    assert any("finding IDs" in item for item in wrong_citations.degradation_reasons)
    assert any("без одобрения Critic'а" in item for item in wrong_citations.degradation_reasons)
    assert wrong_citations.confidence <= _UNTRACED_CONFIDENCE_CAP, (
        "ответ по всему пулу без точечной трассировки не может быть уверенным"
    )


@pytest.mark.asyncio
async def test_deadline_degradation_is_counted_with_a_bounded_reason_code() -> None:
    """Деградация по дедлайну обязана быть видна в метриках кодом, а не только текстом.

    Свободная строка ``degradation_reasons`` в счётчик не идёт: она меняется от
    узла к узлу и разложила бы cardinality метки.
    """
    async def slow() -> ReasoningResult:
        await asyncio.sleep(5)
        return reasoning("Этот ответ достигнут не был.")

    registry = AgentMetricsRegistry()
    provider = RepeatProvider(
        {
            PlanningBundle: planning_bundle(),
            AgentControlDecision: ENOUGH,
            ReasoningResult: slow,
        }
    )
    settings = Settings(knowledge_backend="memory", agent_deadline_seconds=0.4)
    workflow = ResearchWorkflow(provider=provider, settings=settings, metrics=registry)

    answer = await workflow.run(QueryRequest(question=QUESTION))

    assert any("бюджет времени" in item for item in answer.degradation_reasons)
    assert registry.snapshot().degradations_by_reason.get("timeout") == 1


# ── D1: доля бюджета узла ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slow_planning_node_cannot_burn_the_whole_deadline() -> None:
    """Один узел не выжигает бюджет прогона: он ограничен своей долей остатка.

    Провайдер делит весь ``agent_deadline_seconds`` на попытки одного обращения, и
    зависший planning-узел съедал бы 300 с, не оставив времени остальным. Снаружи
    узел ограничен ``asyncio.timeout`` — деградация наступает раньше дедлайна и
    называет узел, а не «время вышло».
    """
    async def slow() -> PlanningBundle:
        await asyncio.sleep(30)
        return planning_bundle()

    registry = AgentMetricsRegistry()
    provider = RepeatProvider({PlanningBundle: slow})
    settings = Settings(knowledge_backend="memory", agent_deadline_seconds=4.0)
    workflow = ResearchWorkflow(provider=provider, settings=settings, metrics=registry)

    started = monotonic()
    answer = await workflow.run(QueryRequest(question=QUESTION))
    elapsed = monotonic() - started

    assert 2.5 <= elapsed < 3.9, f"узел должен быть обрезан своей долей, а не дедлайном: {elapsed}"
    notes = " ".join(answer.degradation_reasons)
    assert "превысил свою долю бюджета" in notes and "planning_agent" in notes
    assert "превышен бюджет времени исследования" not in notes
    assert registry.snapshot().degradations_by_reason.get("node_budget:planning_agent") == 1


# ── D3: приём, согласованный со способностью модели ──────────────────────────


def _llm_settings(**overrides: object) -> Settings:
    material = {
        "_env_file": None,
        "knowledge_backend": "memory",
        "accounts_backend": "memory",
        "gigachat_api_key": "test-key",
        "gigachat_max_concurrent": 1,
        "agent_max_concurrent_runs": 4,
        "agent_deadline_seconds": 300.0,
    }
    material.update(overrides)
    return Settings(**material)  # type: ignore[arg-type]


def test_admission_limit_follows_model_throughput() -> None:
    """Потолок приёма производен от слотов модели, а не от круглого числа в env.

    Четыре принятых прогона при одном LLM-слоте — это ~32 ожидающих обращения:
    очередь съедала дедлайн каждого из них.
    """
    assert agent_run_limit(_llm_settings()) == 2
    assert agent_run_limit(_llm_settings(gigachat_max_concurrent=8)) == 4
    # Явный env перекрывает эвристику — для нескольких воркеров и осознанной
    # переподписки.
    assert agent_run_limit(_llm_settings(agent_admission_limit=6)) == 6
    # Без настроенной модели сравнивать ёмкость не с чем.
    assert agent_run_limit(_llm_settings(gigachat_api_key="")) == 4


def test_admission_refuses_instantly_beyond_the_derived_limit() -> None:
    """Смысл 429 не меняется: отказ сразу, без очереди внутри дедлайна."""
    settings = _llm_settings()
    admission = AgentRunAdmission(
        limit=agent_run_limit(settings), deadline_seconds=settings.agent_deadline_seconds
    )
    first = admission.acquire()
    second = admission.acquire()
    assert second.token is not None
    with pytest.raises(AdmissionRefusedError) as refused:
        admission.acquire()
    assert refused.value.retry_after >= MIN_RETRY_AFTER_SECONDS
    first.release()
    assert admission.acquire().token is not None


def test_startup_warns_when_ceiling_outruns_the_model(caplog: pytest.LogCaptureFixture) -> None:
    """Конфиг, обещающий больше, чем вывезет модель, виден на старте."""
    with caplog.at_level(logging.WARNING, logger="scientific_tangle.config"):
        _llm_settings(agent_max_concurrent_runs=8)
    assert "превышает пропускную способность модели" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="scientific_tangle.config"):
        _llm_settings(agent_max_concurrent_runs=8, agent_admission_limit=8)
    assert "превышает пропускную способность модели" not in caplog.text


# ── D4: sanitized root cause и потолок candidate policy ──────────────────────


class _LeakyKnowledge:
    """Драйверы падают с connection string в тексте — он не вправе уехать в промпт."""

    def __init__(self) -> None:
        self._real = InMemoryKnowledgeBase()
        self.calls: list[str] = []

    def retrieve(
        self, plan: Any, retrieval_plan: Any, allowed: Any, *, abort: Any = None
    ) -> Any:
        self.calls.append(retrieval_plan.lexical_query)
        raise RuntimeError(
            "neo4j unavailable at bolt://neo4j:7687, dsn postgresql://user:secret@postgres:5432"
        )


@pytest.mark.asyncio
async def test_tool_error_hint_reaches_prompts_sanitized() -> None:
    """``root_cause_hint`` уходит planner/controller: только нейтральное название сбоя."""
    knowledge = _LeakyKnowledge()
    executor = ResearchToolExecutor(knowledge)
    bundle = planning_bundle()

    result = await executor.execute(bundle.action_plan, bundle.query_plan, None)

    hints = " ".join(observation.root_cause_hint or "" for observation in result.observations)
    assert knowledge.calls, "действие должно была исполнено (отказа по бюджету нет)"
    for fragment in ("bolt://", "postgresql://", "user:secret", "7687"):
        assert fragment not in hints
    assert hints.startswith("retrieval:")
    assert len(hints) <= 300


@pytest.mark.asyncio
async def test_candidate_policy_is_capped_and_says_so() -> None:
    """Активная политика не смеет молча вытеснять доказательства из промпта."""
    policy = "Цитируй лист источника. " * 400
    provider = ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Вывод."), OK)
    settings = Settings(knowledge_backend="memory", policy_max_tokens=64)

    answer = await ResearchWorkflow(
        provider=provider, settings=settings, extra_policy=policy
    ).run(QueryRequest(question=QUESTION))

    system = provider.prompts["PlanningBundle"][0][0]
    assert "CANDIDATE POLICY" in system
    assert system.count("Цитируй лист") < policy.count("Цитируй лист"), "политика усечена"
    notes = [item for item in answer.degradation_reasons if "CANDIDATE POLICY усечена" in item]
    assert len(notes) == 1, "усечение отмечается ровно один раз на прогон"

    short = await ResearchWorkflow(
        provider=ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Вывод."), OK),
        settings=settings,
        extra_policy="Цитируй лист источника.",
    ).run(QueryRequest(question=QUESTION))
    assert not any("CANDIDATE POLICY усечена" in item for item in short.degradation_reasons)


# ── D5/D6: редьюсеры и битые записи состояния ────────────────────────────────


class _JunkyKnowledge:
    """Отдаёт граф с записью неверной формы — как чекпоинт после смены схемы."""

    def __init__(self) -> None:
        self._real = InMemoryKnowledgeBase()

    def retrieve(
        self, plan: Any, retrieval_plan: Any, allowed: Any, *, abort: Any = None
    ) -> Any:
        context = self._real.retrieve(plan, retrieval_plan, allowed, abort=abort)
        junk = {"id": ["не-строка"], "type": "мусор"}
        graph = context.graph.model_copy(update={"nodes": [*context.graph.nodes, junk]})
        return replace(context, graph=graph)


@pytest.mark.asyncio
async def test_invalid_graph_records_degrade_instead_of_500() -> None:
    """Битая запись хранилища не смеет валить прогон сырым исключением.

    Её ждут и граница tools, и редьюсер канала ``graph`` (он вызывается движком
    LangGraph вне ``_instrument``): в обоих местах запись отбрасывается, а факт
    уходит аналитику в ``degradation_reasons``.
    """
    provider = ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Вывод."), OK)
    workflow = ResearchWorkflow(knowledge=_JunkyKnowledge(), provider=provider)

    answer = await workflow.run(QueryRequest(question=QUESTION))

    assert answer.summary == "Вывод."
    assert any("не прошли проверку" in item for item in answer.degradation_reasons)
    assert answer.graph.nodes, "битая запись не отменяет валидную часть графа"


def test_graph_reducer_survives_checkpoint_records_of_wrong_shape() -> None:
    """``merge_graphs`` с невалидным левым значением деградирует, а не бросает."""
    from scientific_tangle.agents.workflow import EMPTY_GRAPH, merge_graphs

    junky = GraphSnapshot.model_construct(
        nodes=[{"id": ["не-строка"]}], edges=["не-узел"], communities=[7, "сообщество"]
    )

    merged = merge_graphs(junky, EMPTY_GRAPH)

    assert merged.nodes == [] and merged.edges == []
    assert merged.communities == ["сообщество"]


def test_state_validation_error_is_a_degradation_not_a_crash() -> None:
    """``ValidationError`` из каналов состояния входит в список деградируемого."""
    with pytest.raises(ValidationError) as caught:
        GraphSnapshot(nodes=[1])  # type: ignore[list-item]
    error = caught.value
    assert isinstance(error, _DEGRADABLE)
    assert _degradation_code(error) == "state_schema"
    human, technical = _describe_failure(error)
    assert "проверку схемы" in technical
    assert str(error)[:40] not in human, "сырое сообщение не уходит человеку"
    assert str(error)[:40] not in technical, "сырое сообщение не уходит наружу"


def test_model_failure_reaches_the_analyst_without_internals() -> None:
    """Человек читает, что проверка не полна, а не имя узла и текст парсера.

    ``knowledge_gaps`` печатается в интерфейсе, а строка собиралась из
    ``ModelFailureError(node, detail)`` целиком: «модель недоступна на узле
    controller (GigaChat: structured output retry исчерпан
    (AgentControlDecision): ответ не является JSON…)». Тест краснеет, если в
    человекую половину вернётся имя узла, имя схемы или сообщение json, и если
    техническая половина потеряет узел — по нему ищут прогон в журнале.
    """
    error = ModelFailureError(
        "controller",
        "GigaChat: structured output retry исчерпан (AgentControlDecision): "
        "ответ не является JSON (Expecting property name enclosed in double quotes)",
    )

    human, technical = _describe_failure(error)

    for leak in (
        "controller",
        "GigaChat",
        "structured",
        "AgentControlDecision",
        "JSON",
        "Expecting",
    ):
        assert leak not in human, f"внутреннее имя ушло человеку: {leak}"
    assert "модель не ответила" in human
    assert "controller" in technical, "техническая половина обязана сохранить узел"


# ── D7: чекпоинтер вне дедлайна ──────────────────────────────────────────────


class _HangingCheckpointer(InMemorySaver):
    """Имитация боя Postgres: выбранный метод не отвечает первые ``calls`` вызовов.

    ``graph.aget_state`` ходит в ``aget_tuple`` чекпоинтера — вешать надо именно его,
    и только открытие ветки (первый вызов): внутренние супершаги графа ограничены
    дедлайном прогона и к этой проверке отношения не имеют. ``adelete_thread``
    засчитывается, чтобы повесить запись ветки (второй вызов).
    """

    def __init__(self, hang: set[str], *, calls: int = 1) -> None:
        super().__init__()
        self._hang = hang
        self._calls = calls
        self.tuples = 0
        self.deletes = 0

    async def aget_tuple(self, config: Any) -> Any:
        self.tuples += 1
        if "aget_state" in self._hang and self.tuples <= self._calls:
            await asyncio.sleep(3600)
        return await super().aget_tuple(config)

    async def adelete_thread(self, thread_id: str) -> None:
        self.deletes += 1
        if "adelete_thread" in self._hang and self.deletes > self._calls:
            await asyncio.sleep(3600)
        await super().adelete_thread(thread_id)


@pytest.mark.asyncio
async def test_hung_checkpointer_on_open_degrades_without_hanging() -> None:
    """Открытие ветки ограничено: недоступный чекпоинтер не вешает запрос.

    Оно лежит до ``asyncio.timeout`` прогона, поэтому без собственной границы
    бое БД означал зависший HTTP-запрос вообще без деградации.
    """
    provider = ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Ответ есть."), OK)
    settings = Settings(knowledge_backend="memory", agent_deadline_seconds=8.0)
    workflow = ResearchWorkflow(
        provider=provider, settings=settings, checkpointer=_HangingCheckpointer({"aget_state"})
    )

    started = monotonic()
    answer = await workflow.run(QueryRequest(question=QUESTION))
    elapsed = monotonic() - started

    assert elapsed < 7.5, f"ожидание БД обязано быть ограничено: {elapsed}"
    assert answer.summary == "Ответ есть."
    assert any("не открыта за" in item for item in answer.degradation_reasons)


@pytest.mark.asyncio
async def test_hung_checkpointer_on_commit_is_reported() -> None:
    """Запись следа ветки тоже под таймаутом: ответ собран — вешать запрос нельзя."""
    provider = ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Ответ собран."), OK)
    settings = Settings(knowledge_backend="memory", agent_deadline_seconds=8.0)
    workflow = ResearchWorkflow(
        provider=provider,
        settings=settings,
        checkpointer=_HangingCheckpointer({"adelete_thread"}),
    )

    started = monotonic()
    answer = await workflow.run(QueryRequest(question=QUESTION))
    elapsed = monotonic() - started

    assert elapsed < 7.5
    assert answer.summary == "Ответ собран."
    assert any("не принят за" in item for item in answer.degradation_reasons)


# ── D8: кооперативная отмена retrieval ───────────────────────────────────────


@pytest.mark.asyncio
async def test_expired_run_does_not_dispatch_new_retrieval() -> None:
    """Истёкший бюджет не запускает новую работу: ``to_thread`` без токена не отзвать.

    Действие, которое уже не прочитает ни один узел, только занимает worker-поток и
    слот хранилища — поэтому проверка идёт до диспетчеризации.
    """
    knowledge = _CountingKnowledge()
    executor = ResearchToolExecutor(knowledge)
    bundle = planning_bundle()

    expired = await executor.execute(
        bundle.action_plan,
        bundle.query_plan,
        None,
        budget=RunBudget(deadline_at=monotonic() - 1),
    )
    assert knowledge.calls == [], "истёкший прогон не имеет права трогать хранилище"
    assert expired.observations, "план из двух действий обязан отдать наблюдения"
    assert all(observation.status == "error" for observation in expired.observations)
    assert any(
        "исчерпанного бюджета времени прогона" in item for item in expired.degradation_reasons
    )

    fresh = await executor.execute(
        bundle.action_plan,
        bundle.query_plan,
        None,
        budget=RunBudget(deadline_at=monotonic() + 30),
    )
    assert fresh.findings, "с остатком бюджета действия исполняются как раньше"


class _CountingKnowledge:
    """Счётчик обращений к retrieval: без него нельзя отличить «не начали» от «упало»."""

    def __init__(self) -> None:
        self._real = InMemoryKnowledgeBase()
        self.calls: list[str] = []

    def retrieve(
        self, plan: Any, retrieval_plan: Any, allowed: Any, *, abort: Any = None
    ) -> Any:
        self.calls.append(retrieval_plan.lexical_query)
        return self._real.retrieve(plan, retrieval_plan, allowed, abort=abort)
