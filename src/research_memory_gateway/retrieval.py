from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from enum import Enum
from http import HTTPStatus
from logging import getLogger
from typing import Any

import httpx

from .config import EmbeddingConfig, RerankConfig


logger = getLogger(__name__)


class RetrievalFailureReason(str, Enum):
    DISABLED = "disabled"
    NOT_CONFIGURED = "not_configured"
    EMPTY_INPUT = "empty_input"
    HTTP_ERROR = "http_error"
    INVALID_RESPONSE = "invalid_response"
    DIMENSION_MISMATCH = "dimension_mismatch"


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


@dataclass
class EmbeddingClient:
    config: EmbeddingConfig
    last_error: str | None = None
    last_status_code: int | None = None
    last_vector_dimensions: int | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled and bool(self.base_url)

    @property
    def base_url(self) -> str:
        return (os.getenv(self.config.base_url_env) or self.config.base_url or "").rstrip("/")

    @property
    def model(self) -> str:
        return os.getenv(self.config.model_env) or self.config.model or ""

    def embed(self, text: str) -> list[float] | None:
        self.last_error = None
        self.last_status_code = None
        if not self.config.enabled:
            self.last_error = RetrievalFailureReason.DISABLED.value
            return None
        if not self.base_url:
            self.last_error = RetrievalFailureReason.NOT_CONFIGURED.value
            return None
        if not text.strip():
            self.last_error = RetrievalFailureReason.EMPTY_INPUT.value
            return None
        payload: dict[str, Any] = {"input": text}
        if self.model:
            payload["model"] = self.model
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv(self.config.api_key_env)
        if not api_key and self.config.api_key is not None:
            api_key = self.config.api_key.get_secret_value()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        data = self._post_json(payload, headers)
        if data is None:
            return None

        try:
            vector = self._extract_vector(data)
        except (TypeError, ValueError):
            self.last_error = RetrievalFailureReason.INVALID_RESPONSE.value
            logger.warning("Embedding response contains non-numeric vector values")
            return None
        if not vector:
            self.last_error = RetrievalFailureReason.INVALID_RESPONSE.value
            logger.warning("Embedding response did not include a usable vector")
            return None
        self.last_vector_dimensions = len(vector)
        return vector

    def health(self) -> dict[str, Any]:
        status = "disabled"
        if self.config.enabled:
            status = "ready" if self.base_url else "not_configured"
        return {
            "enabled": self.config.enabled,
            "status": status,
            "base_url_configured": bool(self.base_url),
            "endpoint_path": self.config.endpoint_path,
            "model_configured": bool(self.model),
            "timeout_seconds": self.config.timeout_seconds,
            "max_retries": self.config.max_retries,
            "last_error": self.last_error,
            "last_status_code": self.last_status_code,
            "last_vector_dimensions": self.last_vector_dimensions,
        }

    def _url(self) -> str:
        return f"{self.base_url}{self.config.endpoint_path}"

    def _candidate_urls(self) -> list[str]:
        configured = self._url()
        candidates = [configured]
        if self.config.endpoint_path in {"/embeddings", "embeddings"} and self.base_url:
            alternate = f"{self.base_url}/v1/embeddings"
            if alternate not in candidates:
                candidates.append(alternate)
        return candidates

    def _post_json(self, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any] | None:
        attempts = max(1, self.config.max_retries + 1)
        last_exception: Exception | None = None
        for attempt in range(attempts):
            for url in self._candidate_urls():
                try:
                    with httpx.Client(timeout=self.config.timeout_seconds) as client:
                        response = client.post(url, json=payload, headers=headers)
                    self.last_status_code = response.status_code
                    if response.status_code == HTTPStatus.NOT_FOUND and url != self._candidate_urls()[-1]:
                        continue
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError("JSON response is not an object")
                    return data
                except (httpx.HTTPError, ValueError) as exc:
                    last_exception = exc
            if attempt + 1 < attempts:
                time.sleep(min(0.1 * (attempt + 1), 0.5))

        self.last_error = RetrievalFailureReason.HTTP_ERROR.value
        logger.warning("Embedding request failed after %s attempt(s): %s", attempts, last_exception)
        return None

    def _extract_vector(self, data: dict[str, Any]) -> list[float] | None:
        if isinstance(data.get("data"), list) and data["data"]:
            first = data["data"][0]
            embedding = first.get("embedding") if isinstance(first, dict) else None
            if isinstance(embedding, list):
                return [float(value) for value in embedding]
        embedding = data.get("embedding")
        if isinstance(embedding, list):
            return [float(value) for value in embedding]
        return None


@dataclass
class RerankClient:
    config: RerankConfig
    last_error: str | None = None
    last_status_code: int | None = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled and bool(self.base_url)

    @property
    def base_url(self) -> str:
        return (os.getenv(self.config.base_url_env) or self.config.base_url or "").rstrip("/")

    @property
    def model(self) -> str:
        return os.getenv(self.config.model_env) or self.config.model or ""

    def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[tuple[int, float]]:
        self.last_error = None
        self.last_status_code = None
        if not self.config.enabled:
            self.last_error = RetrievalFailureReason.DISABLED.value
            return []
        if not self.base_url:
            self.last_error = RetrievalFailureReason.NOT_CONFIGURED.value
            return []
        if not query.strip() or not documents:
            self.last_error = RetrievalFailureReason.EMPTY_INPUT.value
            return []
        payload: dict[str, Any] = {"query": query, "documents": documents, "top_n": top_n}
        if self.model:
            payload["model"] = self.model
        headers = {"Content-Type": "application/json"}
        api_key = os.getenv(self.config.api_key_env)
        if not api_key and self.config.api_key is not None:
            api_key = self.config.api_key.get_secret_value()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        data = self._post_json(payload, headers)
        if data is None:
            return []

        try:
            scores = self._extract_scores(data)
        except (TypeError, ValueError):
            self.last_error = RetrievalFailureReason.INVALID_RESPONSE.value
            logger.warning("Rerank response contains invalid score values")
            return []
        if not scores:
            self.last_error = RetrievalFailureReason.INVALID_RESPONSE.value
            logger.warning("Rerank response did not include usable scores")
        return scores

    def health(self) -> dict[str, Any]:
        status = "disabled"
        if self.config.enabled:
            status = "ready" if self.base_url else "not_configured"
        return {
            "enabled": self.config.enabled,
            "status": status,
            "base_url_configured": bool(self.base_url),
            "endpoint_path": self.config.endpoint_path,
            "model_configured": bool(self.model),
            "timeout_seconds": self.config.timeout_seconds,
            "max_retries": self.config.max_retries,
            "last_error": self.last_error,
            "last_status_code": self.last_status_code,
        }

    def _url(self) -> str:
        return f"{self.base_url}{self.config.endpoint_path}"

    def _post_json(self, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any] | None:
        attempts = max(1, self.config.max_retries + 1)
        last_exception: Exception | None = None
        for attempt in range(attempts):
            try:
                with httpx.Client(timeout=self.config.timeout_seconds) as client:
                    response = client.post(self._url(), json=payload, headers=headers)
                self.last_status_code = response.status_code
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("JSON response is not an object")
                return data
            except (httpx.HTTPError, ValueError) as exc:
                last_exception = exc
            if attempt + 1 < attempts:
                time.sleep(min(0.1 * (attempt + 1), 0.5))

        self.last_error = RetrievalFailureReason.HTTP_ERROR.value
        logger.warning("Rerank request failed after %s attempt(s): %s", attempts, last_exception)
        return None

    def _extract_scores(self, data: dict[str, Any]) -> list[tuple[int, float]]:
        raw_results = data.get("results") or data.get("data") or []
        scores: list[tuple[int, float]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            parsed = self._extract_score_item(item)
            if parsed is not None:
                scores.append(parsed)
        return scores

    def _extract_score_item(self, item: dict[str, Any]) -> tuple[int, float] | None:
        if "index" in item:
            score = item.get("relevance_score", item.get("score", item.get("relevance", 0.0)))
            return int(item["index"]), float(score)

        document = item.get("document")
        if isinstance(document, dict) and "index" in document:
            score = item.get("score", item.get("relevance_score", document.get("score", 0.0)))
            return int(document["index"]), float(score)

        return None
