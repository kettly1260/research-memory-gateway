//! Generic adapter: any agent that can emit a documented JSON shape.
//!
//! This is the extension point for Cursor, OpenCode, Kilo, Aider, a bespoke
//! wrapper script, or a `curl`-in-a-hook setup.  It performs **no** guessing:
//! the caller supplies already-normalized event types and roles, and anything
//! outside the canonical vocabulary is rejected with a note rather than
//! coerced.
//!
//! ## Accepted payload
//!
//! A single event::
//!
//! ```json
//! {"source_system":"cursor","session_id":"s-1","conversation_id":"s-1",
//!  "event_type":"user_prompt","role":"user","content":"...",
//!  "message_id":"m-1","timestamp":"2026-09-30T10:00:00Z","metadata":{}}
//! ```
//!
//! or a batch::
//!
//! ```json
//! {"source_system":"cursor","session_id":"s-1","events":[ { ... }, { ... } ]}
//! ```
//!
//! Top-level `session_id` / `conversation_id` / `source_system` /
//! `source_account_namespace` are inherited by every event, and per-event values
//! win.  `event_id` may be supplied explicitly; when omitted it is derived with
//! the same algorithm as the gateway, so replays stay idempotent.

use anyhow::Result;
use serde_json::{Map, Value};

use super::{AgentKind, CaptureOutcome};
use crate::config::BridgeConfig;
use crate::event::{EventType, NormalizedEvent, Role};
use crate::normalize::{derive_event_id, EventIdInputs};

fn get_str<'a>(map: &'a Map<String, Value>, keys: &[&str]) -> Option<&'a str> {
    for key in keys {
        if let Some(Value::String(value)) = map.get(*key) {
            return Some(value.as_str());
        }
    }
    None
}

pub fn capture(payload: &str, config: &BridgeConfig) -> Result<CaptureOutcome> {
    let trimmed = payload.trim();
    if trimmed.is_empty() {
        return Ok(CaptureOutcome::empty().with_note("empty generic payload"));
    }
    let value: Value = match serde_json::from_str(trimmed) {
        Ok(value) => value,
        Err(err) => {
            return Ok(CaptureOutcome::empty().with_note(format!("payload is not JSON: {err}")));
        }
    };
    let Some(root) = value.as_object() else {
        return Ok(CaptureOutcome::empty().with_note("payload is not an object"));
    };

    let source_system = get_str(root, &["source_system", "source"])
        .unwrap_or("generic")
        .to_string();
    let session_id = get_str(root, &["session_id", "session"]).unwrap_or("");
    let conversation_id = get_str(root, &["conversation_id"]).unwrap_or(session_id);
    let thread_id = get_str(root, &["thread_id"]).unwrap_or("");
    let branch_id = get_str(root, &["branch_id"]).unwrap_or("");
    let namespace = get_str(root, &["source_account_namespace"]).unwrap_or("");
    let default_timestamp = get_str(root, &["timestamp"]).unwrap_or("");

    let items: Vec<&Map<String, Value>> = match root.get("events") {
        Some(Value::Array(events)) => events.iter().filter_map(Value::as_object).collect(),
        _ => vec![root],
    };

    let mut outcome = CaptureOutcome::empty();
    for item in items {
        let event_type_raw = get_str(item, &["event_type", "type"]).unwrap_or("");
        let Some(event_type) = EventType::parse(event_type_raw) else {
            outcome.notes.push(format!(
                "generic event skipped: unsupported event_type {event_type_raw:?}"
            ));
            continue;
        };
        let role_raw = get_str(item, &["role"]).unwrap_or("");
        let role = if role_raw.is_empty() {
            Role::from_event_type(event_type)
        } else {
            match Role::parse(role_raw) {
                Some(role) => role,
                None => {
                    outcome.notes.push(format!(
                        "generic event skipped: unsupported role {role_raw:?}"
                    ));
                    continue;
                }
            }
        };
        let content = get_str(item, &["content", "text"])
            .unwrap_or("")
            .to_string();
        if event_type.is_transcript() && content.trim().is_empty() {
            outcome
                .notes
                .push(format!("generic {event_type} event skipped: empty content"));
            continue;
        }
        let item_session = get_str(item, &["session_id"]).unwrap_or(session_id);
        let item_conversation = get_str(item, &["conversation_id"]).unwrap_or(conversation_id);
        let item_thread = get_str(item, &["thread_id"]).unwrap_or(thread_id);
        let item_branch = get_str(item, &["branch_id"]).unwrap_or(branch_id);
        let item_namespace = get_str(item, &["source_account_namespace"]).unwrap_or(namespace);
        let message_id = get_str(item, &["message_id"]).unwrap_or("");
        let turn_id = get_str(item, &["turn_id"]).unwrap_or("");
        let timestamp = get_str(item, &["timestamp"]).unwrap_or(default_timestamp);

        let mut event = NormalizedEvent::new(source_system.clone(), event_type, content.clone())
            .with_role(role)
            .with_account_namespace(item_namespace.to_string())
            .with_session(item_session.to_string())
            .with_conversation(item_conversation.to_string())
            .with_thread(item_thread.to_string())
            .with_branch(item_branch.to_string())
            .with_message(message_id.to_string())
            .with_turn(turn_id.to_string())
            .with_timestamp(timestamp.to_string())
            .with_metadata_entry("adapter", Value::String("generic".to_string()));
        if let Some(metadata) = item.get("metadata").and_then(Value::as_object) {
            event = event.with_metadata(Value::Object(metadata.clone()));
        }
        if !config.project_label.trim().is_empty() {
            event = event.with_metadata_entry(
                "projects",
                Value::Array(vec![Value::String(config.project_label.trim().to_string())]),
            );
        }
        event.event_id = get_str(item, &["event_id"])
            .map(|text| text.to_string())
            .filter(|text| text.len() >= 8)
            .unwrap_or_else(|| {
                derive_event_id(&EventIdInputs {
                    schema_version: crate::event::SCHEMA_VERSION,
                    source_system: &event.source_system,
                    source_account_namespace: &event.source_account_namespace,
                    conversation_id: &event.conversation_id,
                    thread_id: &event.thread_id,
                    branch_id: &event.branch_id,
                    message_id: &event.message_id,
                    turn_id: &event.turn_id,
                    event_type: &event.event_type,
                    content: &event.content,
                })
            });
        if let Err(reason) = event.validate() {
            outcome
                .notes
                .push(format!("generic event skipped: {reason}"));
            continue;
        }
        outcome.events.push(event);
    }

    if outcome.events.is_empty() && outcome.notes.is_empty() {
        outcome
            .notes
            .push("generic payload produced no events".to_string());
    }
    Ok(outcome)
}

/// Canonical agent kind for documentation and `status` output.
pub fn agent() -> AgentKind {
    AgentKind::Generic
}
