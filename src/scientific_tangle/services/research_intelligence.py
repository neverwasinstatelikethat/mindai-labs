from __future__ import annotations

from itertools import combinations, product

from scientific_tangle.domain.intelligence import (
    ConflictCandidate,
    IntelligenceReport,
    KnowledgeGap,
    ResearchClaim,
    ResearchSpace,
    ScopeDimension,
)


class ResearchIntelligenceService:
    """Строит проверяемые кандидаты конфликтов и пробелов из типизированных данных."""

    def analyze(
        self,
        claims: list[ResearchClaim],
        research_space: ResearchSpace,
    ) -> IntelligenceReport:
        return IntelligenceReport(
            conflicts=self.detect_conflicts(claims),
            gaps=self.detect_gaps(claims, research_space),
            analyzed_claims=len(claims),
        )

    def detect_conflicts(self, claims: list[ResearchClaim]) -> list[ConflictCandidate]:
        conflicts: list[ConflictCandidate] = []
        for left, right in combinations(claims, 2):
            if (left.subject_id, left.predicate, left.value.property_name) != (
                right.subject_id,
                right.predicate,
                right.value.property_name,
            ):
                continue
            shared_scope = self._shared_scope(left.scope, right.scope)
            if not self._scopes_compatible(left.scope, right.scope):
                continue
            ranges_are_disjoint = (
                left.value.max_value < right.value.min_value
                or right.value.max_value < left.value.min_value
            )
            if ranges_are_disjoint:
                conflicts.append(
                    ConflictCandidate(
                        left_claim_id=left.id,
                        right_claim_id=right.id,
                        property_name=left.value.property_name,
                        shared_scope=shared_scope,
                        reason=(
                            "Непересекающиеся нормализованные диапазоны при совместимых условиях; "
                            "требуется экспертная проверка."
                        ),
                    )
                )
        return conflicts

    def detect_gaps(
        self,
        claims: list[ResearchClaim],
        research_space: ResearchSpace,
    ) -> list[KnowledgeGap]:
        names = sorted(research_space.dimensions)
        covered = {
            tuple((name, scope[name]) for name in names)
            for claim in claims
            for scope in [self._scope_map(claim.scope)]
            if all(name in scope for name in names)
        }
        gaps: list[KnowledgeGap] = []
        value_sets = [research_space.dimensions[name] for name in names]
        for values in product(*value_sets):
            combination = tuple(zip(names, values, strict=True))
            if combination not in covered:
                gaps.append(
                    KnowledgeGap(
                        dimensions=[
                            ScopeDimension(name=name, value=value) for name, value in combination
                        ],
                        reason="В заданном research space нет evidence-backed утверждения.",
                    )
                )
        return gaps

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
