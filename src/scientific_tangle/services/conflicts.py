"""Очередь противоречий: пары тезисов и экспертное решение по ним.

Кандидаты не хранятся — их каждый раз считает детектор по текущему корпусу. В этом
и смысл очереди: после импорта нового документа вчерашняя пара, для которой нашлось
третье подтверждение или сопоставимые условия, обязана перестать быть противоречием,
а не висеть в списке «рассмотренным». Durable-часть одна — решение эксперта,
привязанное к отпечатку пары (см. ``domain/intelligence.py``).

Корпус читают ``all_findings`` с уже отфильтрованным срезом прав: паре, где одна
находка restricted, не место в очереди аккаунта без ``restricted:read``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from scientific_tangle.domain.contracts import (
    ConflictCandidateView,
    ConflictReview,
    ConflictSide,
    Finding,
)
from scientific_tangle.domain.intelligence import (
    ConflictCandidate,
    DataClass,
    ResearchClaim,
)
from scientific_tangle.services.research_intelligence import (
    ResearchIntelligenceService,
    format_range,
    to_research_claims,
)
from scientific_tangle.services.retrieval_semantics import is_demo_finding

# Ограничение очереди, а не детектора: пар в корпусе с сотнями наблюдений
# бывает тысячи, и экран эксперта обязан открываться, а не перебирать всё.
# Нерассмотренные пары ставятся первыми, поэтому отсечение не прячет работу.
CONFLICT_QUEUE_CEILING = 500

_INTELLIGENCE = ResearchIntelligenceService()


@dataclass(frozen=True, slots=True)
class ConflictResolution:
    """Пара, на которую отвечает эксперт, и находки, которых касается решение."""

    candidate: ConflictCandidate
    left_finding_id: str
    right_finding_id: str


def _claims_and_findings(
    findings: Sequence[Finding],
) -> tuple[dict[str, ResearchClaim], dict[str, Finding]]:
    """Тезисы и находки очереди без демо-содержимого.

    Seed-тезисы витрины — не источники корпуса: их цитату нельзя проверить,
    поэтому и «противоречие» между ними фиктивное. Production-бэкенд снимает их
    в ``all_findings``, в in-memory контуре это обязан делать потребитель.
    """
    corpus = [finding for finding in findings if not is_demo_finding(finding)]
    claims = to_research_claims(corpus)
    return (
        {claim.id: claim for claim in claims},
        {finding.id: finding for finding in corpus},
    )


def _side(claim: ResearchClaim, by_finding: dict[str, Finding]) -> ConflictSide:
    finding = by_finding.get(claim.finding_id)
    return ConflictSide(
        claim_id=claim.id,
        finding_id=claim.finding_id,
        statement=finding.statement if finding is not None else "",
        value=format_range(claim.value),
        document_ids=claim.evidence_ids,
        data_class=finding.data_class if finding is not None else DataClass.PUBLIC,
    )


def _sort_key(view: ConflictCandidateView) -> tuple[int, str, str]:
    """Нерассмотренные пары — в начало, внутри группы порядок детерминированный."""
    return (0 if view.status == "candidate" else 1, view.subject, view.property_name)


def candidate_views(
    findings: Sequence[Finding],
    reviews: Sequence[ConflictReview],
    *,
    ceiling: int = CONFLICT_QUEUE_CEILING,
) -> list[ConflictCandidateView]:
    """Кандидаты корпуса с наложенными решениями экспертов.

    Статус берётся из хранимого решения, а не из того, что поставил детектор:
    ``ConflictCandidate`` рождается на каждый прогон со статусом ``candidate``,
    и без наложения очередь показывала бы подтверждённое противоречие как
    нерассмотренное.
    """
    by_claim, by_finding = _claims_and_findings(findings)
    decisions = {review.candidate_id: review for review in reviews}
    views: list[ConflictCandidateView] = []
    for candidate in _INTELLIGENCE.detect_conflicts(list(by_claim.values())):
        left = by_claim.get(candidate.left_claim_id)
        right = by_claim.get(candidate.right_claim_id)
        if left is None or right is None:
            continue
        review = decisions.get(str(candidate.id))
        views.append(
            ConflictCandidateView(
                id=str(candidate.id),
                subject=left.subject_id,
                property_name=candidate.property_name,
                reason=candidate.reason,
                scope={item.name: item.value for item in candidate.shared_scope},
                status=review.status if review is not None else "candidate",
                left=_side(left, by_finding),
                right=_side(right, by_finding),
                decided_by=review.actor_id if review is not None else None,
                decided_at=review.created_at if review is not None else None,
            )
        )
    views.sort(key=_sort_key)
    return views[: max(ceiling, 1)]


def resolve_for_review(candidate_id: str, findings: Sequence[Finding]) -> ConflictResolution | None:
    """Находит пару по отпечатку и отдаёт id обеих находок.

    Отпечаток пересчитывается по текущему корпусу: если пара исчезла (тезис
    заменён, документ удалён, условия применимости перестали совпадать),
    решения записывать не за что, и снаружи это 404, а не молчаливая запись.
    """
    by_claim, by_finding = _claims_and_findings(findings)
    for candidate in _INTELLIGENCE.detect_conflicts(list(by_claim.values())):
        if str(candidate.id) != candidate_id:
            continue
        left = by_claim.get(candidate.left_claim_id)
        right = by_claim.get(candidate.right_claim_id)
        if left is None or right is None:
            return None
        if left.finding_id not in by_finding or right.finding_id not in by_finding:
            return None
        return ConflictResolution(
            candidate=candidate,
            left_finding_id=left.finding_id,
            right_finding_id=right.finding_id,
        )
    return None
