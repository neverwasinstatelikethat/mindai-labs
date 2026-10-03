"""Контракт наблюдаемости: дашборд и правила не могут ссылаться на несуществующую метрику.

Панель Grafana с пустым запросом выглядит как «показаний нет», а на самом деле это
опечатка в имени метрики: переименование счётчика в коде молча обесценивает строку
дашборда и алерт. Проверка сверяет каждое имя `mindai_*`, которое читают
`ops/grafana/dashboards/*.json` и `ops/prometheus/*.yml`, с именами, объявленными в
приложении.

Суффиксы (`_bucket`, `_sum`, `_count`, `_total`, `_max`, `_min`) снимаются: их
добавляет клиент Prometheus для гистограмм и сумматоров, и в коде их может не быть
литералом. Полное имя при этом проверяется первым, иначе `mindai_llm_calls_total`
разобрался бы на несуществующий `mindai_llm_calls`.

Обратное направление (метрика есть, но её нигде не читают) проверкой не держится:
счётчик может служить разовой провеске или оставаться средством разбора для
оператора, и требовать от каждой метрики панель значило бы завести тест, который
ловит не то.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "scientific_tangle"
OPS_FILES = sorted((ROOT / "ops" / "grafana" / "dashboards").glob("*.json")) + sorted(
    (ROOT / "ops" / "prometheus").glob("*.yml")
)

METRIC = re.compile(r"mindai_[a-z0-9]+(?:_[a-z0-9]+)*")
# Порядок важен: сначала длинный составной суффикс, потом короткий.
SUFFIXES = ("_bucket", "_count", "_sum", "_total", "_max", "_min")


def declared_metrics() -> set[str]:
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        names.update(re.findall(r'"(mindai_[a-z0-9_]+)"', path.read_text(encoding="utf-8")))
    return names


def referenced_metrics() -> set[str]:
    names: set[str] = set()
    for path in OPS_FILES:
        names.update(METRIC.findall(path.read_text(encoding="utf-8")))
    return names


def resolves(name: str, declared: set[str]) -> bool:
    """Имя метрики объявлено само или с снятым одним (двумя) служебным суффиксом."""
    if name in declared:
        return True
    for suffix in SUFFIXES:
        base = name.removesuffix(suffix)
        if base != name:
            if base in declared:
                return True
            for second in SUFFIXES:
                inner = base.removesuffix(second)
                if inner != base and inner in declared:
                    return True
    return False


def test_ops_configs_reference_only_declared_metrics() -> None:
    assert OPS_FILES, "не найдено ни одного файла наблюдаемости в ops/"
    declared = declared_metrics()
    assert declared, "в приложении не объявлено ни одной метрики mindai_*"
    ghost = sorted(
        name
        for name in referenced_metrics()
        if not resolves(name, declared)
    )
    assert ghost == [], f"дашборд или правила зовут метрик, которых нет в коде: {ghost}"


def test_the_check_has_teeth() -> None:
    """Гарантия, что проверка ловит переименование, а не проходит пустой.

    Без этого теста зелёный статус ничего не стоит: если регулярка перестанет
    находить имена в дашборде, первая проверка станет пустым множеством и
    «пройдёт» как раз тогда, когда надо падать.
    """
    declared = declared_metrics()
    referenced = referenced_metrics()
    assert len(declared) >= 15, f"объявленных метрик подозрительно мало: {len(declared)}"
    assert len(referenced) >= 10, f"в ops найдено мало имён метрик: {len(referenced)}"
    assert not resolves("mindai_metric_after_rename_seconds", declared)
    assert not resolves("mindai_agent_duration_seconds_typo", declared)


def test_histogram_panels_use_generated_bucket_names() -> None:
    """Квантильные панели обязаны звать `_bucket`, а не сырое имя гистограммы.

    `histogram_quantile` по `..._bucket` работает, а по имени метрики без суффикса
    возвращает пусто, и панель выглядит как «задержек нет». Проверка держит обе
    стороны: базовая гистограмма объявлена, а в дашборде используется её `_bucket`.
    """
    declared = declared_metrics()
    referenced = referenced_metrics()
    histograms = {name for name in declared if name.endswith("_seconds")}
    assert histograms, "гистограмм длительности в коде нет"
    used = {name for name in referenced if name.endswith("_bucket")}
    assert used, "ни одна панель не считает квантиль по гистограмме"
    for bucket in sorted(used):
        assert bucket.removesuffix("_bucket") in declared, bucket
