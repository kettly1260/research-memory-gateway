"""v0.2.4 W10: source identity + deterministic fingerprint tests (taskbook 13.1, 5.x)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from research_memory_gateway.conversations.identity import (
    FINGERPRINT_VERSION,
    ConversationSourceIdentity,
    account_namespace_hash,
    canonical_conversation_id_for,
    codex_legacy_namespace_hash,
    compute_transcript_fingerprints,
    message_fingerprint,
    message_overlap_ratio,
    normalize_message_text,
    sequence_relation,
)
from research_memory_gateway.conversations.models import (
    AttachmentRef,
    NormalizedConversation,
    NormalizedMessage,
    ToolEvent,
)
from research_memory_gateway.conversations.readers import ConversationExportReader
from tests.synthetic_reader import SyntheticExportReader, SyntheticSession


def _identity(system: str, ns: str, cid: str, thread: str = "", branch: str = "") -> ConversationSourceIdentity:
    return ConversationSourceIdentity(
        source_system=system,
        source_account_namespace_hash=account_namespace_hash(system, ns),
        source_conversation_id=cid,
        source_thread_id=thread,
        source_branch_id=branch,
    )


# --- 13.1 #1: same source/account/provider id -> same source_key ---------------

def test_same_identity_yields_same_source_key() -> None:
    left = _identity("codex", "legacy-default-v1", "abc")
    right = _identity("codex", "legacy-default-v1", "abc")
    assert left.source_key == right.source_key
    assert left.source_key.startswith("srcv1_")
    assert len(left.source_key) == len("srcv1_") + 64


# --- 13.1 #2: same bare id, different source system -> different source_key ----

def test_same_bare_id_across_systems_diverges() -> None:
    codex = _identity("codex", "ns", "abc")
    chatgpt = _identity("chatgpt", "ns", "abc")
    assert codex.source_key != chatgpt.source_key


# --- 13.1 #3: same system, different account namespace -> different key --------

def test_same_system_different_namespace_diverges() -> None:
    left = _identity("chatgpt", "acct-a", "abc")
    right = _identity("chatgpt", "acct-b", "abc")
    assert left.source_key != right.source_key


def test_thread_and_branch_change_identity() -> None:
    base = _identity("codex", "ns", "abc")
    thread = _identity("codex", "ns", "abc", thread="t1")
    branch = _identity("codex", "ns", "abc", thread="t1", branch="b1")
    assert len({base.source_key, thread.source_key, branch.source_key}) == 3


# --- 13.1 #4: source key deterministic across processes -------------------------

def test_source_key_deterministic_across_process_interpreters() -> None:
    code = (
        "from research_memory_gateway.conversations.identity import "
        "account_namespace_hash, ConversationSourceIdentity;"
        "ident = ConversationSourceIdentity("
        "source_system='codex',"
        "source_account_namespace_hash=account_namespace_hash('codex','legacy-default-v1'),"
        "source_conversation_id='11111111-2222-3333-4444-555555555555');"
        "print(ident.source_key)"
    )
    results = [
        subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
        for _ in range(2)
    ]
    in_process = _identity("codex", "legacy-default-v1", "11111111-2222-3333-4444-555555555555").source_key
    assert results[0] == results[1] == in_process
    assert results[0].startswith("srcv1_")


# --- 13.1 #5: canonical UUIDv5 deterministic -----------------------------------

def test_canonical_id_is_deterministic_uuidv5_of_source_key() -> None:
    key = _identity("codex", "legacy-default-v1", "abc").source_key
    assert canonical_conversation_id_for(key) == canonical_conversation_id_for(key)
    assert canonical_conversation_id_for(key) != canonical_conversation_id_for(key + "x")
    assert canonical_conversation_id_for(key).count("-") == 4


def test_legacy_codex_namespace_is_stable() -> None:
    assert codex_legacy_namespace_hash() == codex_legacy_namespace_hash()
    assert len(codex_legacy_namespace_hash()) == 64


def test_runtime_hash_never_used() -> None:
    # PYTHONHASHSEED randomization must not affect the derivation.
    import os

    code = (
        "import hashlib;"
        "from research_memory_gateway.conversations.identity import codex_legacy_namespace_hash;"
        "print(hashlib.sha256(codex_legacy_namespace_hash().encode()).hexdigest()[:12])"
    )

    def run(seed: str) -> str:
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    assert run("1") == run("42")


# --- fingerprint normalization (taskbook 5.1) -----------------------------------

def test_message_fingerprint_normalization_is_conservative() -> None:
    base = "Keep exact path G:\\LLM\\memory and 324 nm.\nline two"
    crlf = "Keep exact path G:\\LLM\\memory and 324 nm.\r\nline two\r\n"
    trailing = base + "  \n"
    assert message_fingerprint("user", base) == message_fingerprint("user", crlf)
    assert message_fingerprint("user", base) == message_fingerprint("user", trailing)
    # NFC normalization folds composed forms
    decomposed = "cafe\u0301"
    composed = "caf\u00e9"
    assert normalize_message_text(decomposed) == normalize_message_text(composed)
    # Over-normalization is forbidden: case, inner whitespace, punctuation
    assert message_fingerprint("user", "Hello World") != message_fingerprint("user", "hello world")
    assert message_fingerprint("user", "a  b") != message_fingerprint("user", "a b")
    assert message_fingerprint("user", "a;b") != message_fingerprint("user", "ab")
    # Role normalization is case-insensitive but role-sensitive
    assert message_fingerprint("USER", "x") == message_fingerprint("user", "x")
    assert message_fingerprint("user", "x") != message_fingerprint("assistant", "x")
    # Timestamps and provider ids are not inputs
    assert message_fingerprint("user", "x") == message_fingerprint("user", "x")


def test_fingerprint_version_must_be_supported() -> None:
    with pytest.raises(ValueError):
        normalize_message_text("x", version=99)
    with pytest.raises(ValueError):
        message_fingerprint("user", "x", version=99)


# --- transcript fingerprints (taskbook 5.2/5.3) ----------------------------------

def _conversation(messages: list[tuple[str, str]], *, with_tools: bool = False) -> NormalizedConversation:
    msgs = [
        NormalizedMessage(ordinal=i, timestamp="", role=role, text=text, message_id=f"m{i}")
        for i, (role, text) in enumerate(messages)
    ]
    tools = [
        ToolEvent(
            ordinal=0,
            timestamp="",
            event_type="function_call",
            direction="call",
            name="exec",
            call_id="c1",
            payload_chars=3,
            payload_hash="abc",
            excerpt="",
        )
    ] if with_tools else []
    return NormalizedConversation(
        ref=None,  # type: ignore[arg-type]
        archive_path="",
        archive_sha256="",
        created_at="",
        session_meta={},
        messages=msgs,
        tools=tools,
    )


def test_transcript_fingerprints_fields_and_ordering() -> None:
    conv = _conversation([("user", "hello"), ("assistant", "hi")])
    fp = compute_transcript_fingerprints(conv)
    assert fp.fingerprint_version == FINGERPRINT_VERSION
    assert fp.message_count == 2
    assert len(fp.message_fingerprints) == 2
    reordered = _conversation([("assistant", "hi"), ("user", "hello")])
    fp_reordered = compute_transcript_fingerprints(reordered)
    assert fp.ordered_message_hash != fp_reordered.ordered_message_hash
    assert fp.message_set_hash == fp_reordered.message_set_hash
    assert fp.normalized_transcript_sha256 != fp_reordered.normalized_transcript_sha256


def test_transcript_fingerprints_exclude_injected_and_nonvisible() -> None:
    conv = _conversation([("user", "hello"), ("assistant", "hi")])
    conv.messages.insert(
        0, NormalizedMessage(ordinal=99, timestamp="", role="user", text="<environment_context>x</environment_context>", is_injected=True)
    )
    conv.messages.append(NormalizedMessage(ordinal=100, timestamp="", role="developer", text="hidden"))
    fp = compute_transcript_fingerprints(conv)
    assert fp.message_count == 2


def test_tool_and_attachment_hashes_are_independent_evidence() -> None:
    without = compute_transcript_fingerprints(_conversation([("user", "x")]))
    with_tool = compute_transcript_fingerprints(_conversation([("user", "x")], with_tools=True))
    assert without.tool_event_set_hash != with_tool.tool_event_set_hash
    assert without.ordered_message_hash == with_tool.ordered_message_hash
    assert without.normalized_transcript_sha256 == with_tool.normalized_transcript_sha256


# --- sequence relations (taskbook 7.1C/D/E) --------------------------------------

def test_sequence_relation_classes() -> None:
    a, b = ["f1"], ["f1", "f2"]
    modified = ["f1", "x"]          # second message content changed
    appended = ["f1", "f2", "f3"]   # new message appended
    assert sequence_relation(a, a) == "equal"
    assert sequence_relation(a, b) == "left_strict_prefix"
    assert sequence_relation(b, a) == "right_strict_prefix"
    assert sequence_relation(b, appended) == "left_strict_prefix"
    assert sequence_relation(b, modified) == "diverged"
    assert sequence_relation(modified, b) == "diverged"
    assert sequence_relation([], []) == "equal"
    assert sequence_relation([], a) == "left_strict_prefix"


def test_message_overlap_ratio() -> None:
    assert message_overlap_ratio(["a", "b"], ["a", "b"]) == 1.0
    assert message_overlap_ratio(["a"], ["a", "b"]) == pytest.approx(1 / 2)
    assert message_overlap_ratio(["a"], ["b"]) == 0.0


# --- synthetic reader satisfies the platform-agnostic contract -------------------

def test_synthetic_reader_implements_protocol(tmp_path: Path) -> None:
    reader = SyntheticExportReader(
        tmp_path / "chatgpt.zip",
        source_system="chatgpt",
        namespace_label="acct-a",
        sessions=[SyntheticSession("conv-1", messages=[("user", "hi")])],
    )
    assert isinstance(reader, ConversationExportReader)
    assert reader.source_system == "chatgpt"
    assert reader.parser_version == "synthetic-export-v1"
    refs = reader.list_sessions()
    assert refs[0].conversation_id == "conv-1"
    assert refs[0].source_identity().source_key.startswith("srcv1_")
