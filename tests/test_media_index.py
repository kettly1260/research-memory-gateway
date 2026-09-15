from __future__ import annotations

from pathlib import Path

from research_memory_gateway.media_index import MediaIndexDatabase


class FakeMultimodalEmbeddingClient:
    enabled = True
    model = "multimodal-test"
    last_error = None
    last_status_code = 200

    def __init__(self) -> None:
        self.image_calls = 0
        self.text_calls = 0

    def embed(self, text: str) -> list[float] | None:
        self.text_calls += 1
        return [0.0, 1.0] if "blue" in text.lower() else [1.0, 0.0]

    def embed_image_bytes(self, content: bytes, *, mime_type: str = "image/png") -> list[float] | None:
        self.image_calls += 1
        return [0.0, 1.0] if b"blue" in content else [1.0, 0.0]


class RejectingImageEmbeddingClient(FakeMultimodalEmbeddingClient):
    def embed_image_bytes(self, content: bytes, *, mime_type: str = "image/png") -> list[float] | None:
        self.image_calls += 1
        self.last_error = "http_error"
        self.last_status_code = 400
        return None


def _write_image(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path


def test_media_index_image_cache_and_text_search(tmp_path: Path) -> None:
    client = FakeMultimodalEmbeddingClient()
    index = MediaIndexDatabase(tmp_path / "media.sqlite", embedding_client=client)
    red = _write_image(tmp_path / "red.png", b"fake-png-red")
    red_copy = _write_image(tmp_path / "red-copy.png", b"fake-png-red")
    blue = _write_image(tmp_path / "blue.png", b"fake-png-blue")

    first = index.index_image_file(red, source_system="test", source_resource_id="red")
    cached = index.index_image_file(red_copy, source_system="test", source_resource_id="red-copy")
    index.index_image_file(blue, source_system="test", source_resource_id="blue")

    assert first["embedded"] is True
    assert first["cached"] is False
    assert cached["embedded"] is True
    assert cached["cached"] is True
    assert client.image_calls == 2

    status = index.stats()
    assert status["resources"] == 3
    assert status["images"] == 3
    assert status["embeddings"] == 2
    assert status["resources_with_active_embedding"] == 3
    assert status["vector_coverage"] == 1.0

    result = index.search(query="find the blue image", limit=3)
    assert result["query_kind"] == "text"
    assert result["count"] == 3
    assert result["results"][0]["source_resource_id"] == "blue"
    assert result["results"][0]["vector_score"] > result["results"][-1]["vector_score"]


def test_media_index_supports_image_to_image_search(tmp_path: Path) -> None:
    client = FakeMultimodalEmbeddingClient()
    index = MediaIndexDatabase(tmp_path / "media.sqlite", embedding_client=client)
    red = _write_image(tmp_path / "red.png", b"fake-png-red")
    blue = _write_image(tmp_path / "blue.png", b"fake-png-blue")
    query = _write_image(tmp_path / "query.png", b"another-blue-image")
    index.index_image_file(red, source_resource_id="red")
    index.index_image_file(blue, source_resource_id="blue")

    result = index.search(image_path=query, limit=2)

    assert result["query_kind"] == "image"
    assert result["results"][0]["source_resource_id"] == "blue"


def test_media_index_model_switch_requires_new_active_vectors(tmp_path: Path) -> None:
    client = FakeMultimodalEmbeddingClient()
    index = MediaIndexDatabase(tmp_path / "media.sqlite", embedding_client=client)
    image = _write_image(tmp_path / "sample.png", b"fake-png-red")
    index.index_image_file(image, source_resource_id="sample")
    assert index.stats()["vector_coverage"] == 1.0

    client.model = "multimodal-test-v2"
    switched = index.stats()
    assert switched["resources_with_active_embedding"] == 0
    assert switched["vector_coverage"] == 0.0

    refreshed = index.index_image_file(image, source_resource_id="sample")
    assert refreshed["embedded"] is True
    assert refreshed["cached"] is False
    assert refreshed["model"] == "multimodal-test-v2"
    assert index.stats()["vector_coverage"] == 1.0


def test_media_index_does_not_prejudge_image_capability(tmp_path: Path) -> None:
    client = RejectingImageEmbeddingClient()
    index = MediaIndexDatabase(tmp_path / "media.sqlite", embedding_client=client)
    image = _write_image(tmp_path / "sample.png", b"fake-png-red")

    result = index.index_image_file(image, source_resource_id="sample")

    assert client.image_calls == 1
    assert result["embedded"] is False
    assert result["embedding_error"] == "http_error"
    assert result["embedding_status_code"] == 400
    status = index.stats()
    assert status["resources"] == 1
    assert status["embeddings"] == 0
    assert status["vector_coverage"] == 0.0
