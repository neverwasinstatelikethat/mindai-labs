from __future__ import annotations

from scientific_tangle.domain.contracts import (
    ComparisonCell,
    ComparisonRequest,
    ComparisonRow,
    ComparisonTable,
    Finding,
)
from scientific_tangle.domain.models import NumericObservation


class ComparisonService:
    """Строит матрицу сравнения сущностей по числовым свойствам с evidence."""

    def compare(
        self, findings: list[Finding], request: ComparisonRequest
    ) -> ComparisonTable:
        relevant = self._filter_by_entities(findings, request.entities)
        groups = self._group_by_subject(relevant)
        headers = self._collect_headers(relevant, request.dimensions)
        rows = [
            ComparisonRow(
                item=subject,
                cells={
                    prop: self._extract_cell(group_findings, prop)
                    for prop in headers
                },
            )
            for subject, group_findings in groups.items()
        ]
        return ComparisonTable(
            question=request.question,
            headers=headers,
            rows=rows,
        )

    @staticmethod
    def _filter_by_entities(
        findings: list[Finding], entities: list[str]
    ) -> list[Finding]:
        if not entities:
            return findings
        lowered = [e.lower() for e in entities]
        return [
            finding
            for finding in findings
            if finding.subject
            and any(token in finding.subject.lower() for token in lowered)
        ]

    @staticmethod
    def _group_by_subject(findings: list[Finding]) -> dict[str, list[Finding]]:
        groups: dict[str, list[Finding]] = {}
        for finding in findings:
            key = finding.subject or finding.id
            groups.setdefault(key, []).append(finding)
        return groups

    @staticmethod
    def _collect_headers(
        findings: list[Finding], dimensions: list[str]
    ) -> list[str]:
        props: set[str] = set()
        for finding in findings:
            for obs in finding.observations:
                props.add(obs.property_name)
        if dimensions:
            props = props.intersection(dimensions)
        return sorted(props)

    @staticmethod
    def _extract_cell(
        findings: list[Finding], property_name: str
    ) -> ComparisonCell:
        for finding in findings:
            for obs in finding.observations:
                if obs.property_name != property_name:
                    continue
                return ComparisonCell(
                    value=ComparisonService._format_value(obs),
                    unit=obs.normalized_unit,
                    evidence=(
                        finding.evidence[0].quote if finding.evidence else None
                    ),
                    confidence=finding.confidence,
                )
        return ComparisonCell()

    @staticmethod
    def _format_value(obs: NumericObservation) -> str:
        if obs.operator == "between":
            return f"{obs.min_value}–{obs.max_value}"
        if obs.value is not None:
            return str(obs.value)
        return obs.raw_text
