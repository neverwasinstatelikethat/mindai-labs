"""Ненадёжные ветки рабочего процесса: ревизии, бюджеты, чекпоинтер, стрим.

Регрессионные тесты на находки H-3 (состояние просачивалось между запусками на
одном thread), H-4 (несколько слоёв retry перемножались) и M-серию (не были
проверены error-ветки, цикл critic→improver, лимит tool-раундов). Сюда же —
деградация SSE, перенасыщение бюджета контекста и числовой guardrail.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from scientific_tangle.agents.workflow import ResearchWorkflow, _numbers, _unit_conflicts
from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import (
    AgentControlDecision,
    CritiqueResult,
    Finding,
    PlanningBundle,
    QueryRequest,
    ReasoningResult,
)
from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import EvidenceLocator, NumericObservation
from scientific_tangle.services.governance import AccessPolicyEngine
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
async def test_missing_provider_surfaces_as_unavailable_not_empty_answer() -> None:
    """Недоступная модель — 503-исключение, а не «успешный» пустой ответ."""
    provider = RepeatProvider({PlanningBundle: planning_bundle()})

    with pytest.raises(ModelUnavailableError):
        await ResearchWorkflow(provider=provider).run(QueryRequest(question=QUESTION))


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
async def test_tight_budget_drops_evidence_but_keeps_the_draft_and_says_so() -> None:
    """Перенасыщение бюджета не смеет быть молчаливым и не смеет съедать черновик."""
    provider = ScriptedProvider(planning_bundle(), ENOUGH, reasoning("Черновик ответа."), OK)
    settings = Settings(knowledge_backend="memory", context_token_budget=500)

    answer = await ResearchWorkflow(provider=provider, settings=settings).run(
        QueryRequest(question=QUESTION)
    )

    critic_prompt = provider.prompt_for("CritiqueResult")
    assert "DRAFT" in critic_prompt and "Черновик ответа." in critic_prompt
    critic_notes = [item for item in answer.degradation_reasons if "Узел Critic" in item]
    assert critic_notes and "FINDINGS" in critic_notes[0], "потеря контекста обязана быть видна"


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
