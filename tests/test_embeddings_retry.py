"""Повторы и мемо эмбеддингов: отказ не должен стоить минут, повтор — переплат.

Живой контур с неоплаченным `/embeddings` давал `402` на каждом поиске. Четыре
попытки с backoff 2+4+8 = 14 с под общим замком превращали постоянный отказ
тарифа в минуты, за которые новичок не получал ответа вообще — хотя лексическая
ветка была рабочая. Поверх этого зависший эндпоинт держал замок на четыре
полных транспортных таймаута, а ``infrastructure._semantic_ids`` пересчитывал
эмбеддинг одного и того же вопроса на каждом retrieval-действии. Отсюда три
правила, зафиксированных тестами: постоянный отказ — одна попытка; транзиентный —
четыре попытки, но суммарно не дольше ``gigachat_timeout_seconds``; идентичный
текст переиспользуется из TTL-мемо, ошибка в мемо не попадает.
"""

from __future__ import annotations

import threading
import time

import pytest
from gigachat.exceptions import GigaChatException

from scientific_tangle.services.embeddings import (
    EmbeddingError,
    GigaChatEmbeddingClient,
    _EmbeddingCache,
    _refused_for_good,
)


class _Status(GigaChatException):
    """Так же, как `ResponseError` в SDK: носитель статуса внутри границы сети."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class _Item:
    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class _Response:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_Item(index, vector) for index, vector in enumerate(vectors)]


def _stub_client(
    embeddings,  # noqa: ANN001 - сигнатура заглушки SDK
    *,
    budget_seconds: float = 90.0,
    cache_ttl_seconds: float = 3600.0,
) -> GigaChatEmbeddingClient:
    """Клиент без сети и без конструктора GigaChat: только проверяемые поля."""
    client = GigaChatEmbeddingClient.__new__(GigaChatEmbeddingClient)
    client._lock = threading.Lock()
    client._model = "Embeddings"
    client._dimensions = 4
    client._retry_budget_seconds = budget_seconds
    client._cache = _EmbeddingCache(ttl_seconds=cache_ttl_seconds, max_entries=64)
    client._client = type("Stub", (), {"embeddings": staticmethod(embeddings)})()
    return client


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
def test_client_side_refusals_are_terminal(status: int) -> None:
    assert _refused_for_good(_Status(status))


@pytest.mark.parametrize("status", [408, 409, 425, 429])
def test_overload_statuses_still_get_retried(status: int) -> None:
    assert not _refused_for_good(_Status(status))


def test_error_without_status_keeps_the_retry_budget() -> None:
    """Обрыв соединения — задержка, а не приговор: его чинить повторами."""
    assert not _refused_for_good(RuntimeError("соединение оборвано"))


def test_permanent_refusal_does_not_burn_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """402 снимается одной попыткой и без sleep — деградация в лексику сразу.

    Повторный запрос обязан снова дойти до сети: отказ в мемо не пишется, иначе
    починенный тариф навсегда остался бы «недоступным» внутри TTL.
    """
    calls: list[int] = []

    def _refuse(*args: object, **kwargs: object) -> None:
        calls.append(1)
        raise _Status(402)

    client = _stub_client(_refuse)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda value: slept.append(value))

    with pytest.raises(EmbeddingError):
        client.query("вопрос")
    with pytest.raises(EmbeddingError):
        client.query("вопрос")

    assert len(calls) == 2
    assert slept == []


def test_transient_failure_still_gets_four_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Интенст прежнего теста сохранён: 429 — четыре попытки с backoff 2+4+8.

    Таймер бюджета реальный, а sleep заглушён — часы почти не идут, поэтому
    бюджет `gigachat_timeout_seconds` не отсекает ни одну из попыток.
    """
    calls: list[int] = []

    def _busy(*args: object, **kwargs: object) -> None:
        calls.append(1)
        raise _Status(429)

    client = _stub_client(_busy, budget_seconds=90.0)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda value: slept.append(value))

    with pytest.raises(EmbeddingError):
        client.query("вопрос")

    assert len(calls) == 4
    assert slept == [2.0, 4.0, 8.0]


def test_retry_budget_bounds_the_whole_lock_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Зависший эндпоинт не может держать замок дольше общего бюджета.

    Часы идут вместе с паузами: как только остаток бюджета меньше одной
    сетевой доли, цикл останавливается — суммарно sleep + попытки остаются
    внутри ``gigachat_timeout_seconds`` (10 с), а не размазываются на
    14 с пауз плюс четыре полных таймаута.
    """
    clock = [100.0]
    calls: list[int] = []
    slept: list[float] = []

    def _now() -> float:
        return clock[0]

    def _advance_sleep(value: float) -> None:
        slept.append(value)
        clock[0] += value

    def _busy(*args: object, **kwargs: object) -> None:
        calls.append(1)
        raise _Status(429)

    monkeypatch.setattr(time, "monotonic", _now)
    monkeypatch.setattr(time, "sleep", _advance_sleep)
    client = _stub_client(_busy, budget_seconds=10.0)

    with pytest.raises(EmbeddingError):
        client.query("вопрос")

    # Доля одной попытки = 10/4 = 2.5: после третьей попытки остаток 0 с —
    # четвёртая не начинается, третья пауза урезана с 8 с до остатка.
    assert len(calls) == 3
    assert slept == [2.0, 4.0, 4.0]
    assert clock[0] - 100.0 <= 10.0


def test_identical_query_is_embedded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тот же текст в пределах TTL не идёт в сеть и не трогает замок.

    ``_semantic_ids`` дёргает query на каждом retrieval-действии каждого
    раунда: без мемо один и тот же вопрос платится вектором несколько раз.
    """
    requested: list[list[str]] = []

    def _answer(texts: list[str], **kwargs: object) -> _Response:
        requested.append(list(texts))
        return _Response([[float(len(text)), 1.0, 2.0, 3.0] for text in texts])

    monkeypatch.setattr(time, "sleep", lambda value: None)
    client = _stub_client(_answer)

    first = client.query("Какое извлечение меди даёт РЕАКОМ-М?")
    second = client.query("Какое извлечение меди даёт РЕАКОМ-М?")

    assert requested == [["Какое извлечение меди даёт РЕАКОМ-М?"]]
    assert first == second
    # Наружу уходит копия: мутация вектора одним вызывающим не портит мемо.
    assert first is not second


def test_batch_partially_served_from_memo() -> None:
    """Известные тексты батча не переотправляются: в сеть летит только новое."""
    requested: list[list[str]] = []

    def _answer(texts: list[str], **kwargs: object) -> _Response:
        requested.append(list(texts))
        return _Response([[float(len(text)), 1.0, 2.0, 3.0] for text in texts])

    client = _stub_client(_answer)

    client.document("документ-а")
    vectors = client.documents(["документ-а", "документ-б"])

    assert requested == [["документ-а"], ["документ-б"]]
    assert len(vectors) == 2
    assert vectors[0] == [float(len("документ-а")), 1.0, 2.0, 3.0]


def test_memo_expiry_forces_reembed(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTL истёк — текст снова дорогой: устаревшие векторы не вечно живые."""
    clock = [0.0]
    requested: list[list[str]] = []

    def _answer(texts: list[str], **kwargs: object) -> _Response:
        requested.append(list(texts))
        return _Response([[0.5] * 4 for _ in texts])

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    client = _stub_client(_answer, cache_ttl_seconds=5.0)

    client.query("вопрос")
    clock[0] = 6.0
    client.query("вопрос")

    assert len(requested) == 2


def test_memo_disabled_with_non_positive_ttl() -> None:
    """embedding_cache_ttl_seconds <= 0 — мемо нет, поведение как без кэша."""
    requested: list[list[str]] = []

    def _answer(texts: list[str], **kwargs: object) -> _Response:
        requested.append(list(texts))
        return _Response([[0.5] * 4 for _ in texts])

    client = _stub_client(_answer, cache_ttl_seconds=0.0)

    client.query("вопрос")
    client.query("вопрос")

    assert len(requested) == 2


def test_success_then_refusal_keeps_last_good_vector() -> None:
    """Успешный вектор уже в мемо: последующий отказ не откатывает retrieval.

    Degradation-путь 402 (эмбеддинги недоступны → лексическая ветка) обязан
    работать и для новых текстов: отказ в сеть уходит каждый раз заново.
    """
    responses: list[object] = []
    requested: list[list[str]] = []

    def _answer(texts: list[str], **kwargs: object) -> object:
        requested.append(list(texts))
        reply = responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return _Response([[0.25] * 4 for _ in texts])

    client = _stub_client(_answer)

    responses.append(object())
    assert client.query("первый") == [0.25] * 4

    responses.append(_Status(402))
    with pytest.raises(EmbeddingError):
        client.query("второй")
    # Повтор отказа — снова сеть (в мемо ошибки нет), а первый текст — нет.
    responses.append(_Status(402))
    with pytest.raises(EmbeddingError):
        client.query("второй")

    assert requested == [["первый"], ["второй"], ["второй"]]
    # Мемо отдаёт первый текст без сети: запросов стало не больше.
    assert client.query("первый") == [0.25] * 4
    assert len(requested) == 3
