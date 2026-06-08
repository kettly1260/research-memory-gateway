from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

import httpx

from .config import EmbeddingConfig, RerankConfig


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


@dataclass(frozen=True)
class EmbeddingClient:
    config: EmbeddingConfig

    @property
    def enabled(self) -> bool:
        return self.config.enabled and bool(self.base_url)

    @property
    def base_url(self) -> str:
        return os.getenv(self.config.base_url_env, "").rstrip("/")

    @property
    def model(self) -> str:
        return os.getenv(self.config.model_env, "")

    def embed(self, text: str) -> list[float] | None:
        if not self.enabled or not text.strip():
            return None
        payload: dict[str, Any] = {"input": text}
        if self.model:
            payload["model"] = self.model
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv(self.config.api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            with httpx.Client(timeout=self.config.timeout_seconds) as client:
                response = client.post(self._url(), json=payload, headers=headers)
                response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

        vector = self._extract_vector(data)
        if not vector:
            return None
        return vector

    def _url(self) -> str:
        return f"{self.base_url}{self.config.endpoint_path}"

    def _extract_vector(self, data: dict[str, Any]) -> list[float] | None:
        if isinstance(data.get("data"), list) and data["data"]:
            embedding = data["data"][0].get("embedding")
            if isinstance(embedding, list):
                return [float(value) for value in embedding]
        embedding = data.get("embedding")
        if isinstance(embedding, list):
            return [float(value) for value in embedding]
        return None


@dataclass(frozen=True)
class RerankClient:
    config: RerankConfig

    @property
    def enabled(self) -> bool:
        return self.config.enabled and bool(self.base_url)

    @property
    def base_url(self) -> str:
        return os.getenv(self.config.base_url_env, "").rstrip("/")

    @property
    def model(self) -> str:
        return os.getenv(self.config.model_env, "")

    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[tuple[int, float]]:
        if not self.enabled or not query.strip() or not documents:
            return []
        payload: dict[str, Any] = {"query": query, "documents": documents, "top_n": top_n}
        if self.model:
            payload["model"] = self.model
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv(self.config.api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            with httpx.Client(timeout=self.config.timeout_seconds) as client:
                response = client.post(self._url(), json=payload, headers=headers)
                response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return []

        return self._extract_scores(data)

    def _url(self) -> str:
        return f"{self.base_url}{self.config.endpoint_path}"

    def _extract_scores(self, data: dict[str, Any]) -> list[tuple[int, float]]:
        raw_results = data.get("results") or data.get("data") or []
        scores: list[tuple[int, float]] = []
        for item in raw_results:
            if not isinstance(item, dict) or "index" not in item:
                continue
            score = item.get("relevance_score", item.get("score", 0.0))
            scores.append((int(item["index"]), float(score)))
        return scores
