from __future__ import annotations

import re

from scientific_tangle.domain.contracts import (
    ComparisonCell,
    ComparisonRequest,
    ComparisonRow,
    ComparisonTable,
    Finding,
)
from scientific_tangle.domain.models import NumericObservation

# Слово-маркер несопоставимой ячейки: у ``ComparisonCell`` нет флага, а врать про
# единую единицу измерений матрица не вправе — интерфейс читает это как текст ячейки.
INCOMPARABLE_MARKER = "единицы измерений различаются"


class ComparisonService:
    """Строит матрицу сравнения сущностей по числовым свойствам с evidence."""

    def compare(self, findings: list[Finding], request: ComparisonRequest) -> ComparisonTable:
        relevant = self._filter_by_entities(findings, request.entities)
        groups = self._group_by_subject(relevant)
        headers = self._collect_headers(relevant, request.dimensions)
        rows = [
            ComparisonRow(
                item=subject,
                cells={prop: self._extract_cell(group_findings, prop) for prop in headers},
            )
            for subject, group_findings in groups.items()
        ]
        return ComparisonTable(
            question=request.question,
            headers=headers,
            rows=rows,
        )

    @staticmethod
    def _filter_by_entities(findings: list[Finding], entities: list[str]) -> list[Finding]:
        if not entities:
            return findings
        # Подстрока тянула в столбец «Fe» строки Fe2O3 и FeS: сравнение разных
        # веществ в одной матрице даёт ложный вывод. Совпадение ищет целым токеном.
        patterns = [
            re.compile(rf"(?<!\w){re.escape(entity.strip().lower())}(?!\w)")
            for entity in entities
            if entity.strip()
        ]
        return [
            finding
            for finding in findings
            if finding.subject
            and any(pattern.search(finding.subject.lower()) for pattern in patterns)
        ]

    @staticmethod
    def _group_by_subject(findings: list[Finding]) -> dict[str, list[Finding]]:
        groups: dict[str, list[Finding]] = {}
        for finding in findings:
            key = finding.subject or finding.id
            groups.setdefault(key, []).append(finding)
        return groups

    @staticmethod
    def _collect_headers(findings: list[Finding], dimensions: list[str]) -> list[str]:
        props: set[str] = set()
        for finding in findings:
            for obs in finding.observations:
                props.add(obs.property_name)
        # Запрошенное измерение остаётся колонкой даже без данных: прежнее
        # пересечение молча снимало столбец, и было не видно, что показать нечего.
        return sorted(props | {item.strip() for item in dimensions if item.strip()})

    @classmethod
    def _extract_cell(cls, findings: list[Finding], property_name: str) -> ComparisonCell:
        matches = [
            (finding, obs)
            for finding in findings
            for obs in finding.observations
            if obs.property_name == property_name
        ]
        if not matches:
            return ComparisonCell()
        # Первое совпадение прятало расхождение источников: в матрице оно обязано
        # быть видно, а не заменённым наиболее удачным числом.
        units = {obs.normalized_unit for _, obs in matches}
        if len(units) == 1:
            # Одно нормализованное измерение — значения сравнимы напрямую, и
            # расхождение вида «70 и 70000» остаётся расхождением в тексте ячейки.
            distinct = list(dict.fromkeys(cls._format_value(obs) for _, obs in matches))
            unit = next(iter(units))
            value = " / ".join(distinct)
        else:
            # Подписывать такую ячейку единицей первого совпадения — значит прятать
            # расхождение в разы: «0.3» в g/L и «300» в mg/L это одно число, а
            # «70» и «70000» в разных единицах — возможно, разные величины. Каждый
            # источник показывается со СВОЕЙ единицей, а общая единица снимается,
            # чтобы интерфейс не выдавал несопоставимое за сопоставленное.
            distinct = list(
                dict.fromkeys(
                    f"{obs.value} {obs.unit} "
                    f"({cls._format_value(obs)} {obs.normalized_unit})"
                    for _, obs in matches
                )
            )
            unit = None
            value = f"{INCOMPARABLE_MARKER}: {' / '.join(distinct)}"
        best = max(matches, key=lambda item: item[0].confidence)[0]
        return ComparisonCell(
            value=value,
            unit=unit,
            evidence=(best.evidence[0].quote if best.evidence else None),
            confidence=best.confidence,
        )

    _OPERATOR_SIGNS: dict[str, str] = {"gte": "≥", "lte": "≤", "gt": ">", "lt": "<", "eq": "="}

    @classmethod
    def _format_value(cls, obs: NumericObservation) -> str:
        # Оператор — часть утверждения: «≥95» и «95» в таблице сравнения значат
        # разное, раньше он терялся.
        if obs.operator == "between":
            return f"{obs.normalized_min}–{obs.normalized_max}"
        sign = cls._OPERATOR_SIGNS.get(obs.operator, "")
        if obs.normalized_value is not None:
            return f"{sign}{obs.normalized_value}"
        return obs.raw_text
