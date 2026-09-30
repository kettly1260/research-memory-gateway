//! Deterministic identity derivation, and the cross-language contract.
//!
//! `schemas/event-id-contract-v1.json` is generated from the *Python* gateway
//! implementation and asserted here, and the Python suite asserts the same file
//! from the other direction.  If the two derivations ever drift, one of the two
//! suites fails -- which is the only way to keep hook-captured and
//! snapshot-captured copies of a message collapsing into one archive entry.

use research_memory_bridge::normalize::{
    account_namespace_hash, content_identity, derive_event_id, event_content_hash, session_key,
    EventIdInputs,
};

const CONTRACT: &str = include_str!("../../schemas/event-id-contract-v1.json");

fn contract() -> serde_json::Value {
    serde_json::from_str(CONTRACT).expect("contract fixture is valid JSON")
}

fn str_field(value: &serde_json::Value, key: &str) -> String {
    value
        .get(key)
        .and_then(|item| item.as_str())
        .unwrap_or("")
        .to_string()
}

#[test]
fn contract_fixture_matches_rust_derivation() {
    let contract = contract();
    let cases = contract["cases"].as_array().expect("cases array");
    assert!(!cases.is_empty(), "contract fixture must not be empty");
    for case in cases {
        let inputs = &case["inputs"];
        let content = str_field(inputs, "content");
        let source_system = str_field(inputs, "source_system");
        let namespace = str_field(inputs, "source_account_namespace");
        let conversation_id = str_field(inputs, "conversation_id");
        let thread_id = str_field(inputs, "thread_id");
        let branch_id = str_field(inputs, "branch_id");
        let message_id = str_field(inputs, "message_id");
        let turn_id = str_field(inputs, "turn_id");
        let event_type = str_field(inputs, "event_type");
        let schema_version = inputs
            .get("schema_version")
            .and_then(|item| item.as_i64())
            .unwrap_or(1);

        assert_eq!(
            content_identity(&content),
            str_field(case, "content_identity"),
            "content_identity mismatch for {content:?}"
        );

        let derived = derive_event_id(&EventIdInputs {
            schema_version,
            source_system: &source_system,
            source_account_namespace: &namespace,
            conversation_id: &conversation_id,
            thread_id: &thread_id,
            branch_id: &branch_id,
            message_id: &message_id,
            turn_id: &turn_id,
            event_type: &event_type,
            content: &content,
        });
        assert_eq!(
            derived,
            str_field(case, "event_id"),
            "event_id mismatch for {content:?}"
        );

        assert_eq!(
            event_content_hash(&event_type, "", &content),
            str_field(case, "event_content_hash"),
            "event_content_hash mismatch for {content:?}"
        );
    }
}

#[test]
fn contract_fixture_matches_session_keys() {
    let contract = contract();
    for case in contract["session_key_cases"].as_array().expect("cases") {
        let derived = session_key(
            &str_field(case, "source_system"),
            &str_field(case, "source_account_namespace"),
            &str_field(case, "session_id"),
            &str_field(case, "conversation_id"),
            &str_field(case, "thread_id"),
            &str_field(case, "branch_id"),
        );
        assert_eq!(derived, str_field(case, "session_key"));
    }
}

#[test]
fn contract_fixture_matches_account_namespace_hash() {
    let contract = contract();
    for case in contract["account_namespace_cases"]
        .as_array()
        .expect("cases")
    {
        assert_eq!(
            account_namespace_hash(&str_field(case, "source_system"), &str_field(case, "label")),
            str_field(case, "hash")
        );
    }
}

#[test]
fn event_id_is_stable_across_replays() {
    let inputs = EventIdInputs {
        schema_version: 1,
        source_system: "codex",
        source_account_namespace: "ns",
        conversation_id: "conv",
        thread_id: "",
        branch_id: "",
        message_id: "m-1",
        turn_id: "t-1",
        event_type: "user_prompt",
        content: "identical content",
    };
    let first = derive_event_id(&inputs);
    let second = derive_event_id(&inputs);
    assert_eq!(first, second);
    assert!(first.starts_with("rmb1_"));
    assert_eq!(first.len(), "rmb1_".len() + 64);
}

#[test]
fn event_id_changes_when_identity_changes() {
    let base = EventIdInputs {
        schema_version: 1,
        source_system: "codex",
        source_account_namespace: "",
        conversation_id: "conv",
        thread_id: "",
        branch_id: "",
        message_id: "m-1",
        turn_id: "",
        event_type: "user_prompt",
        content: "content",
    };
    let baseline = derive_event_id(&base);
    let mut other = base.clone();
    other.message_id = "m-2";
    assert_ne!(baseline, derive_event_id(&other));
    let mut other = base.clone();
    other.event_type = "assistant_message";
    assert_ne!(baseline, derive_event_id(&other));
    let mut other = base.clone();
    other.content = "content ";
    // Trailing whitespace is normalized away, so this must NOT change the id.
    assert_eq!(baseline, derive_event_id(&other));
    let mut other = base.clone();
    other.content = "different";
    assert_ne!(baseline, derive_event_id(&other));
    let mut other = base.clone();
    other.branch_id = "branch";
    assert_ne!(baseline, derive_event_id(&other));
}

#[test]
fn turn_id_is_the_documented_fallback_anchor() {
    let with_message = EventIdInputs {
        schema_version: 1,
        source_system: "codex",
        source_account_namespace: "",
        conversation_id: "conv",
        thread_id: "",
        branch_id: "",
        message_id: "m-1",
        turn_id: "t-1",
        event_type: "user_prompt",
        content: "content",
    };
    let mut turn_only = with_message.clone();
    turn_only.message_id = "";
    // The two anchors differ, so the ids differ -- but the turn-only id is still
    // deterministic and content-scoped.
    assert_ne!(derive_event_id(&with_message), derive_event_id(&turn_only));
    assert_eq!(derive_event_id(&turn_only), derive_event_id(&turn_only));
}

#[test]
fn content_identity_ignores_insignificant_whitespace_only() {
    assert_eq!(
        content_identity("line one  \r\nline two"),
        content_identity("line one\nline two")
    );
    assert_ne!(content_identity("10 mM"), content_identity("10  mM"));
}
