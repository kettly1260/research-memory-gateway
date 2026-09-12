from __future__ import annotations

import argparse
import json
from pathlib import Path

from research_memory_gateway.conversations import (
    CodexExportReader,
    ConversationIngestionPipeline,
    ImportManifest,
    ObsidianConversationWriter,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prototype Codex export to Obsidian staging notes.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--session-id", action="append", required=True)
    args = parser.parse_args()

    reader = CodexExportReader(args.archive)
    manifest = ImportManifest(args.output / ".ai-memory" / "manifest.sqlite")
    writer = ObsidianConversationWriter(args.output)
    pipeline = ConversationIngestionPipeline(reader, writer, manifest)
    results = pipeline.run(args.session_id)
    manifest.export_csv(args.output / ".ai-memory" / "manifest.csv")
    print(json.dumps([result.__dict__ for result in results], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
