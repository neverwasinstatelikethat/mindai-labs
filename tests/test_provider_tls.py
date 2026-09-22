"""TLS-контур GigaChat: верификация остаётся включённой, отказ авторизации — наблюдаем.

GigaChat обслуживается цепочкой, упирающейся в российский доверенный корневой УЦ
(Минцифры), которого нет в хранилище certifi. Без дополнительного корня
верификация TLS отбивает авторизацию, и платформа навсегда сидит на
`model_mode: "unavailable"` — поэтому корень добавляется в доверенные якори, а
не обходится отключением проверки.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from scientific_tangle.config import Settings
from scientific_tangle.domain.contracts import PlanningBundle
from scientific_tangle.services.agent_metrics import AgentMetricsRegistry
from scientific_tangle.services.provider import (
    GigaChatProvider,
    ModelUnavailableError,
    _defect_summary,
)

ROOT_CERT = Path(__file__).resolve().parents[1] / "certs" / "russian-trusted-root-ca.crt"


def _settings(**overrides: object) -> Settings:
    return Settings(knowledge_backend="memory", **overrides)  # type: ignore[arg-type]


def _common_names(ca_certs: list[dict]) -> set[str]:
    # subject — кортеж RDN'ов, каждый RDN содержит свои (ключ, значение).
    return {
        value
        for cert in ca_certs
        for rdn in cert["subject"]
        for key, value in rdn
        if key == "commonName"
    }


def test_trusted_root_is_added_to_public_anchors() -> None:
    context = _settings(gigachat_trusted_roots=str(ROOT_CERT)).gigachat_ssl_context

    assert context is not None
    names = _common_names(context.get_ca_certs())
    assert "Russian Trusted Root CA" in names
    # Доверие российскому УЦ не снимает публичные корни: иначе любой другой
    # HTTPS-вызов процесса молча перестал бы верифицироваться.
    assert len(names) > 50


def test_without_trusted_root_sdk_keeps_its_own_defaults() -> None:
    assert _settings(gigachat_trusted_roots=None).gigachat_ssl_context is None


def test_disabled_verification_is_not_silently_reenabled() -> None:
    """Явное временное исключение не должно превращаться в «само включилось»."""
    settings = _settings(
        gigachat_trusted_roots=str(ROOT_CERT),
        gigachat_verify_ssl_certs=False,
    )

    assert settings.gigachat_ssl_context is None


def test_missing_root_file_stops_construction_not_first_request(tmp_path: Path) -> None:
    """Путь в файле проверяется при подъёме провайдера, а не 500-кой в прогоне."""
    settings = _settings(
        gigachat_api_key="test-key",
        gigachat_trusted_roots=str(tmp_path / "absent.crt"),
    )

    with pytest.raises(OSError):
        GigaChatProvider(settings, AgentMetricsRegistry())


class _BrokenAuthClient:
    """Клиент, у которого авторизация падает на транспорте (тот же класс отказа,
    что и неотренированный TLS-корень)."""

    def __init__(self, **_: object) -> None:
        pass

    async def aget_token(self) -> None:
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")


def test_repair_hint_names_the_offending_fields() -> None:
    """Повтор обязан получать поле, а не «Field required»: иначе правка расходится.

    Живой прогон GigaChat на PlanningBundle показывал модели три нарушения без
    путей — и вторая попытка возвращала уже другую поломку вместо исправления.
    """
    with pytest.raises(ValidationError) as error:
        PlanningBundle.model_validate({})

    summary = _defect_summary(error.value.errors())

    assert "intent: Field required" in summary
    assert "query_plan: Field required" in summary
    assert "action_plan: Field required" in summary


def test_repair_hint_is_capped_and_still_carries_paths() -> None:
    """Подсказка короткая: простыня на каждое нарушение раздувает промпт правки."""
    errors = [
        {"loc": ("claims", index, "predicate"), "msg": "Field required"} for index in range(5)
    ]

    summary = _defect_summary(errors)

    assert summary.split("; ") == [
        "claims/0/predicate: Field required",
        "claims/1/predicate: Field required",
        "claims/2/predicate: Field required",
    ]


@pytest.mark.asyncio
async def test_auth_failure_degrades_to_model_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scientific_tangle.services.provider.GigaChat", _BrokenAuthClient)
    provider = GigaChatProvider(
        _settings(gigachat_api_key="test-key"),
        AgentMetricsRegistry(),
    )

    with pytest.raises(ModelUnavailableError, match="transport error"):
        await provider._get_client()

    # Клиент не должен остаться «полуподнятым»: следующий запрос обязан
    # попытаться авторизоваться заново.
    assert provider._client is None
