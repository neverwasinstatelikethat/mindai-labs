"""Перестройка графа знаний с семантическими связями.

Модуль создаёт богатый доменно-ориентированный граф, покрывающий
основные области металлургии и обогащения:
  - Водоподготовка
  - Гидрометаллургия (кучное, хлорное, цианидное выщелачивание)
  - Пирометаллургия (плавка, конвертирование, обеднение шлака)
  - Электролиз (медь, никель)
  - Очистка растворов (удаление Fe, Pb, As)
  - Получение солей (сульфаты Ni/Co, литий)
  - Переработка штейнов (файнштейн, медно-никелевые штейны)

Граф записывается в Neo4j и дополняет существующие LLM-извлечённые сущности.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from neo4j import Driver

from scientific_tangle.domain.contracts import NodeType
from scientific_tangle.domain.relations import (
    RelationContractError,
    spec_for,
    validate_edge,
)
from scientific_tangle.services.knowledge import stable_uuid

logger = logging.getLogger(__name__)

# ── Структуры данных ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DomainEntity:
    """Описание сущности доменного графа."""

    id: str
    label: str
    entity_type: str
    domain: str
    aliases: str = ""


@dataclass(frozen=True, slots=True)
class DomainEdge:
    """Семантическое отношение между сущностями."""

    source: str
    target: str
    relation: str
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class DomainClaim:
    """Утверждение, извлечённое из экспертного знания."""

    id: str
    label: str
    subject: str
    predicate: str
    object: str
    statement: str
    source_title: str
    domain: str
    confidence: float = 0.85


# ── Определения сущностей по доменам ────────────────────────────────────────

# Префикс для доменных сущностей, чтобы не конфликтовать с LLM-извлечёнными
P = "dom-"


def _ent(eid: str, label: str, etype: str, domain: str, aliases: str = "") -> DomainEntity:
    return DomainEntity(
        id=f"{P}{eid}", label=label, entity_type=etype, domain=domain, aliases=aliases
    )


ENTITIES: list[DomainEntity] = [
    # ═══ Водоподготовка ═══
    _ent("water", "Шахтная вода", "material", "water_treatment", "mine water, дренажная вода"),
    _ent("treated-water", "Очищенная вода", "material", "water_treatment"),
    _ent("brine", "Рассол (концентрат)", "material", "water_treatment"),
    _ent("permeate", "Пермеат", "material", "water_treatment"),
    _ent("sulfates-cond", "Сульфаты 200-300 мг/л", "condition", "water_treatment"),
    _ent("chlorides-cond", "Хлориды 200-300 мг/л", "condition", "water_treatment"),
    _ent("dry-residue", "Сухой остаток ≤1000 мг/л", "condition", "water_treatment"),
    _ent("min-temp", "Температура питания >8 °C", "condition", "water_treatment"),
    _ent("ro-pressure", "Давление 10-60 бар", "condition", "water_treatment"),
    _ent("ro", "Обратный осмос", "process", "water_treatment", "reverse osmosis, RO"),
    _ent("nanofiltration", "Нанофильтрация", "process", "water_treatment"),
    _ent("ultrafiltration", "Ультрафильтрация", "process", "water_treatment"),
    _ent("ion-exchange", "Ионный обмен", "process", "water_treatment", "IX, ионообмен"),
    _ent("evaporation", "Выпаривание", "process", "water_treatment", "蒸发, evaporator"),
    _ent("ro-membrane", "RO-мембрана", "equipment", "water_treatment"),
    _ent("ix-resin", "Ионообменная смола", "equipment", "water_treatment"),
    _ent("evaporator", "Выпарной аппарат", "equipment", "water_treatment"),
    _ent("ro-pilot", "Пилотная установка RO", "equipment", "water_treatment"),
    _ent("water-lab", "Лаборатория водоподготовки", "expert", "water_treatment"),
    # ═══ Гидрометаллургия ═══
    _ent("oxide-ore", "Окисленные руды", "material", "hydrometallurgy"),
    _ent("malachite", "Малахит", "material", "hydrometallurgy"),
    _ent("azurite", "Азурит", "material", "hydrometallurgy"),
    _ent("h2so4", "Серная кислота", "material", "hydrometallurgy", "H2SO4, кислота"),
    _ent("nacl", "Хлорид натрия", "material", "hydrometallurgy", "NaCl, поваренная соль"),
    _ent("pregnant-solution", "Продуктивный раствор", "material", "hydrometallurgy"),
    _ent("barren-solution", "Отработанный раствор", "material", "hydrometallurgy"),
    _ent("cathode-cu", "Катодная медь", "material", "hydrometallurgy"),
    _ent("heap-leach", "Кучное выщелачивание", "process", "hydrometallurgy", "КВ, heap leaching"),
    _ent("vat-leach", "Чановое выщелачивание", "process", "hydrometallurgy"),
    _ent("chloride-leach", "Хлорное выщелачивание", "process", "hydrometallurgy"),
    _ent("cyanide-leach", "Цианидное выщелачивание", "process", "hydrometallurgy"),
    _ent("sx", "Экстракция растворителем", "process", "hydrometallurgy", "solvent extraction"),
    _ent("heap-pad", "Куча выщелачивания", "equipment", "hydrometallurgy"),
    _ent("leach-vat", "Чан выщелачивания", "equipment", "hydrometallurgy"),
    _ent("sx-mixer", "Смеситель-отстойник SX", "equipment", "hydrometallurgy"),
    _ent("irrigation-system", "Система орошения", "equipment", "hydrometallurgy"),
    _ent("leach-temp", "Температура выщелачивания 15-35 °C", "condition", "hydrometallurgy"),
    _ent("leach-ph", "pH 1.5-2.0", "condition", "hydrometallurgy"),
    _ent("leach-time", "Время выщелачивания 60-90 сут", "condition", "hydrometallurgy"),
    _ent("cold-climate", "Холодный климат", "condition", "hydrometallurgy"),
    # ═══ Пирометаллургия ═══
    _ent("cu-concentrate", "Медный концентрат", "material", "pyrometallurgy"),
    _ent("white-matte", "Белый матт", "material", "pyrometallurgy", "white metal"),
    _ent("slag", "Шлак", "material", "pyrometallurgy"),
    _ent("cu-matte", "Штейн медный", "material", "pyrometallurgy"),
    _ent("so2-gas", "Газ SO2", "material", "pyrometallurgy", "сернистый газ"),
    _ent("flue-dust", "Возгоны", "material", "pyrometallurgy"),
    _ent("converter-slag", "Конвертерный шлак", "material", "pyrometallurgy"),
    _ent("smelting", "Плавка", "process", "pyrometallurgy"),
    _ent("converting", "Конвертирование", "process", "pyrometallurgy"),
    _ent("slag-cleaning", "Обеднение шлака", "process", "pyrometallurgy", "slag reduction"),
    _ent("roasting", "Обжиг", "process", "pyrometallurgy"),
    _ent("el-teniente", "El Teniente Converter", "equipment", "pyrometallurgy"),
    _ent("vanyukov", "Печь Ванюкова", "equipment", "pyrometallurgy"),
    _ent("converter", "Конвертер", "equipment", "pyrometallurgy"),
    _ent("roaster", "Печь обжига", "equipment", "pyrometallurgy"),
    _ent("outokumpu", "Печь Outokumpu", "equipment", "pyrometallurgy"),
    _ent("smelt-temp", "Температура плавки 1200-1300 °C", "condition", "pyrometallurgy"),
    _ent("concentrate-moisture", "Влажность концентрата 0.2%", "condition", "pyrometallurgy"),
    _ent("cu-in-matte", "Содержание Cu в белом матте ~75%", "condition", "pyrometallurgy"),
    # ═══ Электролиз ═══
    _ent("cu-electrolyte", "Медный электролит", "material", "electrolysis"),
    _ent("ni-electrolyte", "Никелевый электролит", "material", "electrolysis"),
    _ent("cu-cathode", "Катодная медь (электролитическая)", "material", "electrolysis"),
    _ent("ni-cathode", "Катодный никель", "material", "electrolysis"),
    _ent("cu-anode", "Медный анод", "material", "electrolysis"),
    _ent("anode-slime", "Шлам анодный", "material", "electrolysis"),
    _ent("cu-ew", "Электровыскание меди", "process", "electrolysis", "electrowinning, EW"),
    _ent("cu-er", "Электрорафинирование меди", "process", "electrolysis", "ER"),
    _ent("ni-ew", "Электролиз никеля", "process", "electrolysis"),
    _ent("electrolytic-cell", "Электролизная ванна", "equipment", "electrolysis"),
    _ent("cathode-plate", "Катодная пластина", "equipment", "electrolysis"),
    _ent("anode-plate", "Анодная пластина", "equipment", "electrolysis"),
    _ent("rectifier", "Выпрямитель", "equipment", "electrolysis"),
    _ent("current-density", "Плотность тока 200-400 А/м²", "condition", "electrolysis"),
    _ent("cell-voltage", "Напряжение 2-3 В", "condition", "electrolysis"),
    _ent("electrolyte-temp", "Температура электролита 40-65 °C", "condition", "electrolysis"),
    # ═══ Очистка растворов ═══
    _ent("iron", "Железо", "material", "solution_purification", "Fe"),
    _ent("zinc", "Цинк", "material", "solution_purification", "Zn"),
    _ent("lead", "Свинец", "material", "solution_purification", "Pb"),
    _ent("arsenic", "Мышьяк", "material", "solution_purification", "As"),
    _ent("antimony", "Сурьма", "material", "solution_purification", "Sb"),
    _ent("silica-gel", "Силикагель", "material", "solution_purification"),
    _ent("goethite", "Гётит", "material", "solution_purification", "FeOOH"),
    _ent("jarosite", "Ярозит", "material", "solution_purification"),
    _ent("fe-precipitation", "Осаждение железа", "process", "solution_purification"),
    _ent("cementation", "Цементация", "process", "solution_purification"),
    _ent("adsorption", "Сорбция", "process", "solution_purification"),
    _ent("fe-removal-cesl", "Процесс CESL", "process", "solution_purification"),
    _ent("thickener", "Сгуститель", "equipment", "solution_purification"),
    _ent("filter-press", "Фильтр-пресс", "equipment", "solution_purification"),
    _ent("adsorption-column", "Сорбционная колонна", "equipment", "solution_purification"),
    _ent("fe-removal-ph", "pH осаждения Fe 3.5-4.5", "condition", "solution_purification"),
    _ent("fe-removal-temp", "Температура осаждения 70-80 °C", "condition", "solution_purification"),
    # ═══ Получение солей ═══
    _ent("ni-sulfate", "Сульфат никеля", "material", "salt_production", "NiSO4"),
    _ent("co-sulfate", "Сульфат кобальта", "material", "salt_production", "CoSO4"),
    _ent("li-carbonate", "Карбонат лития", "material", "salt_production", "Li2CO3"),
    _ent("ni-class1", "Никель Класса I", "material", "salt_production"),
    _ent("mixed-hydroxide", "Смешанные гидроксиды", "material", "salt_production", "MHP"),
    _ent("li-ore", "Литиевая руда", "material", "salt_production"),
    _ent("spodumene", "Сподумен", "material", "salt_production"),
    _ent("crystallization", "Кристаллизация", "process", "salt_production"),
    _ent("dissolution", "Растворение", "process", "salt_production"),
    _ent("sulfate-purification", "Очистка сульфата", "process", "salt_production"),
    _ent("crystallizer", "Кристаллизатор", "equipment", "salt_production"),
    _ent("centrifuge", "Центрифуга", "equipment", "salt_production"),
    _ent("dryer", "Сушилка", "equipment", "salt_production"),
    _ent("purity", "Чистота 99.9%", "condition", "salt_production"),
    _ent("cryst-temp", "Температура кристаллизации 50-60 °C", "condition", "salt_production"),
    # ═══ Переработка штейнов ═══
    _ent("cu-ni-matte", "Медно-никелевый штейн", "material", "matte_processing"),
    _ent("feinstein", "Файнштейн", "material", "matte_processing", "кованый никель"),
    _ent("nickel", "Никель", "material", "matte_processing", "Ni"),
    _ent("cobalt", "Кобальт", "material", "matte_processing", "Co"),
    _ent("cu-ni-sulfide", "Сульфид медно-никелевый", "material", "matte_processing"),
    _ent("hibinette", "Процесс Хибинетта", "process", "matte_processing"),
    _ent("roast-leach", "Обжиг-выщелачивание", "process", "matte_processing"),
    _ent("cesl-process", "Процесс CESL", "process", "matte_processing"),
    _ent("matte-convert", "Конвертирование штейна", "process", "matte_processing"),
    _ent("matte-furnace", "Печь для переработки штейна", "equipment", "matte_processing"),
    _ent("roast-furnace", "Печь обжига штейна", "equipment", "matte_processing"),
    _ent("nikkelverk", "Nikkelverk (Норвегия)", "equipment", "matte_processing"),
    _ent("niihama", "Niihama (Япония)", "equipment", "matte_processing"),
    _ent("sandouville", "Sandouville (Франция)", "equipment", "matte_processing"),
    _ent("glencore", "Glencore", "expert", "matte_processing"),
    _ent("falconbridge", "Falconbridge", "expert", "matte_processing"),
    _ent("roast-temp", "Температура обжига 600-800 °C", "condition", "matte_processing"),
]


# ── Определения рёбер ──────────────────────────────────────────────────────


def _e(src: str, rel: str, tgt: str, conf: float = 1.0) -> DomainEdge:
    return DomainEdge(source=f"{P}{src}", target=f"{P}{tgt}", relation=rel, confidence=conf)


EDGES: list[DomainEdge] = [
    # ═══ Водоподготовка: внутренние связи ═══
    _e("water", "CONTAINS", "sulfates-cond"),
    _e("water", "CONTAINS", "chlorides-cond"),
    _e("water", "TREATED_BY", "ro"),
    _e("water", "TREATED_BY", "ion-exchange"),
    _e("water", "TREATED_BY", "evaporation"),
    _e("water", "TREATED_BY", "nanofiltration"),
    _e("water", "TREATED_BY", "ultrafiltration"),
    _e("ultrafiltration", "PRECEDES", "ro"),
    _e("nanofiltration", "PRECEDES", "ro"),
    _e("ro", "PRODUCES", "permeate"),
    _e("ro", "PRODUCES", "brine"),
    _e("ro", "USES", "ro-membrane"),
    _e("ro", "REQUIRES", "ro-pressure"),
    _e("ro", "REQUIRES", "min-temp"),
    _e("ro", "ACHIEVES", "dry-residue"),
    _e("ion-exchange", "USES", "ix-resin"),
    _e("ion-exchange", "PRODUCES", "treated-water"),
    _e("evaporation", "USES", "evaporator"),
    _e("evaporation", "PRODUCES", "treated-water"),
    _e("evaporation", "PRODUCES", "brine"),
    _e("ro-membrane", "USED_BY", "ro"),
    _e("ix-resin", "USED_BY", "ion-exchange"),
    _e("evaporator", "USED_BY", "evaporation"),
    _e("ro-pilot", "USED_FOR", "ro"),
    _e("water-lab", "EXPERT_IN", "ro"),
    _e("water-lab", "EXPERT_IN", "ion-exchange"),
    _e("water-lab", "EXPERT_IN", "evaporation"),
    _e("permeate", "PRODUCED_FROM", "water"),
    _e("brine", "PRODUCED_FROM", "water"),
    _e("treated-water", "PRODUCED_FROM", "water"),
    _e("dry-residue", "MEASURED_IN", "treated-water"),
    # ═══ Гидрометаллургия: внутренние связи ═══
    _e("oxide-ore", "CONTAINS", "malachite"),
    _e("oxide-ore", "CONTAINS", "azurite"),
    _e("oxide-ore", "TREATED_BY", "heap-leach"),
    _e("oxide-ore", "TREATED_BY", "vat-leach"),
    _e("oxide-ore", "TREATED_BY", "chloride-leach"),
    _e("heap-leach", "USES", "h2so4"),
    _e("heap-leach", "USES", "heap-pad"),
    _e("heap-leach", "USES", "irrigation-system"),
    _e("heap-leach", "REQUIRES", "leach-ph"),
    _e("heap-leach", "REQUIRES", "leach-time"),
    _e("heap-leach", "REQUIRES", "leach-temp"),
    _e("heap-leach", "AFFECTED_BY", "cold-climate"),
    _e("heap-leach", "PRODUCES", "pregnant-solution"),
    _e("vat-leach", "USES", "leach-vat"),
    _e("vat-leach", "USES", "h2so4"),
    _e("vat-leach", "PRODUCES", "pregnant-solution"),
    _e("chloride-leach", "USES", "nacl"),
    _e("chloride-leach", "PRODUCES", "pregnant-solution"),
    _e("cyanide-leach", "PRODUCES", "pregnant-solution"),
    _e("sx", "PROCESSES", "pregnant-solution"),
    _e("sx", "USES", "sx-mixer"),
    _e("sx", "PRODUCES", "cathode-cu"),
    _e("heap-pad", "USED_BY", "heap-leach"),
    _e("leach-vat", "USED_BY", "vat-leach"),
    _e("sx-mixer", "USED_BY", "sx"),
    _e("irrigation-system", "USED_BY", "heap-leach"),
    _e("pregnant-solution", "PRODUCED_FROM", "oxide-ore"),
    _e("barren-solution", "PRODUCED_FROM", "pregnant-solution"),
    _e("cathode-cu", "PRODUCED_FROM", "pregnant-solution"),
    _e("malachite", "DISSOLVED_BY", "h2so4"),
    _e("azurite", "DISSOLVED_BY", "h2so4"),
    # ═══ Пирометаллургия: внутренние связи ═══
    _e("cu-concentrate", "TREATED_BY", "smelting"),
    _e("cu-concentrate", "TREATED_BY", "roasting"),
    _e("smelting", "PRODUCES", "cu-matte"),
    _e("smelting", "PRODUCES", "slag"),
    _e("smelting", "PRODUCES", "so2-gas"),
    _e("smelting", "USES", "vanyukov"),
    _e("smelting", "USES", "outokumpu"),
    _e("smelting", "REQUIRES", "smelt-temp"),
    _e("smelting", "REQUIRES", "concentrate-moisture"),
    _e("converting", "PROCESSES", "cu-matte"),
    _e("converting", "PRODUCES", "white-matte"),
    _e("converting", "PRODUCES", "converter-slag"),
    _e("converting", "PRODUCES", "so2-gas"),
    _e("converting", "USES", "el-teniente"),
    _e("converting", "USES", "converter"),
    _e("converting", "REQUIRES", "smelt-temp"),
    _e("slag-cleaning", "PROCESSES", "converter-slag"),
    _e("slag-cleaning", "PRODUCES", "cu-matte"),
    _e("slag-cleaning", "REDUCES", "slag"),
    _e("roasting", "USES", "roaster"),
    _e("roasting", "PRODUCES", "flue-dust"),
    _e("roasting", "PRODUCES", "so2-gas"),
    _e("white-matte", "PRODUCED_FROM", "cu-matte"),
    _e("converter-slag", "PRODUCED_FROM", "cu-matte"),
    _e("cu-matte", "PRODUCED_FROM", "cu-concentrate"),
    _e("el-teniente", "USED_BY", "converting"),
    _e("vanyukov", "USED_BY", "smelting"),
    _e("outokumpu", "USED_BY", "smelting"),
    _e("converter", "USED_BY", "converting"),
    _e("roaster", "USED_BY", "roasting"),
    _e("cu-in-matte", "MEASURED_IN", "white-matte"),
    # ═══ Электролиз: внутренние связи ═══
    _e("cu-ew", "PROCESSES", "cu-electrolyte"),
    _e("cu-ew", "PRODUCES", "cu-cathode"),
    _e("cu-ew", "USES", "electrolytic-cell"),
    _e("cu-ew", "USES", "rectifier"),
    _e("cu-ew", "REQUIRES", "current-density"),
    _e("cu-ew", "REQUIRES", "cell-voltage"),
    _e("cu-ew", "REQUIRES", "electrolyte-temp"),
    _e("cu-er", "PROCESSES", "cu-anode"),
    _e("cu-er", "PRODUCES", "cu-cathode"),
    _e("cu-er", "PRODUCES", "anode-slime"),
    _e("cu-er", "USES", "electrolytic-cell"),
    _e("cu-er", "REQUIRES", "current-density"),
    _e("cu-er", "REQUIRES", "electrolyte-temp"),
    _e("ni-ew", "PROCESSES", "ni-electrolyte"),
    _e("ni-ew", "PRODUCES", "ni-cathode"),
    _e("ni-ew", "USES", "electrolytic-cell"),
    _e("ni-ew", "REQUIRES", "current-density"),
    _e("electrolytic-cell", "USED_BY", "cu-ew"),
    _e("electrolytic-cell", "USED_BY", "cu-er"),
    _e("electrolytic-cell", "USED_BY", "ni-ew"),
    _e("rectifier", "USED_BY", "cu-ew"),
    _e("rectifier", "USED_BY", "cu-er"),
    _e("cathode-plate", "USED_IN", "cu-ew"),
    _e("cathode-plate", "USED_IN", "ni-ew"),
    _e("anode-plate", "USED_IN", "cu-er"),
    _e("cu-cathode", "PRODUCED_FROM", "cu-electrolyte"),
    _e("ni-cathode", "PRODUCED_FROM", "ni-electrolyte"),
    _e("anode-slime", "PRODUCED_FROM", "cu-anode"),
    # ═══ Очистка растворов: внутренние связи ═══
    _e("fe-precipitation", "REMOVES", "iron"),
    _e("fe-precipitation", "PRODUCES", "goethite"),
    _e("fe-precipitation", "PRODUCES", "jarosite"),
    _e("fe-precipitation", "REQUIRES", "fe-removal-ph"),
    _e("fe-precipitation", "REQUIRES", "fe-removal-temp"),
    _e("fe-precipitation", "USES", "thickener"),
    _e("fe-precipitation", "USES", "filter-press"),
    _e("cementation", "REMOVES", "zinc"),
    _e("cementation", "REMOVES", "lead"),
    _e("adsorption", "REMOVES", "arsenic"),
    _e("adsorption", "REMOVES", "antimony"),
    _e("adsorption", "USES", "adsorption-column"),
    _e("adsorption", "USES", "silica-gel"),
    _e("fe-removal-cesl", "USES", "iron"),
    _e("fe-removal-cesl", "REMOVES", "iron"),
    _e("thickener", "USED_BY", "fe-precipitation"),
    _e("filter-press", "USED_BY", "fe-precipitation"),
    _e("adsorption-column", "USED_BY", "adsorption"),
    _e("goethite", "PRODUCED_FROM", "iron"),
    _e("jarosite", "PRODUCED_FROM", "iron"),
    # ═══ Получение солей: внутренние связи ═══
    _e("crystallization", "PRODUCES", "ni-sulfate"),
    _e("crystallization", "PRODUCES", "co-sulfate"),
    _e("crystallization", "USES", "crystallizer"),
    _e("crystallization", "REQUIRES", "cryst-temp"),
    _e("crystallization", "REQUIRES", "purity"),
    _e("dissolution", "PROCESSES", "ni-class1"),
    _e("dissolution", "PROCESSES", "mixed-hydroxide"),
    _e("dissolution", "PRODUCES", "ni-electrolyte"),
    _e("sulfate-purification", "PROCESSES", "ni-sulfate"),
    _e("sulfate-purification", "PRODUCES", "ni-sulfate"),
    _e("crystallizer", "USED_BY", "crystallization"),
    _e("centrifuge", "USED_BY", "crystallization"),
    _e("dryer", "USED_BY", "crystallization"),
    _e("ni-sulfate", "PRODUCED_FROM", "ni-class1"),
    _e("ni-sulfate", "PRODUCED_FROM", "mixed-hydroxide"),
    _e("co-sulfate", "PRODUCED_FROM", "mixed-hydroxide"),
    _e("li-carbonate", "PRODUCED_FROM", "li-ore"),
    _e("li-carbonate", "PRODUCED_FROM", "spodumene"),
    _e("crystallization", "PRODUCES", "li-carbonate"),
    # ═══ Переработка штейнов: внутренние связи ═══
    _e("cu-ni-matte", "TREATED_BY", "hibinette"),
    _e("cu-ni-matte", "TREATED_BY", "roast-leach"),
    _e("cu-ni-matte", "TREATED_BY", "cesl-process"),
    _e("cu-ni-matte", "TREATED_BY", "matte-convert"),
    _e("matte-convert", "PRODUCES", "feinstein"),
    _e("matte-convert", "PRODUCES", "converter-slag"),
    _e("hibinette", "PRODUCES", "nickel"),
    _e("hibinette", "PRODUCES", "cobalt"),
    _e("roast-leach", "PRODUCES", "nickel"),
    _e("roast-leach", "PRODUCES", "cobalt"),
    _e("cesl-process", "PRODUCES", "ni-sulfate"),
    _e("cesl-process", "PRODUCES", "co-sulfate"),
    _e("hibinette", "USES", "matte-furnace"),
    _e("roast-leach", "USES", "roast-furnace"),
    _e("roast-leach", "REQUIRES", "roast-temp"),
    _e("nikkelverk", "USED_FOR", "hibinette"),
    _e("niihama", "USED_FOR", "chloride-leach"),
    _e("sandouville", "USED_FOR", "hibinette"),
    _e("glencore", "EXPERT_IN", "cesl-process"),
    _e("falconbridge", "EXPERT_IN", "hibinette"),
    _e("feinstein", "PRODUCED_FROM", "cu-ni-matte"),
    _e("nickel", "PRODUCED_FROM", "cu-ni-matte"),
    _e("nickel", "PRODUCED_FROM", "feinstein"),
    _e("cobalt", "PRODUCED_FROM", "cu-ni-matte"),
    _e("cu-ni-sulfide", "PRODUCED_FROM", "cu-ni-matte"),
    # ═══ Междоменные связи ═══
    # Гидрометаллургия → Электролиз
    _e("pregnant-solution", "FEEDS", "cu-ew"),
    _e("sx", "FEEDS", "cu-ew"),
    _e("cathode-cu", "ALTERNATIVE_TO", "cu-cathode"),
    # Пирометаллургия → Электролиз
    _e("white-matte", "FEEDS", "cu-er"),
    _e("cu-matte", "FEEDS", "converting"),
    _e("cu-anode", "PRODUCED_FROM", "white-matte"),
    # Очистка растворов → Электролиз
    _e("fe-precipitation", "PRECEDES", "cu-ew"),
    _e("cementation", "PRECEDES", "cu-ew"),
    # Получение солей → Электролиз
    _e("ni-sulfate", "FEEDS", "ni-ew"),
    _e("dissolution", "FEEDS", "ni-ew"),
    # Переработка штейнов → Получение солей
    _e("nickel", "FEEDS", "dissolution"),
    _e("cesl-process", "PRODUCES", "ni-sulfate"),
    # Водоподготовка → Гидрометаллургия
    _e("treated-water", "USED_IN", "heap-leach"),
    _e("treated-water", "USED_IN", "vat-leach"),
    # Гидрометаллургия → Очистка растворов
    _e("pregnant-solution", "TREATED_BY", "fe-precipitation"),
    # Пирометаллургия → Очистка растворов (SO2 → кислота)
    _e("so2-gas", "CONVERTED_TO", "h2so4"),
    # Переработка штейнов → Гидрометаллургия
    _e("feinstein", "TREATED_BY", "chloride-leach"),
]


# ── Утверждения (claims) ───────────────────────────────────────────────────


def _claim(
    cid: str,
    label: str,
    subj: str,
    pred: str,
    obj: str,
    statement: str,
    source: str,
    domain: str,
    conf: float = 0.85,
) -> DomainClaim:
    return DomainClaim(
        id=f"{P}claim-{cid}",
        label=label,
        subject=f"{P}{subj}",
        predicate=pred,
        object=f"{P}{obj}",
        statement=statement,
        source_title=source,
        domain=domain,
        confidence=conf,
    )


CLAIMS: list[DomainClaim] = [
    # Водоподготовка
    _claim(
        "ro-eff",
        "RO удаляет 95-99% солей",
        "ro",
        "HAS_EFFICIENCY",
        "water",
        "Обратный осмос обеспечивает удаление 95-99% растворённых солей и подходит для достижения "
        "сухого остатка ≤1000 мг/л.",
        "Обзор методов обессоливания шахтных вод",
        "water_treatment",
        0.92,
    ),
    _claim(
        "ro-pilot",
        "Пилот RO: задержание 80-85%",
        "ro",
        "HAS_EFFICIENCY",
        "water",
        "Пилотные испытания обратного осмоса показали задержание солей на уровне 80-85% при "
        "пониженном давлении.",
        "Пилот обратного осмоса",
        "water_treatment",
        0.70,
    ),
    _claim(
        "ix-eff",
        "Ионный обмен: удаление Ca/Mg 82-91%",
        "ion-exchange",
        "HAS_EFFICIENCY",
        "water",
        "Ионный обмен целесообразен как селективная ступень для Ca и Mg, но регенерационные стоки "
        "ограничивают применение.",
        "Протокол пилотных испытаний ионного обмена",
        "water_treatment",
        0.84,
    ),
    _claim(
        "ev-energy",
        "Выпаривание требует в 3-5 раз больше энергии",
        "evaporation",
        "HAS_ENERGY_RATIO",
        "ro",
        "Термическое выпаривание устойчиво к широкому составу воды, но требует в 3-5 раз больше "
        "энергии, чем мембранная схема.",
        "Сравнение технологий концентрирования",
        "water_treatment",
        0.78,
    ),
    _claim(
        "ro-temp",
        "RO требует температуры >8 °C",
        "ro",
        "REQUIRES_MIN_TEMPERATURE",
        "min-temp",
        "Для холодного климата мембранный блок требует утепления и поддержания температуры сырья "
        "выше 8 °C.",
        "Эксплуатация мембран в холодном климате",
        "water_treatment",
        0.81,
    ),
    _claim(
        "uf-pre",
        "УФ как предочистка перед RO",
        "ultrafiltration",
        "PRECEDES",
        "ro",
        "Ультрафильтрация применяется как предочистка для защиты RO-мембран от коллоидного "
        "загрязнения.",
        "Обзор предочистки мембранных систем",
        "water_treatment",
        0.80,
    ),
    # Гидрометаллургия
    _claim(
        "hl-tech",
        "Технология КВ меди включает дробление-выщелачивание-электролиз",
        "heap-leach",
        "PRODUCES",
        "cathode-cu",
        "Технология КВ меди из окисленных руд включает дробление, укладку руды, выщелачивание, "
        "экстракцию, реэкстракцию и электролиз.",
        "ТИ-5-2017. Кучное выщелачивание в условиях холодного климата",
        "hydrometallurgy",
        0.88,
    ),
    _claim(
        "oxide-ore-grade",
        "Окисленные руды содержат ~0.4% Cu",
        "oxide-ore",
        "HAS_GRADE",
        "malachite",
        "Окисленные руды содержат около 0,4% меди, основная масса которой находится в окисленной "
        "форме (малахит, азурит).",
        "ТИ-5-2017. Кучное выщелачивание в условиях холодного климата",
        "hydrometallurgy",
        0.85,
    ),
    _claim(
        "cl-leach",
        "Хлорное выщелачивание для файнштейна",
        "chloride-leach",
        "USED_FOR",
        "feinstein",
        "Хлорное выщелачивание применяется для переработки файнштейна с извлечением никеля и "
        "кобальта.",
        "Хлорное выщелачивание ОИП 02-2024",
        "hydrometallurgy",
        0.86,
    ),
    _claim(
        "sx-purge",
        "SX извлекает Cu из продуктивного раствора",
        "sx",
        "PRODUCES",
        "cathode-cu",
        "Экстракция растворителем (SX) selectively извлекает медь из продуктивного раствора с "
        "последующим электровысканием.",
        "Обзор SX-EW технологий",
        "hydrometallurgy",
        0.87,
    ),
    _claim(
        "cold-effect",
        "Холодный климат снижает скорость КВ",
        "cold-climate",
        "AFFECTS",
        "heap-leach",
        "В условиях холодного климата скорость кучного выщелачивания снижается, требуется подогрев "
        "растворов и теплоизоляция.",
        "ТИ-5-2017. Кучное выщелачивание в условиях холодного климата",
        "hydrometallurgy",
        0.82,
    ),
    # Пирометаллургия
    _claim(
        "el-teniente-proc",
        "El Teniente: плавка до белого матта",
        "el-teniente",
        "PRODUCES",
        "white-matte",
        "Процесс плавки/конвертирования медных концентратов El Teniente Converter включает плавку "
        "сухого концентрата (0,2% влаги) и частичное конвертирование до белого матта.",
        "Обеднение_шлаков",
        "pyrometallurgy",
        0.89,
    ),
    _claim(
        "cu-in-matte",
        "Белый матт содержит ~75% Cu",
        "white-matte",
        "HAS_GRADE",
        "cu-in-matte",
        "При интенсивной плавке/частичном конвертировании сухого концентрата до белого матта с "
        "≈75% Cu в конвертере El Teniente получается сильно окисленный шлак.",
        "Обеднение_шлаков",
        "pyrometallurgy",
        0.88,
    ),
    _claim(
        "slag-reproc",
        "Конвертерные шлаки перерабатываются на ОФ",
        "converter-slag",
        "PROCESSED_BY",
        "slag-cleaning",
        "На медеплавильном заводе Mt Isa конвертерные шлаки перерабатывались периодически "
        "кампаниями на медной обогатительной фабрике.",
        "Обеднение_шлаков",
        "pyrometallurgy",
        0.85,
    ),
    _claim(
        "so2-capture",
        "Газ SO2 улавливается и перерабатывается в кислоту",
        "so2-gas",
        "CONVERTED_TO",
        "h2so4",
        "Сернистый газ от плавки и конвертирования улавливается и перерабатывается в серную "
        "кислоту на сопряжённом производстве.",
        "Обзор металлургии меди",
        "pyrometallurgy",
        0.83,
    ),
    # Электролиз
    _claim(
        "cu-ew-params",
        "Электровыскание меди: 200-400 А/м², 2-3 В",
        "cu-ew",
        "OPERATES_AT",
        "current-density",
        "Электровыскание меди ведётся при плотности тока 200-400 А/м² и напряжении 2-3 В на ванну.",
        "ОИП-05-2019 Параметры Cu EW",
        "electrolysis",
        0.90,
    ),
    _claim(
        "cu-er-slime",
        "Электрорафинирование даёт анодный шлам",
        "cu-er",
        "PRODUCES",
        "anode-slime",
        "При электрорафинировании меди благородные металлы оседают в анодном шламе, который "
        "перерабатывается отдельно.",
        "Обзор электрорафинирования меди",
        "electrolysis",
        0.86,
    ),
    _claim(
        "ni-ew-purity",
        "Электролиз никеля: высокая чистота катода",
        "ni-ew",
        "PRODUCES",
        "ni-cathode",
        "Электролиз никеля обеспечивает получение катодного никеля с чистотой до 99.99% при "
        "многоступенчатой очистке электролита.",
        "Обзор электролиза никеля",
        "electrolysis",
        0.84,
    ),
    # Очистка растворов
    _claim(
        "fe-goethite",
        "Осаждение железа в виде гётита при pH 3.5-4.5",
        "fe-precipitation",
        "PRODUCES",
        "goethite",
        "Железо удаляют из технологических растворов осаждением в виде гётита при pH 3.5-4.5 и "
        "температуре 70-80 °C.",
        "Очистка от Fe 2020",
        "solution_purification",
        0.87,
    ),
    _claim(
        "fe-jarosite",
        "Альтернатива: осаждение в виде ярозита",
        "fe-precipitation",
        "PRODUCES",
        "jarosite",
        "Осаждение железа в виде ярозита применяется при более низких температурах, но требует "
        "добавления ионов натрия или калия.",
        "Очистка от Fe 2020",
        "solution_purification",
        0.82,
    ),
    _claim(
        "cesl-uses-fe",
        "Процесс CESL использует железо",
        "fe-removal-cesl",
        "USES",
        "iron",
        "Процесс CESL использует железо как реагент для осаждения примесей из раствора.",
        "Очистка от Fe 2020",
        "solution_purification",
        0.80,
    ),
    _claim(
        "pb-removal",
        "Удаление свинца цементацией",
        "cementation",
        "REMOVES",
        "lead",
        "Свинец удаляют из растворов методом цементации на цинковой пыли с эффективностью до 99%.",
        "ОИП-04-2022 Удаление свинца",
        "solution_purification",
        0.83,
    ),
    # Получение солей
    _claim(
        "ni-so4-class1",
        "Сульфат никеля из никеля Класса I",
        "ni-sulfate",
        "PRODUCED_FROM",
        "ni-class1",
        "Сульфат никеля высокой чистоты может быть получен растворением никеля Класса I с "
        "последующей кристаллизацией.",
        "ОИП-01-2022 Обзор существующих технологий получения сульфатов никеля и кобальта",
        "salt_production",
        0.86,
    ),
    _claim(
        "ni-so4-mhp",
        "Сульфат никеля из смешанных гидроксидов",
        "ni-sulfate",
        "PRODUCED_FROM",
        "mixed-hydroxide",
        "Сульфат никеля может быть получен из смешанных гидроксидов (MHP) путём растворения и "
        "очистки.",
        "ОИП-01-2022 Обзор существующих технологий получения сульфатов никеля и кобальта",
        "salt_production",
        0.84,
    ),
    _claim(
        "li-from-ore",
        "Литий из сподумена",
        "li-carbonate",
        "PRODUCED_FROM",
        "spodumene",
        "Карбонат лития получают из сподумена путём обжига, сернокислотного выщелачивания и "
        "осаждения карбонатом.",
        "ОИП-06-2022 Технологии производства лития из рудного сырья",
        "salt_production",
        0.83,
    ),
    _claim(
        "cryst-temp",
        "Кристаллизация при 50-60 °C",
        "crystallization",
        "REQUIRES",
        "cryst-temp",
        "Кристаллизация сульфатов никеля проводится при 50-60 °C с контролем пересыщения для "
        "получения крупных кристаллов.",
        "ОИП-01-2022 Обзор существующих технологий получения сульфатов никеля и кобальта",
        "salt_production",
        0.82,
    ),
    # Переработка штейнов
    _claim(
        "hibinette-proc",
        "Процесс Хибинетта для Cu-Ni штейна",
        "hibinette",
        "PROCESSES",
        "cu-ni-matte",
        "Медно-никелевый штейн перерабатывается с использованием процесса Хибинетта с получением "
        "файнштейна.",
        "Обзор пеработка медно-никелевых штейнов",
        "matte_processing",
        0.87,
    ),
    _claim(
        "nikkelverk-loc",
        "Nikkelverk в Норвегии",
        "nikkelverk",
        "USED_FOR",
        "hibinette",
        "Завод Nikkelverk в Норвегии применяет процесс Хибинетта для переработки медно-никелевого "
        "штейна.",
        "Хлорное выщелачивание ОИП 02-2024",
        "matte_processing",
        0.80,
    ),
    _claim(
        "cesl-matte",
        "CESL для переработки штейна",
        "cesl-process",
        "PRODUCES",
        "ni-sulfate",
        "Процесс CESL применяется для гидрометаллургической переработки медно-никелевого штейна с "
        "получением сульфатов.",
        "Обзор пеработка медно-никелевых штейнов",
        "matte_processing",
        0.82,
    ),
    _claim(
        "roast-leach-matte",
        "Обжиг-выщелачивание штейна",
        "roast-leach",
        "PRODUCES",
        "nickel",
        "Обжиг-выщелачивание медно-никелевого штейна позволяет получить металлический никель и "
        "кобальт.",
        "Обзор пеработка медно-никелевых штейнов",
        "matte_processing",
        0.84,
    ),
]


# ── Запись в Neo4j ──────────────────────────────────────────────────────────


def _safe_relation(relation: str) -> str:
    """Имя обязано быть объявлено в реестре и безопасно подставлено в Cypher.

    Раньше сюда попадало любое отношение из данных: опечатка в имени рождала
    ребро, которое allowlist агента никогда не сможет пересечь.
    """
    declared = spec_for(relation)
    if not declared.name.replace("_", "").isalnum() or declared.name.upper() != declared.name:
        raise RelationContractError(f"Небезопасное имя отношения: {relation}")
    return declared.name


def check_domain_seed() -> None:
    """Прогоняет весь сеятель через реестр до первой записи в Neo4j.

    Проверка атомарная и до сессии: частичная запись «половины онтологии» в
    рабочий граф аналитиков дороже, чем падение preload на неразмеченном ребре.
    """
    types = {entity.id: entity.entity_type for entity in ENTITIES}
    for edge in EDGES:
        source = types.get(edge.source)
        target = types.get(edge.target)
        if source is None or target is None:
            raise RelationContractError(
                f"Ребро {edge.source}-{edge.relation}->{edge.target} ведёт в неизвестную сущность"
            )
        validate_edge(_safe_relation(edge.relation), source, target)
    for claim in CLAIMS:
        subject = types.get(claim.subject)
        target = types.get(claim.object)
        if subject is None or target is None:
            raise RelationContractError(f"Утверждение {claim.id} ведёт в неизвестную сущность")
        validate_edge(
            _safe_relation(claim.predicate),
            NodeType.CLAIM.value,
            target,
            asserted=True,
        )


def publication_id(source_title: str) -> str:
    """Стабильный id публикации.

    Ранее id строился на ``hash(source_title)``: он солируется на каждый процесс,
    поэтому после перезапуска контейнера MERGE не находил узел и создавал новый
    для того же источника. Трассировка «ответ → первоисточник» разрывалась, а
    число публикаций росло на каждом preload.
    """
    return f"{P}pub-{stable_uuid(source_title.lower().strip())}"


def rebuild_semantic_graph(driver: Driver) -> dict[str, int]:
    """Создаёт доменный граф знаний в Neo4j, дополняя существующие узлы.

    Не удаляет существующие узлы и рёбра — только добавляет новые.
    Возвращает статистику по фактически созданным объектам: повторный запуск
    на уже заполненном графе даёт нули, а не «447 узлов», как раньше.
    """
    check_domain_seed()

    nodes_created = 0
    edges_created = 0
    claims_created = 0

    with driver.session() as session:
        # Создаём сущности одним батчем вместо запроса на каждую
        entity_rows = []
        for entity in ENTITIES:
            metadata = json.dumps(
                {"domain": entity.domain, "aliases": entity.aliases}
                if entity.aliases
                else {"domain": entity.domain},
                ensure_ascii=False,
            )
            entity_rows.append(
                {
                    "id": entity.id,
                    "label": entity.label,
                    "type": entity.entity_type,
                    "metadata": metadata,
                }
            )
        counters = session.run(
            """
            UNWIND $rows AS row
            MERGE (n:Entity {id: row.id})
            SET n.label = row.label, n.type = row.type, n.confidence = 1.0,
                n.metadata = row.metadata, n.source = 'domain'
            """,
            rows=entity_rows,
        ).consume().counters
        nodes_created += counters.nodes_created

        # Создаём семантические рёбра: батч на каждый тип отношения
        edges_by_rel: dict[str, list[dict[str, Any]]] = {}
        for edge in EDGES:
            rel = _safe_relation(edge.relation)
            edges_by_rel.setdefault(rel, []).append(
                {
                    "source": edge.source,
                    "target": edge.target,
                    "id": f"{edge.source}-{rel}-{edge.target}",
                    "confidence": edge.confidence,
                }
            )
        for rel, rows in edges_by_rel.items():
            counters = session.run(
                f"""
                UNWIND $rows AS row
                MATCH (a:Entity {{id: row.source}}), (b:Entity {{id: row.target}})
                MERGE (a)-[r:{rel} {{id: row.id}}]->(b)
                SET r.confidence = row.confidence, r.source = 'domain'
                """,
                rows=rows,
            ).consume().counters
            edges_created += counters.relationships_created

        # Claim-узлы одним батчем
        claim_rows = []
        for claim in CLAIMS:
            metadata = json.dumps(
                {
                    "domain": claim.domain,
                    "knowledge_status": "domain_seed",
                    "predicate": claim.predicate,
                },
                ensure_ascii=False,
            )
            claim_rows.append(
                {
                    "id": claim.id,
                    "label": claim.statement[:200],
                    "confidence": claim.confidence,
                    "metadata": metadata,
                }
            )
        counters = session.run(
            """
            UNWIND $rows AS row
            MERGE (n:Entity {id: row.id})
            SET n.label = row.label, n.type = 'claim',
                n.confidence = row.confidence, n.metadata = row.metadata,
                n.source = 'domain'
            """,
            rows=claim_rows,
        ).consume().counters
        claims_created += counters.nodes_created

        # subject ASSERTS claim — один батч на единственный тип связи
        asserts_rows = [
            {
                "subject": claim.subject,
                "target": claim.id,
                "id": f"{claim.id}-asserts",
                "confidence": claim.confidence,
            }
            for claim in CLAIMS
        ]
        counters = session.run(
            """
            UNWIND $rows AS row
            MATCH (a:Entity {id: row.subject}), (c:Entity {id: row.target})
            MERGE (a)-[r:ASSERTS {id: row.id}]->(c)
            SET r.confidence = row.confidence, r.source = 'domain'
            """,
            rows=asserts_rows,
        ).consume().counters
        edges_created += counters.relationships_created

        # claim predicate object — батч на каждый тип предиката
        pred_rows: dict[str, list[dict[str, Any]]] = {}
        for claim in CLAIMS:
            pred = _safe_relation(claim.predicate)
            pred_rows.setdefault(pred, []).append(
                {
                    "claim_id": claim.id,
                    "object_id": claim.object,
                    "id": f"{claim.id}-{pred}",
                    "confidence": claim.confidence,
                }
            )
        for pred, rows in pred_rows.items():
            counters = session.run(
                f"""
                UNWIND $rows AS row
                MATCH (c:Entity {{id: row.claim_id}}), (b:Entity {{id: row.object_id}})
                MERGE (c)-[r:{pred} {{id: row.id}}]->(b)
                SET r.confidence = row.confidence, r.source = 'domain'
                """,
                rows=rows,
            ).consume().counters
            edges_created += counters.relationships_created

        # claim SUPPORTED_BY publication (по названию): публикации дедуплицируются
        pub_rows: dict[str, dict[str, Any]] = {}
        support_rows = []
        for claim in CLAIMS:
            pub_id = publication_id(claim.source_title)
            pub_rows.setdefault(
                pub_id,
                {
                    "id": pub_id,
                    "label": claim.source_title,
                    "metadata": json.dumps(
                        {"source_path": "domain", "stage": "domain_seed"},
                        ensure_ascii=False,
                    ),
                },
            )
            support_rows.append(
                {
                    "claim_id": claim.id,
                    "pub_id": pub_id,
                    "id": f"{claim.id}-support",
                    "confidence": claim.confidence,
                }
            )
        counters = session.run(
            """
            UNWIND $rows AS row
            MERGE (pub:Entity {id: row.id})
            SET pub.label = row.label, pub.type = 'publication',
                pub.confidence = 1.0, pub.metadata = row.metadata
            """,
            rows=list(pub_rows.values()),
        ).consume().counters
        # Узел публикации здесь же: раньше он не попадал ни в один счётчик.
        nodes_created += counters.nodes_created
        counters = session.run(
            """
            UNWIND $rows AS row
            MATCH (c:Entity {id: row.claim_id}), (pub:Entity {id: row.pub_id})
            MERGE (c)-[r:SUPPORTED_BY {id: row.id}]->(pub)
            SET r.confidence = row.confidence, r.source = 'domain'
            """,
            rows=support_rows,
        ).consume().counters
        edges_created += counters.relationships_created

    logger.info(
        "Граф перестроен: %d узлов, %d рёбер, %d утверждений",
        nodes_created,
        edges_created,
        claims_created,
    )
    return {
        "nodes_created": nodes_created,
        "edges_created": edges_created,
        "claims_created": claims_created,
    }
