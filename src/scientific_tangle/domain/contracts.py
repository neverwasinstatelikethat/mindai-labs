from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, get_args, get_origin
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import (
    EvidenceLocator,
    NumericFilter,
    NumericObservation,
    QueryPlan,
)

# Валидатор email без новой зависимости (pydantic[email] тянет email-validator):
# грубой проверки формата достаточно — настоящий контроль даёт подтверждение
# адреса, а оно появится вместе с почтовым контуром.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}")

# ── Толерантность форм, которые наполняет модель ───────────────────────────────
#
# Структурированный ответ GigaChat приходит словарём, и «противоречий нет» для
# модели естественно пишется строкой: `"conflicts": "противоречий нет"`,
# `"recommendations": "-"`, иногда `"[]"` как JSON-строка. Требовательный список
# давал ValidationError → schema-repair → 503 на потолке попыток: холодный
# `GET /api/v1/demo` терял ответ целиком (живой лог контейнера, HEAD ee52e96).
#
# Клиент-facing схемы (`QueryRequest`, `DocumentRequest`, `ComparisonRequest`) от
# этой базы не наследуются: битый вход снаружи обязан оставаться 422, а не
# молча дополненным значением.

# Ключи, по которым из объекта-элемента списка достаётся человекочитаемый текст.
_TEXT_ITEM_KEYS = ("text", "value", "statement", "summary", "reason", "name")
# Обход идёт по ДАННЫМ ответа, а не по схеме: у формы три уровня, остальное —
# мусор, который всё равно не пройдёт валидацию ниже.
_MAX_COERCION_DEPTH = 6


def _list_item_type(annotation: object) -> object | None:
    """Тип элемента поля-списка (`list[str]` → `str`); None — поле не список."""
    if get_origin(annotation) is not list:
        return None
    args = get_args(annotation)
    return args[0] if args else None


def _nested_model(annotation: object) -> type[BaseModel] | None:
    """Модель-носитель поля, в том числе внутри `X | None` (`intent: … | None`)."""
    candidates = get_args(annotation) if get_origin(annotation) is not None else (annotation,)
    for candidate in candidates:
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            return candidate
    return None


def _json_container(text: str) -> list[object] | None:
    """`"[]"` и `"[\"a\"]"` — список, который модель завернула в строку."""
    if not text.startswith(("[", "{")):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return None


# Артефакты сериализации: модель пишет их вместо `[]`. Русское «нет» и прочерк
# сюда не входят сознательно и остаются пунктом списка — в них высказывание
# модели («противоречий нет»), а не способ записать пустоту; контракт закреплён
# тестами толерантности формы.
_NULLISH_TOKENS = frozenset({"null", "none", "nil"})


def _as_sequence(value: object) -> list[object] | None:
    """Приводит ответ модели к последовательности; None — привести нельзя."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        parsed = _json_container(text)
        if parsed is not None:
            return parsed
        # Пустая строка и литералы пустоты — «ничего нет»: это не высказывание,
        # а способ сериализации (модель пишет `null` или «нет» вместо массива), и
        # пунктом списка оно стало бы выдуманной находкой. Прочая строка — один
        # пункт: потерять текст «противоречий нет» в пустом списке хуже, чем
        # показать его пунктом.
        if not text or text.lower() in _NULLISH_TOKENS:
            return []
        return [text]
    if isinstance(value, dict):
        # Модель вернула один объект вместо массива из одного элемента.
        return [value]
    return None


def _item_text(item: object) -> str | None:
    """Элемент `list[str]` в тексте; None — элемент не является высказыванием."""
    if isinstance(item, str):
        return item.strip() or None
    if item is None or isinstance(item, list):
        return None
    if isinstance(item, dict):
        for key in _TEXT_ITEM_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(item, ensure_ascii=False)
    return str(item)


def _coerce_items(items: list[object], item_type: object) -> list[object]:
    if item_type is str:
        return [text for item in items if (text := _item_text(item)) is not None]
    if isinstance(item_type, type) and issubclass(item_type, BaseModel):
        # Вложенные формы приводим тем же обходом; экземпляры, собранные
        # сервером, не трогаем — они уже валидны.
        return [
            _coerce_payload(item_type, item) if isinstance(item, dict) else item for item in items
        ]
    return items


def _coerce_payload(cls: type[BaseModel], data: object, depth: int = 0) -> object:
    """Приводит поля-списки формы и вложенных форм к списку.

    Новых ключей не появляется: отсутствие секции обязано читаться по
    ``model_fields_set``, иначе «модель не вернула секцию» станет «модель ничего
    не нашла», и деградация ответа пропадёт.
    """
    if not isinstance(data, dict) or depth > _MAX_COERCION_DEPTH:
        return data
    coerced = dict(data)
    for name, info in cls.model_fields.items():
        if name not in coerced:
            continue
        item_type = _list_item_type(info.annotation)
        if item_type is not None:
            sequence = _as_sequence(coerced[name])
            if sequence is not None:
                coerced[name] = _coerce_items(sequence, item_type)
            continue
        nested = _nested_model(info.annotation)
        if nested is not None and isinstance(coerced[name], dict):
            coerced[name] = _coerce_payload(nested, coerced[name], depth + 1)
    return coerced


class LlmForm(BaseModel):
    """База structured-output форм: неполный ответ — рабочий исход, а не отказ.

    Форму наполняет модель, поэтому отсутствие секции, строка вместо списка и
    выдуманный элемент чинятся на уровне схемы с явным сигналом. Строгость
    остаётся у контрактов, которые читает клиент или пишет хранилище.
    """

    _llm_dropped: dict[str, int] = PrivateAttr(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _tolerate_llm_shapes(cls, data: object) -> object:
        return _coerce_payload(cls, data)

    def absent_list_sections(self) -> set[str]:
        """Поля-списки, которых в ответе модели не было вовсе.

        Pydantic заносит в ``model_fields_set`` только ключи входящего словаря,
        поэтому пустой список («не нашла») и отсутствующий ключ («не вернула»)
        различимы: вызывающий слой кладёт второе в ``degradation_reasons``.
        """
        return {
            name
            for name, info in type(self).model_fields.items()
            if _list_item_type(info.annotation) is not None and name not in self.model_fields_set
        }

    def dropped_llm_items(self) -> dict[str, int]:
        """Что и сколько отсечено на уровне схемы (см. ``AgentActionPlan``)."""
        return dict(self._llm_dropped)


class NodeType(StrEnum):
    MATERIAL = "material"
    PROCESS = "process"
    EQUIPMENT = "equipment"
    CONDITION = "condition"
    CLAIM = "claim"
    EXPERIMENT = "experiment"
    PUBLICATION = "publication"
    EXPERT = "expert"
    CHUNK = "chunk"
    LOCATION = "location"
    ORGANIZATION = "organization"


class GraphNode(BaseModel):
    id: str
    label: str
    type: NodeType
    confidence: float = Field(default=1, ge=0, le=1)
    data_class: DataClass = DataClass.PUBLIC
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    relation: str
    confidence: float = Field(default=1, ge=0, le=1)
    data_class: DataClass = DataClass.PUBLIC


class GraphSnapshot(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    communities: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    id: str
    statement: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceLocator]
    status: Literal["consensus", "disputed", "hypothesis"] = "consensus"
    observations: list[NumericObservation] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)
    superseded_by: str | None = None
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    fact_kind: str | None = None
    context: str | None = None
    scope: dict[str, str] = Field(default_factory=dict)
    # Единственный источник истины для классификации доступа. Статус finding
    # (consensus/disputed/hypothesis) — про степень консенсуса, а не про права,
    # и не может служить вторым, независимым основанием для ACL.
    data_class: DataClass = DataClass.PUBLIC
    reviewer_id: str | None = None
    review_date: str | None = None
    review_reason: str | None = None


class AgentEvent(BaseModel):
    agent: str
    status: Literal["started", "completed", "revised", "failed"]
    message: str
    duration_ms: int = Field(default=0, ge=0)


class AgentMetricSnapshot(BaseModel):
    agent: str
    calls: int = Field(ge=0)
    successes: int = Field(ge=0)
    failures: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    average_duration_ms: float = Field(ge=0)
    p50_duration_ms: float = Field(ge=0)
    p95_duration_ms: float = Field(ge=0)
    # Хвост распределения — то, что видит пользователь длинного исследования:
    # p95 у маскирует узел, стабильно упирающийся в дедлайн.
    p99_duration_ms: float = Field(default=0, ge=0)


class LlmMetricSnapshot(BaseModel):
    schema_name: str = Field(min_length=1)
    calls: int = Field(ge=0)
    failures: int = Field(ge=0)
    # Schema-repair попытки: повтор был у обращения к модели, а не у агента.
    retries: int = Field(default=0, ge=0)
    # Отмена клиента и таймаут провайдера — не отказы валидации: у каждого своя
    # причина и своя починка, поэтому они считаются отдельно от ``failures``.
    cancelled: int = Field(default=0, ge=0)
    timeouts: int = Field(default=0, ge=0)
    average_duration_ms: float = Field(ge=0)
    p50_duration_ms: float = Field(default=0, ge=0)
    p95_duration_ms: float = Field(ge=0)
    p99_duration_ms: float = Field(default=0, ge=0)


class AgentMetricsResponse(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Кэш structured output и снимок графа принадлежат процессу, а не общему
    # хранилищу. Диагностический снимок обязан называть владельца: при N воркеров
    # здесь видно N независимых счётчиков попаданий, и «кэш почти не помогает»
    # перестаёт быть выводом по чужому процессу.
    process_id: int = Field(default=0, ge=0)
    cache_scope: Literal["process", "shared"] = "process"
    agents: list[AgentMetricSnapshot]
    llm: list[LlmMetricSnapshot] = Field(default_factory=list)
    total_prompt_tokens: int = Field(default=0, ge=0)
    total_completion_tokens: int = Field(default=0, ge=0)
    # Приём агентных прогонов: saturation обязана быть наблюдаемой, иначе 429
    # выглядят как случайные отказы сервиса.
    agent_runs_active: int = Field(default=0, ge=0)
    agent_runs_limit: int = Field(default=0, ge=0)
    agent_runs_refused: int = Field(default=0, ge=0)
    llm_calls_in_flight: int = Field(default=0, ge=0)
    llm_calls_waiting: int = Field(default=0, ge=0)
    llm_slots: int = Field(default=0, ge=0)
    # Повторы structured output — самый диагностичный признак того, что GigaChat
    # портит валидацию: без него деградация ответа выглядит как «модель тормозит».
    llm_retries_total: int = Field(default=0, ge=0)
    llm_retries_by_schema: dict[str, int] = Field(default_factory=dict)
    llm_cancelled_total: int = Field(default=0, ge=0)
    llm_cancelled_by_schema: dict[str, int] = Field(default_factory=dict)
    llm_timeouts_total: int = Field(default=0, ge=0)
    llm_timeouts_by_schema: dict[str, int] = Field(default_factory=dict)
    # Кэш structured output: попадание — сбережённый вызов модели и снятый с
    # очереди слот провайдера.
    llm_cache_hits: int = Field(default=0, ge=0)
    llm_cache_misses: int = Field(default=0, ge=0)
    llm_cache_hit_rate: float = Field(default=0, ge=0, le=1)
    # Ожидание слота внутри агентного дедлайна — будущая молчаливая деградация.
    llm_queue_waits: int = Field(default=0, ge=0)
    retrieval_failures_by_component: dict[str, int] = Field(default_factory=dict)
    # Короткие коды причин деградации (``TimeoutError``, ``no_answer``), а не
    # пользовательский текст ``degradation_reasons``: он разным бывает в каждом
    # ответе и в счётчиках не сводится.
    degradations_by_reason: dict[str, int] = Field(default_factory=dict)


ModelMode = Literal["gigachat", "scripted", "unavailable"]
# Темы ленты, которые продукт действительно порождает: новая тема появляется
# вместе с вызовом _notify, а не как свободная строка клиента.
NotificationTopic = Literal["claim.superseded", "proposal.accepted"]
ServiceState = Literal["ready", "configured", "fallback", "disabled"]
AccountsMode = Literal["postgres", "in-memory"]
# Общий выбор контура хранения для серверного состояния (сессии, ответы,
# экспертные решения): postgres — рабочий контур, in-memory — тесты и запуск
# без базы.
StoreBackend = AccountsMode


class AnswerPayload(BaseModel):
    query_id: UUID = Field(default_factory=uuid4)
    question: str
    summary: str
    intent: IntentClassification | None = None
    query_plan: QueryPlan
    tool_observations: list[ToolObservation] = Field(default_factory=list)
    findings: list[Finding]
    conflicts: list[str]
    knowledge_gaps: list[str]
    recommendations: list[str]
    graph: GraphSnapshot
    trace: list[AgentEvent]
    confidence: float = Field(ge=0, le=1)
    model_mode: ModelMode
    # Честная сигнализация деградации: какие ограничения не дали собрать ответ
    # полностью (дедлайн, лимит рекурсии, пустое доказательное покрытие).
    degradation_reasons: list[str] = Field(default_factory=list)


class QueryRequest(BaseModel):
    thread_id: UUID = Field(default_factory=uuid4)
    question: str = Field(min_length=3)
    language: Literal["ru", "en"] = "ru"
    mode: Literal["local", "global", "hybrid"] = "hybrid"


class IntentClassification(LlmForm):
    primary: Literal[
        "fact_search",
        "literature_review",
        "technology_comparison",
        "contradiction_analysis",
        "gap_analysis",
        "expert_discovery",
        "graph_edit",
        "report_generation",
    ]
    secondary: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


# Границы обхода графа: один источник и для жёсткой схемы, и для приведения
# ответа модели (иначе «потолок» в двух местах разъехался бы).
_MIN_HOPS = 1
_MAX_HOPS = 4


class ToolAction(LlmForm):
    # `query`/`purpose` без min_length: «Fe» — настоящий поисковый запрос горняка,
    # а отказ всей формы из-за двух символов стоил прогона. Непригодное действие
    # (пустой запрос ИЛИ выдуманный инструмент) отсекает ``AgentActionPlan`` —
    # уже с явным сигналом, а не молча.
    id: str = Field(min_length=1)
    tool: Literal[
        "hybrid_search",
        "graph_traverse",
        "community_search",
        "numeric_filter",
        "conflict_scan",
        "gap_scan",
        "expert_lookup",
    ]
    purpose: str = ""
    query: str = ""
    entities: list[str] = Field(default_factory=list)
    relation_types: list[str] = Field(default_factory=list)
    # Потолок прыжков остаётся жёстким в самой схеме: обход графа глубже потолка
    # — это бюджет провайдера, и «нельзя попросить весь provenance» должен
    # проверяться здесь, а не в договорённости с моделью. Ответ модели с
    # `max_hops: 9` приводит к краю ``AgentActionPlan`` (см. его salvage), чтобы
    # привычка модели не стоила годного действия.
    max_hops: int = Field(default=2, ge=_MIN_HOPS, le=_MAX_HOPS)

    @model_validator(mode="after")
    def _fill_from_sibling(self) -> ToolAction:
        """Пустой `query`/`purpose` берутся из парного поля: действие остаётся выполнимым."""
        self.query = self.query.strip()
        self.purpose = self.purpose.strip()
        self.query = self.query or self.purpose
        self.purpose = self.purpose or self.query
        return self


# Потолок плана = бюджет одного tool-раунда (см. agents/tools.py): лишние
# действия модель дописывает «на всякий случай», и они не исполняются.
_MAX_PLAN_ACTIONS = 6


class AgentActionPlan(LlmForm):
    rationale: str = ""
    actions: list[ToolAction] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)

    _dropped_action_ids: frozenset[str] = PrivateAttr(default_factory=frozenset)

    @model_validator(mode="wrap")
    @classmethod
    def _salvage_actions(
        cls, data: object, handler: Callable[[object], AgentActionPlan]
    ) -> AgentActionPlan:
        """Одно непригодное действие не имеет права стоить всего прогона.

        Выдуманное имя инструмента или пустой запрос ломают один элемент списка,
        а `min_length=1`/`Literal` на нём — весь structured output: прогон уходил
        в schema-repair и заканчивался 503. Элемент отсекается здесь, а
        ``dropped_action_ids`` остаётся явным следом для причин деградации.
        """
        coerced = _coerce_payload(cls, data)
        dropped: list[str] = []
        if isinstance(coerced, dict) and isinstance(coerced.get("actions"), list):
            kept: list[ToolAction] = []
            for index, item in enumerate(coerced["actions"]):
                if isinstance(item, ToolAction):
                    kept.append(item)
                    continue
                identifier = f"action-{index + 1}"
                if isinstance(item, dict):
                    identifier = str(item.get("id") or "").strip() or identifier
                    item = {**item, "id": identifier}
                    hops = item.get("max_hops")
                    # `max_hops: 9` от модели — привычка, а не намерение обойти
                    # бюджет обхода: берём край, чтобы не терять годное действие.
                    # Вне схемы потолок по-прежнему непреодолим (ge/le в Field).
                    if isinstance(hops, int) and not isinstance(hops, bool):
                        item = {**item, "max_hops": min(max(hops, _MIN_HOPS), _MAX_HOPS)}
                else:
                    dropped.append(identifier)
                    continue
                try:
                    action = ToolAction.model_validate(item)
                except ValidationError:
                    dropped.append(identifier)
                    continue
                if not action.query and not action.purpose:
                    dropped.append(identifier)
                    continue
                kept.append(action)
            dropped.extend(action.id for action in kept[_MAX_PLAN_ACTIONS:])
            coerced = {**coerced, "actions": kept[:_MAX_PLAN_ACTIONS]}
        instance = handler(coerced)
        instance._dropped_action_ids = frozenset(dropped)
        # Пустой словарь — «ничего не отсечено»: нулевой счётчик в сигнале
        # читался бы как «срезали ноль действий», и отличить план без брака было бы нечем.
        instance._llm_dropped = {"actions": len(dropped)} if dropped else {}
        return instance

    def dropped_action_ids(self) -> set[str]:
        """Действия, отсечённые на уровне схемы: инструмент или запрос непригодны."""
        return set(self._dropped_action_ids)


class PlanningBundle(LlmForm):
    # Назначение запроса — метаданное ответа, а не пропуск: ни маршрутизации, ни
    # расчёта от него не зависит, и интерфейс умеет честить отсутствие. Живые прогоны
    # GigaChat дважды вернули валидные query_plan и action_plan без `intent`, и
    # обязательность этого поля стоила новичку всего ответа.
    intent: IntentClassification | None = None
    query_plan: QueryPlan
    action_plan: AgentActionPlan

    @model_validator(mode="wrap")
    @classmethod
    def _discard_invalid_filters(
        cls, data: object, handler: Callable[[object], PlanningBundle]
    ) -> PlanningBundle:
        """Непригодный фильтр модели не должен терять весь план исследования."""
        coerced = _coerce_payload(cls, data)
        dropped = 0
        if isinstance(coerced, dict) and isinstance((plan := coerced.get("query_plan")), dict):
            if isinstance((filters := plan.get("numeric_filters")), list):
                kept: list[NumericFilter] = []
                for item in filters:
                    try:
                        kept.append(NumericFilter.model_validate(item))
                    except ValidationError:
                        dropped += 1
                coerced = {**coerced, "query_plan": {**plan, "numeric_filters": kept}}
        instance = handler(coerced)
        instance._llm_dropped = {"numeric_filters": dropped} if dropped else {}
        return instance


class ToolObservation(BaseModel):
    action_id: str
    tool: str
    status: Literal["success", "warning", "error"]
    summary: str
    next_actions: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    graph_node_ids: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    # Сколько фактов срезано потолком выдачи: полем, а не разбором строки среза,
    # чтобы счётчики не зависели от формулировки текста для человека.
    omitted_count: int = Field(default=0, ge=0)
    root_cause_hint: str | None = None
    safe_retry: str | None = None
    stop_condition: str | None = None


class AgentControlDecision(LlmForm):
    decision: Literal["continue_tools", "reason"]
    rationale: str = ""
    missing_evidence: list[str] = Field(default_factory=list)

    @field_validator("decision", mode="before")
    @classmethod
    def _normalize_decision(cls, value: object) -> object:
        """`continue` / `continue tools` / `reason_now` — одна команда: сводим к слову схемы."""
        if not isinstance(value, str):
            return value
        token = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
        aliases = {
            "continue_tools": "continue_tools",
            "continue": "continue_tools",
            "tools": "continue_tools",
            "continue_with_tools": "continue_tools",
            "need_more_evidence": "continue_tools",
            "reason": "reason",
            "reason_now": "reason",
            "answer": "reason",
            "final": "reason",
        }
        # Неизвестное слово остаётся приведённым и будет отклонено по существу:
        # выдумывать маршрут управления по обрывку нельзя.
        return aliases.get(token, token)
        return aliases.get(token, value)


class RetrievalPlan(BaseModel):
    lexical_query: str = Field(min_length=3)
    semantic_query: str = Field(min_length=3)
    entity_names: list[str]
    relation_types: list[str]
    max_hops: int = Field(ge=1, le=4)
    use_global_context: bool
    use_community_context: bool = False
    use_local_graph: bool = True


class ExtractedEntity(LlmForm):
    name: str
    canonical_name: str = ""
    type: NodeType
    aliases: list[str] = Field(default_factory=list)

    @field_validator("type", mode="before")
    @classmethod
    def normalize_entity_type(cls, value: object) -> object:
        aliases = {
            "place": NodeType.LOCATION,
            "facility": NodeType.LOCATION,
            "company": NodeType.ORGANIZATION,
            "organisation": NodeType.ORGANIZATION,
            "device": NodeType.EQUIPMENT,
            "apparatus": NodeType.EQUIPMENT,
        }
        return aliases.get(str(value).lower(), value)

    @model_validator(mode="after")
    def _canonical_from_name(self) -> ExtractedEntity:
        """Канон без имени модель не вернула: это то же самое имя, а не новый объект."""
        self.canonical_name = self.canonical_name.strip() or self.name.strip()
        return self


class ExtractedClaim(LlmForm):
    subject: str
    # Паттерн остаётся (именно он ограничивает словарь предикатов онтологии), но
    # падать прогоном из-за регистра модели дороже: `has_property` приводится к
    # канону здесь, а настоящий мусор отклоняется валидацией после приведения.
    predicate: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    object: str
    statement: str
    confidence: float = Field(ge=0, le=1)
    evidence_quote: str = Field(min_length=1)
    fact_kind: str = Field(default="fact", min_length=1, max_length=80)
    context: str | None = Field(default=None, max_length=1000)
    observations: list[NumericObservation] = Field(default_factory=list)

    @field_validator("predicate", mode="before")
    @classmethod
    def _canonical_predicate(cls, value: Any) -> Any:
        """`has_property`/`MATERIALOrd`/`has property` → `HAS_PROPERTY`.

        `Any`, а не `object`: поле `object` этого класса затемняет встроенный тип
        в теле класса, и аннотация валидатора перестала бы быть типом.
        """
        if not isinstance(value, str):
            return value
        token = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).upper().strip("_")
        # Без буквы первой не собрать канон (цифра, кириллица, пусто) — оставляем
        # как есть: валидация отклонит честно, чем выдуманный предикат в граф.
        if not token or not token[0].isalpha() or not token[0].isascii():
            return value
        return token

    @field_validator("evidence_quote", mode="before")
    @classmethod
    def _strip_evidence_quote(cls, value: Any) -> Any:
        """Пробельная «цитата» — это отсутствие цитаты: `min_length` обязан увидеть её такой."""
        return value.strip() if isinstance(value, str) else value


class ExtractionResult(LlmForm):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)

    @model_validator(mode="wrap")
    @classmethod
    def _salvage_claims(
        cls, data: object, handler: Callable[[object], ExtractionResult]
    ) -> ExtractionResult:
        """Выписка без цитаты-доказательства выбрасывается, а не чинится текстом.

        Трассировка тезиса до первоисточника — продуктовый инвариант: claim без
        `evidence_quote` нельзя ни подтвердить, ни оспорить, поэтому её нельзя и
        дополнить выдумкой. Один битый элемент не стоит 503 всего импорта:
        количество отсеченного видно в ``dropped_llm_items()``.
        """
        coerced = _coerce_payload(cls, data)
        dropped: dict[str, int] = {}
        if isinstance(coerced, dict):
            for section, model in (("claims", ExtractedClaim), ("entities", ExtractedEntity)):
                items = coerced.get(section)
                if not isinstance(items, list):
                    continue
                kept = []
                missing = 0
                for item in items:
                    if isinstance(item, model) or not isinstance(item, dict):
                        kept.append(item)
                        continue
                    try:
                        kept.append(model.model_validate(_coerce_payload(model, item)))
                    except ValidationError:
                        missing += 1
                if missing:
                    dropped[section] = missing
                coerced = {**coerced, section: kept}
        instance = handler(coerced)
        instance._llm_dropped = dropped
        return instance


class EntityResolutionProposal(BaseModel):
    mention: str
    canonical_name: str
    action: Literal["link", "create"]
    confidence: float = Field(ge=0, le=1)
    rationale: str


class EntityMergeProposal(BaseModel):
    """Предложение склейки сущностей.

    ``id`` — детерминированный uuid5 от пары (алиас, канон): регистрация той же
    пары на каждом импорте обязана обновлять запись, а не плодить предложения в
    неограниченной очереди. ``source_id``/``target_id`` — реальные ``id`` узлов
    графа (идентичность сущности в этом коде живёт в ``id``, а ``label``
    описателен); заполняются при принятии и остаются пустыми, пока узлы не
    найдены.
    """

    id: UUID = Field(default_factory=uuid4)
    source: str
    target: str
    confidence: float = Field(ge=0, le=1)
    rationale: str
    status: Literal["proposed", "accepted", "rejected", "reverted"] = "proposed"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reviewed_at: datetime | None = None
    reviewer_id: str | None = None
    source_id: str | None = None
    target_id: str | None = None


class MergeReviewRequest(BaseModel):
    action: Literal["accept", "reject", "revert"]


class IngestionBundle(LlmForm):
    extraction: ExtractionResult
    resolutions: list[EntityResolutionProposal] = Field(default_factory=list)


class ReasoningResult(LlmForm):
    summary: str
    # Пустой список — это «не найдено», а не «модель сломалась»: ответ без
    # цитат и без перечня пробелов честен, и штрафовать его за форму нельзя.
    finding_ids: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    knowledge_gaps: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class CritiqueResult(LlmForm):
    # `approved` остаётся обязательным: отсутствие вердикта нельзя молча
    # превращать в отказ — иначе черновик без замечаний уходил бы на ревизию.
    approved: bool
    issues: list[str] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)


class EvolutionDraft(LlmForm):
    kind: Literal["prompt", "rule", "alias", "gold_case"]
    title: str
    change: str
    # Без `impact` предложение остаётся полезным (kind/title/change — суть), и
    # обязательный список здесь стоил прогона так же, как `conflicts` выше.
    # Отсутствие секции различимо через ``absent_list_sections()``.
    impact: list[str] = Field(default_factory=list)


class DocumentRequest(BaseModel):
    title: str = Field(min_length=1)
    text: str = Field(min_length=20)
    language: Literal["ru", "en"] = "ru"
    geography: str | None = None
    year: int | None = Field(default=None, ge=1800, le=2100)
    data_class: DataClass = DataClass.PUBLIC
    fragments: list[DocumentFragment] = Field(default_factory=list)


class DocumentFragment(BaseModel):
    text: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    sheet: str | None = None
    cell_range: str | None = None
    source_char_start: int = Field(default=0, ge=0)


class DocumentReceipt(BaseModel):
    document_id: UUID
    checksum: str
    status: Literal["created", "duplicate"]
    extracted_claims: int
    # Промпт извлечения ограничен бюджетом: молча не досылать часть документа —
    # приём числа из хвоста не должен выглядеть «в корпусе такого нет».
    prompt_truncated: bool = False
    omitted_characters: int = Field(default=0, ge=0)


class PreloadDocumentResult(BaseModel):
    path: str
    title: str
    status: Literal["created", "duplicate", "failed"]
    extracted_claims: int = 0
    error: str | None = None


class PreloadReport(BaseModel):
    started_at: datetime
    finished_at: datetime
    total: int
    created: int
    duplicates: int
    failed: int
    claims: int
    documents: list[PreloadDocumentResult]
    skipped_oversize: int = Field(default=0, ge=0)
    ocr_required: int = Field(default=0, ge=0)


class StructuralDocumentReceipt(BaseModel):
    document_id: UUID
    checksum: str
    status: Literal["created", "duplicate"]
    chunks: int
    vectors_indexed: int = 0


class CorpusCompileReport(BaseModel):
    discovered: int
    eligible: int
    processed: int
    created: int
    duplicates: int
    failed: int
    chunks: int
    skipped_unsupported: int
    skipped_oversize: int
    ocr_required: int
    coverage: float = Field(ge=0, le=1)
    errors: list[str] = Field(default_factory=list)


class CorpusStats(BaseModel):
    documents: int = Field(ge=0)
    chunks: int = Field(ge=0)
    claims: int = Field(ge=0)
    entities: int = Field(ge=0)
    semantic_documents: int = Field(ge=0)
    vectors_indexed: int = Field(default=0, ge=0)


class EvaluationMetrics(BaseModel):
    citation_coverage: float = Field(ge=0, le=1)
    numeric_support: float = Field(ge=0, le=1)
    # Средне-заявленная confidence findings заменена честной парой метрик:
    # доля выводов без поддержки и само-оценка модели не одно и то же.
    unsupported_claim_ratio: float = Field(ge=0, le=1)
    mean_finding_confidence: float = Field(ge=0, le=1)
    overall: float = Field(ge=0, le=1)


class EvaluationRun(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    query_id: UUID
    metrics: EvaluationMetrics
    passed: bool
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GoldCase(BaseModel):
    id: str
    language: Literal["ru", "en"]
    question: str
    source_path: str
    expected_source_titles: list[str]


class RankingMetrics(BaseModel):
    recall_at_3: float = Field(ge=0, le=1)
    precision_at_3: float = Field(ge=0, le=1)
    mrr: float = Field(ge=0, le=1)
    ndcg_at_3: float = Field(ge=0, le=1)


class RetrievalCaseResult(BaseModel):
    case_id: str
    expected_sources: list[str]
    retrieved_sources: list[str]
    reciprocal_rank: float = Field(ge=0, le=1)


class RetrievalBenchmark(BaseModel):
    gold_cases: int = Field(ge=0)
    # Кейсы, которые вообще возможно засчитать: ожидаемый источник лежит в
    # текущем корпусе. Остальные дают recall=0 не из-за плохого поиска, а
    # потому что измерять нечего.
    scored_cases: int = Field(ge=0)
    corpus_documents: int = Field(ge=0)
    top_k: int = Field(ge=1)
    hybrid: RankingMetrics
    lexical_baseline: RankingMetrics
    cases: list[RetrievalCaseResult]
    # Инварианты корректности замера (не «утечки»): baseline обязан быть слабее
    # или равен hybrid, ожидаемый источник обязан быть в корпусе.
    validity_checks: dict[str, bool]
    passed: bool


class PipelineVariantMetrics(BaseModel):
    source_recall: float = Field(ge=0, le=1)
    citation_coverage: float = Field(ge=0, le=1)
    pass_rate: float = Field(ge=0, le=1)
    average_latency_ms: float = Field(ge=0)
    p95_latency_ms: float = Field(default=0, ge=0)
    retries_per_case: float = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class PipelineCaseResult(BaseModel):
    case_id: str
    question: str
    expected_sources: list[str]
    retrieved_sources: list[str]
    latency_ms: float = Field(ge=0)
    degradation_reasons: list[str] = Field(default_factory=list)
    passed: bool


class PipelineBenchmark(BaseModel):
    cases: int = Field(ge=0)
    agentic_graphrag: PipelineVariantMetrics
    lexical_baseline: RankingMetrics
    results: list[PipelineCaseResult]
    passed: bool


class EvolutionExperiment(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    proposal_id: UUID
    cases: int = Field(ge=1)
    baseline: PipelineVariantMetrics
    candidate: PipelineVariantMetrics
    delta_pass_rate: float = Field(ge=-1, le=1)
    regressions: list[str] = Field(default_factory=list)
    decision: Literal["promote", "reject"]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class QueryResponse(BaseModel):
    answer: AnswerPayload
    evaluation: EvaluationRun
    # Идентификатор запроса для разбора инцидента: тот же correlation_id, что
    # попадает в журнал аудита и в заголовок ответа. В SSE он едет в каждом
    # событии, поэтому и в JSON-ответе обязан быть — иначе клиент не свяжет
    # свой запрос с записью в журнале.
    correlation_id: str = ""


class FeedbackRequest(BaseModel):
    query_id: UUID
    finding_id: str | None = None
    verdict: Literal["accept", "reject", "correct"]
    comment: str = Field(min_length=3)
    correction: str | None = None


class EvolutionProposal(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source_query_id: UUID
    kind: Literal["prompt", "rule", "alias", "gold_case"]
    title: str
    change: str
    status: Literal["proposed", "accepted", "rejected"] = "proposed"
    impact: list[str]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FeedbackResult(BaseModel):
    """Экспертное решение и предложение по нему — разные по надёжности части.

    Замена утверждения записывается всегда; proposal генерирует LLM, и без
    настроенной модели экспертное исправление не должно теряться.
    """

    proposal: EvolutionProposal | None = None
    superseded: Finding | None = None
    degradation_reasons: list[str] = Field(default_factory=list)


class ProposalReviewRequest(BaseModel):
    accepted: bool


class SystemStatus(BaseModel):
    status: Literal["ready", "degraded"]
    model_mode: ModelMode
    services: dict[str, ServiceState]
    # Фактическое хранилище учётных записей: "in-memory" означает, что Postgres
    # был недоступен на старте и сессии переживают перезапуск только в памяти.
    accounts: AccountsMode = "in-memory"
    # Тот же выбор для серверных копий ответов и экспертных решений: на памяти
    # экспорт возможен только до перезапуска процесса.
    state_backend: StoreBackend = "in-memory"
    # Причина деградации словами: интерфейс показывает её вместо обещаний
    # «история решений сохранена», которых на этом контуре нет.
    degradation_reasons: list[str] = Field(default_factory=list)
    # Пропускная способность агентного контура: сколько прогонов одновременно
    # принимает сервис, а сколько — уже отказ.
    agent_runs_limit: int = Field(default=0, ge=0)
    agent_runs_active: int = Field(default=0, ge=0)


# ── FT-12/26: Comparison models ─────────────────────────────────────────────


class ComparisonCell(BaseModel):
    value: str | None = None
    unit: str | None = None
    evidence: str | None = None
    confidence: float = Field(default=0, ge=0, le=1)


class ComparisonRow(BaseModel):
    item: str
    cells: dict[str, ComparisonCell]


class ComparisonTable(BaseModel):
    question: str
    headers: list[str]
    rows: list[ComparisonRow]


class ComparisonRequest(BaseModel):
    question: str = Field(min_length=3)
    entities: list[str] = Field(min_length=1)
    dimensions: list[str] = Field(default_factory=list)
    language: Literal["ru", "en"] = "ru"


# ── FT-23: Export models ────────────────────────────────────────────────────


ExportFormat = Literal["markdown", "json-ld", "pdf"]


class ExportRequest(BaseModel):
    """Экспортируется серверный ответ по ``query_id``, а не присланный клиентом.

    Прежняя форма принимала весь ``AnswerPayload``: клиент мог переклеить
    ``data_class: restricted → public`` в теле и получить закрытый текст
    файлом, потому что ACL фильтровал именно присланные данные. Пост-генерационная
    фильтрация контролем доступа не считается (см. services/governance.py),
    поэтому источник данных — только серверное хранилище ответов.
    ``extra="forbid"``: лишние поля (в том числе подставленный ``answer``) —
    явная ошибка 422, а не молчаливо проигнорированный вход.
    """

    model_config = ConfigDict(extra="forbid")

    query_id: UUID
    format: ExportFormat = "markdown"


class StoredAnswerInfo(BaseModel):
    """Метаданные серверной копии ответа (для диагностики и истории)."""

    query_id: UUID
    owner_id: str
    created_at: datetime
    data_classes: list[str]
    findings: int = Field(ge=0)


class AnswerHistoryItem(BaseModel):
    """Краткая запись ответа для личной истории вопросов."""

    query_id: UUID
    question: str
    created_at: datetime


class AnswerHistoryPage(BaseModel):
    items: list[AnswerHistoryItem]
    has_more: bool


class ExpertDecision(BaseModel):
    """Durable-запись экспертного решения: перезапуск процесса её не стирает.

    Свободного текста из источников здесь нет — только идентификаторы и
    структурированные признаки, чтобы журнал решений не становился каналом
    утечки restricted-содержимого.
    """

    id: UUID = Field(default_factory=uuid4)
    actor_id: str
    action: Literal[
        "proposal.created",
        "proposal.reviewed",
        "resolution.reviewed",
        "claim.superseded",
        "answer.exported",
        "conflict.reviewed",
    ]
    object_id: str
    outcome: Literal["success", "denied", "failure"] = "success"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ConflictReview(BaseModel):
    """Решение эксперта по одной паре-противоречию.

    Ключ — отпечаток пары (``ConflictCandidate.id``), а не строка из ответа
    модели: решение обязано переживать перезапуск процесса и следующий прогон
    детектора, иначе подтверждённое противоречие снова вставало бы в очереди
    как нерассмотренное. Свободного текста здесь нет по той же причине, что и в
    ``ExpertDecision``.
    """

    candidate_id: str
    status: Literal["confirmed", "dismissed"]
    actor_id: str
    property_name: str
    left_finding_id: str
    right_finding_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ConflictReviewRequest(BaseModel):
    """Тело решения по противоречию: подтверждено оно или отклонено."""

    confirmed: bool


class ConflictSide(BaseModel):
    """Одна сторона пары: тезис с числом и его первоисточник."""

    claim_id: str
    finding_id: str
    statement: str
    value: str
    document_ids: list[str] = Field(default_factory=list)
    data_class: DataClass = DataClass.PUBLIC


class ConflictCandidateView(BaseModel):
    """Кандидат в противоречия в том виде, в котором его видит эксперт."""

    id: str
    subject: str
    property_name: str
    reason: str
    scope: dict[str, str] = Field(default_factory=dict)
    status: Literal["candidate", "confirmed", "dismissed"] = "candidate"
    left: ConflictSide
    right: ConflictSide
    decided_by: str | None = None
    decided_at: datetime | None = None


# ── FT-08: Claim versioning ─────────────────────────────────────────────────


class ClaimHistoryEntry(BaseModel):
    finding_id: str
    version: int
    statement: str
    status: str
    superseded_by: str | None = None
    reviewer_id: str | None = None
    review_date: str | None = None
    review_reason: str | None = None


class ClaimHistory(BaseModel):
    claim_id: str
    versions: list[ClaimHistoryEntry]


# ── FT-20/21: Учётные записи и доступ ───────────────────────────────────────


class EmailRequest(BaseModel):
    """Общая форма email для запросов входа и регистрации.

    Нормализация (strip + lower) здесь, а не только в хранилище: «Ivan@…» и
    «ivan@…» — один и тот же аккаунт на всём пути запроса.
    """

    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _EMAIL_RE.fullmatch(normalized):
            raise ValueError("Некорректный email")
        return normalized


class AccountRegisterRequest(EmailRequest):
    """Запрос саморегистрации. Минимальную длину пароля задаёт не схема, а
    ``settings.password_min_length`` — обработчик проверяет её после валидации."""

    display_name: str = Field(min_length=1, max_length=120)
    # Потолок нужен, чтобы scrypt не считал бесконечный ввод клиента.
    password: str = Field(min_length=1, max_length=256)


class AccountLoginRequest(EmailRequest):
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ProfileUpdateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class AccountInfo(BaseModel):
    """Ответ ``/api/v1/auth/*``: профиль плюс фактические возможности аккаунта.

    ``capabilities`` и ``data_classes`` всегда приходят из таблицы политик
    (services/governance.py), а не из сохранённых полей, поэтому интерфейс не
    может показать права, которых на самом деле нет.
    """

    id: str
    email: str
    display_name: str
    review_enabled: bool
    created_at: datetime
    capabilities: list[str] = Field(default_factory=list)
    data_classes: list[str] = Field(default_factory=list)


# ── FT-25: Dashboard models ────────────────────────────────────────────────


class ActivityEntry(BaseModel):
    action: str
    actor_id: str
    object_id: str
    outcome: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LlmUsageSummary(BaseModel):
    """Расход модели собственным аккаунтом за окно: «сколько стоили мои вопросы».

    Денег здесь нет намеренно: тарифной сетки GigaChat в конфигурации нет, и
    перевод токенов в рубли был бы выдуманной цифрой в продукте.
    """

    window_days: int = Field(ge=1)
    runs: int = Field(ge=0, description="Число записанных прогонов, а не обращений модели.")
    failed_runs: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class DashboardResponse(BaseModel):
    documents: int = Field(ge=0)
    claims: int = Field(ge=0)
    entities: int = Field(ge=0)
    evidence: int = Field(ge=0)
    conflicts: int = Field(ge=0)
    gaps: int = Field(ge=0)
    gaps_omitted: int = Field(default=0, ge=0)
    recent_activity: list[ActivityEntry]
    agent_metrics: AgentMetricsResponse


# ── FT-24: Notification models ─────────────────────────────────────────────


class Notification(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    topic: NotificationTopic
    message: str = Field(min_length=1, max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
