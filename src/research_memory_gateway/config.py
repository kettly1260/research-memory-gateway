from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    name: str = "research-memory-gateway"
    host: str = "127.0.0.1"
    port: int = 8787
    auth_token_env: str = "RESEARCH_MEMORY_TOKEN"


class BackendConfig(BaseModel):
    type: Literal["sqlite", "nocturne"] = "sqlite"
    sqlite_path: str = "./data/research_memory.db"
    nocturne_url_env: str = "NOCTURNE_URL"
    nocturne_token_env: str = "NOCTURNE_TOKEN"


class EmbeddingConfig(BaseModel):
    enabled: bool = False
    base_url_env: str = "EMBEDDING_BASE_URL"
    api_key_env: str = "EMBEDDING_API_KEY"
    model_env: str = "EMBEDDING_MODEL"
    endpoint_path: str = "/embeddings"
    timeout_seconds: float = 30.0


class RerankConfig(BaseModel):
    enabled: bool = False
    base_url_env: str = "RERANK_BASE_URL"
    api_key_env: str = "RERANK_API_KEY"
    model_env: str = "RERANK_MODEL"
    endpoint_path: str = "/rerank"
    timeout_seconds: float = 30.0


class RetrievalConfig(BaseModel):
    mode: Literal["keyword", "hybrid"] = "keyword"
    vector_candidate_limit: int = 50
    rerank_candidate_limit: int = 20
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)


class MemoryConfig(BaseModel):
    require_user_confirmation: bool = True
    require_evidence_for_claims: bool = True
    max_summary_chars: int = 1800
    default_verification_status: str = "unverified"
    enable_light_graph: bool = True
    overlap_limit: int = 5


class SourceAllowlistEntry(BaseModel):
    name: str
    type: str
    path: str
    readonly: bool = True


class SourcesConfig(BaseModel):
    allowlist: list[SourceAllowlistEntry] = Field(default_factory=list)


class ExportConfig(BaseModel):
    markdown_dir: str = "./exports/markdown"
    json_dir: str = "./exports/json"


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    backend: BackendConfig = Field(default_factory=BackendConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    return AppConfig.model_validate(raw)
