"""Семантика ограничений retrieval — единственный источник истины для обоих бэкендов.

Числовые фильтры, ограничения по году/географии и признак демо-контента живут
здесь, а не продублированы в memory- и neo4j-ветках. Причина конкретна:
``numeric_filter`` применялся только в памяти, а production-ветка рапортовала
«доказательств после фильтрации: N» по нефильтрованному числу, то есть агент
утверждал эффект, которого не было.

Соглашение об неизвестности одинаковое для всех ограничений: отсутствие
измерения или документированного года — это неизвестность, а не нарушение,
поэтому такой тезис сохраняется и учитывается в ``unknown_*``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from scientific_tangle.domain.contracts import Finding
from scientific_tangle.domain.models import (
    NumericFilter,
    NumericObservation,
    QueryPlan,
)

# Ключи Finding.scope, которыми бэкенды документируют метаданные источника.
# Contract не расширяем: scope уже сериализуется вместе с находкой и доезжает
# до обоих хранилищ в finding_json. В scope кладём только настоящие условия
# применимости (год, территория): «research space» и детектор конфликтов
# строят измерения из scope, поэтому служебные ключи вроде пути к файлу туда
# добавлять нельзя — несущие разные значения пары перестали бы считаться
# сравнимыми и противоречия между источниками просто пропадали бы.
SCOPE_YEAR = "year"
SCOPE_GEOGRAPHY = "geography"
SCOPE_ORIGIN = "origin"

# Демо-контент (seed адаптера) обязателен для in-memory-контура и тестов, но не
# должен участвовать в ранжировании рабочего контура.
DEMO_ORIGIN = "demo"

# Одинаковый top_k для обоих бэкендов: раньше память брала 4, production — 6
# при идентичном вызове.
RETRIEVAL_TOP_K = 6

# Сколько кандидатов просить у rank_findings, чтобы фильтры имели из чего
# выбирать: без запаса ограничение по году срезало бы выдачу до нуля.
CANDIDATE_FANOUT = 3
CONSTRAINED_CANDIDATE_FANOUT = 6

# Топонимы планировщик возвращает и кодом, и названием; сверяем по нормализованной
# форме, иначе «RU» никогда не совпадёт с «Россия».
GEOGRAPHY_ALIASES: dict[str, str] = {
    "ru": "россия",
    "rf": "россия",
    "russia": "россия",
    "россия": "россия",
    "su": "россия",
    "sssr": "россия",
    "kz": "казахстан",
    "kazakhstan": "казахстан",
    "uz": "узбекистан",
}


def observation_matches(obs: NumericObservation, flt: NumericFilter) -> bool:
    """Сверяет observation с фильтром в единицах фильтра.

    При совпадении исходных единиц сравнение идёт по raw-значениям, иначе —
    по нормализованным; несопоставимые единицы консервативно исключаются.
    Диапазонное наблюдение против точечного порога (gt/gte/lt/lte/eq) решается
    пересечением интервала с условием: середина диапазона превращала бы решение
    в подбрасывание монеты (85–95 проходит gte 90, но не lt 90).
    """
    operation = flt.operator
    if obs.unit == flt.unit:
        value, low, high = obs.value, obs.min_value, obs.max_value
    elif obs.normalized_unit == flt.unit:
        value, low, high = obs.normalized_value, obs.normalized_min, obs.normalized_max
    else:
        return False
    if operation in {"between", "range"}:
        if flt.min_value is None or flt.max_value is None:
            return True
        if value is not None:
            return flt.min_value <= value <= flt.max_value
        if low is not None and high is not None:
            return low <= flt.max_value and high >= flt.min_value
        return True
    threshold = flt.value
    if threshold is None:
        return True
    if value is not None:
        match operation:
            case "eq":
                return abs(value - threshold) < 1e-9
            case "lt":
                return value < threshold
            case "lte":
                return value <= threshold
            case "gt":
                return value > threshold
            case "gte":
                return value >= threshold
            case _:
                return True
    if low is None or high is None:
        return True
    match operation:
        case "eq":
            return low <= threshold <= high
        case "lt":
            return low < threshold
        case "lte":
            return low <= threshold
        case "gt":
            return high > threshold
        case "gte":
            return high >= threshold
        case _:
            return True


def numeric_filter_keeps(finding: Finding, filters: Sequence[NumericFilter]) -> bool:
    """True, если находка совместима со всеми ограничениями.

    Нет observation для свойства — нет и нарушения: иначе фильтр «не числа»
    превращал бы количественный вопрос в пустую выдачу.
    """
    for flt in filters:
        matches = [
            observation
            for observation in finding.observations
            if observation.property_name == flt.property_name
            and (observation.normalized_unit == flt.unit or observation.unit == flt.unit)
        ]
        if not matches:
            continue
        if not any(observation_matches(observation, flt) for observation in matches):
            return False
    return True


def apply_numeric_filters(
    findings: Sequence[Finding], filters: Sequence[NumericFilter]
) -> list[Finding]:
    if not filters:
        return list(findings)
    return [finding for finding in findings if numeric_filter_keeps(finding, filters)]


def is_demo_finding(finding: Finding) -> bool:
    return finding.scope.get(SCOPE_ORIGIN) == DEMO_ORIGIN


def exclude_demo(findings: Iterable[Finding]) -> list[Finding]:
    """Скринг демо-контента в рабочем контуре.

    Seed-находки нужны in-memory-адаптеру (на них стоят тесты и бенчмарк), но в
    production-выдаче они притворяются реальными источниками горно-металлургического
    корпуса: их страницы и цитаты нельзя проверить, а значит трассировка тезиса
    до первоисточника на них фиктивная.
    """
    return [finding for finding in findings if not is_demo_finding(finding)]


def split_demo(findings: Iterable[Finding]) -> tuple[list[Finding], list[Finding]]:
    """(реальные источники, демо-контент) с сохранением порядка внутри групп."""
    real: list[Finding] = []
    demo: list[Finding] = []
    for finding in findings:
        (demo if is_demo_finding(finding) else real).append(finding)
    return real, demo


def documented_year(finding: Finding) -> int | None:
    """Год источника находки; None — источник без датировки, а не «год нулевой»."""
    raw = finding.scope.get(SCOPE_YEAR)
    if raw is None:
        return None
    try:
        year = int(raw)
    except ValueError:
        return None
    return year if 1800 <= year <= 2100 else None


def documented_geography(finding: Finding) -> str | None:
    raw = finding.scope.get(SCOPE_GEOGRAPHY)
    if not raw or not raw.strip():
        return None
    return raw.strip()


def normalize_geography(value: str) -> str:
    normalized = "".join(char if char.isalnum() else " " for char in value.lower())
    normalized = " ".join(normalized.replace("ё", "е").split())
    alias = GEOGRAPHY_ALIASES.get(normalized)
    return alias or normalized


def geography_matches(finding_value: str, wanted: Iterable[str]) -> bool:
    """Совпадение с подстрокой в обе стороны: «Кольский полуостров, Мурманская
    область» отвечает на запрос «Кольский», и наоборот."""
    haystack = normalize_geography(finding_value)
    tokens = haystack.split()
    for mention in wanted:
        needle = normalize_geography(mention)
        if len(needle) < 2:
            continue
        if needle in haystack:
            return True
        if any(needle in token or token in needle for token in tokens):
            return True
    return False


@dataclass(frozen=True, slots=True)
class ScopeEnforcement:
    """Что фильтр реально сделал с выборкой — для честного рапорта."""

    kept: list[Finding] = field(default_factory=list)
    dropped: int = 0
    unknown_year: int = 0
    unknown_geography: int = 0

    @property
    def constrains_anything(self) -> bool:
        return self.dropped > 0

    @property
    def undocumented(self) -> int:
        return self.unknown_year + self.unknown_geography


def enforce_scope(
    findings: Sequence[Finding], plan: QueryPlan
) -> ScopeEnforcement:
    """Ограничивает выборку по году и географии, когда они документированы."""
    wants_year = plan.year_from is not None or plan.year_to is not None
    wants_geography = bool(plan.countries)
    kept: list[Finding] = []
    dropped = 0
    unknown_year = 0
    unknown_geography = 0
    for finding in findings:
        year = documented_year(finding)
        geography = documented_geography(finding)
        if wants_year:
            if year is None:
                unknown_year += 1
            elif plan.year_from is not None and year < plan.year_from:
                dropped += 1
                continue
            elif plan.year_to is not None and year > plan.year_to:
                dropped += 1
                continue
        if wants_geography:
            if geography is None:
                unknown_geography += 1
            elif not geography_matches(geography, plan.countries):
                dropped += 1
                continue
        kept.append(finding)
    return ScopeEnforcement(
        kept=kept,
        dropped=dropped,
        unknown_year=unknown_year,
        unknown_geography=unknown_geography,
    )


def scope_notes(scope: ScopeEnforcement, plan: QueryPlan) -> list[str]:
    """Формулировки деградации: ограничение применено, но покрыто не всем корпусом."""
    notes: list[str] = []
    if plan.year_from is not None or plan.year_to is not None:
        window = f"{plan.year_from or '…'}–{plan.year_to or '…'}"
        if scope.unknown_year:
            notes.append(
                f"Диапазон лет {window}: у {scope.unknown_year} из {len(scope.kept)} "
                "доказательств год источника не документирован — ограничение по времени "
                "сузило только датированную часть корпуса."
            )
        if scope.dropped:
            notes.append(
                f"Вне диапазона лет {window} отброшено доказательств: {scope.dropped}."
            )
    if plan.countries:
        if scope.unknown_geography:
            notes.append(
                f"География {', '.join(plan.countries)}: у {scope.unknown_geography} из "
                f"{len(scope.kept)} доказательств территория источника не документирована — "
                "в выдаче могли остаться неподходящие по региону источники."
            )
    return notes


def numeric_notes(before: int, after: int) -> list[str]:
    """Число снятых с выдачи доказательств — то, что обязан увидеть исполнитель
    numeric_filter, а не «доказательств после фильтрации: N» по несрезанному списку."""
    removed = before - after
    if removed <= 0:
        return []
    return [f"Числовые ограничения сняли с выдачи доказательств: {removed}."]


def demo_notes(real: int, demo: int) -> list[str]:
    """Демо-корпус попал в выдачу — это надо назвать, а не выдавать за источники."""
    if not demo:
        return []
    if real:
        return [
            f"Доказательств из реального корпуса не осталось: {demo} позиций заняты "
            "демонстрационным seed-содержимым (origin=demo), а не документами из «Источники»."
        ]
    return [
        f"Вся выдача (доказательств: {demo}) состоит из демонстрационного seed-контента "
        "(origin=demo): это не источники предметной области и не подтверждается корпусом."
    ]


def cap_notes(candidates: int, kept: int, returned: int, top_k: int = RETRIEVAL_TOP_K) -> list[str]:
    """Сколько срезано на каком шаге: окно кандидатов → фильтры → потолок контекста."""
    notes: list[str] = []
    if kept < candidates:
        notes.append(
            f"Из {candidates} отобранных кандидатов ограничения сняли {candidates - kept}."
        )
    if returned < kept:
        notes.append(
            f"В контекст вошло {returned} из {kept} подходящих доказательств "
            f"(потолок выдачи top_k={top_k}); остальные не прочитаны, а не отброшены."
        )
    return notes


def global_context_notes(requested: bool, briefs: Sequence[str]) -> list[str]:
    """Global-контекст запрошен, но сообществ нет — молчать об этом нельзя."""
    if not requested or briefs:
        return []
    return [
        "Запрошен глобальный контекст, но сообщества графа не построены: "
        "сводки по кластерам отсутствуют, ответ опирается только на найденные доказательства."
    ]


def candidate_window(plan: QueryPlan, top_k: int = RETRIEVAL_TOP_K) -> int:
    """Запас кандидатов под запрос с ограничениями."""
    constrained = bool(plan.numeric_filters or plan.countries or plan.year_from or plan.year_to)
    fanout = CONSTRAINED_CANDIDATE_FANOUT if constrained else CANDIDATE_FANOUT
    return top_k * fanout


def dedupe_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Первое вхождение id значимо: порядок списков задаёт релевантность."""
    seen: set[str] = set()
    result: list[Finding] = []
    for finding in findings:
        if finding.id in seen:
            continue
        seen.add(finding.id)
        result.append(finding)
    return result


__all__ = [
    "CANDIDATE_FANOUT",
    "CONSTRAINED_CANDIDATE_FANOUT",
    "DEMO_ORIGIN",
    "RETRIEVAL_TOP_K",
    "SCOPE_GEOGRAPHY",
    "SCOPE_ORIGIN",
    "SCOPE_YEAR",
    "ScopeEnforcement",
    "apply_numeric_filters",
    "candidate_window",
    "cap_notes",
    "dedupe_findings",
    "demo_notes",
    "documented_geography",
    "documented_year",
    "enforce_scope",
    "exclude_demo",
    "geography_matches",
    "global_context_notes",
    "is_demo_finding",
    "normalize_geography",
    "numeric_filter_keeps",
    "numeric_notes",
    "observation_matches",
    "scope_notes",
    "split_demo",
]
