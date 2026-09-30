//! Durable spool: idempotency, crash recovery, retry bookkeeping and bounds.

mod common;

use common::TestHome;
use research_memory_bridge::event::{EventType, NormalizedEvent};
use research_memory_bridge::spool::{
    EnqueueOutcome, Spool, STATUS_ACKED, STATUS_DEAD_LETTER, STATUS_PENDING,
};

fn event(id: &str, content: &str) -> NormalizedEvent {
    let mut event = NormalizedEvent::new("codex", EventType::UserPrompt, content)
        .with_session("session-1")
        .with_conversation("session-1");
    event.event_id = id.to_string();
    event
}

#[test]
fn enqueue_is_idempotent() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    assert_eq!(
        spool
            .enqueue(&event("rmb1_aaaaaaaa", "hello"))
            .expect("first"),
        EnqueueOutcome::Inserted
    );
    assert_eq!(
        spool
            .enqueue(&event("rmb1_aaaaaaaa", "hello"))
            .expect("second"),
        EnqueueOutcome::Duplicate
    );
    let counts = spool.counts().expect("counts");
    assert_eq!(counts.pending, 1);
    assert_eq!(counts.total(), 1);
}

#[test]
fn spool_survives_a_restart() {
    let home = TestHome::new();
    {
        let spool = Spool::open(&home.spool_path()).expect("spool");
        spool
            .enqueue(&event("rmb1_persist1", "one"))
            .expect("insert");
        spool
            .enqueue(&event("rmb1_persist2", "two"))
            .expect("insert");
        spool
            .cursor_set("codex", "/tmp/rollout.jsonl", 4096)
            .expect("cursor");
    }
    let reopened = Spool::open(&home.spool_path()).expect("reopen");
    assert_eq!(reopened.counts().expect("counts").pending, 2);
    assert_eq!(
        reopened
            .cursor_get("codex", "/tmp/rollout.jsonl")
            .expect("cursor"),
        4096
    );
    // A replay after restart must still be rejected.
    assert_eq!(
        reopened
            .enqueue(&event("rmb1_persist1", "one"))
            .expect("replay"),
        EnqueueOutcome::Duplicate
    );
}

#[test]
fn claim_ack_cycle_round_trips_payloads() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool
        .enqueue(&event("rmb1_claim001", "payload one"))
        .expect("insert");
    let claimed = spool.claim_batch(10).expect("claim");
    assert_eq!(claimed.len(), 1);
    assert_eq!(claimed[0].event.content, "payload one");
    assert_eq!(claimed[0].event.event_type, "user_prompt");
    assert_eq!(claimed[0].event.role, "user");
    assert_eq!(claimed[0].attempts, 0);
    assert_eq!(spool.counts().expect("counts").inflight, 1);

    spool.ack(&[claimed[0].event_id.clone()]).expect("ack");
    let counts = spool.counts().expect("counts");
    assert_eq!(counts.acked, 1);
    assert_eq!(counts.pending, 0);
    assert_eq!(counts.inflight, 0);
    // ACKed rows are never re-claimed.
    assert!(spool.claim_batch(10).expect("claim again").is_empty());
}

#[test]
fn attempts_are_incremented_per_claim() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_attempt1", "x")).expect("insert");
    let first = spool.claim_batch(1).expect("claim");
    assert_eq!(first[0].attempts, 0);
    spool
        .reschedule(&[first[0].event_id.clone()], 0, "boom")
        .expect("reschedule");
    let second = spool.claim_batch(1).expect("claim");
    assert_eq!(second[0].attempts, 1);
    assert_eq!(
        spool.attempts_for(&second[0].event_id).expect("attempts"),
        2
    );
}

#[test]
fn stale_inflight_rows_are_reclaimed_after_a_crash() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_crash001", "x")).expect("insert");
    let claimed = spool.claim_batch(1).expect("claim");
    assert_eq!(spool.counts().expect("counts").inflight, 1);
    // A drain that died mid-upload leaves the row inflight forever unless the
    // next pass reclaims it.
    let reclaimed = spool.reclaim_stale_inflight(0).expect("reclaim");
    assert_eq!(reclaimed, 1);
    let counts = spool.counts().expect("counts");
    assert_eq!(counts.inflight, 0);
    assert_eq!(counts.pending, 1);
    let again = spool.claim_batch(1).expect("claim again");
    assert_eq!(again[0].event_id, claimed[0].event_id);
}

#[test]
fn reschedule_defers_the_retry_until_due() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_retry001", "x")).expect("insert");
    let claimed = spool.claim_batch(1).expect("claim");
    let future = research_memory_bridge::spool::now_epoch() + 3600;
    spool
        .reschedule(&[claimed[0].event_id.clone()], future, "gateway down")
        .expect("reschedule");
    assert!(
        !spool.has_due_pending().expect("due"),
        "a future retry must not be due"
    );
    assert!(spool.claim_batch(10).expect("claim").is_empty());
    let counts = spool.counts().expect("counts");
    assert_eq!(counts.pending, 1);
    assert_eq!(spool.next_retry_epoch().expect("next"), Some(future),);
}

#[test]
fn dead_letter_is_terminal() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_dead0001", "x")).expect("insert");
    let claimed = spool.claim_batch(1).expect("claim");
    spool
        .dead_letter(&[claimed[0].event_id.clone()], "content_too_large")
        .expect("dead letter");
    let counts = spool.counts().expect("counts");
    assert_eq!(counts.dead_letter, 1);
    assert_eq!(counts.pending, 0);
    assert!(spool.claim_batch(10).expect("claim").is_empty());
    let sample = spool.dead_letter_sample(5).expect("sample");
    assert_eq!(sample[0].0, "rmb1_dead0001");
    assert_eq!(sample[0].1, "content_too_large");
}

#[test]
fn prune_acked_keeps_pending_rows() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool
        .enqueue(&event("rmb1_prune001", "acked"))
        .expect("insert");
    spool
        .enqueue(&event("rmb1_prune002", "pending"))
        .expect("insert");
    let claimed = spool.claim_batch(10).expect("claim");
    let acked_id = claimed
        .iter()
        .find(|row| row.event.content == "acked")
        .map(|row| row.event_id.clone())
        .expect("acked row");
    spool.ack(&[acked_id]).expect("ack");
    // Return the other claimed row to `pending` so the assertion below is about
    // retention, not about which state the claim left it in.
    let other: Vec<String> = claimed
        .iter()
        .filter(|row| row.event.content != "acked")
        .map(|row| row.event_id.clone())
        .collect();
    spool.reschedule(&other, 0, "").expect("reschedule");

    // Retention of 0 days must not delete anything.
    assert_eq!(spool.prune_acked(0).expect("prune"), 0);
    assert_eq!(spool.counts().expect("counts").acked, 1);
    // A positive window keeps fresh ACKed rows, and never touches pending ones.
    let pruned = spool.prune_acked(1).expect("prune");
    assert_eq!(pruned, 0, "fresh ACKed rows are inside the window");
    let counts = spool.counts().expect("counts");
    assert_eq!(counts.pending, 1);
    assert_eq!(counts.acked, 1);
}

#[test]
fn prune_acked_deletes_rows_older_than_the_retention_window() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_old00001", "old")).expect("insert");
    let claimed = spool.claim_batch(1).expect("claim");
    spool.ack(&[claimed[0].event_id.clone()]).expect("ack");

    // Backdate the row so it falls outside a one-day retention window. This is
    // the only way to exercise real deletion without sleeping a day.
    let connection = rusqlite::Connection::open(home.spool_path()).expect("open");
    connection
        .execute(
            "UPDATE events SET created_at = ?1 WHERE event_id = ?2",
            rusqlite::params!["2000-01-01T00:00:00+00:00", "rmb1_old00001"],
        )
        .expect("backdate");

    let pruned = spool.prune_acked(1).expect("prune");
    assert_eq!(pruned, 1);
    assert_eq!(spool.counts().expect("counts").total(), 0);
}

#[test]
fn pending_cap_parks_the_oldest_events_instead_of_dropping_them() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    for index in 0..5 {
        spool
            .enqueue(&event(
                &format!("rmb1_cap{index:05}"),
                &format!("body {index}"),
            ))
            .expect("insert");
    }
    let parked = spool.enforce_pending_cap(2).expect("cap");
    assert_eq!(parked, 3);
    let counts = spool.counts().expect("counts");
    assert_eq!(counts.pending, 2);
    assert_eq!(counts.dead_letter, 3);
    // Nothing was silently destroyed: every event is still inspectable.
    assert_eq!(counts.total(), 5);
}

#[test]
fn cursor_round_trip_and_missing_file_pruning() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    assert_eq!(spool.cursor_get("codex", "/nope.jsonl").expect("get"), 0);
    spool.cursor_set("codex", "/nope.jsonl", 128).expect("set");
    assert_eq!(spool.cursor_get("codex", "/nope.jsonl").expect("get"), 128);
    spool.cursor_set("codex", "/nope.jsonl", 256).expect("set");
    assert_eq!(spool.cursor_get("codex", "/nope.jsonl").expect("get"), 256);
    assert_eq!(spool.cursor_count().expect("count"), 1);
    assert_eq!(spool.prune_missing_cursors("codex").expect("prune"), 1);
    assert_eq!(spool.cursor_count().expect("count"), 0);
}

#[test]
fn session_tracking_counts_only_message_events() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    let mut user = event("rmb1_sess0001", "question");
    user.session_id = "s-9".to_string();
    user.conversation_id = "s-9".to_string();
    let mut assistant = NormalizedEvent::new("codex", EventType::AssistantMessage, "answer")
        .with_session("s-9")
        .with_conversation("s-9");
    assistant.event_id = "rmb1_sess0002".to_string();
    let mut tool = NormalizedEvent::new("codex", EventType::ToolCall, "{}")
        .with_session("s-9")
        .with_conversation("s-9");
    tool.event_id = "rmb1_sess0003".to_string();

    spool.enqueue(&user).expect("insert");
    spool.enqueue(&assistant).expect("insert");
    spool.enqueue(&tool).expect("insert");

    assert_eq!(
        spool.count_session_messages("codex", "s-9").expect("count"),
        2
    );
    spool
        .session_upsert("codex", "s-9", "s-9", "m-2", 2)
        .expect("upsert");
    assert_eq!(spool.session_count().expect("sessions"), 1);
    spool.session_mark_ended("codex", "s-9").expect("ended");
    let pending = spool.unreconciled_sessions(10).expect("pending");
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].observed_message_count, 2);
    spool
        .mark_session_reconciled("codex", "s-9")
        .expect("reconciled");
    assert!(spool.unreconciled_sessions(10).expect("pending").is_empty());
}

#[test]
fn oldest_pending_age_tracks_unuploaded_work() {
    let home = TestHome::new();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    assert_eq!(spool.oldest_pending_epoch().expect("oldest"), None);
    spool.enqueue(&event("rmb1_age00001", "x")).expect("insert");
    let oldest = spool.oldest_pending_epoch().expect("oldest").expect("some");
    let now = research_memory_bridge::spool::now_epoch();
    assert!(
        oldest <= now && now - oldest < 60,
        "oldest={oldest} now={now}"
    );
    let claimed = spool.claim_batch(1).expect("claim");
    spool.ack(&[claimed[0].event_id.clone()]).expect("ack");
    assert_eq!(spool.oldest_pending_epoch().expect("oldest"), None);
}

#[test]
fn permanent_rejection_codes_are_classified() {
    use research_memory_bridge::spool::is_permanent_rejection;
    for code in [
        "unsupported_schema_version",
        "invalid_payload",
        "content_too_large",
        "event_id_conflict",
        "invalid_role",
    ] {
        assert!(is_permanent_rejection(code), "{code} should be permanent");
    }
    for code in ["internal_error", "archive_write_failed", "something_new"] {
        assert!(!is_permanent_rejection(code), "{code} should be retryable");
    }
}

#[test]
fn status_strings_are_the_documented_set() {
    assert_eq!(STATUS_PENDING, "pending");
    assert_eq!(STATUS_ACKED, "acked");
    assert_eq!(STATUS_DEAD_LETTER, "dead_letter");
}
