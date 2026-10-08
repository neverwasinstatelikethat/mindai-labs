from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from scientific_tangle.domain.intelligence import DataClass
from scientific_tangle.domain.models import EvidenceLocator


class HypothesisSignal(BaseModel):
    """Короткий аналитический вывод с проверяемыми доказательствами."""

    id: UUID
    kind: str = Field(min_length=1)
    statement: str = Field(min_length=1, max_length=600)
    proposal: str | None = Field(default=None, max_length=600)
    confidence: float = Field(ge=0, le=1)
    data_class: DataClass
    evidence: list[EvidenceLocator] = Field(min_length=1)

    @field_validator("kind", "statement")
    @classmethod
    def strip_nonempty_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("значение не должно быть пустым")
        return cleaned

    @field_validator("proposal")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        cleaned = value.strip() if value is not None else None
        return cleaned or None


class HypothesisWindow(BaseModel):
    signals: list[HypothesisSignal]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)

    @classmethod
    def from_signals(
        cls,
        signals: list[HypothesisSignal],
        *,
        allowed_data_classes: set[DataClass],
        limit: int,
        offset: int,
    ) -> HypothesisWindow:
        visible = [
            signal for signal in signals if signal.data_class in allowed_data_classes
        ]
        ordered = sorted(visible, key=lambda item: (item.kind, item.statement, str(item.id)))
        return cls(
            signals=ordered[offset : offset + limit],
            total=len(ordered),
            limit=limit,
            offset=offset,
        )
