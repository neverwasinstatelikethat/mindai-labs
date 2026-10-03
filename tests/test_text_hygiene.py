"""Текстовая гигиена: в файлах репозитория не бывает иероглифики и полноширинных знаков.

Проверка появилась не из эстетики. Длинные русские блоки (комментарии, docstring,
тексты экранов, сообщения CLI) регулярно получают вклейку из CJK-диапазона: один
иероглиф или полноширинная скобка внутри предложения, которое выглядит целым. За
последнюю кампанию такое случилось трижды, и один раз вкралось в осмысленные данные
списка сущностей, где глаз его не видит вовсе. В продукте, где язык интерфейса это
рабочий инструмент аналитика, вклейка читается как испорченная строка, а в логе
контейнера она ещё и ломает вывод консоли на cp1251.

Диапазон взят узким и только под идеографику: CJK-пунктуация (U+3000…U+303F),
расширение A (U+3400…U+4DBF), основные иероглифы (U+4E00…U+9FFF), зона совместимости
(U+F900…U+FAFF) и полноширинные формы (U+FF00…U+FFEF). Кириллицу, латиницу, греческий
и знаки вроде «≤» или «°» ни один из них не задевает, поэтому ложных срабатываний
нет. Смешение кириллицы с латиницей внутри слова здесь не проверяется намеренно:
оно законно в классах регулярных выражений и в именах вида «A/B-прогон», а список
исключений превратил бы проверку в гадание.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEXT_GLOBS = (
    "src/scientific_tangle/**/*.py",
    "tests/**/*.py",
    "frontend/src/**/*.ts",
    "frontend/src/**/*.svelte",
    "ops/**/*.json",
    "ops/**/*.yml",
    "*.yaml",
    "*.yml",
    "*.md",
)

# Границы задаются кодовыми точками, а не символами: иначе файл с этим правилом
# попадал бы под собственную проверку.
CJK_RANGES = (
    (0x3000, 0x303F),  # знаки и пунктуация CJK
    (0x3400, 0x4DBF),  # идеографы, расширение A
    (0x4E00, 0x9FFF),  # основные идеографы
    (0xF900, 0xFAFF),  # идеографы, зона совместимости
    (0xFF00, 0xFFEF),  # полноширинные и полуширинные формы
)

CJK = re.compile("[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in CJK_RANGES) + "]")


def tracked_text_files() -> list[Path]:
    files: list[Path] = []
    for pattern in TEXT_GLOBS:
        files.extend(path for path in ROOT.glob(pattern) if path.is_file())
    return sorted(set(files))


def test_no_cjk_in_repository_text() -> None:
    files = tracked_text_files()
    assert len(files) > 40, f"под проверку попало подозрительно мало файлов: {len(files)}"
    offenders: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for index, line in enumerate(text.splitlines(), 1):
            hits = CJK.findall(line)
            if hits:
                offenders.append(
                    f"{path.relative_to(ROOT)}:{index} -> {ascii(''.join(sorted(set(hits))))}"
                )
    assert offenders == [], "иероглифика в тексте репозитория: " + "; ".join(offenders)


def test_check_has_teeth() -> None:
    """Паттерн обязан ловить то, ради чего написан, и не трогать рабочий алфавит."""
    for legit in (
        "Выпаривание, evaporator",
        "сухой остаток ≤1000 мг/л, сульфат-ион",
        "A/B-прогон (2026) при 8 °C",
        "«Находки» — раздел 3.1",
    ):
        assert CJK.search(legit) is None, f"{legit!r} помечено неверно"
    # Образцы собраны из кодовых точек: литералы поймал бы сам себя.
    for codepoint in (0x84B8, 0x3002, 0xFF21, 0x303F):
        sample = chr(codepoint)
        assert CJK.search(sample) is not None, f"U+{codepoint:04X} пропущен проверкой"


def test_glob_actually_covers_the_known_text_files() -> None:
    """Если шаблон перестанет находить файлы, проверка станет зелёной молча."""
    covered = {path.relative_to(ROOT).as_posix() for path in tracked_text_files()}
    for reference in (
        "src/scientific_tangle/api/app.py",
        "src/scientific_tangle/services/graph_rebuilder.py",
        "frontend/src/lib/terms.ts",
        "ops/grafana/dashboards/agent-quality.json",
    ):
        assert reference in covered, f"{reference} не попадает под проверку"
