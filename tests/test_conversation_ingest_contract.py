"""Cross-language contract: the JSON Schema, the pydantic models and the shared
event-id fixture must all agree.

`schemas/event-id-contract-v1.json` is generated from
`research_memory_gateway.ingest.identity` and asserted by *both* this suite and
the Rust bridge suite (`bridge/tests/event_id.rs`).  That is the only mechanism
keeping the two id derivations from drifting, and drifting would silently break
snapshot/hook deduplication.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_memory_gateway.ingest import schema as ingest_schema
from research_memory_gateway.ingest.identity import (
    content_identity,
    derive_event_id,
    event_content_hash,
    note_relative_path,
    session_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "schemas" / "event-id-contract-v1.json"
JSON_SCHEMA_PATH = REPO_ROOT / "schemas" / "conversation-ingest-v1.json"


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def json_schema() -> dict:
    return json.loads(JSON_SCHEMA_PATH.read_text(encoding="utf-8"))


def test_contract_fixture_matches_python_derivation(contract: dict) -> None:
    assert contract["derivation_version"] == 1
    cases = contract["cases"]
    assert len(cases) >= 8
    for case in cases:
        inputs = case["inputs"]
        assert content_identity(inputs["content"]) == case["content_identity"]
        assert derive_event_id(**inputs) == case["event_id"]
        assert (
            event_content_hash(inputs["event_type"], "", inputs["content"])
            == case["event_content_hash"]
        )


def test_contract_fixture_matches_session_keys(contract: dict) -> None:
    for case in contract["session_key_cases"]:
        derived = session_key(
            source_system=case["source_system"],
            source_account_namespace=case["source_account_namespace"],
            session_id=case["session_id"],
            conversation_id=case["conversation_id"],
            thread_id=case["thread_id"],
            branch_id=case["branch_id"],
        )
        assert derived == case["session_key"]


def test_event_id_is_stable_across_replays() -> None:
    kwargs = dict(
        schema_version=1,
        source_system="codex",
        source_account_namespace="ns",
        conversation_id="conv",
        thread_id="",
        branch_id="",
        message_id="m-1",
        turn_id="",
        event_type="user_prompt",
        content="identical",
    )
    first = derive_event_id(**kwargs)
    assert first == derive_event_id(**kwargs)
    assert first.startswith("rmb1_")
    assert len(first) == len("rmb1_") + 64


def test_whitespace_only_differences_do_not_change_identity() -> None:
    base = dict(
        schema_version=1,
        source_system="codex",
        source_account_namespace="",
        conversation_id="conv",
        thread_id="",
        branch_id="",
        message_id="m-1",
        turn_id="",
        event_type="user_prompt",
    )
    assert derive_event_id(**base, content="a  \r\nb") == derive_event_id(**base, content="a\nb")
    assert derive_event_id(**base, content="10 mM") != derive_event_id(**base, content="10  mM")


def test_turn_id_is_the_fallback_anchor() -> None:
    base = dict(
        schema_version=1,
        source_system="codex",
        source_account_namespace="",
        conversation_id="conv",
        thread_id="",
        branch_id="",
        event_type="user_prompt",
        content="content",
    )
    assert derive_event_id(**base, message_id="", turn_id="t-1") != derive_event_id(
        **base, message_id="m-1", turn_id="t-1"
    )
    assert derive_event_id(**base, message_id="", turn_id="t-1") == derive_event_id(
        **base, message_id="", turn_id="t-1"
    )


def test_json_schema_agrees_with_pydantic_models(json_schema: dict) -> None:
    """The published JSON Schema must not drift from the runtime models."""
    definitions = json_schema["$defs"]
    mapping = {
        "IngestEvent": ingest_schema.IngestEvent,
        "BatchRequest": ingest_schema.BatchRequest,
        "BatchResponse": ingest_schema.BatchResponse,
        "RejectedEvent": ingest_schema.RejectedEvent,
        "SessionEndRequest": ingest_schema.SessionEndRequest,
        "SnapshotRequest": ingest_schema.SnapshotRequest,
        "SnapshotMessage": ingest_schema.SnapshotMessage,
    }
    # `BatchResponse` is server-emitted, so its schema deliberately requires
    # every field even though the model carries defaults.
    response_models = {"BatchResponse"}
    for name, model in mapping.items():
        definition = definitions[name]
        model_fields = set(model.model_fields)
        schema_props = set(definition.get("properties", {}))
        assert schema_props == model_fields, f"{name} property mismatch"
        assert definition.get("additionalProperties") is False, f"{name} must forbid extras"
        if name in response_models:
            assert set(definition.get("required", [])) == model_fields, (
                f"{name} must document every field as always present"
            )
            continue
        required = {
            field for field, info in model.model_fields.items() if info.is_required()
        }
        assert set(definition.get("required", [])) == required, f"{name} required mismatch"


def test_batch_response_always_serializes_every_documented_field() -> None:
    payload = ingest_schema.BatchResponse().model_dump(mode="json")
    assert set(payload) == {
        "schema_version",
        "accepted",
        "duplicates",
        "rejected",
        "session",
    }
    assert payload["schema_version"] == ingest_schema.INGEST_SCHEMA_VERSION
    assert payload["accepted"] == []
    assert payload["duplicates"] == []
    assert payload["rejected"] == []


def test_requests_require_an_explicit_schema_version() -> None:
    """A client must never silently receive a version it did not ask for."""
    for model in (
        ingest_schema.BatchRequest,
        ingest_schema.SessionEndRequest,
        ingest_schema.SnapshotRequest,
    ):
        assert model.model_fields["schema_version"].is_required(), model.__name__
        with pytest.raises(ValidationError):
            model.model_validate({})
    with pytest.raises(ValidationError):
        ingest_schema.BatchRequest.model_validate({"schema_version": 1})


def test_json_schema_enumerations_match_the_code(json_schema: dict) -> None:
    assert set(json_schema["$defs"]["EventType"]["enum"]) == set(ingest_schema.EVENT_TYPES)
    assert set(json_schema["$defs"]["Role"]["enum"]) == set(ingest_schema.ROLES)
    assert set(json_schema["$defs"]["RejectionCode"]["enum"]) == set(ingest_schema.REJECTION_CODES)
    assert json_schema["x-schema-version"] == ingest_schema.INGEST_SCHEMA_VERSION


def test_note_paths_are_safe_and_deterministic() -> None:
    assert (
        note_relative_path(source_system="codex", conversation_id="abc")
        == "bridge/codex/abc.md"
    )
    # Traversal attempts cannot escape the staging root.
    assert ".." not in note_relative_path(
        source_system="codex", conversation_id="../../etc/passwd"
    )
    assert note_relative_path(source_system="codex", conversation_id="") == (
        "bridge/codex/unidentified.md"
    )
    # Windows reserved device names must not be used verbatim.
    assert note_relative_path(source_system="codex", conversation_id="CON") == (
        "bridge/codex/_CON.md"
    )
    # Branch/thread siblings get distinct, stable paths.
    a = note_relative_path(
        source_system="codex", conversation_id="c", thread_id="t1", branch_id=""
    )
    b = note_relative_path(
        source_system="codex", conversation_id="c", thread_id="t2", branch_id=""
    )
    assert a != b
    assert a == note_relative_path(
        source_system="codex", conversation_id="c", thread_id="t1", branch_id=""
    )
