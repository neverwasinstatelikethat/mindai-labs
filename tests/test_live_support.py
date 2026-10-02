"""Порог боевого корпуса проверяется офлайн: решение не зависит от хранилища.

Живые тесты без поднятого контура пропускаются, поэтому таблица решений порога
тестируется здесь — иначе правило «пустой прогон не выдаётся за измерение»
осталось бы проверкой, которую никто не исполнял.
"""

from __future__ import annotations

import pytest

from scientific_tangle.domain.contracts import DocumentRequest, ExtractionResult
from scientific_tangle.domain.intelligence import DataClass
from tests.live_support import SEED_CONFIRMATION, corpus_gate, seed_corpus


def test_non_empty_corpus_is_read_without_writes() -> None:
    gate = corpus_gate(documents=3, findings=7, seed_mode="")

    assert gate.decision == "read"


def test_empty_corpus_without_confirmation_skips_instead_of_passing() -> None:
    """Ноль документов и ноль находок — «не измеряли», а не «измерили и сошлось».

    Любая форма подтверждения, кроме точного слова, обязана дать пропуск: `1`,
    `yes` и значение в верхнем регистре не считаются разрешением писать в базу.
    """
    for seed_mode in ("", "0", "1", "yes", "True", SEED_CONFIRMATION.upper()):
        gate = corpus_gate(documents=0, findings=0, seed_mode=seed_mode)
        assert gate.decision == "skip", seed_mode
        assert "проверять нечего" in gate.reason, seed_mode


def test_documents_without_findings_is_an_anomaly_not_a_vacuum() -> None:
    """Документы есть, доказательств нет — каталог не восстановился.

    Засевка здесь запретила бы себя сама: она спрятала бы поломку хранения под
    «мы же измерили». Причина обязана называть аномалию, а не пустоту.
    """
    gate = corpus_gate(documents=12, findings=0, seed_mode=SEED_CONFIRMATION)

    assert gate.decision == "skip"
    assert "аномалия" in gate.reason
    assert "12" in gate.reason


def test_confirmed_throwaway_store_may_be_seeded() -> None:
    gate = corpus_gate(documents=0, findings=0, seed_mode=SEED_CONFIRMATION)

    assert gate.decision == "seed"


def test_seed_corpus_is_deterministic_and_comparable() -> None:
    """Засевка даёт два разных по году, территории и классу документа — с выпиской.

    Разные условия применимости нужны, чтобы ограничения плана и ACL-срез
    исполнялись на настоящих данных, детерминированность — чтобы повторный прогон
    не плодил третью копию корпуса, а непересекающиеся диапазоны одного и того же
    свойства — чтобы на одноразовом контуре нашлось и противоречие.
    """
    (first, first_x), (second, second_x) = seed_corpus()
    again = seed_corpus()

    assert (first, first_x) == again[0]
    assert (second, second_x) == again[1]
    assert first.year != second.year
    assert first.geography != second.geography
    assert first.data_class == DataClass.PUBLIC
    assert second.data_class == DataClass.INTERNAL
    assert "97" in first.text and "70" in second.text
    # Пустая выписка превратила бы засеянный контур в «документы без доказательств»
    # — ровно ту аномалию, которую порог отказывается маскировать записью.
    assert len(first_x.claims) == 2 and len(second_x.claims) == 2
    assert all(
        claim.observations for extraction in (first_x, second_x) for claim in extraction.claims
    )
    # Общая сущность: два документа должны соединяться в графе, а не лежать парой
    # изолированных снимков.
    assert {entity.canonical_name for entity in first_x.entities} & {
        entity.canonical_name for entity in second_x.entities
    }

    def salt_range(extraction: ExtractionResult) -> tuple[float, float]:
        observation = next(
            item
            for claim in extraction.claims
            for item in claim.observations
            if item.property_name == "salt_rejection"
        )
        return float(observation.normalized_min), float(observation.normalized_max)

    (a_low, a_high), (b_low, b_high) = salt_range(first_x), salt_range(second_x)
    assert a_high < b_low or b_high < a_low


def test_seed_corpus_passes_the_strict_schema() -> None:
    """Схема продукта строже тестовой: засевка обязана пройти её без поблажек.

    Цитата каждого тезиса — подстрока текста документа: трассировка до
    первоисточника проверяется даже на засевке, иначе живой тест доказывал бы
    находки, которых в источнике нет.
    """
    for document, extraction in seed_corpus():
        rebuilt_document = DocumentRequest.model_validate(document.model_dump())
        rebuilt_extraction = ExtractionResult.model_validate(extraction.model_dump())

        assert rebuilt_document == document
        assert rebuilt_extraction == extraction
        assert len(rebuilt_document.text) >= 20
        for claim in extraction.claims:
            assert claim.evidence_quote in document.text


@pytest.mark.parametrize("decision", ["read", "seed", "skip"])
def test_gate_decisions_are_a_closed_set(decision: str) -> None:
    """Три исхода исчерпывающие: четвёртого путь живого теста не имеет."""
    counts = {
        "read": {"documents": 1, "findings": 1},
        "seed": {"documents": 0, "findings": 0},
        "skip": {"documents": 0, "findings": 0},
    }[decision]
    gate = corpus_gate(
        seed_mode=SEED_CONFIRMATION if decision == "seed" else "",
        **counts,
    )

    assert gate.decision == decision
