//! Transcript reconciliation framework: discovery, incremental cursors, and
//! "hook and transcript never archive the same message twice".

mod common;

use common::{fixture, TestHome};
use research_memory_bridge::adapters::AgentKind;
use research_memory_bridge::adapters::{claude_code, codex};
use research_memory_bridge::spool::Spool;
use research_memory_bridge::watcher::{
    ensure_event_ids, source_for, watch_once, ClaudeTranscriptSource, CodexTranscriptSource,
    TranscriptSource,
};

fn seed(home: &TestHome, relative: &str, destination: &str) -> std::path::PathBuf {
    let target = home.path().join(destination);
    std::fs::create_dir_all(target.parent().expect("parent")).expect("mkdir");
    std::fs::write(&target, fixture(relative)).expect("write fixture");
    target
}

#[test]
fn codex_source_discovers_rollouts_only() {
    let home = TestHome::new();
    seed(
        &home,
        "codex/rollout_sample.jsonl",
        "codex-sessions/2026/05/18/rollout-a.jsonl",
    );
    std::fs::write(home.path().join("codex-sessions/notes.txt"), "ignore me").expect("write");
    let source = CodexTranscriptSource;
    let found = source.discover(&home.config());
    assert_eq!(found.len(), 1, "{found:?}");
    assert!(found[0].to_string_lossy().ends_with("rollout-a.jsonl"));
}

#[test]
fn claude_source_discovers_jsonl_transcripts() {
    let home = TestHome::new();
    seed(
        &home,
        "claude/transcript_sample.jsonl",
        "claude-projects/slug/session.jsonl",
    );
    let source = ClaudeTranscriptSource;
    let found = source.discover(&home.config());
    assert_eq!(found.len(), 1, "{found:?}");
}

#[test]
fn codex_reconciliation_is_incremental_and_idempotent() {
    let home = TestHome::new();
    let path = seed(
        &home,
        "codex/rollout_sample.jsonl",
        "codex-sessions/2026/05/18/rollout-a.jsonl",
    );
    let config = home.config();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    let source = CodexTranscriptSource;

    let first = watch_once(&source, &config, &spool).expect("watch");
    assert_eq!(first.files_scanned, 1);
    assert_eq!(first.files_advanced, 1);
    assert!(first.events_inserted >= 3, "{first:?}");
    let after_first = spool.counts().expect("counts").total();

    // Second pass: the cursor means nothing is re-read.
    let second = watch_once(&source, &config, &spool).expect("watch");
    assert_eq!(second.files_advanced, 0);
    assert_eq!(second.events_parsed, 0);
    assert_eq!(spool.counts().expect("counts").total(), after_first);

    // Appending new records advances the cursor and inserts only the new ones.
    let original = std::fs::read_to_string(&path).expect("read");
    let extra = r#"{"timestamp":"2026-05-18T19:21:00.000Z","ordinal":10,"type":"response_item","payload":{"type":"message","role":"user","id":"msg_user_2","content":[{"type":"input_text","text":"第二个问题"}]}}"#;
    std::fs::write(&path, format!("{original}{extra}\n")).expect("append");
    let third = watch_once(&source, &config, &spool).expect("watch");
    assert_eq!(third.events_inserted, 1);
    assert_eq!(spool.counts().expect("counts").total(), after_first + 1);

    // Replaying the whole file from scratch is still safe: ids are stable.
    spool
        .cursor_set("codex", &path.display().to_string(), 0)
        .expect("reset cursor");
    let replay = watch_once(&source, &config, &spool).expect("watch");
    assert_eq!(
        replay.events_inserted, 0,
        "replay must only produce duplicates"
    );
    assert!(replay.events_duplicate > 0);
}

#[test]
fn claude_reconciliation_advances_cursor_per_file() {
    let home = TestHome::new();
    let path = seed(
        &home,
        "claude/transcript_sample.jsonl",
        "claude-projects/slug/session.jsonl",
    );
    let config = home.config();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    let source = ClaudeTranscriptSource;

    let report = watch_once(&source, &config, &spool).expect("watch");
    assert_eq!(report.files_advanced, 1);
    assert_eq!(report.events_inserted, 2, "{report:?}");
    let offset = spool
        .cursor_get("claude-code", &path.display().to_string())
        .expect("cursor");
    assert!(offset > 0);
}

#[test]
fn a_hook_captured_message_is_not_archived_twice_by_the_transcript() {
    // The realtime hook and the transcript reconciliation path must agree on the
    // identity of a user prompt, otherwise the same message lands in the spool
    // (and therefore the archive) twice.
    let home = TestHome::new();
    let config = home.config();
    let spool = Spool::open(&home.spool_path()).expect("spool");

    let hook_payload = fixture("claude/hook_user_prompt_submit.json");
    let mut hook_events = claude_code::capture(&hook_payload, &config)
        .expect("capture")
        .events;
    ensure_event_ids(&mut hook_events);
    assert_eq!(hook_events.len(), 1);
    let first = spool.enqueue_many(&hook_events).expect("enqueue hook");
    assert_eq!(first.len(), 1);

    let path = seed(
        &home,
        "claude/transcript_sample.jsonl",
        "claude-projects/slug/session.jsonl",
    );
    let (transcript_events, _) = claude_code::parse_transcript(&path, 0, &config).expect("parse");
    let mut transcript_events = transcript_events;
    ensure_event_ids(&mut transcript_events);

    let matching = transcript_events
        .iter()
        .filter(|event| event.event_id == hook_events[0].event_id)
        .count();
    assert_eq!(
        matching, 1,
        "the transcript must reproduce the hook's event id for the same user prompt"
    );

    let outcomes = spool
        .enqueue_many(&transcript_events)
        .expect("enqueue transcript");
    let inserted = outcomes
        .iter()
        .filter(|outcome| {
            matches!(
                outcome,
                research_memory_bridge::spool::EnqueueOutcome::Inserted
            )
        })
        .count();
    let duplicates = outcomes.len() - inserted;
    assert_eq!(
        duplicates, 1,
        "the hook copy must be recognised as a duplicate"
    );
    assert_eq!(spool.counts().expect("counts").total(), 2);
}

#[test]
fn unparseable_lines_are_consumed_without_producing_events() {
    let home = TestHome::new();
    // A file that exists but is not valid JSONL must not crash the pass or
    // manufacture events. Its complete lines *are* consumed (they can never
    // become JSON later), which keeps the pass idempotent.
    let target = home
        .path()
        .join("codex-sessions/2026/05/18/rollout-bad.jsonl");
    std::fs::create_dir_all(target.parent().expect("parent")).expect("mkdir");
    let body = "this is not json\nnor is this\n";
    std::fs::write(&target, body).expect("write");
    let config = home.config();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    let source = CodexTranscriptSource;

    let report = watch_once(&source, &config, &spool).expect("watch");
    assert_eq!(
        report.errors, 0,
        "unparseable lines are skipped, not errors"
    );
    assert_eq!(report.events_inserted, 0);
    assert_eq!(
        spool
            .cursor_get("codex", &target.display().to_string())
            .expect("cursor"),
        body.len() as i64
    );

    let second = watch_once(&source, &config, &spool).expect("watch");
    assert_eq!(second.files_advanced, 0, "the pass must be idempotent");
    assert_eq!(second.events_inserted, 0);
}

#[test]
fn an_incremental_pass_derives_the_same_ids_as_a_cold_pass() {
    // Regression guard: `session_meta` is the file's first record, so a pass that
    // starts after the cursor must still recover the thread id. Guessing it
    // would change every later message's derived event id and re-archive the
    // whole conversation.
    let original = fixture("codex/rollout_sample.jsonl");
    let extra = r#"{"timestamp":"2026-05-18T19:21:00.000Z","ordinal":10,"type":"response_item","payload":{"type":"message","role":"user","id":"msg_user_2","content":[{"type":"input_text","text":"第二个问题"}]}}"#;
    let full = format!("{original}{extra}\n");
    let relative = "codex-sessions/2026/05/18/rollout-a.jsonl";

    let ids_of = |spool: &Spool| -> Vec<String> {
        let mut ids: Vec<String> = spool
            .claim_batch(1000)
            .expect("claim")
            .into_iter()
            .map(|row| row.event.event_id)
            .collect();
        ids.sort();
        ids
    };

    // Cold: parse the complete file in one pass.
    let cold_home = TestHome::new();
    let cold_path = cold_home.path().join(relative);
    std::fs::create_dir_all(cold_path.parent().expect("parent")).expect("mkdir");
    std::fs::write(&cold_path, &full).expect("write");
    let cold_spool = Spool::open(&cold_home.spool_path()).expect("spool");
    let source = CodexTranscriptSource;
    watch_once(&source, &cold_home.config(), &cold_spool).expect("watch");
    let cold_ids = ids_of(&cold_spool);

    // Incremental: parse the prefix, append, then reconcile only the increment.
    let incr_home = TestHome::new();
    let incr_path = incr_home.path().join(relative);
    std::fs::create_dir_all(incr_path.parent().expect("parent")).expect("mkdir");
    std::fs::write(&incr_path, &original).expect("write prefix");
    let incr_spool = Spool::open(&incr_home.spool_path()).expect("spool");
    let first = watch_once(&source, &incr_home.config(), &incr_spool).expect("watch");
    assert!(first.events_inserted >= 3, "{first:?}");
    std::fs::write(&incr_path, &full).expect("append");
    let incremental = watch_once(&source, &incr_home.config(), &incr_spool).expect("watch");
    assert_eq!(incremental.events_inserted, 1, "{incremental:?}");

    assert_eq!(
        ids_of(&incr_spool),
        cold_ids,
        "incremental and cold parses must produce identical event ids"
    );
}

#[test]
fn source_for_covers_the_supported_agents_only() {
    assert!(source_for(AgentKind::Codex).is_some());
    assert!(source_for(AgentKind::ClaudeCode).is_some());
    assert!(source_for(AgentKind::Generic).is_none());
    assert_eq!(
        source_for(AgentKind::Codex).expect("codex").source_system(),
        codex::SOURCE_SYSTEM
    );
    assert_eq!(
        source_for(AgentKind::ClaudeCode)
            .expect("claude")
            .source_system(),
        claude_code::SOURCE_SYSTEM
    );
}
