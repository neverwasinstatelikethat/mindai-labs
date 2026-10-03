"""Контракт поверхности: у каждого `/api/v1`-маршрута есть потребитель или названная причина.

Проверка появилась из съёма ревью: маршруты переживают свои экраны молча. Сервер
начинает писать данные, интерфейс о них не знает, и обещание «история решений
сохранена» остаётся строчкой в тесте. Обратная сторона опаснее: новый маршрут без
потребителя выглядит как работа, сделанная до конца.

Список разрешений разделён на две группы, и различие принципиальное:

* `SERVICE_ONLY` — маршрут существует для инфраструктуры или оператора, и экрана у
  него не будет по контракту;
* `NO_SURFACE_YET` — это продуктовые данные без поверхности, то есть открытое
  намерение ревью (№8), а не завершённая работа.

Сверка двусторонняя: множество маршрутов без потребителя обязано равняться объединению
списков. Поэтому новый маршрут без экрана роняет проверку, а добавленный экран требует
убрать запись из списка, иначе она превращается в ложь о состоянии продукта.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scientific_tangle.api.app import app

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = ROOT / "frontend" / "src"
PREFIX = "/api/v1"

SERVICE_ONLY: dict[str, str] = {
    f"{PREFIX}/agents/metrics": (
        "диагностический снимок агентных метрик для разбора прогона; Prometheus читает "
        "/metrics, пользовательской витрины у этого JSON нет по контракту"
    ),
    f"{PREFIX}/evaluations/retrieval-benchmark": (
        "прогон retrieval-оценки запускают оператор и CI (описан в src/README.md), "
        "интерфейса он не требует"
    ),
    f"{PREFIX}/evaluations/pipeline-benchmark": (
        "конвейерная оценка: тот же порядок вызова, что у retrieval-прогона"
    ),
}

NO_SURFACE_YET: dict[str, str] = {
    f"{PREFIX}/decisions": "открытое №8: сервер пишет решения, истории решений на экране нет",
    f"{PREFIX}/entity-resolution/proposals": (
        "открытое №8: очередь предложений слияния без поверхности"
    ),
    f"{PREFIX}/entity-resolution/proposals/{{proposal_id}}/review": (
        "открытое №8: запись решения по предложению слияния вызывается только из теста"
    ),
    f"{PREFIX}/me/usage": (
        "открытое №8: расход модели своим аккаунтом («сколько стоили мои вопросы») "
        "не показан ни на одном экране"
    ),
    f"{PREFIX}/notifications": (
        "открытое №8: сервер держит фид последних событий, интерфейс его не читает, "
        "поэтому о событиях пользователь не узнаёт"
    ),
}

pytestmark = pytest.mark.skipif(
    not FRONTEND_SRC.is_dir(), reason="фронтенда в этом checkout нет: сверять потребителей не с чем"
)


def _frontend_blob() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(FRONTEND_SRC.rglob("*"))
        if path.suffix in {".ts", ".js", ".svelte"}
    )


def _consumer_pattern(tail: str) -> re.Pattern[str]:
    """Путь с `{параметром}` превращается в шаблон, годный и для `${id}`, и для голого слова."""
    segments = []
    for segment in tail.split("/"):
        if not segment:
            continue
        if segment.startswith("{"):
            segments.append(r"(?:\$\{[^}]*\}|[^/${'\"]+)")
        else:
            segments.append(re.escape(segment))
    return re.compile("/".join(["", *segments]))


def routes_without_consumer() -> list[str]:
    paths = sorted(path for path in app.openapi().get("paths", {}) if path.startswith(PREFIX))
    blob = _frontend_blob()
    silent: list[str] = []
    for path in paths:
        pattern = _consumer_pattern(path[len(PREFIX) :])
        full = re.compile(re.escape(PREFIX) + pattern.pattern)
        if full.search(blob) or pattern.search(blob):
            continue
        silent.append(path)
    return silent


def test_silent_routes_are_exactly_the_allowed_lists() -> None:
    assert routes_without_consumer() == sorted({*SERVICE_ONLY, *NO_SURFACE_YET})


def test_no_surface_entries_have_reasons() -> None:
    """Разрешение без объяснения — это не разрешение, а забытая запись."""
    for path, reason in {**SERVICE_ONLY, **NO_SURFACE_YET}.items():
        assert path.startswith(PREFIX), path
        assert len(reason) > 40, f"{path}: причина не объясняет ничего"


def test_service_only_and_pending_surfaces_do_not_overlap() -> None:
    """Нельзя одновременно значить «экрана не будет» и «экрана ещё нет»."""
    assert not set(SERVICE_ONLY) & set(NO_SURFACE_YET)


def test_routes_really_are_read_from_openapi() -> None:
    """Пустая карта маршрутов превратила бы сверку в молчание.

    Если `app.openapi()` не соберётся (например, сломается импорт), `silent` станет
    пустым, и первая проверка «пройдёт» при полном наборе разрешений. Поэтому опорные
    маршруты названы здесь явно.
    """
    paths = app.openapi().get("paths", {})
    for reference in (
        f"{PREFIX}/findings",
        f"{PREFIX}/conflicts",
        f"{PREFIX}/auth/me",
    ):
        assert reference in paths, f"в OpenAPI нет опорного маршрута {reference}"
    assert len(paths) > 25, f"OpenAPI подозрительно мал: {len(paths)} путей"
