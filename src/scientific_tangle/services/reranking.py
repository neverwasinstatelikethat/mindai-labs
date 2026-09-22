"""Детерминированный re-rank слитых кандидатов retrieval.

RRF даёт порядок по согласию списков, но не смотрит на содержимое: в топ могли
попасть находки без единого совпадения с запросом. Здесь к слитым кандидатам
применяются только локальные сигналы — лексическое перекрытие, совпадение
источника и subject, наличие числовых наблюдений и согласие сообществ.

LLM не вызывается (контур провайдера — ответственность другого модуля), новые
зависимости не добавляются: скоринг должен переживать недоступность модели и
оставать измеримым тем же бенчмарком recall@3/MRR/NDCG@3.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from math import log

from scientific_tangle.domain.contracts import Finding

# Веса согласованы с памятью-бэкендом: там те же сигналы входят в базовый скор.
W_FUSION = 0.45
W_LEXICAL = 0.30
W_SUBJECT = 0.10
W_SOURCE = 0.10
W_NUMERIC = 0.05
W_COMMUNITY = 0.08

# Насыщение лексического перекрытия: после пяти совпавших токенов рост не должен
# перебивать согласие источников.
_LEXICAL_SATURATION = 5.0


@dataclass(frozen=True, slots=True)
class RerankSignals:
    """Компоненты оценки одной находки — чтобы их можно было объяснить в тесте."""

    fusion: float = 0.0
    lexical: float = 0.0
    subject: float = 0.0
    source: float = 0.0
    numeric: float = 0.0
    community: float = 0.0
    total: float = 0.0


@dataclass(frozen=True, slots=True)
class RerankResult:
    findings: list[Finding] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    signals: dict[str, RerankSignals] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """Ни один кандидат не набрал содержательного сигнала — выдача случайна."""
        return bool(self.findings) and all(score <= 0 for score in self.scores.values())


def query_tokens(query: str) -> set[str]:
    """Токены запроса длиной от 4 символов: служебные слова не должны ранжировать."""
    return {token.lower().strip(".,;:!?()[]{}«»\"'") for token in query.split() if len(token) >= 4}


def _lexical_overlap(tokens: set[str], finding: Finding) -> float:
    if not tokens:
        return 0.0
    statement = finding.statement.lower()
    evidence = " ".join(item.quote for item in finding.evidence).lower()
    hits = sum(1 for token in tokens if token in statement or token in evidence)
    return min(hits, _LEXICAL_SATURATION) / _LEXICAL_SATURATION


def _subject_agreement(tokens: set[str], query: str, finding: Finding) -> float:
    subject = (finding.subject or "").lower().strip()
    if not subject:
        return 0.0
    if subject in query.lower():
        return 1.0
    hits = sum(1 for token in tokens if token in subject)
    return min(hits, 2) / 2.0


def _source_agreement(tokens: set[str], finding: Finding) -> float:
    titles = " ".join(item.source_title for item in finding.evidence).lower()
    if not titles or not tokens:
        return 0.0
    hits = sum(1 for token in tokens if token in titles)
    return min(hits, 2) / 2.0


def _numeric_support(finding: Finding) -> float:
    """Есть числовые наблюдения — тезис трассируем до условия, а не до слова."""
    observations = finding.observations
    if not observations:
        return 0.0
    units = {item.normalized_unit for item in observations}
    return min(1.0, log(1 + len(observations)) / log(4)) + (0.1 if len(units) > 1 else 0.0)


def rerank_findings(
    query: str,
    findings: Sequence[Finding],
    *,
    top_k: int,
    fusion: Mapping[str, float] | None = None,
    communities: Mapping[str, str] | None = None,
) -> RerankResult:
    """Пересобирает порядок слитых кандидатов по локальным сигналам.

    ``communities`` — соответствия ``finding.id → имя сообщества`` из уже
    посчитанного графа; согласие сообществ считается жадно по отобранным, чтобы
    подкрепляющие друг друга доказательства шли рядом, а не размывались.
    """
    tokens = query_tokens(query)
    best_fusion = max(fusion.values(), default=0.0) if fusion else 0.0
    scored: dict[str, RerankSignals] = {}
    for finding in findings:
        raw_fusion = (fusion or {}).get(finding.id, 0.0)
        signals = RerankSignals(
            fusion=raw_fusion / best_fusion if best_fusion else 0.0,
            lexical=_lexical_overlap(tokens, finding),
            subject=_subject_agreement(tokens, query, finding),
            source=_source_agreement(tokens, finding),
            numeric=_numeric_support(finding),
        )
        scored[finding.id] = signals

    # Обход в порядке id: при равных оценках побеждает стабильный разбор,
    # а не порядок входа в словарь.
    remaining = sorted(findings, key=lambda finding: finding.id)
    selected: list[Finding] = []
    taken_communities: set[str] = set()
    while remaining and len(selected) < max(top_k, 0):
        best_index = 0
        best_total = float("-inf")
        for index, finding in enumerate(remaining):
            signals = scored[finding.id]
            community = (communities or {}).get(finding.id)
            agree = 1.0 if community and community in taken_communities else 0.0
            total = (
                W_FUSION * signals.fusion
                + W_LEXICAL * signals.lexical
                + W_SUBJECT * signals.subject
                + W_SOURCE * signals.source
                + W_NUMERIC * signals.numeric
                + W_COMMUNITY * agree
            )
            scored[finding.id] = replace(signals, community=agree, total=total)
            if total > best_total:
                best_total = total
                best_index = index
        chosen = remaining.pop(best_index)
        selected.append(chosen)
        community = (communities or {}).get(chosen.id)
        if community:
            taken_communities.add(community)

    scores = {finding_id: signals.total for finding_id, signals in scored.items()}
    return RerankResult(findings=selected, scores=scores, signals=scored)


__all__ = ["RerankResult", "RerankSignals", "query_tokens", "rerank_findings"]
