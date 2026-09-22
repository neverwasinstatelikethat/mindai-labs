"""Метрики retrieval-бенчмарка: recall, hit и NDCG считаются по определениям.

Тесты нужны потому, что корпус (`Источники информации/`) на этой машине отсутствует,
и прогон `run_benchmark.py` даёт лишь «нет findings». Ошибку в формуле это не
поймало бы, а здесь она видна напрямую.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from scientific_tangle.domain.contracts import Finding
from scientific_tangle.domain.models import EvidenceLocator
from scientific_tangle.evaluation.run_benchmark import TOP_K_VALUES, compute_case_metrics

TOP_K = TOP_K_VALUES


def _f(source: str) -> Finding:
    """Находка, трассированная к одному именованному источнику."""
    return Finding(
        id=f"finding-{source}",
        statement=f"Тезис: {source}",
        confidence=0.8,
        evidence=[
            EvidenceLocator(
                document_id=uuid5(UUID(int=1), source),
                source_title=source,
                page=1,
                quote="Обратный осмос даёт 70 % обессоливания",
            )
        ],
    )


def test_recall_counts_missed_sources_not_only_the_first_hit() -> None:
    """Из двух ожидаемых найден один — это recall 0.5, а не 1.0 (прежний hit-rate)."""
    metrics = compute_case_metrics([_f("A")], {"A", "B"}, TOP_K)
    assert metrics["recall@3"] == 0.5
    assert metrics["hit@3"] == 1.0


def test_perfect_retrieval_scores_one_everywhere() -> None:
    metrics = compute_case_metrics([_f("A"), _f("B"), _f("C")], {"A", "B", "C"}, TOP_K)
    assert metrics["recall@3"] == 1.0
    assert metrics["precision@3"] == 1.0
    assert metrics["mrr"] == 1.0
    assert metrics["ndcg@3"] == 1.0


def test_ndcg_penalizes_sources_that_never_appeared() -> None:
    """Идеал берётся по известным релевантным: один найденный из трёх не может быть 1.0."""
    metrics = compute_case_metrics([_f("A"), _f("X"), _f("Y")], {"A", "B", "C"}, TOP_K)
    assert metrics["hit@3"] == 1.0
    assert metrics["ndcg@3"] < 0.6
    assert metrics["recall@3"] == 1 / 3


def test_mrr_uses_first_relevant_rank() -> None:
    metrics = compute_case_metrics([_f("X"), _f("A"), _f("B")], {"A", "B"}, TOP_K)
    assert metrics["mrr"] == 0.5
    assert metrics["precision@3"] == 2 / 3


def test_nothing_retrieved_is_zero_not_an_error() -> None:
    metrics = compute_case_metrics([], {"A", "B"}, TOP_K)
    assert metrics["recall@3"] == 0.0
    assert metrics["hit@3"] == 0.0
    assert metrics["mrr"] == 0.0
    assert metrics["ndcg@3"] == 0.0


def test_empty_expectation_does_not_divide_by_zero() -> None:
    metrics = compute_case_metrics([_f("A")], set(), TOP_K)
    assert metrics["recall@3"] == 0.0
    assert metrics["ndcg@3"] == 0.0
