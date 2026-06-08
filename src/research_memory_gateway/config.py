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
