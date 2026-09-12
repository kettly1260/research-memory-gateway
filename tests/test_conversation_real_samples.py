from __future__ import annotations

from pathlib import Path
import pytest

from research_memory_gateway.conversations import CodexExportReader

REAL_ARCHIVE = Path(r"D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip")
SAMPLES = [
    "019e3ad1-05d6-7382-972f-0d377e6092c6",
    "019eab7a-3a54-70b1-afd2-b89c0c98e8b2",
    "01a08fdc-6da5-7f93-9119-ff79d9fea710",
    "01a01289-e1b4-7242-98d9-368a75a16194",
    "019ea2ad-9a74-75c2-bf10-4246ca00ab25",
]


@pytest.mark.skipif(not REAL_ARCHIVE.exists(), reason="Real archive not present")
def test_real_5_samples_parse_and_provenance() -> None:
    reader = CodexExportReader(REAL_ARCHIVE)
    for session_id in SAMPLES:
        conv = reader.parse(session_id)
        assert conv.ref.conversation_id == session_id
        assert conv.provenance is not None
        assert conv.provenance.hash_matched is True
        assert conv.provenance.entry_size_bytes > 0
        assert conv.completion_status in {"complete", "incomplete_or_unknown", "aborted"}
        assert isinstance(conv.event_mirror_counts, dict)
