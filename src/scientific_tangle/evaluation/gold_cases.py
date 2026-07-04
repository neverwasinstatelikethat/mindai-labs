"""Реальные gold-кейсы для benchmark оценки retrieval на документах корпуса.

Каждый кейс основан на фактическом содержании документа из папки
«Источники информации/Обзоры». Запросы сформулированы на русском языке
и соответствуют вопросам, которые задаёт исследователь-металлург.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RealGoldCase:
    """Один gold-кейс для retrieval-бенчмарка."""

    query: str
    expected_entities: list[str]
    expected_keywords: list[str]
    source_documents: list[str]
    category: str


# ── Заголовки документов (совпадают с Path(filename).stem из парсера) ──

SLAG = "Обеднение_шлаков"
CU_EW = "ОИП-05-2019 Параметры Cu EW"
HEAP_LEACH = "ТИ-5-2017. Кучное выщелачивание в условиях холодного климата"
CHLOR_LEACH = "Хлорное выщелачивание ОИП 02-2024"
FE_REMOVAL = "Очистка от Fe 2020"
NI_CO_SULFATE = "ОИП-01-2022 Обзор существующих технологий получения сульфатов никеля и кобальта"
CU_NI_MATTE = "Обзор пеработка медно-никелевых штейнов (обжиг-выщелачивание) фул"
LI_PROD = "ОИП-06-2022 Технологии производства лития из рудного сырья"
PB_REMOVAL = "ОИП-04-2022 Удаление свинца"
CYAN_PGM = "Цианидное выщелачивание МПГ"


REAL_GOLD_CASES: list[RealGoldCase] = [
    # 1. Обеднение шлаков
    RealGoldCase(
        query="Какие параметры влияют на обеднение металлургических шлаков?",
        expected_entities=["магнетит", "флотация", "El Teniente", "штейн", "конвертер"],
        expected_keywords=["обеднение", "шлак", "медь", "восстановление", "угл"],
        source_documents=[SLAG],
        category="slag_cleaning",
    ),
    # 2. Электроэкстракция меди
    RealGoldCase(
        query="Какие параметры определяют электроэкстракцию меди?",
        expected_entities=["электроэкстракция", "катод", "электролит", "анод", "Cu EW"],
        expected_keywords=["электроэкстракция", "медь", "катод", "ток", "напряжение"],
        source_documents=[CU_EW],
        category="electrowinning",
    ),
    # 3. Кучное выщелачивание в холодном климате
    RealGoldCase(
        query="Как холодный климат влияет на кучное выщелачивание?",
        expected_entities=["кучное выщелачивание", "медь", "Михеевское", "Томинское"],
        expected_keywords=["кучное", "выщелачивание", "холодный", "климат", "температура"],
        source_documents=[HEAP_LEACH],
        category="heap_leaching",
    ),
    # 4. Хлорное выщелачивание никеля
    RealGoldCase(
        query="Какие технологические схемы применяют для хлорного выщелачивания никеля?",
        expected_entities=["хлорное выщелачивание", "файнштейн", "Nikkelverk", "Niihama", "Sandouville"],
        expected_keywords=["хлорное", "выщелачивание", "никель", "HCl", "файнштейн"],
        source_documents=[CHLOR_LEACH],
        category="chloride_leaching",
    ),
    # 5. Удаление железа из растворов
    RealGoldCase(
        query="Какими способами удаляют железо из технологических растворов?",
        expected_entities=["железо", "гематит", "ярозит", "гетит", "осаждение"],
        expected_keywords=["железо", "гематит", "ярозит", "осаждение", "раствор"],
        source_documents=[FE_REMOVAL],
        category="iron_removal",
    ),
    # 6. Сульфаты никеля и кобальта
    RealGoldCase(
        query="Какие технологии используют для получения сульфатов никеля и кобальта?",
        expected_entities=["сульфат никеля", "сульфат кобальта", "MHP", "Sumitomo", "Terrafame"],
        expected_keywords=["сульфат", "никель", "кобальт", "батарей", "выщелачивание"],
        source_documents=[NI_CO_SULFATE],
        category="sulfate_production",
    ),
    # 7. Переработка медно-никелевых штейнов
    RealGoldCase(
        query="Как перерабатывают медно-никелевые штейны обжигом и выщелачиванием?",
        expected_entities=["штейн", "обжиг", "выщелачивание", "Хибинетт", "Nikkelverk"],
        expected_keywords=["штейн", "обжиг", "выщелачивание", "медь", "никель"],
        source_documents=[CU_NI_MATTE],
        category="matte_processing",
    ),
    # 8. Производство лития из рудного сырья
    RealGoldCase(
        query="Какие технологии применяют для производства лития из рудного сырья?",
        expected_entities=["литий", "сподумен", "Li2CO3", "LiOH", "автоклав"],
        expected_keywords=["литий", "сподумен", "выщелачивание", "спекание", "рассол"],
        source_documents=[LI_PROD],
        category="lithium_production",
    ),
    # 9. Удаление свинца из растворов
    RealGoldCase(
        query="Какими методами удаляют свинец из металлургических потоков?",
        expected_entities=["свинец", "Cyanex-301", "экстракция", "Nikkelverk", "хлорид"],
        expected_keywords=["свинец", "очистка", "экстракция", "хлоридный", "раствор"],
        source_documents=[PB_REMOVAL],
        category="lead_removal",
    ),
    # 10. Цианидное выщелачивание МПГ
    RealGoldCase(
        query="Как цианидное выщелачивание применяют для металлов платиновой группы?",
        expected_entities=["цианидное выщелачивание", "МПГ", "платина", "палладий", "Panton"],
        expected_keywords=["цианид", "выщелачивание", "платина", "палладий", "Albion"],
        source_documents=[CYAN_PGM],
        category="pgm_leaching",
    ),
    # 11. Дополнительный: соединения железа при осаждении
    RealGoldCase(
        query="В виде каких соединений осаждают железо из технологических растворов?",
        expected_entities=["гематит", "гетит", "ярозит", "оксигидроксиды"],
        expected_keywords=["гематит", "гетит", "ярозит", "осаждение", "оксид"],
        source_documents=[FE_REMOVAL],
        category="iron_removal",
    ),
    # 12. Дополнительный: переработка сподумена
    RealGoldCase(
        query="Какие способы переработки сподумена существуют?",
        expected_entities=["сподумен", "спекание", "серная кислота", "автоклав", "β-сподумен"],
        expected_keywords=["сподумен", "спекание", "кислота", "автоклав", "выщелачивание"],
        source_documents=[LI_PROD],
        category="lithium_production",
    ),
]
