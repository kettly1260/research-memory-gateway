//! Adapter tests driven by fixtures that mirror the *verified* provider payloads.

mod common;

use common::{fixture, TestHome};
use research_memory_bridge::adapters::{self, claude_code, codex, AgentKind};
use research_memory_bridge::event::EventType;

fn capture(agent: AgentKind, payload: &str, home: &TestHome) -> adapters::CaptureOutcome {
    adapters::capture(agent, payload, &home.config()).expect("capture")
}

fn copy_fixture(home: &TestHome, relative: &str, name: &str) -> std::path::PathBuf {
    let target = home.path().join(name);
    std::fs::write(&target, fixture(relative)).expect("write fixture");
    target
}

// ---------------------------------------------------------------------------
// Codex: notify (realtime)
// ---------------------------------------------------------------------------

#[test]
fn codex_notify_maps_turn_completion_to_messages() {
    let home = TestHome::new();
    let outcome = capture(
        AgentKind::Codex,
        &fixture("codex/notify_turn_complete.json"),
        &home,
    );
    assert_eq!(outcome.events.len(), 3, "{:?}", outcome.notes);
    let user_events: Vec<_> = outcome
        .events
        .iter()
        .filter(|event| event.event_type == "user_prompt")
        .collect();
    let assistant_events: Vec<_> = outcome
        .events
        .iter()
        .filter(|event| event.event_type == "assistant_message")
        .collect();
    assert_eq!(user_events.len(), 2);
    assert_eq!(assistant_events.len(), 1);
    assert_eq!(user_events[0].content, "之前 Fe 的硝酸溶液怎么配的？");
    assert_eq!(user_events[0].role, "user");
    assert_eq!(assistant_events[0].role, "assistant");
    let thread = "019e3ad1-05d6-7382-972f-0d377e6092c6";
    assert_eq!(user_events[0].session_id, thread);
    assert_eq!(user_events[0].conversation_id, thread);
    assert_eq!(user_events[0].thread_id, thread);
    assert_eq!(assistant_events[0].conversation_id, thread);
    // No provider ids exist on this path, so the adapter must say so.
    for event in &outcome.events {
        assert_eq!(event.metadata["id_source"], "synthesized");
        assert_eq!(event.metadata["adapter"], "codex-notify");
        assert!(!event.event_id.is_empty());
    }
}

#[test]
fn codex_notify_replay_produces_identical_event_ids() {
    let home = TestHome::new();
    let payload = fixture("codex/notify_turn_complete.json");
    let first = capture(AgentKind::Codex, &payload, &home);
    let second = capture(AgentKind::Codex, &payload, &home);
    let ids = |outcome: &adapters::CaptureOutcome| {
        outcome
            .events
            .iter()
            .map(|event| event.event_id.clone())
            .collect::<Vec<_>>()
    };
    assert_eq!(ids(&first), ids(&second));
    assert_eq!(ids(&first).len(), 3);
}

#[test]
fn codex_notify_ignores_unsupported_events_without_erroring() {
    let home = TestHome::new();
    let outcome = capture(
        AgentKind::Codex,
        r#"{"type":"something-else","thread-id":"t"}"#,
        &home,
    );
    assert!(outcome.events.is_empty());
    assert!(!outcome.notes.is_empty());
}

#[test]
fn codex_notify_handles_malformed_input_gracefully() {
    let home = TestHome::new();
    for payload in ["", "not json", "[]", "{}"] {
        let outcome = capture(AgentKind::Codex, payload, &home);
        assert!(
            outcome.events.is_empty(),
            "payload {payload:?} should not produce events"
        );
        assert!(
            !outcome.notes.is_empty(),
            "payload {payload:?} should explain why"
        );
    }
}

#[test]
fn codex_notify_skips_injected_user_messages() {
    let home = TestHome::new();
    let payload = serde_json::json!({
        "type": "agent-turn-complete",
        "thread-id": "thread-x",
        "last-assistant-message": "ok",
        "input-messages": [
            "<environment_context>cwd=G:\\LLM</environment_context>",
            "real question"
        ]
    })
    .to_string();
    let outcome = capture(AgentKind::Codex, &payload, &home);
    let users: Vec<_> = outcome
        .events
        .iter()
        .filter(|event| event.event_type == "user_prompt")
        .collect();
    assert_eq!(users.len(), 1);
    assert_eq!(users[0].content, "real question");
}

// ---------------------------------------------------------------------------
// Codex: rollout transcript (reconciliation)
// ---------------------------------------------------------------------------

#[test]
fn codex_rollout_parses_messages_and_skips_noise() {
    let home = TestHome::new();
    let path = copy_fixture(&home, "codex/rollout_sample.jsonl", "rollout.jsonl");
    let config = home.config();
    let (events, offset) = codex::parse_rollout(&path, 0, &config).expect("parse");
    assert!(offset > 0);

    let types: Vec<&str> = events
        .iter()
        .map(|event| event.event_type.as_str())
        .collect();
    assert!(types.contains(&"session_start"), "{types:?}");
    assert!(types.contains(&"user_prompt"), "{types:?}");
    assert!(types.contains(&"assistant_message"), "{types:?}");
    // Tool telemetry is off by default.
    assert!(!types.contains(&"tool_call"), "{types:?}");

    let user = events
        .iter()
        .find(|event| event.event_type == "user_prompt")
        .expect("user event");
    assert_eq!(user.content, "之前 Fe 的硝酸溶液怎么配的？");
    assert_eq!(user.message_id, "msg_user_1");
    assert_eq!(user.metadata["id_source"], "provider");

    // `developer` messages and injected `<environment_context>` user text are
    // packaging noise, not conversation.
    assert!(!events
        .iter()
        .any(|event| event.content.contains("environment_context")));

    let assistant = events
        .iter()
        .find(|event| event.event_type == "assistant_message")
        .expect("assistant event");
    assert_eq!(assistant.message_id, "msg_asst_1");
    assert_eq!(assistant.turn_id, "turn-1");
    assert_eq!(assistant.metadata["phase"], "final_answer");

    let session_start = events
        .iter()
        .find(|event| event.event_type == "session_start")
        .expect("session start");
    assert_eq!(session_start.metadata["cwd"], "G:\\LLM\\memory");
    assert_eq!(session_start.metadata["agent_version"], "0.9.1");
}

#[test]
fn codex_rollout_is_incremental_and_line_boundary_safe() {
    let home = TestHome::new();
    let path = copy_fixture(&home, "codex/rollout_sample.jsonl", "rollout.jsonl");
    let config = home.config();
    let (first, offset) = codex::parse_rollout(&path, 0, &config).expect("parse");
    assert!(!first.is_empty());
    // Nothing new after the cursor.
    let (second, offset2) = codex::parse_rollout(&path, offset, &config).expect("parse");
    assert!(second.is_empty());
    assert_eq!(offset, offset2);

    // Append a partial line: it must not be consumed.
    let original = std::fs::read_to_string(&path).expect("read");
    let partial = r#"{"timestamp":"2026-05-18T19:20:45.000Z","ordinal":10,"type":"response_item","payload":{"type":"message","role":"user","id":"msg_user_2","content":[{"type":"input_text","text":"second question"}]}}"#;
    std::fs::write(&path, format!("{original}{partial}")).expect("append partial");
    let (third, offset3) = codex::parse_rollout(&path, offset2, &config).expect("parse");
    assert!(
        third.is_empty(),
        "an unterminated line must not be consumed"
    );
    assert_eq!(offset2, offset3);

    // Complete the line and it is picked up exactly once.
    std::fs::write(&path, format!("{original}{partial}\n")).expect("append complete");
    let (fourth, offset4) = codex::parse_rollout(&path, offset3, &config).expect("parse");
    assert_eq!(fourth.len(), 1);
    assert_eq!(fourth[0].content, "second question");
    assert!(offset4 > offset3);
}

#[test]
fn codex_rollout_tool_events_are_opt_in() {
    let home = TestHome::new();
    let path = copy_fixture(&home, "codex/rollout_sample.jsonl", "rollout.jsonl");
    let mut config = home.config();
    config.tool_events = true;
    let (events, _) = codex::parse_rollout(&path, 0, &config).expect("parse");
    let tool_types: Vec<&str> = events
        .iter()
        .filter(|event| event.event_type.starts_with("tool"))
        .map(|event| event.event_type.as_str())
        .collect();
    assert_eq!(tool_types, vec!["tool_call", "tool_result"], "{events:?}");
    let call = events
        .iter()
        .find(|event| event.event_type == "tool_call")
        .expect("call");
    assert_eq!(call.metadata["tool_name"], "shell");
    assert_eq!(call.metadata["call_id"], "call_1");
}

// ---------------------------------------------------------------------------
// Claude Code: hooks (realtime)
// ---------------------------------------------------------------------------

#[test]
fn claude_hook_user_prompt_submit_uses_prompt_id() {
    let home = TestHome::new();
    let outcome = capture(
        AgentKind::ClaudeCode,
        &fixture("claude/hook_user_prompt_submit.json"),
        &home,
    );
    assert_eq!(outcome.events.len(), 1, "{:?}", outcome.notes);
    let event = &outcome.events[0];
    assert_eq!(event.event_type, "user_prompt");
    assert_eq!(event.role, "user");
    assert_eq!(event.content, "之前 Fe 的硝酸溶液怎么配的？");
    assert_eq!(event.session_id, "3ba920a2-714f-4aad-a556-0b9b2a18587e");
    assert_eq!(
        event.conversation_id,
        "3ba920a2-714f-4aad-a556-0b9b2a18587e"
    );
    // prompt_id is the one id shared with the transcript; using it is what makes
    // hook + transcript collapse onto a single archived message.
    assert_eq!(event.message_id, "550e8400-e29b-41d4-a716-446655440000");
    assert_eq!(event.metadata["id_source"], "provider");
    assert_eq!(event.metadata["hook_event_name"], "UserPromptSubmit");
    assert!(!event.source_account_namespace.is_empty());
}

#[test]
fn claude_hook_session_start_captures_lifecycle_metadata() {
    let home = TestHome::new();
    let outcome = capture(
        AgentKind::ClaudeCode,
        &fixture("claude/hook_session_start.json"),
        &home,
    );
    assert_eq!(outcome.events.len(), 1);
    let event = &outcome.events[0];
    assert_eq!(event.event_type, "session_start");
    assert_eq!(event.metadata["source"], "resume");
    assert_eq!(event.metadata["model"], "claude-opus-5");
    assert_eq!(event.metadata["session_title"], "Fe stock preparation");
}

#[test]
fn claude_hook_stop_is_a_turn_boundary_not_a_session_end() {
    let home = TestHome::new();
    let outcome = capture(
        AgentKind::ClaudeCode,
        &fixture("claude/hook_stop.json"),
        &home,
    );
    assert_eq!(outcome.events.len(), 1);
    let event = &outcome.events[0];
    assert_eq!(event.event_type, EventType::TurnEnd.as_str());
    assert_ne!(
        event.event_type, "session_end",
        "Stop must never be treated as a real session end"
    );
    assert_eq!(event.metadata["stop_hook_active"], true);
}

#[test]
fn claude_hook_session_end_carries_the_reason() {
    let home = TestHome::new();
    let outcome = capture(
        AgentKind::ClaudeCode,
        &fixture("claude/hook_session_end.json"),
        &home,
    );
    assert_eq!(outcome.events.len(), 1);
    assert_eq!(outcome.events[0].event_type, "session_end");
    assert_eq!(outcome.events[0].metadata["reason"], "prompt_input_exit");
}

#[test]
fn claude_hook_tool_events_are_opt_in() {
    let home = TestHome::new();
    let payload = fixture("claude/hook_post_tool_use.json");
    let mut config = home.config();
    config.tool_events = false;
    let off = adapters::capture(AgentKind::ClaudeCode, &payload, &config).expect("capture");
    assert!(off.events.is_empty());
    assert!(!off.notes.is_empty());

    config.tool_events = true;
    let on = adapters::capture(AgentKind::ClaudeCode, &payload, &config).expect("capture");
    assert_eq!(on.events.len(), 1);
    assert_eq!(on.events[0].event_type, "tool_result");
    assert_eq!(on.events[0].role, "tool");
    assert_eq!(on.events[0].metadata["tool_name"], "Bash");
    assert_eq!(on.events[0].metadata["call_id"], "toolu_01ABC123");
}

#[test]
fn claude_hook_unknown_event_is_reported_not_guessed() {
    let home = TestHome::new();
    let outcome = capture(
        AgentKind::ClaudeCode,
        r#"{"session_id":"s","hook_event_name":"Notification"}"#,
        &home,
    );
    assert!(outcome.events.is_empty());
    assert!(outcome.notes[0].contains("Notification"));
}

// ---------------------------------------------------------------------------
// Claude Code: transcript (reconciliation)
// ---------------------------------------------------------------------------

#[test]
fn claude_transcript_parses_conversation_and_drops_tool_records() {
    let home = TestHome::new();
    let path = copy_fixture(&home, "claude/transcript_sample.jsonl", "transcript.jsonl");
    let config = home.config();
    let (events, offset) = claude_code::parse_transcript(&path, 0, &config).expect("parse");
    assert!(offset > 0);
    assert_eq!(events.len(), 2, "{events:#?}");

    let user = &events[0];
    assert_eq!(user.event_type, "user_prompt");
    assert_eq!(user.content, "之前 Fe 的硝酸溶液怎么配的？");
    // Anchored on promptId so it matches the realtime hook event.
    assert_eq!(user.message_id, "550e8400-e29b-41d4-a716-446655440000");
    assert_eq!(user.metadata["id_source"], "provider");

    let assistant = &events[1];
    assert_eq!(assistant.event_type, "assistant_message");
    assert_eq!(
        assistant.content,
        "Fe3+ 储备液为 10 mM，介质为 0.1 M HNO3。"
    );
    assert_eq!(assistant.message_id, "bbbbbbbb-1111-2222-3333-444444444444");
    assert_eq!(assistant.metadata["git_branch"], "main");
    assert_eq!(assistant.metadata["agent_version"], "2.1.196");

    // tool_result user records and isMeta records never become transcript.
    assert!(!events
        .iter()
        .any(|event| event.content.contains("local-command-caveat")));
    assert!(!events
        .iter()
        .any(|event| event.content.contains("10 mM Fe(NO3)3 in")));
}

#[test]
fn claude_transcript_hook_and_transcript_agree_on_user_prompt_identity() {
    let home = TestHome::new();
    let hook = capture(
        AgentKind::ClaudeCode,
        &fixture("claude/hook_user_prompt_submit.json"),
        &home,
    );
    let path = copy_fixture(&home, "claude/transcript_sample.jsonl", "transcript.jsonl");
    let (mut transcript, _) =
        claude_code::parse_transcript(&path, 0, &home.config()).expect("parse");
    // `parse_transcript` is the raw parser; the watcher fills ids afterwards.
    research_memory_bridge::watcher::ensure_event_ids(&mut transcript);
    let transcript_user = transcript
        .iter()
        .find(|event| event.event_type == "user_prompt")
        .expect("user event");
    assert_eq!(
        hook.events[0].event_id, transcript_user.event_id,
        "hook and transcript must derive the same id for the same user prompt"
    );
}

#[test]
fn claude_transcript_is_incremental() {
    let home = TestHome::new();
    let path = copy_fixture(&home, "claude/transcript_sample.jsonl", "transcript.jsonl");
    let config = home.config();
    let (_, offset) = claude_code::parse_transcript(&path, 0, &config).expect("parse");
    let (again, offset2) = claude_code::parse_transcript(&path, offset, &config).expect("parse");
    assert!(again.is_empty());
    assert_eq!(offset, offset2);
}

// ---------------------------------------------------------------------------
// Generic adapter
// ---------------------------------------------------------------------------

#[test]
fn generic_batch_maps_documented_shape() {
    let home = TestHome::new();
    let outcome = capture(AgentKind::Generic, &fixture("generic/batch.json"), &home);
    assert_eq!(outcome.events.len(), 2, "{:?}", outcome.notes);
    assert_eq!(outcome.events[0].source_system, "cursor");
    assert_eq!(outcome.events[0].event_type, "user_prompt");
    assert_eq!(outcome.events[0].session_id, "cursor-session-1");
    assert_eq!(outcome.events[0].message_id, "cursor-msg-1");
    assert_eq!(outcome.events[1].event_type, "assistant_message");
    assert!(outcome
        .events
        .iter()
        .all(|event| !event.event_id.is_empty()));
}

#[test]
fn generic_rejects_unknown_event_types_without_coercing() {
    let home = TestHome::new();
    let payload = serde_json::json!({
        "source_system": "cursor",
        "session_id": "s",
        "events": [
            {"event_type": "something_proprietary", "role": "user", "content": "x"},
            {"event_type": "user_prompt", "role": "wizard", "content": "y"}
        ]
    })
    .to_string();
    let outcome = capture(AgentKind::Generic, &payload, &home);
    assert!(outcome.events.is_empty());
    assert_eq!(outcome.notes.len(), 2, "{:?}", outcome.notes);
}

#[test]
fn generic_accepts_explicit_event_id() {
    let home = TestHome::new();
    let payload = serde_json::json!({
        "source_system": "cursor",
        "session_id": "s",
        "event_id": "explicit-event-id-1234",
        "event_type": "user_prompt",
        "role": "user",
        "content": "hello"
    })
    .to_string();
    let outcome = capture(AgentKind::Generic, &payload, &home);
    assert_eq!(outcome.events.len(), 1);
    assert_eq!(outcome.events[0].event_id, "explicit-event-id-1234");
}

// ---------------------------------------------------------------------------
// Adapter registry
// ---------------------------------------------------------------------------

#[test]
fn agent_kind_parsing_is_forgiving_but_bounded() {
    assert_eq!(AgentKind::parse("codex"), Some(AgentKind::Codex));
    assert_eq!(AgentKind::parse("Codex"), Some(AgentKind::Codex));
    assert_eq!(AgentKind::parse("claude_code"), Some(AgentKind::ClaudeCode));
    assert_eq!(AgentKind::parse("claude-code"), Some(AgentKind::ClaudeCode));
    assert_eq!(AgentKind::parse("Claude"), Some(AgentKind::ClaudeCode));
    assert_eq!(AgentKind::parse("generic"), Some(AgentKind::Generic));
    assert_eq!(AgentKind::parse("chatgpt-web"), None);
    assert_eq!(AgentKind::Codex.source_system(), "codex");
    assert_eq!(AgentKind::ClaudeCode.source_system(), "claude-code");
}

#[test]
fn generic_agent_has_no_hook_configuration() {
    let home = TestHome::new();
    let config = home.config();
    let status = adapters::status(AgentKind::Generic, &config);
    assert!(!status.supported);
    assert!(status.config_path.is_none());
    let report =
        adapters::install_hooks(AgentKind::Generic, home.path(), false, false).expect("install");
    assert!(!report.changed);
    assert!(report.message.contains("no hook configuration"));
}
