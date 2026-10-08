"""Env-контракт: каждая настройка `Settings` обязана быть описана в `.env.example`.

Причина в том, как ищется причина отказа. Настройку, которой нет в примере,
оператор не находит: она работает молча на значении по умолчанию, а разбор
«почему агент деградировал» начинается с чтения `config.py`. Так четыре ключа
(`AGENT_NODE_BUDGET_SECONDS`, `POLICY_MAX_TOKENS`, `AGENT_ADMISSION_LIMIT`,
`EMBEDDING_REFUSAL_COOLDOWN_SECONDS`) существовали в приложении и ни одного
входа в контур не имели: потолок времени одного узла и пауза после терминального
отказа `/embeddings` влияли на прогоны, а изменить их можно было только правкой
кода.

Обратная проверка (лишние ключи в примере) не делается намеренно: часть строк
кормит `compose.yaml`, Grafana и Postgres, а не приложение, и требовать от
них соответствия `Settings` значило бы завести тест, который врёт.
"""

from __future__ import annotations

import re
from pathlib import Path

from scientific_tangle.config import Settings

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"

# Ключ-строка: с необязательным `#` и без отступов. Комментарий считается
# документированием, потому что `int | None` со пустым значением в файле
# окружения читается не как «не задано», а как строка "".
_KEY_LINE = re.compile(r"^\s*#?\s*([A-Z0-9_]{3,})\s*=")
# Прокидка в compose: `${КЛЮЧ:-значение}`; вместе с именем захватывается дефолт.
_COMPOSE_DEFAULT = re.compile(r"\$\{([A-Z0-9_]+):-([^}]*)\}")


def _same_value(text: str, default: object) -> bool:
    """Сравнение без ложного разъезда: `90` и `90.0` одно число, `true` и `True` одна правда."""
    if isinstance(default, bool):
        return text.strip().lower() == str(default).lower()
    try:
        return float(text) == float(default)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return text.strip() == str(default)


def documented_keys() -> set[str]:
    keys: set[str] = set()
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = _KEY_LINE.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def test_every_setting_is_documented_in_env_example() -> None:
    """Поле `Settings` без строки в примере = настройка, до которой не достать."""
    keys = documented_keys()
    missing = sorted(name.upper() for name in Settings.model_fields if name.upper() not in keys)
    assert missing == [], f"в .env.example нет этих настроек: {missing}"


def test_optional_int_knob_is_documented_commented() -> None:
    """Порог приёма к модели остаётся закомментированным, а не пустым.

    `AGENT_ADMISSION_LIMIT=` приложение получило бы как строку "", и валидация
    `int | None` отвергла бы значение на старте: пример не должен быть способом
    уронить контур.
    """
    lines = [
        line
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines()
        if _KEY_LINE.match(line) and _KEY_LINE.match(line).group(1) == "AGENT_ADMISSION_LIMIT"
    ]
    assert lines, "ключ пропал из примера"
    assert all(line.lstrip().startswith("#") for line in lines), lines


def test_documented_defaults_match_application_defaults() -> None:
    """Числа в примере не должны разъезжаться со значениями по умолчанию.

    Разъезд читается так: оператор правит `.env` по примеру и думает, что ничего
    не поменял, либо наоборот: правит пример, а контур живёт старым. Сравнение
    численные, потому что `90` и `90.0` это одно значение, а `true` и `True`
    одна правда.
    """
    annotated = {
        name.upper(): field.default
        for name, field in Settings.model_fields.items()
        if isinstance(field.default, (int, float, bool))
    }
    drifted: list[str] = []
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _KEY_LINE.match(line)
        if not match or "=" not in line:
            continue
        key = match.group(1)
        default = annotated.get(key)
        if default is None:
            continue
        text = line.split("=", 1)[1].strip()
        if not _same_value(text, default):
            drifted.append(f"{key}: в примере {text!r}, в приложении {default!r}")
    assert drifted == [], "; ".join(drifted)


def test_compose_defaults_match_application_defaults() -> None:
    """Третья нога контракта: дефолты прокидки `compose.yaml` не расходятся с кодом.

    Значение живёт в трёх местах: дефолт `Settings`, строка `.env.example` и
    `${КЛЮЧ:-значение}` в compose. Расходятся они по-тихому: контейнер стартует с
    одним потолком, локальный прогон без Docker читает другой, а пример обещает
    третий, и оператор правит не тот файл.
    """
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    passed = dict(_COMPOSE_DEFAULT.findall(compose))
    assert passed, "в compose.yaml не найдено ни одной прокидки вида КЛЮЧ:-значение"

    fields = {name.upper(): name for name in Settings.model_fields}
    drifted: list[str] = []
    for key, comp_default in sorted(passed.items()):
        name = fields.get(key)
        if name is None:
            continue
        default = Settings.model_fields[name].default
        if default is None:
            continue
        if not _same_value(comp_default, default):
            drifted.append(f"{key}: compose {comp_default!r}, приложение {default!r}")
    assert drifted == [], "; ".join(drifted)


def test_every_compose_passthrough_is_documented() -> None:
    """Ключ, который контейнер читает из окружения, обязан быть описан в примере.

    Прокидка без строки в `.env.example` настраивается только правкой
    `compose.yaml`: такой ручки оператор в примере не видит.
    """
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    passed = {key for key, _ in _COMPOSE_DEFAULT.findall(compose)}
    missing = sorted(passed - documented_keys())
    assert missing == [], f"прокинуты в compose, но не описаны в примере: {missing}"
