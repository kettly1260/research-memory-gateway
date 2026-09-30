"""End-to-end: agent hook fixture -> spool -> HTTP gateway -> archive -> recall.

What is exercised for real here:

* the **real** Starlette app with the **real** ``BearerAuthMiddleware``;
* the real ingest ledger, archive materialization and FTS index;
* the real retrieval service that backs ``conversation_search`` /
  ``conversation_recall`` / ``conversation_read``;
* the **real** provider fixtures that ship with the Rust crate, so the payload
  shapes on both sides are the same bytes.

What is emulated: the Rust process itself.  ``pytest`` cannot assume a compiled
``research-memory-bridge`` binary (the Rust suite runs in its own CI job), so the
adapter mapping is reproduced in :mod:`tests.bridge_emulator` from the same
documented rules.  The Rust adapters are covered by ``bridge/tests/adapters.rs``
against these very fixtures, and both sides derive event ids with the algorithm
pinned by ``schemas/event-id-contract-v1.json``.

If ``RESEARCH_MEMORY_BRIDGE_BIN`` points at a built binary,
``test_real_bridge_binary_round_trip`` additionally runs the actual Rust client
end to end.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from research_memory_gateway.ingest.identity import derive_event_id
from tests.bridge_emulator import (
    claude_hook_events,
    claude_transcript_events,
    codex_notify_events,
    codex_rollout_events,
)
from tests.test_conversation_ingest_api import GatewayServer

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "bridge" / "tests" / "fixtures"


def fixture_text(relative: str) -> str:
    return (FIXTURES / relative).read_text(encoding="utf-8")


class Spool:
    """A stand-in for the bridge's durable SQLite spool.

    Mirrors the two properties that matter for the contract: replaying an event
    is a no-op, and nothing is dropped while the gateway is unreachable.
    """

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}

    def enqueue(self, events: list[dict]) -> tuple[list[str], list[str]]:
        inserted, duplicates = [], []
        for event in events:
            if event["event_id"] in self.events:
                duplicates.append(event["event_id"])
            else:
                self.events[event["event_id"]] = event
                inserted.append(event["event_id"])
        return inserted, duplicates

    def batch(self, size: int) -> list[dict]:
        return list(self.events.values())[:size]

    def ack(self, event_ids: list[str]) -> None:
        for event_id in event_ids:
            self.events.pop(event_id, None)

    def __len__(self) -> int:
        return len(self.events)


def drain(spool: Spool, gateway: GatewayServer, *, batch_size: int = 100) -> dict:
    """One drain pass: upload a batch and ACK whatever the gateway accepted."""
    events = spool.batch(batch_size)
    if not events:
        return {"batches": 0}
    with gateway.client() as client:
        response = client.post(
            "/api/conversations/events/batch",
            json={"schema_version": 1, "client_id": "e2e-machine", "events": events},
        )
    if response.status_code == 401:
        # Authentication failure must never be treated as an ACK.
        return {"batches": 1, "auth_failed": True, "pending": len(spool)}
    body = response.json()
    spool.ack(body["accepted"] + body["duplicates"])
    return {
        "batches": 1,
        "accepted": body["accepted"],
        "duplicates": body["duplicates"],
        "rejected": body["rejected"],
        "pending": len(spool),
    }


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def test_codex_notify_round_trip_is_searchable(tmp_path: Path) -> None:
    with GatewayServer(tmp_path) as gateway:
        spool = Spool()
        inserted, duplicates = spool.enqueue(
            codex_notify_events(json.loads(fixture_text("codex/notify_turn_complete.json")))
        )
        assert len(inserted) == 3
        assert duplicates == []

        report = drain(spool, gateway)
        assert len(report["accepted"]) == 3
        assert report["rejected"] == []
        assert len(spool) == 0, "every ACKed event leaves the spool"

        search = gateway.service.conversation_retrieval.search("HNO3", limit=5)
        assert search["count"] >= 1
        hit = search["results"][0]
        assert hit["source_system"] == "codex"
        assert hit["source_key"].startswith("srcv1_")

        recall = gateway.service.conversation_retrieval.recall("HNO3")
        assert recall["items"], recall
        assert "10 mM" in recall["context"]

        read = gateway.service.conversation_retrieval.read(hit["vault_path"])
        assert "Fe3+ 储备液" in read["content"]
        assert read["metadata"]["source_conversation_id"] == (
            "019e3ad1-05d6-7382-972f-0d377e6092c6"
        )


def test_codex_transcript_reconciliation_completes_the_archive(tmp_path: Path) -> None:
    """The hook carries no message ids; the rollout does. One archive entry.

    The two paths derive *different* event ids for the same message (the hook has
    no provider id to anchor on), so the collapse happens in the gateway's
    archive layer: a block without a provider id is superseded by a
    provider-identified block with the same role and content.
    """
    with GatewayServer(tmp_path) as gateway:
        spool = Spool()
        # Realtime hook first.
        hook_events = codex_notify_events(
            json.loads(fixture_text("codex/notify_turn_complete.json"))
        )
        spool.enqueue(hook_events)
        first = drain(spool, gateway)
        assert len(first["accepted"]) == 3

        # Then the transcript reconciliation pass over the same conversation.
        transcript = codex_rollout_events(fixture_text("codex/rollout_sample.jsonl"))
        assert transcript, "the rollout fixture must parse"
        spool.enqueue(transcript)
        second = drain(spool, gateway)
        assert second["rejected"] == [], second
        assert second["accepted"], "the transcript contributes the provider-identified copy"

        note_path = gateway.service.conversation_ingest.store.sessions_for_conversation(
            source_system="codex",
            conversation_id="019e3ad1-05d6-7382-972f-0d377e6092c6",
        )[0]["note_path"]
        text = Path(note_path).read_text(encoding="utf-8")
        assert text.count("之前 Fe 的硝酸溶液怎么配的？") == 1, (
            "hook and transcript must not archive the same user prompt twice"
        )
        assert "Fe3+ 储备液为 10 mM，介质为 0.1 M HNO3。" in text
        # The provider-identified copy is the one that survives.
        assert "msg_user_1" in text


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


def test_claude_hook_and_transcript_round_trip(tmp_path: Path) -> None:
    with GatewayServer(tmp_path) as gateway:
        spool = Spool()
        spool.enqueue(
            claude_hook_events(json.loads(fixture_text("claude/hook_user_prompt_submit.json")))
        )
        spool.enqueue(claude_hook_events(json.loads(fixture_text("claude/hook_session_end.json"))))
        drain(spool, gateway)

        inserted, duplicates = spool.enqueue(
            claude_transcript_events(fixture_text("claude/transcript_sample.jsonl"))
        )
        assert duplicates == [], "the spool was already drained"
        assert len(inserted) == 2, "the transcript replays the prompt and adds the reply"
        report = drain(spool, gateway)
        # The prompt is already in the gateway's ledger, so it comes back as a
        # duplicate rather than being archived twice.
        assert len(report["accepted"]) == 1, report
        assert len(report["duplicates"]) == 1, report
        assert report["rejected"] == [], report

        search = gateway.service.conversation_retrieval.search("HNO3", limit=5)
        assert search["count"] >= 1
        recall = gateway.service.conversation_retrieval.recall("HNO3")
        assert recall["items"]
        assert "10 mM" in recall["context"]

        note_path = gateway.service.conversation_ingest.store.sessions_for_conversation(
            source_system="claude-code",
            conversation_id="3ba920a2-714f-4aad-a556-0b9b2a18587e",
        )[0]["note_path"]
        text = Path(note_path).read_text(encoding="utf-8")
        assert text.count("之前 Fe 的硝酸溶液怎么配的？") == 1
        assert "Fe3+ 储备液为 10 mM，介质为 0.1 M HNO3。" in text


# ---------------------------------------------------------------------------
# idempotency and outage recovery
# ---------------------------------------------------------------------------


def test_replaying_a_batch_never_duplicates_the_archive(tmp_path: Path) -> None:
    with GatewayServer(tmp_path) as gateway:
        events = codex_notify_events(
            json.loads(fixture_text("codex/notify_turn_complete.json"))
        )
        spool = Spool()
        spool.enqueue(events)
        first = drain(spool, gateway)
        assert len(first["accepted"]) == 3

        # The ACK was lost, so the bridge retries the identical batch.
        spool.enqueue(events)
        second = drain(spool, gateway)
        assert second["accepted"] == []
        assert sorted(second["duplicates"]) == sorted(event["event_id"] for event in events)

        note_path = gateway.service.conversation_ingest.store.sessions_for_conversation(
            source_system="codex",
            conversation_id="019e3ad1-05d6-7382-972f-0d377e6092c6",
        )[0]["note_path"]
        text = Path(note_path).read_text(encoding="utf-8")
        assert text.count("之前 Fe 的硝酸溶液怎么配的？") == 1
        assert text.count("顺便给出浓度") == 1


def test_gateway_outage_loses_nothing_and_recovers(tmp_path: Path) -> None:
    """A dead gateway must not block the agent and must not drop events."""
    events = codex_notify_events(json.loads(fixture_text("codex/notify_turn_complete.json")))
    spool = Spool()

    # The gateway is down: the hook still spools successfully.
    spool.enqueue(events)
    assert len(spool) == 3

    # Authentication is broken for a while: still no ACK, still no loss.
    with GatewayServer(tmp_path) as gateway:
        with gateway.client(token="wrong-token") as client:
            response = client.post(
                "/api/conversations/events/batch",
                json={"schema_version": 1, "client_id": "e2e-machine", "events": events},
            )
        assert response.status_code == 401
        assert len(spool) == 3, "an auth failure must never be treated as an ACK"

        # Token fixed: the same events upload cleanly.
        report = drain(spool, gateway)
        assert len(report["accepted"]) == 3
        assert len(spool) == 0

        search = gateway.service.conversation_retrieval.search("HNO3", limit=5)
        assert search["count"] >= 1


def test_session_end_reports_missing_events_for_reconciliation(tmp_path: Path) -> None:
    with GatewayServer(tmp_path) as gateway:
        spool = Spool()
        events = codex_notify_events(
            json.loads(fixture_text("codex/notify_turn_complete.json"))
        )
        spool.enqueue(events)
        drain(spool, gateway)

        with gateway.client() as client:
            response = client.post(
                "/api/conversations/session-end",
                json={
                    "schema_version": 1,
                    "client_id": "e2e-machine",
                    "source_system": "codex",
                    "session_id": "019e3ad1-05d6-7382-972f-0d377e6092c6",
                    "conversation_id": "019e3ad1-05d6-7382-972f-0d377e6092c6",
                    # The agent saw two more messages than the gateway holds.
                    "observed_message_count": 5,
                },
            )
        body = response.json()
        assert body["stored_message_count"] == 3
        assert body["missing_event_count"] == 2
        assert body["reconciliation_required"] is True


def test_event_ids_are_stable_across_a_full_replay(tmp_path: Path) -> None:
    """The same fixture must derive byte-identical ids on every run."""
    payload = json.loads(fixture_text("codex/notify_turn_complete.json"))
    first = [event["event_id"] for event in codex_notify_events(payload)]
    second = [event["event_id"] for event in codex_notify_events(payload)]
    assert first == second
    assert len(set(first)) == 3, "distinct messages must get distinct ids"
    # And the derivation is the shared one, not a local reimplementation.
    assert all(event_id.startswith("rmb1_") for event_id in first)


# ---------------------------------------------------------------------------
# the real Rust binary, when one is available
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("RESEARCH_MEMORY_BRIDGE_BIN"),
    reason="set RESEARCH_MEMORY_BRIDGE_BIN to a built research-memory-bridge binary",
)
def test_real_bridge_binary_round_trip(tmp_path: Path) -> None:
    binary = os.environ["RESEARCH_MEMORY_BRIDGE_BIN"]
    home = tmp_path / "bridge-home"
    home.mkdir()

    with GatewayServer(tmp_path / "gateway") as gateway:
        env = {
            **os.environ,
            "RESEARCH_MEMORY_BRIDGE_HOME": str(home),
            "RESEARCH_MEMORY_TOKEN": "test-token",
        }
        config = home / "config.toml"
        config.write_text(
            "\n".join(
                [
                    f'server_url = "{gateway.url}"',
                    'token_env = "RESEARCH_MEMORY_TOKEN"',
                    'client_id = "e2e-machine"',
                    "batch_size = 10",
                    "capture_drain = false",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        payload = fixture_text("codex/notify_turn_complete.json").strip()
        capture = subprocess.run(
            [binary, "capture", "--agent", "codex", payload],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        # Fail-open: capture always exits 0 and never writes to stdout.
        assert capture.returncode == 0, capture.stderr
        assert capture.stdout == "", "capture must not pollute the agent's context"

        drain_result = subprocess.run(
            [binary, "drain", "--once"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert drain_result.returncode == 0, drain_result.stderr

        search = gateway.service.conversation_retrieval.search("HNO3", limit=5)
        assert search["count"] >= 1

        status = subprocess.run(
            [binary, "status", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert status.returncode == 0
        state = json.loads(status.stdout)
        assert state["spool"]["pending"] == 0
        assert state["spool"]["acked"] >= 3


def test_emulator_matches_the_documented_identity_rules() -> None:
    """Guard the emulator itself: its ids must come from the shared algorithm."""
    payload = json.loads(fixture_text("codex/notify_turn_complete.json"))
    events = codex_notify_events(payload)
    for event in events:
        expected = derive_event_id(
            schema_version=1,
            source_system=event["source_system"],
            source_account_namespace=event["source_account_namespace"],
            conversation_id=event["conversation_id"],
            thread_id=event["thread_id"],
            branch_id=event["branch_id"],
            message_id=event["message_id"],
            turn_id=event["turn_id"],
            event_type=event["event_type"],
            content=event["content"],
        )
        assert event["event_id"] == expected
