"""Duplicate candidate detector (v0.2.4 W5).

Candidates are *review suggestions only*.  Nothing in this module may modify a
source record, canonical id, output path or Markdown note.  Every heuristic
produces evidence that a human reviews through the dedup CLI.  Cross-source
similarity -- however strong -- never auto-merges in v0.2.4.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Sequence

from .identity import message_overlap_ratio
from .identity_store import ConversationIdentityStore, SourceRecord

CANDIDATE_EXACT_SAME_SOURCE_MISSING_ID = "exact_transcript_same_source_missing_id"
CANDIDATE_CROSS_SOURCE_EXACT = "cross_source_exact_transcript"
CANDIDATE_STRICT_PREFIX = "strict_prefix_possible_continuation"
CANDIDATE_HIGH_OVERLAP = "high_message_overlap"
CANDIDATE_TITLE_TIME_WINDOW = "same_title_time_window"

CANDIDATE_TYPES = (
    CANDIDATE_EXACT_SAME_SOURCE_MISSING_ID,
    CANDIDATE_CROSS_SOURCE_EXACT,
    CANDIDATE_STRICT_PREFIX,
    CANDIDATE_HIGH_OVERLAP,
    CANDIDATE_TITLE_TIME_WINDOW,
)

# Thresholds are deliberately conservative.  Scores only order the review
# queue; they are never an automatic merge basis.
HIGH_OVERLAP_THRESHOLD = 0.90
TITLE_SIMILARITY_THRESHOLD = 0.90
TITLE_TIME_WINDOW_HOURS = 48.0

# Evidence weight used purely for review-queue ordering.
SCORE_WEIGHTS = {
    CANDIDATE_EXACT_SAME_SOURCE_MISSING_ID: 0.9,
    CANDIDATE_CROSS_SOURCE_EXACT: 0.8,
    CANDIDATE_STRICT_PREFIX: 0.7,
    CANDIDATE_HIGH_OVERLAP: 0.6,
    CANDIDATE_TITLE_TIME_WINDOW: 0.2,
}


@dataclass(frozen=True)
class CandidateProposal:
    left_source_key: str
    right_source_key: str
    candidate_type: str
    score: float
    evidence: dict[str, Any]


@dataclass
class DetectionReport:
    created: int = 0
    suppressed: int = 0
    refreshed: int = 0
    proposals: list[CandidateProposal] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.proposals is None:
            self.proposals = []


def _title_similarity(left: str, right: str) -> float:
    left_norm = " ".join((left or "").split()).lower()
    right_norm = " ".join((right or "").split()).lower()
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_between(left: str, right: str) -> float | None:
    left_dt = _parse_timestamp(left)
    right_dt = _parse_timestamp(right)
    if left_dt is None or right_dt is None:
        return None
    if left_dt.tzinfo is None:
        left_dt = left_dt.replace(tzinfo=timezone.utc)
    if right_dt.tzinfo is None:
        right_dt = right_dt.replace(tzinfo=timezone.utc)
    return abs((left_dt - right_dt).total_seconds()) / 3600.0


def _pair_blocked(
    store: ConversationIdentityStore, left: str, right: str
) -> bool:
    """A pair already decided (or already linked) must not re-enter the queue."""
    left_record = store.get_source_record(left)
    right_record = store.get_source_record(right)
    if left_record and right_record:
        if left_record.canonical_conversation_id == right_record.canonical_conversation_id:
            return True
    for existing in store.list_candidates():
        if {existing.left_source_key, existing.right_source_key} == {left, right}:
            if existing.status in {"confirmed_same", "rejected"}:
                return True
    return False


def _same_canonical_group(store: ConversationIdentityStore, record: SourceRecord) -> set[str]:
    return {
        other.source_key
        for other in store.list_source_records()
        if other.canonical_conversation_id == record.canonical_conversation_id
    }


def build_candidate_proposals(
    store: ConversationIdentityStore,
    record: SourceRecord,
    *,
    fingerprints: Any | None = None,
    title: str = "",
    updated_at: str = "",
) -> list[CandidateProposal]:
    """Deterministic candidate proposals comparing one record against the store.

    ``fingerprints`` (a TranscriptFingerprints) and ``title``/``updated_at``
    describe the new snapshot; when omitted the stored record metadata is used.

    Comparisons are batched (one fingerprint query for all peers, one
    candidates read) so importing a full multi-hundred-conversation export
    stays linear-ish instead of opening a connection per pair (v0.2.6 C12).
    """
    if fingerprints is not None:
        new_ordered = list(fingerprints.message_fingerprints)
        new_set_hash = fingerprints.message_set_hash
        new_transcript = fingerprints.normalized_transcript_sha256
        new_count = fingerprints.message_count
    else:
        stored_sequence = store.message_fingerprint_sequence(record.source_key)
        new_ordered = stored_sequence
        new_set_hash = record.message_set_hash
        new_transcript = record.normalized_transcript_sha256
        new_count = record.message_count

    linked_group = _same_canonical_group(store, record)
    proposals: list[CandidateProposal] = []

    peers = [other for other in store.list_source_records() if other.source_key != record.source_key]
    all_candidates = store.list_candidates()
    decided_pairs: set[frozenset[str]] = set()
    for existing in all_candidates:
        pair = frozenset({existing.left_source_key, existing.right_source_key})
        if existing.status in {"confirmed_same", "rejected"}:
            decided_pairs.add(pair)
    for other in peers:
        if other.source_key in linked_group:
            continue
        if record.canonical_conversation_id and (
            other.canonical_conversation_id == record.canonical_conversation_id
        ):
            continue

    peer_sequences = store.message_fingerprint_sequences([other.source_key for other in peers])

    for other in peers:
        if other.source_key in linked_group:
            continue
        if frozenset({record.source_key, other.source_key}) in decided_pairs:
            continue

        other_sequence = peer_sequences.get(other.source_key, [])
        overlap = message_overlap_ratio(new_ordered, other_sequence)
        same_system = other.source_system == record.source_system
        same_namespace = (
            other.source_account_namespace_hash == record.source_account_namespace_hash
        )
        both_id_less = (
            record.source_conversation_id.startswith("synthetic-content-")
            and other.source_conversation_id.startswith("synthetic-content-")
        )

        # exact transcripts -------------------------------------------------
        if new_transcript and new_transcript == other.normalized_transcript_sha256:
            evidence = _evidence(
                same_system=same_system,
                same_namespace=same_namespace,
                exact_transcript=True,
                ordered_prefix=overlap == 1.0,
                message_overlap=overlap,
                left_message_count=new_count,
                right_message_count=other.message_count,
            )
            if same_system and same_namespace and both_id_less:
                proposals.append(
                    CandidateProposal(
                        record.source_key,
                        other.source_key,
                        CANDIDATE_EXACT_SAME_SOURCE_MISSING_ID,
                        SCORE_WEIGHTS[CANDIDATE_EXACT_SAME_SOURCE_MISSING_ID],
                        evidence,
                    )
                )
            elif not same_system:
                proposals.append(
                    CandidateProposal(
                        record.source_key,
                        other.source_key,
                        CANDIDATE_CROSS_SOURCE_EXACT,
                        SCORE_WEIGHTS[CANDIDATE_CROSS_SOURCE_EXACT],
                        evidence,
                    )
                )

        # strict prefix -----------------------------------------------------
        if new_ordered and other_sequence and list(new_ordered) != list(other_sequence):
            common = min(len(new_ordered), len(other_sequence))
            if list(new_ordered[:common]) == list(other_sequence[:common]):
                evidence = _evidence(
                    same_system=same_system,
                    same_namespace=same_namespace,
                    exact_transcript=False,
                    ordered_prefix=True,
                    message_overlap=overlap,
                    left_message_count=new_count,
                    right_message_count=other.message_count,
                )
                proposals.append(
                    CandidateProposal(
                        record.source_key,
                        other.source_key,
                        CANDIDATE_STRICT_PREFIX,
                        SCORE_WEIGHTS[CANDIDATE_STRICT_PREFIX],
                        evidence,
                    )
                )

        # high message-set overlap --------------------------------------------
        if (
            new_set_hash
            and other.message_set_hash
            and new_set_hash != other.message_set_hash
            and overlap >= HIGH_OVERLAP_THRESHOLD
            and overlap < 1.0
        ):
            evidence = _evidence(
                same_system=same_system,
                same_namespace=same_namespace,
                exact_transcript=False,
                ordered_prefix=False,
                message_overlap=overlap,
                left_message_count=new_count,
                right_message_count=other.message_count,
            )
            proposals.append(
                CandidateProposal(
                    record.source_key,
                    other.source_key,
                    CANDIDATE_HIGH_OVERLAP,
                    SCORE_WEIGHTS[CANDIDATE_HIGH_OVERLAP],
                    evidence,
                )
            )

        # same title inside a close time window (low evidence weight) ----------
        title_sim = _title_similarity(title, _record_title(store, other))
        hours = _hours_between(updated_at or record.last_seen_at, other.last_seen_at)
        if title_sim >= TITLE_SIMILARITY_THRESHOLD and hours is not None and hours <= TITLE_TIME_WINDOW_HOURS:
            evidence = _evidence(
                same_system=same_system,
                same_namespace=same_namespace,
                exact_transcript=False,
                ordered_prefix=False,
                message_overlap=overlap,
                left_message_count=new_count,
                right_message_count=other.message_count,
                title_similarity=round(title_sim, 4),
                time_window_hours=round(hours, 2),
            )
            proposals.append(
                CandidateProposal(
                    record.source_key,
                    other.source_key,
                    CANDIDATE_TITLE_TIME_WINDOW,
                    SCORE_WEIGHTS[CANDIDATE_TITLE_TIME_WINDOW] * title_sim,
                    evidence,
                )
            )

    return proposals


def _record_title(store: ConversationIdentityStore, record: SourceRecord) -> str:
    return record.output_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


def _evidence(
    *,
    same_system: bool,
    same_namespace: bool,
    exact_transcript: bool,
    ordered_prefix: bool,
    message_overlap: float,
    left_message_count: int,
    right_message_count: int,
    **extra: Any,
) -> dict[str, Any]:
    evidence = {
        "same_source_system": same_system,
        "same_account_namespace": same_namespace,
        "exact_transcript": exact_transcript,
        "ordered_prefix": ordered_prefix,
        "message_overlap": round(message_overlap, 4),
        "left_message_count": left_message_count,
        "right_message_count": right_message_count,
    }
    evidence.update(extra)
    return evidence


def run_candidate_detection(
    store: ConversationIdentityStore,
    record: SourceRecord,
    *,
    fingerprints: Any | None = None,
    title: str = "",
    updated_at: str = "",
) -> DetectionReport:
    """Create pending candidates for a source record; never blocks ingestion."""
    report = DetectionReport()
    for proposal in build_candidate_proposals(
        store,
        record,
        fingerprints=fingerprints,
        title=title,
        updated_at=updated_at,
    ):
        candidate, created = store.upsert_candidate(
            proposal.left_source_key,
            proposal.right_source_key,
            proposal.candidate_type,
            score=proposal.score,
            evidence=proposal.evidence,
        )
        if created:
            report.created += 1
            report.proposals.append(proposal)
        elif candidate.status == "pending":
            report.refreshed += 1
        else:
            report.suppressed += 1
    return report


def summarize_evidence(evidence_json: str) -> str:
    try:
        return json.dumps(json.loads(evidence_json), ensure_ascii=False, sort_keys=True)
    except json.JSONDecodeError:
        return evidence_json


def filter_candidates(
    candidates: Sequence[Any],
    *,
    status: str | None = None,
) -> list[Any]:
    if status is None:
        return list(candidates)
    return [c for c in candidates if c.status == status]
