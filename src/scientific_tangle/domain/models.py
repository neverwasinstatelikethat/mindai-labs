from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class KnowledgeStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class EvidenceLocator(BaseModel):
    document_id: UUID
    source_title: str = "Источник"
    page: int | None = Field(default=None, ge=1)
    sheet: str | None = None
    cell_range: str | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_offsets(self) -> EvidenceLocator:
        if self.char_end is not None and self.char_start is None:
            raise ValueError("char_start обязателен при наличии char_end")
        if self.char_start is not None and self.char_end is not None:
            if self.char_end <= self.char_start:
                raise ValueError("char_end должен быть больше char_start")
        if self.page is None and self.sheet is None:
            raise ValueError("evidence должен указывать страницу или лист")
        return self


class NumericObservation(BaseModel):
    property_name: str = Field(min_length=1)
    operator: Literal["eq", "lt", "lte", "gt", "gte", "between"]
    value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    unit: str = Field(min_length=1)
    normalized_value: float | None = None
    normalized_min: float | None = None
    normalized_max: float | None = None
    normalized_unit: str = Field(min_length=1)
    raw_text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_shape(self) -> NumericObservation:
        if self.operator == "between":
            if self.min_value is None or self.max_value is None:
                raise ValueError("between требует min_value и max_value")
            if self.min_value > self.max_value:
                raise ValueError("min_value не может превышать max_value")
            if self.normalized_min is None or self.normalized_max is None:
                raise ValueError("between требует нормализованные границы")
        elif self.value is None or self.normalized_value is None:
            raise ValueError(f"{self.operator} требует value и normalized_value")
        return self


class Claim(BaseModel):
    id: UUID
    subject_id: UUID
    predicate: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    object_id: UUID | None = None
    literal_value: str | None = None
    polarity: Literal["positive", "negative"] = "positive"
    status: KnowledgeStatus = KnowledgeStatus.PROPOSED
    extraction_confidence: Annotated[float, Field(ge=0, le=1)]
    version: int = Field(default=1, ge=1)
    valid_from: datetime
    evidence: list[EvidenceLocator] = Field(min_length=1)
    observations: list[NumericObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_object(self) -> Claim:
        if (self.object_id is None) == (self.literal_value is None):
            raise ValueError("claim должен иметь ровно один object_id или literal_value")
        return self


class NumericFilter(BaseModel):
    property_name: str = Field(min_length=1)
    operator: Literal["eq", "lt", "lte", "gt", "gte", "between", "range"]
    value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    unit: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_operator_fields(self) -> NumericFilter:
        if self.operator in {"eq", "lt", "lte", "gt", "gte"} and self.value is None:
            raise ValueError(f"Оператор {self.operator} требует значение value")
        if self.operator in {"between", "range"} and (
            self.min_value is None or self.max_value is None
        ):
            raise ValueError(
                f"Оператор {self.operator} требует min_value и max_value"
            )
        return self


class QueryPlan(BaseModel):
    question: str = Field(min_length=3)
    language: Literal["ru", "en"]
    mode: Literal["local", "global", "hybrid"] = "hybrid"
    entity_ids: list[UUID] = Field(default_factory=list)
    entity_mentions: list[str] = Field(default_factory=list)
    numeric_filters: list[NumericFilter] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    year_from: int | None = Field(default=None, ge=1800, le=2100)
    year_to: int | None = Field(default=None, ge=1800, le=2100)
    max_hops: int = Field(default=3, ge=1, le=4)

    @model_validator(mode="after")
    def validate_years(self) -> QueryPlan:
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from не может быть больше year_to")
        return self
