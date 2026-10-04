"""Толерантность structured-output форм: «не вернула» не равно «сломалось».

Живой лог холодного `GET /api/v1/demo` на HEAD ee52e96: GigaChat отдаёт
`"conflicts": "противоречий нет"`, `"recommendations": "-"` и `"[]"` как строку,
требовательный список давал ValidationError → schema-repair → 503 на потолке
попыток — ответ терялся целиком. Проверяется приведение на уровне схемы и то,
что пустая секция («не нашла») отличается от отсутствующей («не вернула»).
"""

import pytest
from pydantic import ValidationError

from scientific_tangle.domain.contracts import (
    AgentActionPlan,
    AgentControlDecision,
    ComparisonRequest,
    CritiqueResult,
    DocumentRequest,
    EvolutionDraft,
    ExtractedClaim,
    ExtractedEntity,
    ExtractionResult,
    FeedbackRequest,
    PlanningBundle,
    QueryRequest,
    ReasoningResult,
    ToolAction,
)

# Дословный ответ из журнала контейнера: три секции строкой, списков нет ни в одной.
LIVE_DEMO_ANSWER = {
    "summary": "Плотность руд РЕАКОМ-М — 1,8 г/см³.",
    "finding_ids": [],
    "conflicts": "противоречий нет",
    "knowledge_gaps": "нет",
    "recommendations": "-",
}


def test_live_demo_answer_with_string_sections_validates() -> None:
    """Тот самый ответ обязан проходить схему с первого раза, а не чиниться."""
    result = ReasoningResult.model_validate(LIVE_DEMO_ANSWER)

    assert result.conflicts == ["противоречий нет"]
    assert result.knowledge_gaps == ["нет"]
    assert result.recommendations == ["-"]
    assert result.finding_ids == []


def test_json_string_section_becomes_the_parsed_list() -> None:
    """`"[]"` и `"[\"a\"]"` — список, завернутый моделью в строку: разбираем его."""
    parsed = ReasoningResult.model_validate({"summary": "s", "conflicts": '["a", "b"]'})
    empty = ReasoningResult.model_validate({"summary": "s", "conflicts": "[]"})
    single = CritiqueResult.model_validate({"approved": True, "issues": '["нет цитат"]'})

    assert parsed.conflicts == ["a", "b"]
    assert empty.conflicts == []
    assert single.issues == ["нет цитат"]


def test_null_and_blank_sections_are_empty_lists() -> None:
    """None и пробельная строка — «ничего нет»: пустой список, а не отказ формы."""
    result = ReasoningResult.model_validate(
        {"summary": "s", "conflicts": None, "knowledge_gaps": "   ", "recommendations": ""}
    )

    assert (result.conflicts, result.knowledge_gaps, result.recommendations) == ([], [], [])


def test_missing_section_is_distinguishable_from_empty_one() -> None:
    """Ключевое различие для `degradation_reasons`: секция не пришла или пуста.

    Пустой список и приведённая строка — ответ модели, поэтому ключ считается
    присутствующим; молча дополненные отсутствующие ключи остались бы без следа,
    если бы приведение дописывало секции в словарь.
    """
    silent = ReasoningResult.model_validate({"summary": "s"})
    answered = ReasoningResult.model_validate({"summary": "s", "conflicts": []})

    assert "conflicts" in silent.absent_list_sections()
    assert "conflicts" not in answered.absent_list_sections()
    assert answered.conflicts == []
    assert silent.recommendations == answered.recommendations == []


def test_scalar_and_object_items_become_readable_texts() -> None:
    """Элементы `list[str]` числом или объектом не имеют права ронять прогон."""
    result = ReasoningResult.model_validate(
        {
            "summary": "s",
            "conflicts": [12, None, {"text": "источники расходятся"}, ["мусор"]],
            "knowledge_gaps": {"statement": "нет данных по 2024"},
        }
    )

    assert result.conflicts == ["12", "источники расходятся"]
    assert result.knowledge_gaps == ["нет данных по 2024"]


def test_nested_and_composite_forms_are_coerced_too() -> None:
    """Приведение идёт внутрь вложенных форм, включая чужие классы (QueryPlan).

    `PlanningBundle` — один вызов модели на весь план: битый `entity_mentions`
    внутри `query_plan` ронял бы план целиком, хотя толерантность нужна у
    носителя, а не только у формы верхнего уровня.
    """
    bundle = PlanningBundle.model_validate(
        {
            "intent": {"primary": "fact_search", "secondary": "нет", "entities": None},
            "query_plan": {
                "question": "Какое извлечение меди даёт РЕАКОМ-М?",
                "entity_mentions": None,
                "countries": '["Перу"]',
                "numeric_filters": [],
            },
            "action_plan": {
                "rationale": "число берут из первоисточника",
                "actions": '[{"tool": "hybrid_search", "query": "Fe", "purpose": "найти число"}]',
                "completion_criteria": None,
            },
        }
    )

    assert bundle.intent is not None
    assert bundle.intent.secondary == ["нет"]
    assert bundle.intent.entities == []
    assert bundle.query_plan.entity_mentions == []
    assert bundle.query_plan.countries == ["Перу"]
    assert bundle.action_plan.actions[0].query == "Fe"
    assert bundle.action_plan.completion_criteria == []
    assert bundle.absent_list_sections() == set()


def test_client_facing_contracts_stay_strict() -> None:
    """Толерантность — только у форм модели: вход клиента обязан давать 422.

    `QueryRequest`/`DocumentRequest`/`ComparisonRequest`/`FeedbackRequest` не
    наследуют приведение: строка вместо списка или пустой запрос снаружи — это
    ошибка вызывающего, а не недоработка ответа модели.
    """
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"question": ""})
    with pytest.raises(ValidationError):
        QueryRequest.model_validate({"question": None})
    with pytest.raises(ValidationError):
        DocumentRequest.model_validate({"title": "Т", "text": "коротко"})
    with pytest.raises(ValidationError):
        ComparisonRequest.model_validate({"question": "сравни", "entities": []})
    with pytest.raises(ValidationError):
        ComparisonRequest.model_validate({"question": "сравни", "entities": "РЕАКОМ-М"})
    with pytest.raises(ValidationError):
        FeedbackRequest.model_validate({"query_id": "00000000-0000-0000-0000-000000000000"})


def test_evolution_draft_without_impact_is_not_rejected() -> None:
    """`impact` — полезное дополнение, а не суть предложения: без него оно валидно."""
    draft = EvolutionDraft.model_validate(
        {"kind": "prompt", "title": "Точнее лимиты", "change": "просить страницу в цитате"}
    )

    assert draft.impact == []
    assert "impact" in draft.absent_list_sections()
    assert EvolutionDraft.model_validate(
        {"kind": "prompt", "title": "Т", "change": "С", "impact": "влияет на цитируемость"}
    ).impact == ["влияет на цитируемость"]


def test_control_decision_synonyms_and_blank_rationale_survive() -> None:
    """Слово модели про управление сведётся к схеме, а пустое обоснование не отказ."""
    continue_tools = AgentControlDecision.model_validate({"decision": "continue"})
    reason = AgentControlDecision.model_validate(
        {"decision": "Reason now", "rationale": "", "missing_evidence": "нет"}
    )

    assert continue_tools.decision == "continue_tools"
    assert reason.decision == "reason"
    assert reason.missing_evidence == ["нет"]
    # Настоящий вымысел остаётся отклонённым: маршрут управления не выдумываем.
    with pytest.raises(ValidationError):
        AgentControlDecision.model_validate({"decision": "teleport"})


def test_tool_action_keeps_short_query_and_hops_ceiling_stays_strict() -> None:
    """Короткий термин — настоящий запрос; потолок прыжков остаётся непреодолимым.

    `max_hops` вне [1, 4] — привычка модели, и её срезает план, чтобы не терять
    годное действие; но сама схема по-прежнему не даёт попросить обход глубже
    бюджета (`ToolAction(max_hops=9)` — ошибка, не тихое приведение).
    """
    action = ToolAction.model_validate(
        {"id": "a1", "tool": "hybrid_search", "query": "Fe", "purpose": "найти число"}
    )
    sibling = ToolAction.model_validate(
        {"id": "a2", "tool": "gap_scan", "purpose": "искать пробел"}
    )
    without_id = AgentActionPlan.model_validate(
        {"actions": [{"tool": "conflict_scan", "query": "расхождения да и нет"}]}
    )
    clamped = AgentActionPlan.model_validate(
        {"actions": [{"id": "a3", "tool": "graph_traverse", "query": "рудник", "max_hops": 9}]}
    )
    with pytest.raises(ValidationError):
        ToolAction(id="a4", tool="graph_traverse", query="рудник", max_hops=9)

    assert (action.query, action.purpose) == ("Fe", "найти число")
    assert sibling.query == "искать пробел"
    assert without_id.actions[0].id == "action-1"
    assert clamped.actions[0].max_hops == 4
    assert clamped.dropped_action_ids() == set()


def test_action_plan_drops_unusable_actions_with_a_signal() -> None:
    """Выдуманный инструмент или пустой запрос отсекаются, а не хоронят прогон.

    `Literal` на элементе и `min_length=1` на списке давали ValidationError на
    всём structured output: один битый пункт стоил всего ответа. След остаётся в
    ``dropped_action_ids`` — для причин деградации, а не для тихой подмены.
    """
    plan = AgentActionPlan.model_validate(
        {
            "rationale": "нужно число из источника",
            "actions": [
                {"id": "good", "tool": "hybrid_search", "query": "РЕАКОМ-М медь"},
                {"id": "ghost", "tool": "quantum_search", "query": "всё"},
                {"id": "empty", "tool": "gap_scan"},
            ],
            "completion_criteria": ["есть цитата со страницей"],
        }
    )

    assert [action.id for action in plan.actions] == ["good"]
    assert plan.dropped_action_ids() == {"ghost", "empty"}
    assert plan.dropped_llm_items() == {"actions": 2}


def test_action_plan_without_actions_is_a_plan_not_an_error() -> None:
    """Пустой план — рабочий исход ревьюера («инструменты не нужны»), а не 503."""
    empty = AgentActionPlan.model_validate({"rationale": "данных достаточно", "actions": []})
    missing = AgentActionPlan.model_validate({"rationale": "данных достаточно"})

    assert empty.actions == []
    assert empty.dropped_action_ids() == set()
    assert "actions" in missing.absent_list_sections()
    assert "actions" not in empty.absent_list_sections()


def test_action_plan_caps_the_budget_and_reports_overflow() -> None:
    """Свыше шести действий модель дописывает «на всякий случай»: лишние не исполняются."""
    plan = AgentActionPlan.model_validate(
        {
            "actions": [
                {"id": f"a{index}", "tool": "gap_scan", "query": f"пробел {index}"}
                for index in range(9)
            ]
        }
    )

    assert len(plan.actions) == 6
    assert plan.dropped_action_ids() == {"a6", "a7", "a8"}


def test_claim_without_evidence_quote_is_dropped_not_invented() -> None:
    """Тезис без цитаты подтвердить нельзя: дополнить его выдумкой нельзя, срезается он.

    Продуктовый инвариант — трассировка каждого тезиса до фрагмента источника,
    поэтому `evidence_quote` остаётся обязательным; но один такой claim не имеет
    права превращать импорт документа в 503, и количество срезанного видно.
    """
    result = ExtractionResult.model_validate(
        {
            "entities": [{"name": "РЕАКОМ-М", "type": "device"}],
            "claims": [
                {
                    "subject": "РЕАКОМ-М",
                    "predicate": "PRODUCES",
                    "object": "медь",
                    "statement": "Комплекс даёт медь.",
                    "confidence": 0.9,
                    "evidence_quote": "выход по меди 82%",
                },
                {
                    "subject": "РЕАКОМ-М",
                    "predicate": "LOCATED_AT",
                    "object": "медь",
                    "statement": "Без доказательства.",
                    "confidence": 0.4,
                    "evidence_quote": "   ",
                },
            ],
        }
    )

    assert len(result.claims) == 1
    assert result.claims[0].predicate == "PRODUCES"
    assert result.dropped_llm_items() == {"claims": 1}
    assert result.entities[0].canonical_name == "РЕАКОМ-М"


def test_predicate_is_canonicalized_but_garbage_still_rejected() -> None:
    """Регистр и разделитель чиним, выдуманный предикат — по-прежнему ошибка."""
    claim = ExtractedClaim.model_validate(
        {
            "subject": "РЕАКОМ-М",
            "predicate": "has property",
            "object": "медь",
            "statement": "s",
            "confidence": 0.8,
            "evidence_quote": "цитата",
        }
    )

    assert claim.predicate == "HAS_PROPERTY"
    for broken in ("противоречие", "18_pct", ""):
        with pytest.raises(ValidationError):
            ExtractedClaim.model_validate(
                {
                    "subject": "s",
                    "predicate": broken,
                    "object": "o",
                    "statement": "s",
                    "confidence": 0.8,
                    "evidence_quote": "цитата",
                }
            )


def test_entity_alias_type_and_missing_canonical_name_survive() -> None:
    """Тип-алиас и канон из имени — рабочие ответы модели, а не отказ схемы."""
    with_aliases = ExtractedEntity.model_validate(
        {"name": "РЕАКОМ-М", "type": "facility", "aliases": None}
    )
    without_aliases = ExtractedEntity.model_validate({"name": "РЕАКОМ-М", "type": "facility"})

    assert with_aliases.aliases == []
    assert with_aliases.canonical_name == "РЕАКОМ-М"
    assert with_aliases.type.value == "location"
    assert without_aliases.absent_list_sections() == {"aliases"}


def test_server_built_instances_pass_the_salvage_untouched() -> None:
    """Сборка форм кодом (не из ответа модели) не должна терять действия.

    Приведение и отсечение работают по словарю ответа; экземпляры, которые
    `agents/workflow.py` строит сам, проходят через те же схемы как есть.
    """
    plan = AgentActionPlan(
        rationale="проверка контроля",
        actions=[ToolAction(id="a1", tool="gap_scan", query="пробелы в данных", purpose="найти")],
        completion_criteria=["есть список пробелов"],
    )

    assert plan.dropped_action_ids() == set()
    assert plan.dropped_llm_items() == {}
    assert plan.actions[0].query == "пробелы в данных"


def test_critique_keeps_its_verdict_strict_but_not_its_lists() -> None:
    """Вердикт остаётся обязательным: без него черновик уходил бы на ревизию молча."""
    critique = CritiqueResult.model_validate({"approved": False, "issues": "нет цитат"})

    assert critique.issues == ["нет цитат"]
    assert critique.absent_list_sections() == {"revision_instructions"}
    with pytest.raises(ValidationError, match="approved"):
        CritiqueResult.model_validate({"issues": []})


def test_planner_zero_year_and_zero_hops_are_absent_values_not_errors() -> None:
    """`year_from: 0` у плана — это «фильтра нет», а не испорченный год.

    Приёмка 4 октября на живом GigaChat: модель возвращала нули, `ge=1800`
   читало это как ошибку схемы, ремонт вывода повторял то же, и агентный запрос
    умирал на `ModelUnavailableError` до всякого retrieval. Настояще чужой
    год (1700) обязан оставаться ошибкой: иначе план начал бы молча
    переписывать временной фильтр.
    """
    from scientific_tangle.domain.models import QueryPlan

    plan = QueryPlan.model_validate(
        {"question": "сравнение технологий", "year_from": 0, "year_to": "-1", "max_hops": 0}
    )
    assert plan.year_from is None and plan.year_to is None
    assert plan.max_hops == 3, "нулевая глубина обязана вернуться к default, а не к 1"

    with pytest.raises(ValidationError):
        QueryPlan.model_validate({"question": "сравнение технологий", "year_from": 1700})


def test_absence_literal_is_not_promoted_into_a_list_item() -> None:
    """Модель пишет `null` вместо `[]` — это отсутствие данных, не пункт.

    Прежний привод превращал этот отказ в элемент списка: в ответ попадала
    «находка» со значением «null», то есть вакуум становился содержательным
    утверждением. Целое предложение при этом остаётся пунктом — вывод
    «противоречий нет» терять нельзя, он и есть ответ.
    """
    from pydantic import Field

    from scientific_tangle.domain.contracts import LlmForm, _as_sequence

    class _Form(LlmForm):
        conflicts: list[str] = Field(default_factory=list)

    for literal in ("null", "None", "NIL", ""):
        assert _as_sequence(literal) == [], literal
        assert _Form.model_validate({"conflicts": literal}).conflicts == [], literal

    # Высказывание модели пунктом остаётся: «нет» и прочерк — это ответ, а не
    # способ записать пустоту (тот же контракт держат тесты выше).
    assert _Form.model_validate({"conflicts": "противоречий нет"}).conflicts == [
        "противоречий нет"
    ]
    assert _Form.model_validate({"conflicts": "нет"}).conflicts == ["нет"]
    assert _Form.model_validate({"conflicts": "-"}).conflicts == ["-"]
