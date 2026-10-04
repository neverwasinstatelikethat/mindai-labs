"""Отказ провайдера или хранилища называется аналитику без сырого ответа.

`degradation_reasons` и деталь 503 читает человек, и текст туда попадает ещё раз
в промпте модели. ``gigachat.ResponseError`` и ``ConnectionError`` приводят к
строке с адресом, телом ответа и заголовками (``x-request-id``): это разведданные
о контуре, а не причина для пользователя. Проверки ниже держат этот контракт на
трёх путях, где текст раньше уходил наружу напрямую.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from scientific_tangle.services.infrastructure import Neo4jElasticsearchKnowledgeBase
from scientific_tangle.services.provider import (
    GigaChatProvider,
    ModelUnavailableError,
    redact_provider_error,
)

# Ровно так выглядит живой отказ тарифа: код, адрес шлюза, тело и заголовки.
RAW_RESPONSE_ERROR = (
    "402 https://gw.u3.gigachat.mts.ru:443/v1/completion: "
    "b'{\"status\":402,\"errors\":[\"\"]} ', "
    "Headers({'x-request-id': '9a7c-42', 'content-type': 'application/json'})"
)

LEAKED = ("mts.ru", "x-request-id", "Headers(", "9a7c-42", 'b\'{"status"')


def test_coded_refusal_is_named_by_meaning_not_by_the_reply():
    note = redact_provider_error(RAW_RESPONSE_ERROR, context="модель")

    assert note == "модель: тариф провайдера не оплачен (HTTP 402)"
    for fragment in LEAKED:
        assert fragment not in note


def test_uncoded_failure_keeps_the_sentence_but_loses_addresses_and_headers():
    error = httpx.ConnectError(
        "Не удалось соединиться с bolt://10.12.3.44:7687 после 3 попыток, "
        "Headers({'x-request-id': 'ab12'}) b'{\"errors\":[1]}'"
    )

    note = redact_provider_error(error, context="хранилище")

    assert note.startswith("хранилище: Не удалось соединиться с")
    assert "10.12.3.44" not in note
    assert "x-request-id" not in note
    assert len(note) <= len("хранилище: ") + 120


def test_error_body_from_the_model_is_reduced_to_the_status():
    note = redact_provider_error(
        str({"status": 402, "errors": ["payment required"]}),
        context="GigaChat вернул ошибку",
    )

    assert note == "GigaChat вернул ошибку: тариф провайдера не оплачен (HTTP 402)"


def test_provider_wraps_transport_failure_without_the_endpoint():
    error = GigaChatProvider._wrap(
        httpx.ConnectError("connect timeout for https://gw.u3.gigachat.mts.ru/oauth/token")
    )

    assert isinstance(error, ModelUnavailableError)
    assert str(error).startswith("GigaChat transport error:")
    assert "gigachat.mts.ru" not in str(error)
    assert "oauth/token" not in str(error)


def test_retrieval_degradation_note_names_the_branch_not_the_connection():
    note = Neo4jElasticsearchKnowledgeBase._note_degradation(
        SimpleNamespace(),
        "векторная",
        httpx.ConnectError(
            "Connection error for https://elasticsearch.internal:9200/knowledge-entities, "
            "Headers({'x-elastic-product': 'Elasticsearch'})"
        ),
    )

    assert note.startswith("Ветка retrieval «векторная» завершилась ошибкой: хранилище:")
    assert "elasticsearch.internal" not in note
    assert "x-elastic-product" not in note
