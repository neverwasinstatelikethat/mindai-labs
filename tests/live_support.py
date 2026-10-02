"""Порог боевого корпуса и засебка одноразового контура для живых тестов.

Живой тест на пустом хранилище зелёный и ничего не доказывает: пустое окно
сходится с пустым каталогом, пустой граф сходится с пустой статистикой. Это ровно
тот «зелёный вместо измерения», из-за которого в проекте появился отдельный
инвариант про вакуум. Поэтому прогон против реальных Neo4j и Elasticsearch требует
непустой корпус, а на пустом имеет три исхода: читать, засевать или честно
пропустить с названием причины.

Засевка возможна только на явно одноразовом контуре: у storage-слоя нет пространства
имён на прогон (индексы заданы модульными константами, отдельной Neo4j-базы нет),
а удаления документа в продукте нет — значит засеянное в рабочий корпус аналитиков
нечем убрать. Подтверждение требует не «1», а слово `throwaway`: ровно для того,
чтобы опечатка в CI-переменной не оставила мусор в живой базе.
"""

from __future__ import annotations

from dataclasses import dataclass

from scientific_tangle.domain.contracts import (
    DocumentRequest,
    ExtractedClaim,
    ExtractedEntity,
    ExtractionResult,
    NodeType,
)
from scientific_tangle.domain.intelligence import DataClass

# Значение `LIVE_SEED`, разрешающее запись в хранилище.
SEED_CONFIRMATION = "throwaway"

SKIP_REASON = (
    "боевой корпус пуст: проверять нечего. Для одноразового контура подними "
    "LIVE_SEED=throwaway — тесты засееют структуру сами; в рабочий корпус "
    "аналитиков не пишут"
)


@dataclass(frozen=True)
class CorpusGate:
    """Исход порога: что делать с живым хранилищем до первой проверки."""

    decision: str
    reason: str


def corpus_gate(*, documents: int, findings: int, seed_mode: str) -> CorpusGate:
    """Порог по числу документов и доказательств в боевом хранилище.

    Разрешена засевка только когда пусто совсем: документы есть, а находок нет —
    это не «нечего читать», а поломка восстановления каталога, и маскировать её
    записью нельзя. Такой прогон пропускается с причиной, называющей аномалию.
    """
    if documents > 0 and findings > 0:
        return CorpusGate("read", "боевой корпус непустой: читаем как есть")
    if documents > 0:
        return CorpusGate(
            "skip",
            f"документов {documents}, а доказательств ноль: каталог находок пуст "
            "при непустом корпусе — это аномалия хранения, а не пустой контур",
        )
    if seed_mode == SEED_CONFIRMATION:
        return CorpusGate("seed", "корпус пуст, разрешена засебка одноразового контура")
    return CorpusGate("skip", SKIP_REASON)


def _observation(
    property_name: str, low: float, high: float, unit: str, raw: str
) -> dict[str, object]:
    """Числовое условие в форме, которую принимает схема продукта."""
    return {
        "property_name": property_name,
        "operator": "between",
        "min_value": low,
        "max_value": high,
        "unit": unit,
        "normalized_min": low,
        "normalized_max": high,
        "normalized_unit": unit,
        "raw_text": raw,
    }


def seed_corpus() -> list[tuple[DocumentRequest, ExtractionResult]]:
    """Два детерминированных документа с выпиской — вместе с доказательствами.

    Разные годы, разная территория и разный класс доступа нужны затем, чтобы
    ограничения плана и ACL-срез исполнялись на настоящих данных. Структурный
    импорт (`index_document`) выписку не делает: на одноразовом контуре он дал бы
    документы с нулём находок, то есть ровно ту аномалию хранения, которую порог
    выше отказывается маскировать. Поэтому засевка идёт через `ingest` и приносит
    готовую `ExtractionResult` — без обращения к модели, детерминированно.

    Диапазоны задержания солей (96.5–97.5 против 70–72) при одном субъекте и
    свойстве не пересекаются намеренно: на этом же корпусе должны находиться и
    противоречие, и общее свойство двух документов.
    """
    membrane = ExtractedEntity(
        name="Мембрана обратного осмоса",
        canonical_name="Мембрана обратного осмоса",
        type=NodeType.EQUIPMENT,
    )
    brine = ExtractedEntity(
        name="Шахтная вода", canonical_name="Шахтная вода", type=NodeType.MATERIAL
    )
    first = DocumentRequest(
        title="Протокол пилота обессоливания шахтной воды (засебка CI)",
        text=(
            "Пилотная установка обратного осмоса на шахтной воде показала "
            "задержание солей 97 процентов при минерализации 3200 мг/л и "
            "удельном расходе 3.4 кВт·ч на кубический метр. Сухой остаток на "
            "выходе — 96 мг/л. Испытания велись зимой при температуре "
            "питания 6 градусов."
        ),
        geography="Мурманская область",
        year=2024,
        data_class=DataClass.PUBLIC,
    )
    second = DocumentRequest(
        title="Мембранный метод сравнительный отчёт (засебка CI)",
        text=(
            "Сравнительный отчёт по той же схеме: задержание солей составило "
            "70 процентов при минерализации 1800 мг/л и удельном расходе "
            "2.1 кВт·ч на кубический метр. Сухой остаток на выходе — 540 мг/л. "
            "Отчёт внутренний, подготовлен лабораторией водоподготовки."
        ),
        geography="Кемеровская область",
        year=2021,
        data_class=DataClass.INTERNAL,
    )
    return [
        (
            first,
            ExtractionResult(
                entities=[membrane, brine],
                claims=[
                    ExtractedClaim(
                        subject="Мембрана обратного осмоса",
                        predicate="HAS_PROPERTY",
                        object="salt_rejection",
                        statement=(
                            "Мембрана обратного осмоса задерживает соли на 97 % "
                            "при минерализации питания 3200 мг/л."
                        ),
                        confidence=0.9,
                        evidence_quote="задержание солей 97 процентов",
                        observations=[
                            _observation(
                                "salt_rejection", 96.5, 97.5, "%", "97 процентов"
                            )
                        ],
                    ),
                    ExtractedClaim(
                        subject="Мембрана обратного осмоса",
                        predicate="HAS_PROPERTY",
                        object="specific_energy",
                        statement=(
                            "Удельный расход энергии установки — 3.4 кВт·ч "
                            "на кубический метр."
                        ),
                        confidence=0.85,
                        evidence_quote="удельном расходе 3.4 кВт·ч на кубический метр",
                        observations=[
                            _observation(
                                "specific_energy",
                                3.3,
                                3.5,
                                "kWh/m3",
                                "3.4 кВт·ч на кубический метр",
                            )
                        ],
                    ),
                ],
            ),
        ),
        (
            second,
            ExtractionResult(
                entities=[membrane, brine],
                claims=[
                    ExtractedClaim(
                        subject="Мембрана обратного осмоса",
                        predicate="HAS_PROPERTY",
                        object="salt_rejection",
                        statement=(
                            "Мембранная схема задерживает соли на 70 % при "
                            "минерализации питания 1800 мг/л."
                        ),
                        confidence=0.8,
                        evidence_quote="задержание солей составило 70 процентов",
                        observations=[
                            _observation(
                                "salt_rejection", 70.0, 72.0, "%", "70 процентов"
                            )
                        ],
                    ),
                    ExtractedClaim(
                        subject="Мембрана обратного осмоса",
                        predicate="HAS_PROPERTY",
                        object="specific_energy",
                        statement=(
                            "Удельный расход энергии схемы — 2.1 кВт·ч "
                            "на кубический метр."
                        ),
                        confidence=0.8,
                        evidence_quote="удельном расходе 2.1 кВт·ч на кубический метр",
                        observations=[
                            _observation(
                                "specific_energy",
                                2.0,
                                2.2,
                                "kWh/m3",
                                "2.1 кВт·ч на кубический метр",
                            )
                        ],
                    ),
                ],
            ),
        ),
    ]
