from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .config import AppConfig, SourceAllowlistEntry
from .models import SourceRef


class SourceResolver:
    def __init__(self, config: AppConfig) -> None:
        self.entries = config.sources.allowlist

    def resolve(self, source_ref: SourceRef, max_chars: int = 4000) -> dict[str, Any]:
        if source_ref.url or source_ref.doi:
            return {
                "kind": "external",
                "url": source_ref.url,
                "doi": source_ref.doi,
                "excerpt": source_ref.excerpt,
                "metadata": source_ref.metadata,
            }

        if not source_ref.path:
            return {"kind": "missing", "message": "source_ref has no path, url, or doi"}

        path = Path(source_ref.path).expanduser().resolve()
        entry = self._find_allowlist_entry(path)
        if entry is None:
            return {
                "kind": "blocked",
                "message": f"Path is outside configured source allowlist: {path}",
            }

        if not path.exists():
            return {"kind": "missing", "message": f"Path does not exist: {path}"}
        if path.is_dir():
            children = sorted(child.name for child in path.iterdir())[:100]
            return {"kind": "directory", "allowlist": entry.name, "path": str(path), "children": children}

        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        text = data.decode("utf-8", errors="replace")[:max_chars]
        return {
            "kind": "file",
            "allowlist": entry.name,
            "path": str(path),
            "sha256": digest,
            "content_hash_matches": source_ref.content_hash in (None, digest),
            "content_preview": text,
        }

    def _find_allowlist_entry(self, path: Path) -> SourceAllowlistEntry | None:
        for entry in self.entries:
            root = Path(entry.path).expanduser().resolve()
            try:
                path.relative_to(root)
                return entry
            except ValueError:
                continue
        return None
