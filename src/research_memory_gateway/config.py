from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

import yaml
from pydantic import BaseModel, Field, SecretStr


class ServerConfig(BaseModel):
    name: str = "research-memory-gateway"
    host: str = "127.0.0.1"
    port: int = 8787
    auth_token_env: str = "RESEARCH_MEMORY_TOKEN"
    surface: Literal["agent", "admin", "full"] = "agent"


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
    base_url: str | None = None
    api_key: SecretStr | None = None
    model: str | None = None
    endpoint_path: str = "/embeddings"
    timeout_seconds: float = 300.0
    max_retries: int = 1


class RerankConfig(BaseModel):
    enabled: bool = False
    base_url_env: str = "RERANK_BASE_URL"
    api_key_env: str = "RERANK_API_KEY"
    model_env: str = "RERANK_MODEL"
    base_url: str | None = None
    api_key: SecretStr | None = None
    model: str | None = None
    endpoint_path: str = "/rerank"
    timeout_seconds: float = 30.0
    max_retries: int = 1


class RetrievalConfig(BaseModel):
    mode: Literal["keyword", "hybrid"] = "keyword"
    vector_candidate_limit: int = 50
    rerank_candidate_limit: int = 20
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)


class WebUIConfig(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8788
    web_config_path: str = "./data/web_config.yaml"
    auth_store_path: str = "./data/webui-auth.json"
    secret_store_path: str = "./data/webui-secrets.json.enc"
    initial_password: SecretStr | None = None
    session_max_age_seconds: int = 43200


class WebRuntimeProviderConfig(BaseModel):
    enabled: bool = False
    base_url: str | None = None
    model: str | None = None
    endpoint_path: str = "/embeddings"
    timeout_seconds: float = 30.0
    max_retries: int = 1


class WebRuntimeRerankConfig(WebRuntimeProviderConfig):
    endpoint_path: str = "/rerank"


class WebRuntimeNocturneConfig(BaseModel):
    transport: Literal["unknown", "rest", "sse", "streamable_http", "stdio"] = "unknown"
    url: str | None = None


class WebRuntimeBackfillConfig(BaseModel):
    default_scope: str = "active"
    default_batch_size: int = 8
    default_concurrency: int = 2
    default_request_timeout_seconds: int = 30
    default_job_timeout_seconds: int = 1800


class WebRuntimeConfig(BaseModel):
    retrieval: dict[str, Literal["keyword", "hybrid"]] = Field(default_factory=lambda: {"mode": "keyword"})
    embedding: WebRuntimeProviderConfig = Field(default_factory=WebRuntimeProviderConfig)
    rerank: WebRuntimeRerankConfig = Field(default_factory=WebRuntimeRerankConfig)
    nocturne: WebRuntimeNocturneConfig = Field(default_factory=WebRuntimeNocturneConfig)
    backfill: WebRuntimeBackfillConfig = Field(default_factory=WebRuntimeBackfillConfig)


class MemoryConfig(BaseModel):
    require_user_confirmation: bool = True
    require_evidence_for_claims: bool = True
    max_summary_chars: int = 1800
    default_verification_status: str = "unverified"
    enable_light_graph: bool = True
    overlap_limit: int = 5
    recall_default_limit: int = 5
    recall_max_limit: int = 10
    recall_compact_token_budget: int = Field(default=1000, ge=100, le=10000)
    recall_standard_token_budget: int = Field(default=1500, ge=100, le=20000)
    capture_min_chars: int = 12
    ambient_auto_save: bool = True
    trusted_capture_requires_review: bool = True


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


class ConversationArchiveConfig(BaseModel):
    enabled: bool = False
    staging_dir: str = "./exports/conversation-staging"
    vault_root: str | None = None
    canonical_subdir: str = "90_System/AI-Memory"
    # Deprecated compatibility field. Production conversation manifests are
    # archive-local: <conversation_root>/.ai-memory/manifest.sqlite.
    manifest_path: str = "./data/conversation-imports.sqlite"
    index_path: str = "./data/conversation-index.sqlite"
    require_explicit_vault_confirmation: bool = True
    copy_embedded_images: bool = False

    def resolve_staging_dir(self, base_dir: str | Path | None = None) -> Path:
        base = Path(base_dir or ".").resolve()
        path = Path(self.staging_dir)
        resolved = path.resolve() if path.is_absolute() else (base / path).resolve()
        if not path.is_absolute():
            try:
                resolved.relative_to(base)
            except ValueError:
                raise PermissionError(f"staging_dir escapes base directory: {self.staging_dir}")
        return resolved

    def resolve_vault_root(self, *, confirmed: bool = False) -> Path:
        if not self.vault_root:
            raise ValueError("vault_root is not configured")
        if self.require_explicit_vault_confirmation and not confirmed:
            raise PermissionError("Explicit confirmation is required to access canonical vault_root")
        vault = Path(self.vault_root).resolve()
        if not vault.exists() or not vault.is_dir():
            raise FileNotFoundError(
                f"Canonical vault_root does not exist: {vault}. Automatic creation of empty vault is forbidden."
            )
        return vault


    def resolve_canonical_root(self, *, confirmed: bool = False) -> Path:
        vault = self.resolve_vault_root(confirmed=confirmed)
        subdir = Path(self.canonical_subdir)
        canonical = (vault / subdir).resolve() if not subdir.is_absolute() else subdir.resolve()
        try:
            canonical.relative_to(vault)
        except ValueError:
            raise PermissionError(f"canonical_subdir escapes vault root: {self.canonical_subdir}")
        return canonical

    def resolve_manifest_path(self, base_dir: str | Path | None = None) -> Path:
        """Resolve the deprecated global manifest path for compatibility only."""
        base = Path(base_dir or ".").resolve()
        path = Path(self.manifest_path)
        resolved = path.resolve() if path.is_absolute() else (base / path).resolve()
        if not path.is_absolute():
            try:
                resolved.relative_to(base)
            except ValueError:
                raise PermissionError(f"manifest_path escapes base directory: {self.manifest_path}")
        return resolved

    def resolve_archive_manifest_path(self, conversation_root: str | Path) -> Path:
        """Resolve the production archive-local manifest ledger path."""
        root = Path(conversation_root).resolve()
        return root / ".ai-memory" / "manifest.sqlite"

    def resolve_index_path(self, base_dir: str | Path | None = None) -> Path:
        base = Path(base_dir or ".").resolve()
        path = Path(self.index_path)
        resolved = path.resolve() if path.is_absolute() else (base / path).resolve()
        if not path.is_absolute():
            try:
                resolved.relative_to(base)
            except ValueError:
                raise PermissionError(f"index_path escapes base directory: {self.index_path}")
        return resolved


class MediaIndexConfig(BaseModel):
    """Persistent multimodal resource index.

    Media vectors intentionally reuse ``retrieval.embedding``. This config
    only controls local indexing/storage behaviour; it does not describe or
    probe downstream model capabilities.
    """

    enabled: bool = True
    index_path: str = "./data/research-media.sqlite"
    max_image_bytes: int = 32 * 1024 * 1024

    def resolve_index_path(self, base_dir: str | Path | None = None) -> Path:
        base = Path(base_dir or ".").resolve()
        path = Path(self.index_path)
        resolved = path.resolve() if path.is_absolute() else (base / path).resolve()
        if not path.is_absolute():
            try:
                resolved.relative_to(base)
            except ValueError:
                raise PermissionError(f"media index_path escapes base directory: {self.index_path}")
        return resolved


class UploadConfig(BaseModel):
    enabled: bool = True
    base_dir: str = "./data/uploads"
    upload_path: str = "/uploads"
    max_size_bytes: int = 1024 * 1024 * 1024  # 1 GB
    default_expiry_hours: int = 24

    def resolve_base_dir(self, base_dir: str | Path | None = None) -> Path:
        base = Path(base_dir or ".").resolve()
        path = Path(self.base_dir)
        return path.resolve() if path.is_absolute() else (base / path).resolve()


def validate_safe_path(target_path: str | Path, allowed_roots: Sequence[str | Path]) -> Path:
    target = Path(target_path).resolve()
    resolved_roots = [Path(root).resolve() for root in allowed_roots]
    for root in resolved_roots:
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    raise PermissionError(f"Target path {target} is outside allowed roots: {resolved_roots}")


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    backend: BackendConfig = Field(default_factory=BackendConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    webui: WebUIConfig = Field(default_factory=WebUIConfig)
    conversation_archive: ConversationArchiveConfig = Field(default_factory=ConversationArchiveConfig)
    media_index: MediaIndexConfig = Field(default_factory=MediaIndexConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    return AppConfig.model_validate(raw)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Keep the temporary sibling filename independent of the target name.
    # Filesystems such as ext4 commonly cap each filename component at 255
    # bytes; repeating an already-long UTF-8 target name in the temp filename
    # can exceed that limit even when the final target itself is valid.
    tmp = target.with_name(f".{secrets.token_hex(12)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)


class WebConfigStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ValueError("web_config must be a mapping")
        # Validate the sparse overlay without materializing model defaults into
        # the persisted file. RuntimeConfigResolver needs to distinguish
        # "not configured in WebUI" from an explicit false/default value.
        validated = WebRuntimeConfig.model_validate(raw)
        return _sparse_projection(validated.model_dump(mode="json"), raw)

    def load(self) -> WebRuntimeConfig:
        return WebRuntimeConfig.model_validate(self.load_raw())

    def save(self, config: WebRuntimeConfig) -> None:
        payload = config.model_dump(mode="json", exclude_none=False)
        atomic_write_text(self.path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))

    def patch(self, updates: dict[str, Any]) -> WebRuntimeConfig:
        data = self.load_raw()
        updates = _expand_dotted_keys(updates)
        _deep_update(data, updates)
        updated = WebRuntimeConfig.model_validate(data)
        normalized = _sparse_projection(updated.model_dump(mode="json"), data)
        atomic_write_text(self.path, yaml.safe_dump(normalized, sort_keys=False, allow_unicode=True))
        return updated


class AuthStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def bootstrap(self, config: WebUIConfig) -> dict[str, Any]:
        if self.exists():
            return self.load()
        env_hash = os.getenv("WEBUI_PASSWORD_HASH")
        if env_hash:
            return {"version": 1, "password_hash": env_hash, "created_at": None, "updated_at": None, "source": "env"}
        if config.initial_password is None:
            raise RuntimeError("WebUI requires webui.initial_password or WEBUI_PASSWORD_HASH on first start")
        initial_password = (
            config.initial_password.get_secret_value()
            if hasattr(config.initial_password, "get_secret_value")
            else str(config.initial_password)
        )
        record = {
            "version": 1,
            "password_hash": hash_password(initial_password),
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        self._write(record)
        return record

    def load(self) -> dict[str, Any]:
        with self.path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def verify(self, password: str, config: WebUIConfig | None = None) -> bool:
        record = self.load() if self.exists() else self.bootstrap(config or WebUIConfig())
        return verify_password(password, record["password_hash"])

    def change_password(self, current_password: str, new_password: str) -> None:
        if not self.exists():
            raise RuntimeError("Cannot change env-only WebUI password; create auth store first")
        record = self.load()
        if not verify_password(current_password, record["password_hash"]):
            raise PermissionError("Current password is incorrect")
        record["password_hash"] = hash_password(new_password)
        record["updated_at"] = utc_now()
        self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        atomic_write_text(self.path, json.dumps(record, indent=2))


class SecretStore:
    SECRET_FIELDS = {"embedding.api_key", "rerank.api_key", "nocturne.token"}

    def __init__(self, path: str | Path, secret_key: str | None = None) -> None:
        self.path = Path(path)
        self.secret_key = secret_key if secret_key is not None else os.getenv("WEBUI_SECRET_KEY")

    @property
    def writable(self) -> bool:
        return bool(self.secret_key)

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        if not self.secret_key:
            return {}
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            ciphertext = base64.b64decode(envelope["ciphertext"])
            stream = self._keystream(len(ciphertext), envelope["nonce"])
            plaintext = bytes(a ^ b for a, b in zip(ciphertext, stream, strict=True))
            return json.loads(plaintext.decode("utf-8"))
        except (KeyError, ValueError, json.JSONDecodeError):
            return {}

    def save_secret(self, dotted_name: str, value: str) -> None:
        if dotted_name not in self.SECRET_FIELDS:
            raise ValueError(f"Unsupported secret field: {dotted_name}")
        if not self.secret_key:
            raise RuntimeError("WEBUI_SECRET_KEY is required to save WebUI secrets")
        data = self.load()
        data[dotted_name] = value
        self._write(data)

    def delete_secret(self, dotted_name: str) -> bool:
        if not self.secret_key:
            raise RuntimeError("WEBUI_SECRET_KEY is required to update WebUI secrets")
        data = self.load()
        existed = dotted_name in data
        data.pop(dotted_name, None)
        self._write(data)
        return existed

    def masked(self, dotted_name: str) -> dict[str, Any]:
        value = self.load().get(dotted_name)
        return {"configured": bool(value), "masked": mask_secret(value), "source": "secret_store" if value else "unset"}

    def _write(self, data: dict[str, str]) -> None:
        nonce = secrets.token_hex(12)
        plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
        stream = self._keystream(len(plaintext), nonce)
        ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream, strict=True))
        envelope = {"version": 1, "nonce": nonce, "ciphertext": base64.b64encode(ciphertext).decode("ascii")}
        atomic_write_text(self.path, json.dumps(envelope, indent=2))

    def _keystream(self, length: int, nonce: str) -> bytes:
        if not self.secret_key:
            raise RuntimeError("Missing secret key")
        seed = hashlib.sha256(self.secret_key.encode("utf-8") + nonce.encode("utf-8")).digest()
        output = bytearray()
        counter = 0
        while len(output) < length:
            output.extend(hashlib.sha256(seed + counter.to_bytes(8, "big")).digest())
            counter += 1
        return bytes(output[:length])


class RuntimeConfigResolver:
    def __init__(self, app_config: AppConfig, web_config_store: WebConfigStore | None = None, secret_store: SecretStore | None = None) -> None:
        self.app_config = app_config
        self.web_config_store = web_config_store or WebConfigStore(app_config.webui.web_config_path)
        self.secret_store = secret_store or SecretStore(app_config.webui.secret_store_path)

    def effective(self) -> dict[str, Any]:
        web_raw = self.web_config_store.load_raw()
        web = WebRuntimeConfig.model_validate(web_raw)
        secrets_data = self.secret_store.load()
        result = {
            "retrieval": {
                "mode": self._field(
                    os.getenv("RETRIEVAL_MODE"),
                    _nested_value(web_raw, "retrieval", "mode"),
                    self.app_config.retrieval.mode,
                    "keyword",
                )
            },
            "embedding": self._provider_effective(
                "embedding",
                self.app_config.retrieval.embedding,
                secrets_data,
                web_raw.get("embedding") if isinstance(web_raw.get("embedding"), dict) else {},
            ),
            "rerank": self._provider_effective(
                "rerank",
                self.app_config.retrieval.rerank,
                secrets_data,
                web_raw.get("rerank") if isinstance(web_raw.get("rerank"), dict) else {},
            ),
            "nocturne": {
                "transport": self._field(
                    os.getenv("NOCTURNE_TRANSPORT"),
                    _nested_value(web_raw, "nocturne", "transport"),
                    None,
                    "unknown",
                ),
                "url": self._field(
                    os.getenv("NOCTURNE_URL"),
                    _nested_value(web_raw, "nocturne", "url"),
                    None,
                    None,
                ),
                "token": self._secret_field(os.getenv("NOCTURNE_TOKEN"), secrets_data.get("nocturne.token")),
            },
            "backfill": web.backfill.model_dump(mode="json"),
        }
        return result

    def retrieval_config(self) -> RetrievalConfig:
        effective = self.effective()
        return RetrievalConfig(
            mode=effective["retrieval"]["mode"]["value"],
            vector_candidate_limit=self.app_config.retrieval.vector_candidate_limit,
            rerank_candidate_limit=self.app_config.retrieval.rerank_candidate_limit,
            embedding=EmbeddingConfig(
                enabled=effective["embedding"]["enabled"]["value"],
                base_url=effective["embedding"]["base_url"]["value"],
                api_key=effective["embedding"]["api_key"]["value"],
                model=effective["embedding"]["model"]["value"],
                endpoint_path=effective["embedding"]["endpoint_path"]["value"],
                timeout_seconds=effective["embedding"]["timeout_seconds"]["value"],
                max_retries=effective["embedding"]["max_retries"]["value"],
            ),
            rerank=RerankConfig(
                enabled=effective["rerank"]["enabled"]["value"],
                base_url=effective["rerank"]["base_url"]["value"],
                api_key=effective["rerank"]["api_key"]["value"],
                model=effective["rerank"]["model"]["value"],
                endpoint_path=effective["rerank"]["endpoint_path"]["value"],
                timeout_seconds=effective["rerank"]["timeout_seconds"]["value"],
                max_retries=effective["rerank"]["max_retries"]["value"],
            ),
        )

    def provider_env(self, provider: str) -> dict[str, str]:
        effective = self.effective()[provider]
        env: dict[str, str] = {}
        if effective["base_url"]["value"]:
            env[f"{provider.upper()}_BASE_URL"] = str(effective["base_url"]["value"])
        if effective["model"]["value"]:
            env[f"{provider.upper()}_MODEL"] = str(effective["model"]["value"])
        secret = effective["api_key"]["value"]
        if secret:
            env[f"{provider.upper()}_API_KEY"] = str(secret)
        return env

    def _provider_effective(
        self,
        name: str,
        base: EmbeddingConfig | RerankConfig,
        secrets_data: dict[str, str],
        web_raw: dict[str, Any],
    ) -> dict[str, Any]:
        prefix = name.upper()
        return {
            "enabled": self._field(os.getenv(f"{prefix}_ENABLED"), web_raw.get("enabled"), base.enabled, False, value_type="bool"),
            "base_url": self._field(os.getenv(base.base_url_env), web_raw.get("base_url"), base.base_url, None),
            "model": self._field(os.getenv(base.model_env), web_raw.get("model"), base.model, None),
            "endpoint_path": self._field(os.getenv(f"{prefix}_ENDPOINT_PATH"), web_raw.get("endpoint_path"), base.endpoint_path, "/embeddings" if name == "embedding" else "/rerank"),
            "timeout_seconds": self._field(os.getenv(f"{prefix}_TIMEOUT_SECONDS"), web_raw.get("timeout_seconds"), base.timeout_seconds, 30.0, value_type="float"),
            "max_retries": self._field(os.getenv(f"{prefix}_MAX_RETRIES"), web_raw.get("max_retries"), base.max_retries, 1, value_type="int"),
            "api_key": self._secret_field(os.getenv(base.api_key_env), secrets_data.get(f"{name}.api_key")),
        }

    def _field(self, env_value: Any, web_value: Any, config_value: Any, default_value: Any, *, value_type: str | None = None) -> dict[str, Any]:
        for value, source in ((env_value, "env"), (web_value, "web_config"), (config_value, "config"), (default_value, "default")):
            if value is None or value == "":
                continue
            return {"value": _coerce(value, value_type), "source": source}
        return {"value": None, "source": "unset"}

    def _secret_field(self, env_value: str | None, store_value: str | None) -> dict[str, Any]:
        if env_value:
            return {"value": env_value, "configured": True, "masked": mask_secret(env_value), "source": "env"}
        if store_value:
            return {"value": store_value, "configured": True, "masked": mask_secret(store_value), "source": "secret_store"}
        return {"value": None, "configured": False, "masked": None, "source": "unset"}


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    rounds = 200_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${base64.b64encode(digest).decode('ascii')}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, rounds, salt, digest = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(base64.b64encode(actual).decode("ascii"), digest)
    except (ValueError, TypeError):
        return False


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 6)}{value[-4:]}"


def _deep_update(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = deepcopy(value)


def _nested_value(data: dict[str, Any], section: str, field: str) -> Any:
    value = data.get(section)
    if not isinstance(value, dict):
        return None
    return value.get(field)


def _sparse_projection(validated: Any, raw: Any) -> Any:
    """Project validated/coerced values onto only keys explicitly present in raw."""
    if isinstance(validated, dict) and isinstance(raw, dict):
        return {
            key: _sparse_projection(validated[key], raw_value)
            for key, raw_value in raw.items()
            if key in validated
        }
    return deepcopy(validated)


def _expand_dotted_keys(updates: dict[str, Any]) -> dict[str, Any]:
    expanded: dict[str, Any] = {}
    for key, value in updates.items():
        if "." not in key:
            if isinstance(value, dict):
                value = _expand_dotted_keys(value)
            if isinstance(value, dict) and isinstance(expanded.get(key), dict):
                _deep_update(expanded[key], value)
            else:
                expanded[key] = deepcopy(value)
            continue

        cursor = expanded
        parts = key.split(".")
        for part in parts[:-1]:
            next_value = cursor.setdefault(part, {})
            if not isinstance(next_value, dict):
                next_value = {}
                cursor[part] = next_value
            cursor = next_value
        cursor[parts[-1]] = deepcopy(value)
    return expanded


def _coerce(value: Any, value_type: str | None) -> Any:
    if value_type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).lower() in {"1", "true", "yes", "on"}
    if value_type == "int":
        return int(value)
    if value_type == "float":
        return float(value)
    return value
