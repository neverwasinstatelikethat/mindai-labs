"""Повторы эмбеддингов: постоянный отказ аккаунта не должен стоить минут ожидания.

Живой контур с неоплаченным `/embeddings` давал `402` на каждом поиске. Четыре
попытки с backoff 2+4+8 = 14 с под общим замком превращали постоянный отказ
тарифа в минуты, за которые новичок не получал ответа вообще — хотя лексическая
ветка была рабочая.
"""

from __future__ import annotations

import threading
import time

import pytest
from gigachat.exceptions import GigaChatException

from scientific_tangle.services.embeddings import (
    EmbeddingError,
    GigaChatEmbeddingClient,
    _refused_for_good,
)


class _Status(GigaChatException):
    """Так же, как `ResponseError` в SDK: носитель статуса внутри границы сети."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


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
    calls: list[int] = []

    client = GigaChatEmbeddingClient.__new__(GigaChatEmbeddingClient)
    client._lock = threading.Lock()
    client._model = "Embeddings"
    client._dimensions = 4

    def _refuse(*args: object, **kwargs: object) -> None:
        calls.append(1)
        raise _Status(402)

    client._client = type("Stub", (), {"embeddings": staticmethod(_refuse)})()
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda value: slept.append(value))

    with pytest.raises(EmbeddingError):
        client.query("вопрос")

    assert len(calls) == 1
    assert slept == []


def test_transient_failure_still_gets_four_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    client = GigaChatEmbeddingClient.__new__(GigaChatEmbeddingClient)
    client._lock = threading.Lock()
    client._model = "Embeddings"
    client._dimensions = 4

    def _busy(*args: object, **kwargs: object) -> None:
        calls.append(1)
        raise _Status(429)

    client._client = type("Stub", (), {"embeddings": staticmethod(_busy)})()
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda value: slept.append(value))

    with pytest.raises(EmbeddingError):
        client.query("вопрос")

    assert len(calls) == 4
    assert slept == [2.0, 4.0, 8.0]
