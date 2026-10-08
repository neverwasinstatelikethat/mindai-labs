"""Контракт поверхности: у каждого `/api/v1`-маршрута есть потребитель или названная причина.

Проверка появилась из съёма ревью: маршруты переживают свои экраны молча. Сервер
начинает писать данные, интерфейс о них не знает, и обещание «история решений
сохранена» остаётся строчкой в тесте. Обратная сторона опаснее: новый маршрут без
потребителя выглядит как работа, сделанная до конца.

Список разрешений разделён на две группы, и различие принципиальное:

* `SERVICE_ONLY` — маршрут существует для инфраструктуры или оператора, и экрана у
  него не будет по контракту;
* `NO_SURFACE_YET` — это продуктовые данные без поверхности, то есть открытое
  намерение ревью (№8), а не завершённая работа. Список пуст с 3 октября 2026 года:
  маршрутов, которые сервер пишет, а интерфейс не читает, не осталось. Запись,
  вернувшаяся сюда, — новый открытый долг, и она обязана называть, чего не хватает.

Сверка двусторонняя: множество маршрутов без потребителя обязано равняться объединению
списков. Поэтому новый маршрут без экрана роняет проверку, а добавленный экран требует
убрать запись из списка, иначе она превращается в ложь о состоянии продукта.

Потребителем считается вызов, а не объявление и не упоминание. Функция клиента в
`frontend/src/lib/api.ts` сама по себе маршрут не закрывает: иначе заглушка в транспорте
прятала бы отсутствие экрана. Комментарий не потребитель тоже: так было с `/demo` (слово
о нём в тексте витрины) и с пояснением в словаре `terms.ts`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from scientific_tangle.api.app import app
from scientific_tangle.domain.contracts import (
    ConflictReview,
    EvolutionExperiment,
    EvolutionProposal,
    ExpertDecision,
    ExportFormat,
    MergeReviewRequest,
)

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = ROOT / "frontend" / "src"
API_CLIENT = FRONTEND_SRC / "lib" / "api.ts"
PREFIX = "/api/v1"

SERVICE_ONLY: dict[str, str] = {
    f"{PREFIX}/audit": (
        "операторский журнал с правом audit:read; пользовательской ленты аудита нет"
    ),
    f"{PREFIX}/corpus/stats": (
        "сводка корпуса для внутренней диагностики состава, экран её не показывает"
    ),
    f"{PREFIX}/demo": (
        "пошаговый прогон живого контура для замера и разбора: ни один экран его не зовёт "
        "(витрина объясняет это словом и не притворяется прогоном), доступ закрыт сессией и Origin"
    ),
    f"{PREFIX}/documents": (
        "программный импорт по JSON-телу (описан в src/README.md); интерфейс корпуса "
        "загружает файлы через /documents/upload, экранного потребителя у этого формата нет"
    ),
    f"{PREFIX}/agents/metrics": (
        "диагностический снимок агентных метрик для разбора прогона; Prometheus читает "
        "/metrics, пользовательской витрины у этого JSON нет по контракту"
    ),
    f"{PREFIX}/notifications": (
        "фид последних событий для опроса внешним читателем: по PRODUCT.md ленты и колокола "
        "в интерфейсе нет, формулировка обязана оставаться «записано в ленте, читаем опросом»"
    ),
    f"{PREFIX}/evaluations/retrieval-benchmark": (
        "прогон retrieval-оценки запускают оператор и CI (описан в src/README.md), "
        "интерфейса он не требует"
    ),
    f"{PREFIX}/evaluations/pipeline-benchmark": (
        "конвейерная оценка: тот же порядок вызова, что у retrieval-прогона"
    ),
    f"{PREFIX}/evaluations": "служебная история оценочных прогонов, пользовательского экрана нет",
    f"{PREFIX}/evaluations/gold": "эталонные примеры читают оценочные прогоны, не интерфейс",
    f"{PREFIX}/experiments": (
        "служебные записи экспериментов политики агента, пользовательского экрана нет"
    ),
    f"{PREFIX}/proposals": "внутренние предложения самообучения не показываются пользователю",
    f"{PREFIX}/proposals/{{proposal_id}}/experiment": (
        "запуск сравнительного прогона доступен только внутреннему контуру"
    ),
    f"{PREFIX}/proposals/{{proposal_id}}/review": (
        "решение по внутренним предложениям не является действием пользователя"
    ),
    f"{PREFIX}/me/usage": (
        "сводка использования аккаунта не является поверхностью пользовательского интерфейса"
    ),
}

NO_SURFACE_YET: dict[str, str] = {}

pytestmark = pytest.mark.skipif(
    not FRONTEND_SRC.is_dir(), reason="фронтенда в этом checkout нет: сверять потребителей не с чем"
)


def _strip_comments(text: str) -> str:
    """Комментарии в сторону: пояснение в словаре не является вызовом.

    `//` снимается только вне префикса `://`, иначе адрес бэкенда из строки
    превратился бы в обрезанный код.
    """

    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", " ", text)


def _call_sites() -> str:
    """Код фронтенда без самого клиента и без комментариев: здесь живут вызовы."""

    return "\n".join(
        _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for path in sorted(FRONTEND_SRC.rglob("*"))
        if path.suffix in {".ts", ".js", ".svelte"} and path != API_CLIENT
    )


def _client_paths() -> list[tuple[str, str]]:
    """Пары (функция клиента, литерал пути) из `frontend/src/lib/api.ts`.

    Записи объекта `api` идут с двумя отступами, поэтому границы функции
    восстанавливаются по следующему объявлению: тело между ними и есть окно, в
    котором лежит путь этого вызова.
    """

    text = API_CLIENT.read_text(encoding="utf-8")
    marks = [
        (match.group(1), match.start())
        for match in re.finditer(r"^  (\w+): ", text, re.MULTILINE)
    ]
    pairs: list[tuple[str, str]] = []
    for index, (name, start) in enumerate(marks):
        end = marks[index + 1][1] if index + 1 < len(marks) else len(text)
        for literal in re.findall(r"/api/v1/[^\s'\"`]+", text[start:end]):
            pairs.append((name, literal.split("?")[0]))
    return pairs


def _segments(path: str) -> tuple[str, ...]:
    """Путь как кортеж сегментов: параметр вида `{id}` или `${id}` становится `*`."""

    tail = path[len(PREFIX) :] if path.startswith(PREFIX) else path
    return tuple(
        "*" if segment.startswith(("{", "$")) else segment
        for segment in tail.split("/")
        if segment
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
    """Маршруты, которые интерфейс не читает.

    Объявление пути в клиенте (`api.ts`) потребителем НЕ считается: экран,
    которого нет, функция-заглушка не закрывает. Маршрут считается прочитанным,
    если вызов его клиентской функции (`api.<имя>`) или сам литерал пути
    встречается где-то вне транспорта. Иначе проверка молча разрешала бы «клиент
    есть, экрана нет» ровно то состояние ради которого она и написана.
    """

    paths = sorted(path for path in app.openapi().get("paths", {}) if path.startswith(PREFIX))
    pairs = _client_paths()
    blob = _call_sites()
    silent: list[str] = []
    for path in paths:
        segments = _segments(path)
        names = {name for name, literal in pairs if _segments(literal) == segments}
        called = any(
            re.search(rf"\bapi\.{re.escape(name)}\s*\(", blob)
            or re.search(rf"[{{,]\s*{re.escape(name)}\s*[,}}]", blob)
            for name in names
        )
        # Хвост `(?!/)` обязателен: без него префикс `/proposals` засчитывался бы
        # вызовом соседнего `/proposals/{proposal_id}/review`.
        pattern = _consumer_pattern(path[len(PREFIX) :])
        written_outside = bool(re.search(pattern.pattern + r"(?!/)", blob))
        if not (called or written_outside):
            silent.append(path)
    return silent


def test_silent_routes_are_exactly_the_allowed_lists() -> None:
    assert routes_without_consumer() == sorted({*SERVICE_ONLY, *NO_SURFACE_YET})


def test_routes_without_a_nav_entry_are_named_and_reasoned() -> None:
    """Раздел без ссылки в меню либо назван здесь, либо его не существует.

    Обратная сторона №8: экран появляется, а добраться до него можно только по
    прямому адресу. Тогда он для аналитика не существует, и проверка «у Surface
    есть потребитель» этого не заметит: она смотрит на серверные маршруты.
    """

    nav = (FRONTEND_SRC / "lib" / "nav.ts").read_text(encoding="utf-8")
    linked = set(re.findall(r"href: '(/[a-z0-9-]+)'", nav))
    pages = {
        f"/{path.parent.name}"
        for path in FRONTEND_SRC.glob("routes/(app)/*/+page.svelte")
    }
    # Экран профиля доступен из шапки (аватар), а не из списка разделов.
    outside_nav = {"/account"}
    for href in sorted(linked):
        assert href in pages | outside_nav, f"мень зовёт на {href}, экрана там нет"
    assert pages - linked == outside_nav, (
        f"экран без ссылки в меню: {sorted(pages - linked - outside_nav)}, "
        f"лишние записи: {sorted(outside_nav - pages)}"
    )


def test_matching_has_teeth_against_comments_and_prefixes() -> None:
    """Две дыры, которые эта сверка держала на себе, и закрываются здесь.

    Пояснение в словаре `terms.ts` упоминало маршрут прямым текстом и превращалось
    в «потребителя»; префикс `/proposals` засчитывал вызов соседнего
    `/proposals/{id}/review`. Оба случая проверяются отдельно, чтобы любая будущая
    правка сопоставления не вернула их молча.
    """

    assert "api.findings(" not in _strip_comments("// api.findings(50, 0) тут не вызов")
    assert "живой" in _strip_comments("код // хвост\nживой")
    # Адрес не должен обрезаться на `://`.
    assert "http://127.0.0.1:46617" in _strip_comments("const u = 'http://127.0.0.1:46617';")

    prefix = _consumer_pattern("/entity-resolution/proposals")
    review_literal = "/api/v1/entity-resolution/proposals/${id}/review"
    assert re.search(prefix.pattern + r"(?!/)", review_literal) is None


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


def test_analytical_hypotheses_use_a_dedicated_api_and_evidence_surface() -> None:
    """Гипотезы-аналитические сигналы не должны подменяться сырыми находками."""
    types = (FRONTEND_SRC / "lib" / "types.ts").read_text(encoding="utf-8")
    api = API_CLIENT.read_text(encoding="utf-8")
    page = (FRONTEND_SRC / "routes" / "(app)" / "findings" / "+page.svelte").read_text(
        encoding="utf-8"
    )
    card_path = FRONTEND_SRC / "lib" / "ui" / "HypothesisCard.svelte"
    card = card_path.read_text(encoding="utf-8") if card_path.exists() else ""

    assert "export interface HypothesisSignal" in types
    assert "requestWithTotal<HypothesisSignal[]>" in api
    assert "`/api/v1/hypotheses?limit=${limit}&offset=${offset}`" in api
    assert "request<HypothesisSignal>(`/api/v1/hypotheses/${encodeURIComponent(id)}`)" in api
    assert "api.hypotheses(" in page
    assert "hypotheses = mergeHypotheses([], result.items)" in page
    assert "{#each hypotheses as signal (signal.id)}" in page
    assert "<HypothesisCard {signal} />" in page
    assert "api.findings(" in page and "api.upload(file)" in page
    assert "params.set('hypothesis', signal.id)" in card
    assert "params.set('evidence', String(evidenceIndex))" in card
    assert "params.set('source_quote'" not in card and "params.set('source_title'" not in card
    assert "api.hypothesis(signalId)" in page
    assert "sourceEvidence.quote" in page and "id=\"source-view-title\"" in page
    assert "params.get('source_quote')" not in page
    assert "sourcePhase === 'loading'" in page and "sourcePhase === 'failed'" in page
    assert "sourcePhase === 'empty'" in page
    assert "data_class" in card


def _ts_object(text: str, name: str) -> str:
    """Тело литерала объекта по имени объявления, включая вложенные фигурные скобки."""
    anchor = text.index(f"const {name}")
    open_at = text.index("{", anchor)
    depth, quote = 0, ""
    for index in range(open_at, len(text)):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : index]
    raise AssertionError(f"не закрыт литерал {name}")


_KEY = re.compile(r"^\s*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][\w.-]*))\s*:", re.M)


def _ts_keys(body: str) -> set[str]:
    return {next(group for group in match.groups() if group) for match in _KEY.finditer(body)}


def test_history_screen_names_every_recorded_decision_verb() -> None:
    """«Ваши экспертные решения» обязаны называть глагол, а не только «запись успешна».

    Съём экрана 4 октября показал четыре одинаковые строки «Решение по склейке
    сущностей / успех»: сервис кладёт глагол решения в `metadata` той же записи, а
    экран его выбрасывал, и эксперт не восстанавливал, что он сделал с парой.
    Сверка двусторонняя: каждое значение, которое бэкенд правда умеет писать, обязано
    быть подписано, и ни одной подписи выдуманного значения быть не должно.
    """
    terms = (FRONTEND_SRC / "lib" / "terms" / "merges.ts").read_text(encoding="utf-8")
    body = _ts_object(terms, "DECISION_DETAILS")
    details = {
        action: _ts_keys(inner)
        for action, inner in re.findall(r"'([\w.]+)':\s*\{([^}]*)\}", body)
    }
    # Действие записано в контракте, а глагола у него нет: `proposal.created` и
    # `claim.superseded` описываются идентификатором и версией, решать там нечего.
    verbs: dict[str, set[str]] = {
        "resolution.reviewed": set(
            get_args(MergeReviewRequest.model_fields["action"].annotation)
        ),
        "conflict.reviewed": set(get_args(ConflictReview.model_fields["status"].annotation)),
        "proposal.reviewed": (
            set(get_args(EvolutionProposal.model_fields["status"].annotation)) - {"proposed"}
        )
        | set(get_args(EvolutionExperiment.model_fields["decision"].annotation)),
        "answer.exported": set(get_args(ExportFormat)),
    }

    assert details, "в терминах нет ни одной подписи глагола решения"
    for action, values in verbs.items():
        assert values, f"контракт {action} не называет ни одного значения"
        assert details.get(action) == values, (
            f"история решений называет {sorted(details.get(action, set()))}, "
            f"а сервис пишет {sorted(values)}"
        )

    account_terms = (FRONTEND_SRC / "lib" / "terms" / "account.ts").read_text(
        encoding="utf-8"
    )
    labelled = _ts_keys(_ts_object(account_terms, "DECISION_ACTION_LABELS"))
    actions = set(get_args(ExpertDecision.model_fields["action"].annotation))
    assert actions - labelled == set(), (
        f"действие попалось в истории без читаемого имени: {sorted(actions - labelled)}"
    )
    assert actions, "контракт ExpertDecision не перечисляет действий"


def test_the_verb_check_has_teeth() -> None:
    """Проверка не должна превратиться в сверку саму с собой.

    Парсер обязан различать ключи без кавычек, с кавычками и с дефисом, видеть второй
    блок и не проходить на пустом теле. Иначе недостающая подпись стала бы молчанием,
    а не падением. Ключи читаются с начала строки: таков формат литерала в `terms.ts`.
    """
    sample = _ts_object(
        "const THING = {\n  'a': {\n    accept: 'x',\n    'json-ld': 'y',\n  },\n"
        "  b: {\n    reject: 'z',\n  },\n};",
        "THING",
    )
    parsed = dict(re.findall(r"'?([\w.]+)'?:\s*\{([^}]*)\}", sample))
    assert _ts_keys(parsed["a"]) == {"accept", "json-ld"}
    assert _ts_keys(parsed["b"]) == {"reject"}
    assert _ts_object("const EMPTY = {};", "EMPTY").strip() == ""
