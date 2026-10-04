"""Формы домена, общие для графа, извлечения и оценки.

Живой носитель утверждения в продукте — ``contracts.Finding``: у него есть
``status`` (consensus/disputed/hypothesis), ``version``/``superseded_by`` для
версионирования и ``observations`` с числовыми условиями. Отдельные ``Claim`` и
``KnowledgeStatus`` здесь когда-то описывали то же самое вторым словарём, не
импортировались ни из одного модуля ``src`` и только создавали соблазн разойтись
с реальным состоянием графа, поэтому удалены.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


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
        # Форма — одна на оператор. `observation_bounds` берёт сначала нормализованные
        # границы, затем min/max: случайно заполненные границы у `eq` превращают точку
        # в интервал, и детектор конфликтов сравнивает уже не те числа.
        bounds = (self.min_value, self.max_value, self.normalized_min, self.normalized_max)
        scalars = (self.value, self.normalized_value)
        if self.operator == "between":
            if any(item is not None for item in scalars):
                raise ValueError(
                    "between задаётся границами: value и normalized_value не заполняются"
                )
            low, high, norm_low, norm_high = bounds
            if low is None or high is None or norm_low is None or norm_high is None:
                raise ValueError("between требует min_value, max_value и нормализованные границы")
            if low > high or norm_low > norm_high:
                raise ValueError("нижняя граница не может превышать верхнюю")
            return self
        if any(item is not None for item in bounds):
            raise ValueError(f"{self.operator} — одно значение; min/max принадлежат between")
        if self.value is None or self.normalized_value is None:
            raise ValueError(f"{self.operator} требует value и normalized_value")
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
            raise ValueError(f"Оператор {self.operator} требует min_value и max_value")
        return self


# Глубина обхода по умолчанию: 0 от модели читается как «не указано»,
# и это же значение стоит как default поля.
_DEFAULT_MAX_HOPS = 3


class QueryPlan(BaseModel):
    question: str = Field(min_length=3)
    # Язык плана — факт входящего запроса, а не вывод модели: planning_agent
    # проставляет его сам. Без значения по умолчанию пролёт падал на валидации
    # всякий раз, когда модель не пересказывала язык.
    language: Literal["ru", "en"] = "ru"
    mode: Literal["local", "global", "hybrid"] = "hybrid"
    entity_mentions: list[str] = Field(default_factory=list)
    numeric_filters: list[NumericFilter] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    year_from: int | None = Field(default=None, ge=1800, le=2100)
    year_to: int | None = Field(default=None, ge=1800, le=2100)
    max_hops: int = Field(default=_DEFAULT_MAX_HOPS, ge=1, le=4)

    @field_validator("year_from", "year_to", mode="before")
    @classmethod
    def _absent_year_is_no_filter(cls, value: object) -> object:
        """Нулевой год у модели означает «фильтра нет», а не «год 0».

        Приёмка 4 октября на живом GigaChat: план приходил с `year_from: 0`,
        `ge=1800` читал это как испорченный год, ремонт вывода не помогал
        (модель повторяла то же), и весь агентный запрос умирал с
        `ModelUnavailableError`. Настояще негодное значение (1700) по-прежнему
        остаётся ошибкой: терпимость касается только явных пустых маркеров.
        """
        return None if value in (0, -1, "0", "-1") else value

    @field_validator("max_hops", mode="before")
    @classmethod
    def _zero_hops_is_default(cls, value: object) -> object:
        return _DEFAULT_MAX_HOPS if value in (0, "0", None, "") else value

    @model_validator(mode="after")
    def validate_years(self) -> QueryPlan:
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from не может быть больше year_to")
        return self
