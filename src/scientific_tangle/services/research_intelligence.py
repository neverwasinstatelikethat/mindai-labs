from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations, product
from math import prod

from scientific_tangle.domain.contracts import Finding
from scientific_tangle.domain.intelligence import (
    ComparableValue,
    ConflictCandidate,
    IntelligenceReport,
    KnowledgeGap,
    KnowledgeKind,
    ResearchClaim,
    ResearchSpace,
    ScopeDimension,
)
from scientific_tangle.domain.models import NumericObservation

# Полный декартов произведение измерений перечисляет все необследованные комбинации;
# без потолка на один запрос с 4 измерениями по 3 значения приходило 54 «пробела»,
# и все они попадали в ответ пользователю.
DEFAULT_GAP_LIMIT = 12

# Порог, с которого измерение считается «широким»: при 8 значениях и трёх других
# измерениях декартов произведение уходит в тысячи комбинаций, и показатель
# непокрытых начинает описывать размер матрицы, а не реальную неполноту корпуса.
MAX_VALUES_PER_DIMENSION = 8

# Сколько комбинаций просмотрено на один вызов: total считается аналитически,
# а перебор ограничен, иначе широкое пространство превращает gap_scan в
# минутный цикл внутри агентного дедлайна.
MAX_GAP_COMBINATIONS_SCANNED = 5000

# Имена измерений резервной матрицы «субъект × числовое свойство»: условия
# применимости размечены не в каждом тезисе, но сравнивать субъекты по набору
# показателей можно и по реальным полям тезиса — они же служат ключами матрицы.
SUBJECT_DIMENSION = "subject"
PROPERTY_DIMENSION = "property"


@dataclass(frozen=True, slots=True)
class GapSummary:
    gaps: list[KnowledgeGap]
    omitted: int
    total: int
    covered: int = 0
    # True — просмотр части комбинаций не состоялся (упор в бюджет перебора):
    # omitted тогда верхняя оценка, и выдавать её за точный список нельзя.
    partial: bool = False


def _dimension_value(claim: ResearchClaim, name: str) -> str | None:
    """Значение измерения тезиса: поля самого тезиса либо условие применимости."""
    if name == SUBJECT_DIMENSION:
        return claim.subject_id
    if name == PROPERTY_DIMENSION:
        return claim.value.property_name
    for item in claim.scope:
        if item.name == name:
            return item.value
    return None


def build_research_space(claims: list[ResearchClaim]) -> ResearchSpace | None:
    """Собирает research space только из реальных данных пула.

    Приоритет — условия применимости (``scope``): они и есть смысл «при каких
    условиях измерено». Если размечено меньше двух измерений, берётся матрица
    «субъект × числовое свойство» — тоже из полей тезисов. Синтетических
    измерений здесь нет намеренно: пространство из ``plan.entity_mentions``
    давало ``total == covered`` и инструмент возвращал 0 при любом корпусе.
    """
    dimensions: dict[str, set[str]] = defaultdict(set)
    for claim in claims:
        for dimension in claim.scope:
            dimensions[dimension.name].add(dimension.value)
    if len(dimensions) >= 2:
        return ResearchSpace(dimensions={k: sorted(v) for k, v in dimensions.items()})
    subjects = {claim.subject_id for claim in claims}
    properties = {claim.value.property_name for claim in claims}
    if len(subjects) >= 2 and len(properties) >= 2:
        return ResearchSpace(
            dimensions={
                SUBJECT_DIMENSION: sorted(subjects),
                PROPERTY_DIMENSION: sorted(properties),
            }
        )
    if not dimensions:
        return None
    return ResearchSpace(dimensions={k: sorted(v) for k, v in dimensions.items()})


def comparable_groups(
    claims: Sequence[ResearchClaim],
) -> dict[tuple[str, str, str, str], list[ResearchClaim]]:
    """Группы сопоставимых тезисов: субъект × предикат × свойство × единица.

    Единица измерения в ключе обязательна: «0,2–0,3 г/л» и «200–300 мг/л» —
    один и тот же показатель в разных шкалах, а не противоречие источников.
    """
    groups: dict[tuple[str, str, str, str], list[ResearchClaim]] = defaultdict(list)
    for item in claims:
        key = (item.subject_id, item.predicate, item.value.property_name, item.value.unit)
        groups[key].append(item)
    return dict(groups)


class ResearchIntelligenceService:
    """Строит проверяемые кандидаты конфликтов и пробелов из типизированных данных."""

    def analyze(
        self,
        claims: list[ResearchClaim],
        research_space: ResearchSpace | None,
        gap_limit: int = DEFAULT_GAP_LIMIT,
    ) -> IntelligenceReport:
        summary = (
            self.summarize_gaps(claims, research_space, gap_limit)
            if research_space
            else GapSummary(gaps=[], omitted=0, total=0)
        )
        return IntelligenceReport(
            conflicts=self.detect_conflicts(claims),
            gaps=summary.gaps,
            analyzed_claims=len(claims),
        )

    def detect_conflicts(self, claims: list[ResearchClaim]) -> list[ConflictCandidate]:
        """Пары сравниваются только внутри групп «субъект × предикат × свойство × единица».

        Конфликт — расхождение чисел об одном показателе одного субъекта, а
        объединённый пул всех раундов легко даёт сотни тезисов: квадрат по всему
        пулу сравнивал бы заведомо несопоставимые пары ради тех же самых пар.
        """
        conflicts: list[ConflictCandidate] = []
        for group in comparable_groups(claims).values():
            for left, right in combinations(group, 2):
                if not self._scopes_compatible(left.scope, right.scope):
                    continue
                shared_scope = self._shared_scope(left.scope, right.scope)
                disjoint = (
                    left.value.max_value < right.value.min_value
                    or right.value.max_value < left.value.min_value
                )
                if not disjoint:
                    continue
                same_source = left.finding_id == right.finding_id
                conflicts.append(
                    ConflictCandidate(
                        left_claim_id=left.id,
                        right_claim_id=right.id,
                        property_name=left.value.property_name,
                        shared_scope=shared_scope,
                        reason=(
                            f"{_format_range(left.value)} против {_format_range(right.value)} "
                            + (
                                "внутри одного тезиса — источник приводит противоречивые числа."
                                if same_source
                                else "в непересекающихся диапазонах при совместимых условиях "
                                "применимости; требуется экспертная проверка источников."
                            )
                        ),
                    )
                )
        return conflicts

    def summarize_gaps(
        self,
        claims: Sequence[ResearchClaim],
        research_space: ResearchSpace | None,
        limit: int | None = DEFAULT_GAP_LIMIT,
    ) -> GapSummary:
        if not research_space:
            return GapSummary(gaps=[], omitted=0, total=0, covered=0)
        names = sorted(research_space.dimensions)
        covered: set[tuple[tuple[str, str], ...]] = set()
        for claim in claims:
            picked: list[str] = []
            for name in names:
                value = _dimension_value(claim, name)
                if value is None:
                    break
                picked.append(value)
            else:
                covered.add(tuple(zip(names, picked, strict=True)))
        value_sets = [research_space.dimensions[name] for name in names]
        # Размер матрицы считается аналитически: перечислять декартов произведение
        # ради одного числа — источник минутных пауз на широком пространстве.
        total = prod(len(values) for values in value_sets)
        gaps: list[KnowledgeGap] = []
        scanned = 0
        partial = False
        # Комбинации перебираются детерминированно (product по отсортированным
        # измерениям), поэтому отсечение по лимиту воспроизводимо между запросами.
        for values in product(*value_sets):
            if limit is not None and len(gaps) >= limit:
                break
            if scanned >= MAX_GAP_COMBINATIONS_SCANNED:
                partial = True
                break
            scanned += 1
            combination = tuple(zip(names, values, strict=True))
            if combination in covered:
                continue
            gaps.append(
                KnowledgeGap(
                    dimensions=[
                        ScopeDimension(name=name, value=value) for name, value in combination
                    ],
                    reason="В заданном research space нет evidence-backed утверждения.",
                )
            )
        return GapSummary(
            gaps=gaps,
            omitted=max(total - len(covered) - len(gaps), 0),
            total=total,
            covered=len(covered),
            partial=partial,
        )

    @staticmethod
    def _scope_map(scope: list[ScopeDimension]) -> dict[str, str]:
        return {item.name: item.value for item in scope}

    @classmethod
    def _scopes_compatible(
        cls,
        left: list[ScopeDimension],
        right: list[ScopeDimension],
    ) -> bool:
        left_map = cls._scope_map(left)
        right_map = cls._scope_map(right)
        return all(left_map[key] == right_map[key] for key in left_map.keys() & right_map.keys())

    @classmethod
    def _shared_scope(
        cls,
        left: list[ScopeDimension],
        right: list[ScopeDimension],
    ) -> list[ScopeDimension]:
        left_map = cls._scope_map(left)
        right_map = cls._scope_map(right)
        return [
            ScopeDimension(name=key, value=left_map[key])
            for key in sorted(left_map.keys() & right_map.keys())
            if left_map[key] == right_map[key]
        ]


def _format_range(value: object) -> str:
    low = getattr(value, "min_value", 0.0)
    high = getattr(value, "max_value", 0.0)
    unit = getattr(value, "unit", "")
    if low == high:
        return f"{low:g} {unit}".strip()
    return f"{low:g}–{high:g} {unit}".strip()


def to_research_claims(findings: Sequence[Finding]) -> list[ResearchClaim]:
    """Преобразует findings с числовыми наблюдениями в типизированные тезисы.

    Идентичность тезиса уникальна на уровне наблюдения, а происхождение хранится
    в ``finding_id``: иначе два наблюдения одного finding давали «конфликт X vs X».
    Наблюдения без какого-либо числового значения пропускаются — подстановка 0.0
    превращала отсутствие данных в диапазон [0,0] и порождала ложные конфликты.
    Вход — объединённый пул доказательств, а не список из одного действия.
    """
    claims: list[ResearchClaim] = []
    for finding in findings:
        if not finding.observations or not finding.evidence:
            continue
        scope = [ScopeDimension(name=k, value=v) for k, v in finding.scope.items()]
        kind = (
            KnowledgeKind.EXPERT_VALIDATED
            if finding.status == "consensus"
            else KnowledgeKind.EXTRACTED
        )
        evidence_ids = [str(item.document_id) for item in finding.evidence]
        for index, observation in enumerate(finding.observations):
            bounds = observation_bounds(observation)
            if bounds is None:
                continue
            low, high, unit = bounds
            claims.append(
                ResearchClaim(
                    id=f"{finding.id}#{observation.property_name}#{index}",
                    finding_id=finding.id,
                    subject_id=finding.subject or finding.id,
                    predicate=finding.predicate or "HAS_PROPERTY",
                    value=ComparableValue(
                        property_name=observation.property_name,
                        min_value=low,
                        max_value=high,
                        unit=unit,
                    ),
                    scope=scope,
                    evidence_ids=evidence_ids,
                    knowledge_kind=kind,
                )
            )
    return claims


def observation_bounds(obs: NumericObservation) -> tuple[float, float, str] | None:
    """Нормализованные границы приоритетны; при их отсутствии берутся исходные."""
    if obs.normalized_min is not None and obs.normalized_max is not None:
        return obs.normalized_min, obs.normalized_max, obs.normalized_unit
    if obs.normalized_value is not None:
        return obs.normalized_value, obs.normalized_value, obs.normalized_unit
    if obs.min_value is not None and obs.max_value is not None:
        return obs.min_value, obs.max_value, obs.unit
    if obs.value is not None:
        return obs.value, obs.value, obs.unit
    return None
