from __future__ import annotations

import time

import httpx

from scientific_tangle.config import Settings


class YandexEmbeddingClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.yandex_api_key or not settings.folder_id:
            raise ValueError("YANDEX_API_KEY и FOLDER_ID обязательны для embeddings")
        self._settings = settings
        self._headers = {
            "Authorization": f"Api-Key {settings.yandex_api_key}",
            "Content-Type": "application/json",
        }

    def document(self, text: str) -> list[float]:
        return self._embed(text, "doc")

    def query(self, text: str) -> list[float]:
        return self._embed(text, "query")

    def _embed(self, text: str, kind: str) -> list[float]:
        payload = {
            "modelUri": self._settings.yandex_embedding_uri("doc" if kind == "doc" else "query"),
            "text": text[:8000],
        }
        with httpx.Client(timeout=30) as client:
            for attempt in range(4):
                response = client.post(
                    self._settings.yandex_embeddings_url,
                    json=payload,
                    headers=self._headers,
                )
                if response.status_code < 400:
                    vector = response.json()["embedding"]
                    return [float(value) for value in vector]
                if response.status_code != 429 and response.status_code < 500:
                    response.raise_for_status()
                if attempt == 3:
                    response.raise_for_status()
                time.sleep(min(2 ** (attempt + 1), 8))
        raise RuntimeError("Yandex embedding retry exhausted")
