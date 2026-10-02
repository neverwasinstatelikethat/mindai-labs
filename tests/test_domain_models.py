"""Формы домена: локатор доказательства, числовое наблюдение и живой статус тезиса.

``Finding`` — единственный носитель утверждения в рабочем контуре (у него есть
``status``, ``version``/``superseded_by`` и ``observations``), поэтому проверка
«один объект утверждения» переведена на него: прежний тест обращался к мёртвому
``domain.models.Claim``, который не импортировался ни из одного модуля ``src`` и
описывал тот же словарь статусов второй копией.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from scientific_tangle.domain.contracts import Finding
from scientific_tangle.domain.models import EvidenceLocator, NumericObservation

DOC_ID = uuid4()


def evidence() -> EvidenceLocator:
    return EvidenceLocator(
        document_id=DOC_ID,
        page=3,
        char_start=10,
        char_end=32,
        quote="Сульфаты не более 300 мг/л",
    )


def _finding(**overrides: object) -> Finding:
    fields: dict[str, object] = {
        "id": "finding-1",
        "statement": "Сульфаты не более 300 мг/л",
        "confidence": 0.91,
        "evidence": [evidence()],
    }
    fields.update(overrides)
    return Finding(**fields)  # type: ignore[arg-type]


def test_finding_status_accepts_only_the_live_dictionary() -> None:
    """Словарь статусов тезиса — ``consensus``/``disputed``/``hypothesis``.

    Пятый «статус знаний» (proposed/accepted/rejected/superseded) в продукте живёт
    у экспертных решений и предложений, а не у тезиса: смешивать их в одном поле
    нельзя, и второго носителя больше нет.
    """
    for status in ("consensus", "disputed", "hypothesis"):
        assert _finding(status=status).status == status

    with pytest.raises(ValidationError):
        _finding(status="superseded")


def test_superseded_claim_is_a_versioned_finding_not_a_separate_type() -> None:
    """Замена тезиса выражается парой ``version`` + ``superseded_by`` на ``Finding``."""
    original = _finding()
    replacement = _finding(
        id="finding-2",
        statement="Сульфаты не более 250 мг/л",
        status="consensus",
    )
    superseded = original.model_copy(
        update={
            "status": "disputed",
            "version": 2,
            "superseded_by": replacement.id,
            "reviewer_id": "expert-1",
            "review_date": datetime.now(UTC).date().isoformat(),
            "review_reason": "Экспертное замечание: источник переиздан.",
        }
    )

    assert superseded.version == 2
    assert superseded.superseded_by == replacement.id
    assert superseded.review_reason is not None
    assert replacement.superseded_by is None


def test_numeric_range_preserves_source_and_normalized_units() -> None:
    observation = NumericObservation(
        property_name="sulfate_concentration",
        operator="between",
        min_value=200,
        max_value=300,
        unit="mg/L",
        normalized_min=0.2,
        normalized_max=0.3,
        normalized_unit="g/L",
        raw_text="сульфаты 200–300 мг/л",
    )

    assert observation.normalized_min == pytest.approx(0.2)
    assert observation.normalized_max == pytest.approx(0.3)


def test_scalar_operator_rejects_bounds() -> None:
    """Точка и интервал — разные формы: случайно заполненные границы ломают детектор."""
    with pytest.raises(ValidationError):
        NumericObservation(
            property_name="sulfate_concentration",
            operator="eq",
            value=300,
            min_value=200,
            unit="mg/L",
            normalized_value=0.3,
            normalized_unit="g/L",
            raw_text="сульфаты 300 мг/л",
        )


def test_evidence_rejects_invalid_offsets() -> None:
    with pytest.raises(ValidationError):
        EvidenceLocator(
            document_id=DOC_ID,
            page=1,
            char_start=20,
            char_end=10,
            quote="Некорректный диапазон",
        )


def test_evidence_without_address_is_not_traceable() -> None:
    """Ни страницы, ни листа — и цитату некуда проверить: такая форма отвергается."""
    with pytest.raises(ValidationError):
        EvidenceLocator(
            document_id=DOC_ID,
            quote="Сульфаты не более 300 мг/л",
        )
