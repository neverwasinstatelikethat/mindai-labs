"""Открытые источники: фрагменты API, а не сгенерированные моделью ответы.

Europe PMC доступен без ключа и ограничен научной литературой о живых системах.
Tavily с ключом даёт поиск по вебу. Сетевые адреса сервисов фиксированы: модель
не может читать внутренние URL. Результаты не записываются в общий корпус.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import HttpUrl, ValidationError

from scientific_tangle.domain.contracts import Finding, GraphSnapshot
from scientific_tangle.domain.models import EvidenceLocator
from scientific_tangle.services.knowledge import RetrievalContext

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_QUOTE_CHARS = 6000


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain(value: str) -> str:
    parser = _Text()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


def _excerpt(value: str, limit: int) -> str:
    # Срез в середине числа меняет значение цитаты (1200 -> 12).
    if len(value) <= limit:
        return value
    return value[:limit].rsplit(" ", 1)[0]


class PublicSourceSearch:
    def __init__(
        self, tavily_api_key: str | None = None,
        *, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = tavily_api_key
        self._transport = transport

    @property
    def description(self) -> str:
        return (
            "Tavily: поиск по открытым веб-страницам, только прочитанные фрагменты."
            if self._key else
            "Europe PMC: поиск аннотаций научных публикаций по биологии и медицине; "
            "не общий веб-поиск. Запрос формулируй на английском."
        )

    async def search(self, query: str, *, timeout: float = 20) -> RetrievalContext:
        async with httpx.AsyncClient(
            timeout=timeout, transport=self._transport, follow_redirects=False,
            headers={"User-Agent": "StormIdea/1.0 (scientific literature research)"},
        ) as client:
            if self._key:
                payload = await self._request(
                    client, "POST", "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={"query": query[:500], "max_results": 3,
                          "include_raw_content": "text", "include_answer": False},
                )
                rows = [
                    (item.get("title"), item.get("url"), item.get("raw_content"))
                    for item in payload.get("results", []) if isinstance(item, dict)
                ]
            else:
                payload = await self._request(
                    client, "GET", "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                    params={"query": query[:500], "format": "json", "resultType": "core",
                            "pageSize": 3},
                )
                rows = [
                    (item.get("title"),
                     f"https://europepmc.org/article/{item['source']}/{item['id']}",
                     item.get("abstractText"))
                    for item in payload.get("resultList", {}).get("result", [])
                    if isinstance(item, dict) and item.get("source") and item.get("id")
                ]
        findings: list[Finding] = []
        retrieved_at = datetime.now(UTC).isoformat()
        for title, url, text in rows[:3]:
            # Поисковый сниппет и автоматически сгенерированный answer не заменяют чтение.
            if not isinstance(title, str) or not isinstance(url, str) or not isinstance(text, str):
                continue
            if not title.strip() or not url.strip() or not text.strip():
                continue
            try:
                source_url = HttpUrl(url)
            except ValidationError:
                continue
            quote = _excerpt(_plain(text), MAX_QUOTE_CHARS)
            if not quote:
                continue
            identifier = uuid5(NAMESPACE_URL, str(source_url))
            findings.append(Finding(
                id=f"public-{identifier}", statement=_excerpt(quote, 500), confidence=0.5,
                status="hypothesis", scope={"origin": "public_source",
                                            "passage": "web_page" if self._key else "abstract"},
                evidence=[EvidenceLocator(
                    document_id=identifier, source_title=_plain(title),
                    source_url=str(source_url), retrieved_at=retrieved_at, quote=quote,
                )],
            ))
        return RetrievalContext(
            findings=findings, graph=GraphSnapshot(nodes=[], edges=[]),
            community_summaries=[], no_evidence=not findings,
        )

    @staticmethod
    async def _request(
        client: httpx.AsyncClient, method: str, url: str, **kwargs: Any,
    ) -> dict[str, Any]:
        async with client.stream(method, url, **kwargs) as response:
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_RESPONSE_BYTES:
                    raise ValueError("Ответ открытого источника превышает допустимый размер")
        payload = json.loads(chunks)
        if not isinstance(payload, dict):
            raise ValueError("Открытый источник вернул некорректную форму ответа")
        return payload
