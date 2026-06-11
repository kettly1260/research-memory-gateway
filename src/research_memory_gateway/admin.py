from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

from .backends import SQLiteMemoryBackend, memory_to_search_document
from .config import load_config
from .models import ResearchMemory


@dataclass(frozen=True)
class BackfillOptions:
    project: str | None = None
    memory_type: str | None = None
    dry_run: bool = False
    limit: int | None = None
    force: bool = False


def inspect_sqlite_db(db_path: str) -> dict[str, Any]:
    backend = SQLiteMemoryBackend(db_path)
    with backend._connect() as connection:
        memory_count = connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        embedding_count = connection.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
        project_rows = connection.execute(
            "SELECT project, COUNT(*) AS count FROM memories GROUP BY project ORDER BY count DESC"
        ).fetchall()
        type_rows = connection.execute(
            "SELECT memory_type, COUNT(*) AS count FROM memories GROUP BY memory_type ORDER BY count DESC"
        ).fetchall()
        missing_embedding_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM memories m
            LEFT JOIN memory_embeddings e ON e.memory_id = m.memory_id
            WHERE e.memory_id IS NULL
            """
        ).fetchone()[0]
        backfill_needed_count = connection.execute(
            "SELECT COUNT(*) FROM embedding_backfill_needed"
        ).fetchone()[0]
    health = backend.retrieval_health()
    return {
        "sqlite_path": str(backend.path),
        "memory_count": memory_count,
        "embedding_count": embedding_count,
        "missing_embedding_count": missing_embedding_count,
        "backfill_needed_count": backfill_needed_count,
        "projects": {row["project"]: row["count"] for row in project_rows},
        "memory_types": {row["memory_type"]: row["count"] for row in type_rows},
        "stored_embedding_dimensions": health["stored_embedding_dimensions"],
        "invalid_stored_embeddings": health["invalid_stored_embeddings"],
    }


def backfill_embeddings(
    config_path: str,
    options: BackfillOptions,
    *,
    embedding_client: Any | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    backend = SQLiteMemoryBackend(config.backend.sqlite_path, config.retrieval)
    if embedding_client is not None:
        backend.embedding_client = embedding_client
    if not backend.embedding_client.enabled:
        return _backfill_result(status="embedding_disabled")

    filters: list[str] = []
    params: list[Any] = []
    if options.project:
        filters.append("project = ?")
        params.append(options.project)
    if options.memory_type:
        filters.append("memory_type = ?")
        params.append(options.memory_type)
    where = filters or ["1 = 1"]
    limit_sql = "LIMIT ?" if options.limit is not None else ""
    if options.limit is not None:
        params.append(max(0, options.limit))

    with backend._connect() as connection:
        rows = connection.execute(
            f"""
            SELECT data
            FROM memories
            WHERE {' AND '.join(where)}
            ORDER BY updated_at DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
        existing_ids = {
            row["memory_id"]
            for row in connection.execute("SELECT memory_id FROM memory_embeddings").fetchall()
        }

    result = _backfill_result(status="ok")
    for row in rows:
        memory = ResearchMemory.model_validate_json(row["data"])
        if memory.memory_id in existing_ids and not options.force:
            result["skipped_existing"] += 1
            continue
        if options.dry_run:
            result["would_backfill"] += 1
            continue

        embedding = backend.embedding_client.embed(memory_to_search_document(memory))
        if not embedding:
            result["service_errors"] += 1
            continue
        if result["expected_dimensions"] is None:
            result["expected_dimensions"] = len(embedding)
        elif len(embedding) != result["expected_dimensions"]:
            result["dimension_mismatches"] += 1
            continue
        with backend._connect() as connection:
            connection.execute(
                "DELETE FROM memory_embeddings WHERE memory_id = ?",
                (memory.memory_id,),
            )
            connection.execute(
                """
                INSERT INTO memory_embeddings(memory_id, embedding, updated_at)
                VALUES (?, ?, ?)
                """,
                (memory.memory_id, json.dumps(embedding), memory.updated_at),
            )
            connection.execute("DELETE FROM embedding_backfill_needed WHERE memory_id = ?", (memory.memory_id,))
        result["backfilled"] += 1
    result["matched_memories"] = len(rows)
    return result


def _backfill_result(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "matched_memories": 0,
        "would_backfill": 0,
        "backfilled": 0,
        "skipped_existing": 0,
        "failed": 0,
        "service_errors": 0,
        "dimension_mismatches": 0,
        "expected_dimensions": None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research Memory Gateway admin CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect-db", help="Inspect SQLite memory database")
    inspect_parser.add_argument("--config", default="config.yaml", help="Path to config YAML")

    audit_parser = subparsers.add_parser("audit-integrity", help="Audit SQLite integrity")
    audit_parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    audit_parser.add_argument("--repair-fts", action="store_true")
    audit_parser.add_argument("--repair-orphan-embeddings", action="store_true")

    backfill_parser = subparsers.add_parser(
        "backfill-embeddings", help="Generate embeddings for existing SQLite memories"
    )
    backfill_parser.add_argument("--config", default="config.yaml", help="Path to config YAML")
    backfill_parser.add_argument("--project", default=None)
    backfill_parser.add_argument("--memory-type", default=None)
    backfill_parser.add_argument("--dry-run", action="store_true")
    backfill_parser.add_argument("--limit", type=int, default=None)
    backfill_parser.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    if args.command == "inspect-db":
        result = inspect_sqlite_db(config.backend.sqlite_path)
    elif args.command == "audit-integrity":
        backend = SQLiteMemoryBackend(config.backend.sqlite_path, config.retrieval)
        result = backend.audit_integrity(
            repair_fts=args.repair_fts,
            repair_orphans=args.repair_orphan_embeddings,
        )
    else:
        result = backfill_embeddings(
            args.config,
            BackfillOptions(
                project=args.project,
                memory_type=args.memory_type,
                dry_run=args.dry_run,
                limit=args.limit,
                force=args.force,
            ),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
