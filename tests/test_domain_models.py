from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from scientific_tangle.domain.models import Claim, EvidenceLocator, NumericObservation


def evidence() -> EvidenceLocator:
    return EvidenceLocator(
        document_id=uuid4(),
        page=3,
        char_start=10,
        char_end=32,
        quote="Сульфаты не более 300 мг/л",
    )


def test_claim_requires_exactly_one_object_representation() -> None:
    with pytest.raises(ValidationError):
        Claim(
            id=uuid4(),
            subject_id=uuid4(),
            predicate="OPERATES_AT",
            extraction_confidence=0.91,
            valid_from=datetime.now(UTC),
            evidence=[evidence()],
        )


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


def test_evidence_rejects_invalid_offsets() -> None:
    with pytest.raises(ValidationError):
        EvidenceLocator(
            document_id=uuid4(),
            page=1,
            char_start=20,
            char_end=10,
            quote="Некорректный диапазон",
        )
