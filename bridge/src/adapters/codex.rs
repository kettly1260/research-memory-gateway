//! Codex adapter.
//!
//! ## Realtime path: `notify`
//!
//! Codex runs the command configured in `notify` (`~/.codex/config.toml`) when it
//! emits `agent-turn-complete` -- currently its only supported lifecycle event.
//! The payload is **appended as the final CLI argument**, not written to stdin:
//!
//! ```json
//! {"type":"agent-turn-complete",
//!  "thread-id":"...",
//!  "last-assistant-message":"...",
//!  "input-messages":["..."]}
//! ```
//!
//! Codex exposes no `SessionStart`, `SessionEnd` or tool lifecycle through
//! `notify`, so a turn completion is mapped to the turn's messages and is
//! explicitly **not** treated as a session end.  `finalize-session` is the
//! documented fallback for hosts where no SessionEnd signal exists.
//!
//! ## Reconciliation path: session rollouts
//!
//! `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl` is the durable
//! transcript.  Its record shape is the one already validated by this
//! repository's `CodexExportReader` (`session_meta`, `response_item`,
//! `event_msg`, `turn_context`), so the watcher reuses that mapping rather than
//! inventing a second interpretation.
//!
//! ## Identity across both paths
//!
//! `notify` carries no message ids, so a hook-captured message is anchored on
//! its content while a rollout-captured message is anchored on its provider id.
//! The gateway therefore treats an *unidentified* block as superseded by an
//! *identified* block with the same role and content, which is what keeps
//! hook + transcript from archiving the same message twice.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde_json::{Map, Value};

use super::{
    backup_file, count_transcript_files, AdapterStatus, AgentKind, CaptureOutcome,
    HookInstallReport,
};
use crate::config::BridgeConfig;
use crate::event::{EventType, NormalizedEvent, Role};

pub const SOURCE_SYSTEM: &str = "codex";

/// User-message prefixes Codex injects itself; they are packaging noise, not
/// conversation, and the archive must not treat them as user speech.  Kept
/// identical to `conversations/codex_export.py`.
const INJECTION_PREFIXES: &[&str] = &[
    "<recommended_plugins>",
    "<app-context>",
    "<environment_context>",
    "<skills_instructions>",
    "<turn_aborted>",
    "# AGENTS.md instructions for",
    "Another language model started to solve this problem",
    "The following is the Codex agent history whose request action you are assessing.",
];

const TEXT_CONTENT_TYPES: &[&str] = &["input_text", "output_text", "text"];
const TOOL_CALL_TYPES: &[&str] = &["function_call", "custom_tool_call"];
const TOOL_OUTPUT_TYPES: &[&str] = &["function_call_output", "custom_tool_call_output"];

/// Bytes of the file head re-scanned to recover `session_meta` on an incremental
/// pass.  The record is the first line of a rollout, so this is very generous.
pub const HEAD_SCAN_BYTES: i64 = 64 * 1024;

/// Session identity carried by a rollout's `session_meta` record.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SessionMeta {
    pub thread_id: String,
    pub cwd: String,
    pub version: String,
    pub model_provider: String,
}

impl SessionMeta {
    fn from_payload(payload: &Map<String, Value>) -> SessionMeta {
        SessionMeta {
            thread_id: payload
                .get("id")
                .or_else(|| payload.get("session_id"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            cwd: payload
                .get("cwd")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            version: payload
                .get("cli_version")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            model_provider: payload
                .get("model_provider")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        }
    }
}

/// Locate the `session_meta` record in a byte slice, considering only complete
/// lines so a partially written file cannot yield a half-parsed identity.
pub fn scan_session_meta(bytes: &[u8]) -> Option<SessionMeta> {
    let consumed = match bytes.iter().rposition(|byte| *byte == b'\n') {
        Some(index) => index + 1,
        None => 0,
    };
    let text = String::from_utf8_lossy(&bytes[..consumed]);
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(record) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if record.get("type").and_then(Value::as_str) != Some("session_meta") {
            continue;
        }
        let payload = record.get("payload").and_then(Value::as_object)?;
        return Some(SessionMeta::from_payload(payload));
    }
    None
}

/// Namespace label used for this machine's Codex account, hashed before it is
/// ever sent.  Matches the gateway's legacy Codex library namespace.
pub const DEFAULT_NAMESPACE_LABEL: &str = "legacy-default-v1";

pub fn namespace_hash() -> String {
    crate::normalize::account_namespace_hash(SOURCE_SYSTEM, DEFAULT_NAMESPACE_LABEL)
}

// ---------------------------------------------------------------------------
// Realtime notify payload
// ---------------------------------------------------------------------------

fn get_str<'a>(map: &'a Map<String, Value>, keys: &[&str]) -> Option<&'a str> {
    for key in keys {
        if let Some(Value::String(value)) = map.get(*key) {
            return Some(value.as_str());
        }
    }
    None
}

fn get_array<'a>(map: &'a Map<String, Value>, keys: &[&str]) -> Option<&'a Vec<Value>> {
    for key in keys {
        if let Some(Value::Array(value)) = map.get(*key) {
            return Some(value);
        }
    }
    None
}

/// Parse a Codex `notify` payload into normalized events.
pub fn capture(payload: &str, config: &BridgeConfig) -> Result<CaptureOutcome> {
    let trimmed = payload.trim();
    if trimmed.is_empty() {
        return Ok(CaptureOutcome::empty().with_note("empty codex notify payload"));
    }
    let value: Value = match serde_json::from_str(trimmed) {
        Ok(value) => value,
        Err(err) => {
            return Ok(CaptureOutcome::empty()
                .with_note(format!("codex notify payload is not JSON: {err}")));
        }
    };
    // Some Codex builds wrap the payload in an envelope.
    let map = match value.get("payload").and_then(Value::as_object) {
        Some(inner) if value.get("type").is_none() => inner,
        _ => match value.as_object() {
            Some(map) => map,
            None => {
                return Ok(
                    CaptureOutcome::empty().with_note("codex notify payload is not an object")
                )
            }
        },
    };

    let kind = get_str(map, &["type", "event", "hook_event_name"]).unwrap_or("");
    if !kind.eq_ignore_ascii_case("agent-turn-complete") {
        return Ok(CaptureOutcome::empty()
            .with_note(format!("unsupported codex notify event type: {kind:?}")));
    }

    let thread = get_str(
        map,
        &["thread-id", "thread_id", "session_id", "conversation_id"],
    )
    .unwrap_or("")
    .to_string();
    if thread.is_empty() {
        return Ok(CaptureOutcome::empty()
            .with_note("codex notify payload has no thread-id; cannot anchor the conversation"));
    }

    let inputs: Vec<String> = get_array(map, &["input-messages", "input_messages"])
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(|text| text.to_string()))
                .collect()
        })
        .unwrap_or_default();
    let assistant = get_str(map, &["last-assistant-message", "last_assistant_message"])
        .unwrap_or("")
        .to_string();
    let cwd = get_str(map, &["cwd", "working_directory"]).unwrap_or("");
    let model = get_str(map, &["model", "model_name"]).unwrap_or("");
    let version = get_str(map, &["cli_version", "version"]).unwrap_or("");

    // Turn anchor: identical turns are indistinguishable and are meant to
    // collapse, different turns never do.
    let turn_anchor = crate::normalize::sha256_hex(
        &[thread.clone(), inputs.join("\u{1f}"), assistant.clone()].join("\0"),
    );
    let turn_anchor = turn_anchor[..16].to_string();
    let timestamp = crate::normalize::now_rfc3339();

    let mut outcome = CaptureOutcome::empty();
    let mut index = 0usize;
    for text in &inputs {
        let trimmed_text = text.trim();
        if trimmed_text.is_empty() || is_injected_user_message(trimmed_text) {
            index += 1;
            continue;
        }
        let mut event = NormalizedEvent::new(SOURCE_SYSTEM, EventType::UserPrompt, trimmed_text)
            .with_account_namespace(namespace_hash())
            .with_session(thread.clone())
            .with_conversation(thread.clone())
            .with_thread(thread.clone())
            .with_turn(format!("notify-{turn_anchor}"))
            .with_message(format!("{thread}#input{index}"))
            .with_timestamp(timestamp.clone())
            .with_metadata_entry("adapter", Value::String("codex-notify".to_string()))
            .with_metadata_entry("agent", Value::String("codex".to_string()))
            // Codex's notify payload carries no message ids, so the gateway may
            // let a provider-identified transcript copy supersede this block.
            .with_metadata_entry("id_source", Value::String("synthesized".to_string()));
        if !cwd.is_empty() {
            event = event.with_metadata_entry("cwd", Value::String(cwd.to_string()));
        }
        if !version.is_empty() {
            event = event.with_metadata_entry("agent_version", Value::String(version.to_string()));
        }
        if !model.is_empty() {
            event = event.with_metadata_entry("model_name", Value::String(model.to_string()));
        }
        if !config.project_label.trim().is_empty() {
            event = event.with_metadata_entry(
                "projects",
                Value::Array(vec![Value::String(config.project_label.trim().to_string())]),
            );
        }
        outcome.events.push(event);
        index += 1;
    }

    if !assistant.trim().is_empty() {
        let mut event =
            NormalizedEvent::new(SOURCE_SYSTEM, EventType::AssistantMessage, assistant.trim())
                .with_account_namespace(namespace_hash())
                .with_session(thread.clone())
                .with_conversation(thread.clone())
                .with_thread(thread.clone())
                .with_turn(format!("notify-{turn_anchor}"))
                .with_message(format!("{thread}#assistant#{turn_anchor}"))
                .with_timestamp(timestamp)
                .with_metadata_entry("adapter", Value::String("codex-notify".to_string()))
                .with_metadata_entry("id_source", Value::String("synthesized".to_string()))
                .with_metadata_entry("notify_summary", Value::Bool(true));
        if !cwd.is_empty() {
            event = event.with_metadata_entry("cwd", Value::String(cwd.to_string()));
        }
        if !config.project_label.trim().is_empty() {
            event = event.with_metadata_entry(
                "projects",
                Value::Array(vec![Value::String(config.project_label.trim().to_string())]),
            );
        }
        outcome.events.push(event);
    }

    if outcome.events.is_empty() {
        outcome
            .notes
            .push("codex notify payload produced no capturable events".to_string());
    }
    Ok(outcome)
}

fn is_injected_user_message(text: &str) -> bool {
    let stripped = text.trim_start();
    INJECTION_PREFIXES
        .iter()
        .any(|prefix| stripped.starts_with(prefix))
}

// ---------------------------------------------------------------------------
// Transcript reconciliation
// ---------------------------------------------------------------------------

/// Parse the *new* portion of one Codex rollout file.
///
/// `offset` is a byte offset previously stored in the spool's `cursors` table;
/// only complete lines after it are consumed, and the returned offset always
/// lands on a line boundary so a partially written trailing line is re-read
/// rather than lost.
///
/// The session identity lives in the file's *first* record (`session_meta`), so
/// an incremental pass that starts after the cursor would otherwise have to
/// guess the thread id.  Guessing would change the derived `event_id` for every
/// later message and re-archive the whole conversation, so the head of the file
/// is always re-read to recover the identity.  The `session_start` event itself
/// is only emitted when the record is actually consumed (`start == 0`).
pub fn parse_rollout(
    path: &Path,
    offset: i64,
    config: &BridgeConfig,
) -> Result<(Vec<NormalizedEvent>, i64)> {
    use std::io::{Read, Seek, SeekFrom};

    let mut file = std::fs::File::open(path)
        .with_context(|| format!("cannot open rollout {}", path.display()))?;
    let length = file.metadata()?.len() as i64;
    let start = offset.max(0).min(length);

    // Recover the session identity from the head of the file (see doc comment).
    let mut head = vec![0u8; start.min(HEAD_SCAN_BYTES) as usize];
    file.seek(SeekFrom::Start(0))?;
    let read = file.read(&mut head)?;
    head.truncate(read);
    let mut thread_id = String::new();
    if let Some(meta) = scan_session_meta(&head) {
        thread_id = meta.thread_id;
    }

    file.seek(SeekFrom::Start(start as u64))?;
    let mut buffer = Vec::new();
    file.read_to_end(&mut buffer)?;

    let consumed = match buffer.iter().rposition(|byte| *byte == b'\n') {
        Some(index) => index + 1,
        None => 0,
    };
    let chunk = &buffer[..consumed];
    let text = String::from_utf8_lossy(chunk);

    let mut events = Vec::new();

    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let record: Value = match serde_json::from_str(line) {
            Ok(value) => value,
            Err(_) => continue,
        };
        let outer_type = record.get("type").and_then(Value::as_str).unwrap_or("");
        let timestamp = record
            .get("timestamp")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let Some(payload) = record.get("payload").and_then(Value::as_object) else {
            continue;
        };
        let payload_type = payload
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();

        match outer_type {
            "session_meta" => {
                let meta = SessionMeta::from_payload(payload);
                if !meta.thread_id.is_empty() {
                    thread_id = meta.thread_id.clone();
                }
                if start != 0 {
                    // Already consumed in an earlier pass; re-emitting would only
                    // produce a duplicate row.
                    continue;
                }
                let cwd = meta.cwd;
                let version = meta.version;
                let model_provider = meta.model_provider;
                let mut event = NormalizedEvent::new(SOURCE_SYSTEM, EventType::SessionStart, "")
                    .with_account_namespace(namespace_hash())
                    .with_session(thread_id.clone())
                    .with_conversation(thread_id.clone())
                    .with_thread(thread_id.clone())
                    .with_timestamp(timestamp)
                    .with_metadata_entry("adapter", Value::String("codex-rollout".to_string()))
                    .with_metadata_entry(
                        "transcript_path",
                        Value::String(path.display().to_string()),
                    );
                if !cwd.is_empty() {
                    event = event.with_metadata_entry("cwd", Value::String(cwd.clone()));
                }
                if !version.is_empty() {
                    event =
                        event.with_metadata_entry("agent_version", Value::String(version.clone()));
                }
                if !model_provider.is_empty() {
                    event = event.with_metadata_entry(
                        "model_provider",
                        Value::String(model_provider.clone()),
                    );
                }
                if !config.project_label.trim().is_empty() {
                    event = event.with_metadata_entry(
                        "projects",
                        Value::Array(vec![Value::String(config.project_label.trim().to_string())]),
                    );
                }
                events.push(event);
            }
            "response_item" => {
                if thread_id.is_empty() {
                    thread_id = session_id_from_filename(path);
                }
                if payload_type == "message" {
                    if let Some(event) =
                        message_event(payload, &timestamp, &thread_id, path, config)
                    {
                        events.push(event);
                    }
                } else if (TOOL_CALL_TYPES.contains(&payload_type.as_str())
                    || TOOL_OUTPUT_TYPES.contains(&payload_type.as_str()))
                    && config.tool_events
                {
                    events.push(tool_event(
                        payload,
                        &payload_type,
                        &timestamp,
                        &thread_id,
                        path,
                    ));
                }
            }
            _ => {}
        }
    }

    Ok((events, start + consumed as i64))
}

fn session_id_from_filename(path: &Path) -> String {
    let stem = path
        .file_stem()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();
    // rollout-2026-04-20T19-59-09-019daac2-3bb1-7201-86c3-8707bf242cd9
    match stem.rsplit_once('-') {
        Some(_) => {
            let parts: Vec<&str> = stem.split('-').collect();
            if parts.len() >= 11 {
                parts[parts.len() - 10..].join("-")
            } else {
                stem
            }
        }
        None => stem,
    }
}

fn message_event(
    payload: &Map<String, Value>,
    timestamp: &str,
    thread_id: &str,
    path: &Path,
    config: &BridgeConfig,
) -> Option<NormalizedEvent> {
    let role = payload.get("role").and_then(Value::as_str).unwrap_or("");
    if role == "developer" {
        return None;
    }
    let mut texts: Vec<String> = Vec::new();
    if let Some(Value::Array(content)) = payload.get("content") {
        for item in content {
            let Some(block) = item.as_object() else {
                continue;
            };
            let block_type = block.get("type").and_then(Value::as_str).unwrap_or("");
            if !TEXT_CONTENT_TYPES.contains(&block_type) {
                continue;
            }
            if let Some(text) = block.get("text").and_then(Value::as_str) {
                if !text.trim().is_empty() {
                    texts.push(text.trim_end().to_string());
                }
            }
        }
    }
    if texts.is_empty() {
        return None;
    }
    let content = texts.join("\n\n");
    let event_type = match role {
        "user" => {
            if is_injected_user_message(&content) {
                return None;
            }
            EventType::UserPrompt
        }
        "assistant" => EventType::AssistantMessage,
        _ => EventType::SystemMessage,
    };
    let message_id = payload
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let turn_id = payload
        .get("internal_chat_message_metadata_passthrough")
        .and_then(Value::as_object)
        .and_then(|meta| meta.get("turn_id"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let phase = payload
        .get("phase")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    // Computed before `message_id` is moved into the builder below.
    let id_source = if message_id.is_empty() {
        "content"
    } else {
        "provider"
    };

    let mut event = NormalizedEvent::new(SOURCE_SYSTEM, event_type, content)
        .with_account_namespace(namespace_hash())
        .with_session(thread_id.to_string())
        .with_conversation(thread_id.to_string())
        .with_thread(thread_id.to_string())
        .with_message(message_id)
        .with_turn(turn_id)
        .with_timestamp(timestamp.to_string())
        .with_metadata_entry("adapter", Value::String("codex-rollout".to_string()))
        .with_metadata_entry("id_source", Value::String(id_source.to_string()))
        .with_metadata_entry("transcript_path", Value::String(path.display().to_string()));
    if !phase.is_empty() {
        event = event.with_metadata_entry("phase", Value::String(phase));
    }
    if let Some(cwd) = payload.get("cwd").and_then(Value::as_str) {
        event = event.with_metadata_entry("cwd", Value::String(cwd.to_string()));
    }
    if !config.project_label.trim().is_empty() {
        event = event.with_metadata_entry(
            "projects",
            Value::Array(vec![Value::String(config.project_label.trim().to_string())]),
        );
    }
    Some(event)
}

fn tool_event(
    payload: &Map<String, Value>,
    payload_type: &str,
    timestamp: &str,
    thread_id: &str,
    path: &Path,
) -> NormalizedEvent {
    let is_call = TOOL_CALL_TYPES.contains(&payload_type);
    let event_type = if is_call {
        EventType::ToolCall
    } else {
        EventType::ToolResult
    };
    let raw = if is_call {
        payload
            .get("arguments")
            .or_else(|| payload.get("input"))
            .cloned()
            .unwrap_or(Value::Null)
    } else {
        payload
            .get("output")
            .or_else(|| payload.get("content"))
            .cloned()
            .unwrap_or(Value::Null)
    };
    let content = match raw {
        Value::String(text) => text,
        Value::Null => String::new(),
        other => other.to_string(),
    };
    let name = payload
        .get("name")
        .or_else(|| payload.get("tool_name"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let call_id = payload
        .get("call_id")
        .or_else(|| payload.get("id"))
        .and_then(Value::as_str)
        .unwrap_or("");

    NormalizedEvent::new(SOURCE_SYSTEM, event_type, content)
        .with_account_namespace(namespace_hash())
        .with_session(thread_id.to_string())
        .with_conversation(thread_id.to_string())
        .with_thread(thread_id.to_string())
        .with_message(call_id.to_string())
        .with_timestamp(timestamp.to_string())
        .with_role(Role::Tool)
        .with_metadata_entry("adapter", Value::String("codex-rollout".to_string()))
        .with_metadata_entry("tool_name", Value::String(name.to_string()))
        .with_metadata_entry("call_id", Value::String(call_id.to_string()))
        .with_metadata_entry("tool_event_type", Value::String(payload_type.to_string()))
        .with_metadata_entry("transcript_path", Value::String(path.display().to_string()))
}

/// All rollout files under `root`, oldest first.
pub fn discover_rollouts(root: &Path) -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = Vec::new();
    if !root.is_dir() {
        return files;
    }
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path
                .file_name()
                .map(|name| {
                    let name = name.to_string_lossy();
                    name.starts_with("rollout-") && name.ends_with(".jsonl")
                })
                .unwrap_or(false)
            {
                files.push(path);
            }
        }
    }
    files.sort();
    files
}

// ---------------------------------------------------------------------------
// Hook installation
// ---------------------------------------------------------------------------

fn bridge_command(agent: AgentKind) -> Result<Vec<String>> {
    let exe = super::bridge_executable()?;
    // Codex appends the notify JSON as the final argument, so the marker must
    // sit before it.
    Ok(vec![
        exe.display().to_string(),
        "capture".to_string(),
        "--agent".to_string(),
        agent.as_str().to_string(),
        super::MANAGED_FLAG.to_string(),
    ])
}

/// True when a `notify` array was installed by this bridge.
pub fn notify_is_ours(existing: &[String]) -> bool {
    existing.iter().any(|item| item == super::MANAGED_FLAG)
}

pub fn status(config: &BridgeConfig) -> AdapterStatus {
    let config_path = AgentKind::Codex.config_path();
    let transcript_root = AgentKind::Codex.transcript_root(config);
    let mut installed = false;
    let mut config_exists = false;
    let mut command = String::new();
    if let Some(path) = &config_path {
        config_exists = path.exists();
        if let Ok(text) = std::fs::read_to_string(path) {
            if let Some(existing) = read_root_array(&text, "notify") {
                installed = notify_is_ours(&existing);
                command = existing.join(" ");
            }
        }
    }
    let transcript_files = transcript_root
        .as_deref()
        .map(|root| {
            count_transcript_files(root, &|name| {
                name.starts_with("rollout-") && name.ends_with(".jsonl")
            })
        })
        .unwrap_or(0);
    AdapterStatus {
        agent: AgentKind::Codex,
        supported: true,
        installed,
        config_path,
        config_exists,
        command,
        transcript_root,
        transcript_files,
    }
}

pub fn install_hooks(root: &Path, force: bool) -> Result<HookInstallReport> {
    let Some(path) = AgentKind::Codex.config_path() else {
        anyhow::bail!("cannot resolve ~/.codex/config.toml on this platform");
    };
    install_hooks_at(&path, root, force)
}

/// Install into an explicit config path (used by tests).
pub fn install_hooks_at(path: &Path, root: &Path, force: bool) -> Result<HookInstallReport> {
    let path = path.to_path_buf();
    let existing_text = if path.exists() {
        std::fs::read_to_string(&path)?
    } else {
        String::new()
    };
    let existing = read_root_array(&existing_text, "notify").unwrap_or_default();
    let ours = bridge_command(AgentKind::Codex)?;

    if !existing.is_empty() && !notify_is_ours(&existing) && !force {
        return Ok(HookInstallReport {
            agent: AgentKind::Codex,
            config_path: Some(path),
            changed: false,
            backup_path: None,
            message: format!(
                "Codex already has a notify command ({}). Codex supports only ONE notify command, \
                 so installing would replace it. Re-run with --force to back it up and replace it; \
                 `uninstall-hooks` restores the original.",
                existing.join(" ")
            ),
        });
    }
    if notify_is_ours(&existing) {
        return Ok(HookInstallReport {
            agent: AgentKind::Codex,
            config_path: Some(path),
            changed: false,
            backup_path: None,
            message: "Codex notify hook is already installed".to_string(),
        });
    }

    let backup_path = backup_file(root, &path)?;
    if backup_path.is_some() {
        let mut record = Map::new();
        record.insert(
            "agent".to_string(),
            Value::String(AgentKind::Codex.as_str().to_string()),
        );
        record.insert(
            "config_path".to_string(),
            Value::String(path.display().to_string()),
        );
        record.insert(
            "previous_notify".to_string(),
            Value::Array(existing.iter().cloned().map(Value::String).collect()),
        );
        record.insert(
            "had_notify_key".to_string(),
            Value::Bool(!existing.is_empty() || existing_text.contains("notify")),
        );
        let sidecar = crate::paths::ensure_backup_dir(root)?.join("codex-notify.json");
        super::write_json_object(&sidecar, &Value::Object(record))?;
    }

    let literal = toml_array_literal(&ours);
    let (updated, replaced) = upsert_root_array(&existing_text, "notify", &literal);
    let text = if replaced {
        updated
    } else {
        // No prior notify key.  It must be inserted at the END OF THE ROOT TABLE
        // (before the first `[table]` header), never at EOF: appending after a
        // table header would silently define `<table>.notify` instead.
        insert_root_key(&existing_text, "notify", &literal)
    };
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&path, text)?;

    Ok(HookInstallReport {
        agent: AgentKind::Codex,
        config_path: Some(path),
        changed: true,
        backup_path,
        message: format!("installed Codex notify hook: {}", ours.join(" ")),
    })
}

pub fn uninstall_hooks(root: &Path) -> Result<HookInstallReport> {
    let Some(path) = AgentKind::Codex.config_path() else {
        anyhow::bail!("cannot resolve ~/.codex/config.toml on this platform");
    };
    uninstall_hooks_at(&path, root)
}

/// Uninstall from an explicit config path (used by tests).
pub fn uninstall_hooks_at(path: &Path, root: &Path) -> Result<HookInstallReport> {
    let path = path.to_path_buf();
    if !path.exists() {
        return Ok(HookInstallReport {
            agent: AgentKind::Codex,
            config_path: Some(path),
            changed: false,
            backup_path: None,
            message: "no Codex config to update".to_string(),
        });
    }
    let text = std::fs::read_to_string(&path)?;
    let existing = read_root_array(&text, "notify").unwrap_or_default();
    if !notify_is_ours(&existing) {
        return Ok(HookInstallReport {
            agent: AgentKind::Codex,
            config_path: Some(path),
            changed: false,
            backup_path: None,
            message: "Codex notify hook is not ours; leaving it untouched".to_string(),
        });
    }

    let backup_path = backup_file(root, &path)?;
    let sidecar = crate::paths::ensure_backup_dir(root)?.join("codex-notify.json");
    let mut restored: Vec<String> = Vec::new();
    if let Ok(record) = super::read_json_object(&sidecar) {
        if let Some(Value::Array(items)) = record.get("previous_notify") {
            restored = items
                .iter()
                .filter_map(|item| item.as_str().map(|text| text.to_string()))
                .collect();
        }
    }

    let updated = if restored.is_empty() {
        remove_root_key(&text, "notify")
    } else {
        upsert_root_array(&text, "notify", &toml_array_literal(&restored)).0
    };
    std::fs::write(&path, updated)?;

    Ok(HookInstallReport {
        agent: AgentKind::Codex,
        config_path: Some(path),
        changed: true,
        backup_path,
        message: if restored.is_empty() {
            "removed Codex notify hook".to_string()
        } else {
            format!(
                "restored previous Codex notify hook: {}",
                restored.join(" ")
            )
        },
    })
}

/// TOML basic-string escaping for a literal.
fn toml_string_literal(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for ch in value.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            other => out.push(other),
        }
    }
    out.push('"');
    out
}

fn toml_array_literal(items: &[String]) -> String {
    let rendered: Vec<String> = items.iter().map(|item| toml_string_literal(item)).collect();
    format!("[{}]", rendered.join(", "))
}

/// Read a top-level array key without a full TOML round-trip.
///
/// A `toml::Value` round-trip would strip every comment from a user's
/// `config.toml`; the hook installer must be surgical instead.
pub fn read_root_array(text: &str, key: &str) -> Option<Vec<String>> {
    let (value, _, _) = locate_root_key(text, key)?;
    parse_toml_string_array(&value)
}

fn parse_toml_string_array(value: &str) -> Option<Vec<String>> {
    let trimmed = value.trim();
    let inner = trimmed.strip_prefix('[')?.strip_suffix(']')?;
    let mut items = Vec::new();
    let mut current = String::new();
    let mut in_string = false;
    let mut escape = false;
    for ch in inner.chars() {
        if escape {
            current.push(match ch {
                'n' => '\n',
                'r' => '\r',
                't' => '\t',
                other => other,
            });
            escape = false;
            continue;
        }
        match ch {
            '\\' if in_string => escape = true,
            '"' => {
                in_string = !in_string;
                if !in_string {
                    items.push(current.clone());
                    current.clear();
                }
            }
            _ if in_string => current.push(ch),
            _ => {}
        }
    }
    Some(items)
}

/// Locate a root-level `key = value` assignment.
///
/// Returns `(value_text, start_index, end_index)` as byte offsets into `text`.
fn locate_root_key(text: &str, key: &str) -> Option<(String, usize, usize)> {
    let bytes = text.as_bytes();
    let mut index = 0usize;
    let mut in_table = false;
    while index < bytes.len() {
        let line_start = index;
        let line_end = text[index..]
            .find('\n')
            .map(|offset| index + offset)
            .unwrap_or(bytes.len());
        let line = &text[line_start..line_end];
        let trimmed = line.trim_start();
        if trimmed.starts_with('[') {
            in_table = true;
        } else if !in_table && !trimmed.starts_with('#') {
            if let Some(rest) = trimmed.strip_prefix(key) {
                let rest = rest.trim_start();
                if let Some(after_eq) = rest.strip_prefix('=') {
                    let value_start = line_start + (line.len() - after_eq.len());
                    let (value_end, block_end) = scan_toml_value(text, value_start);
                    return Some((
                        text[value_start..value_end].trim().to_string(),
                        line_start,
                        block_end,
                    ));
                }
            }
        }
        index = if line_end >= bytes.len() {
            bytes.len()
        } else {
            line_end + 1
        };
    }
    None
}

/// Scan a TOML value starting at `start`, returning `(value_end, block_end)`
/// where `block_end` includes the rest of the line.
fn scan_toml_value(text: &str, start: usize) -> (usize, usize) {
    let bytes = text.as_bytes();
    let mut depth = 0i32;
    let mut in_string = false;
    let mut escape = false;
    let mut index = start;
    let mut last_content = start;
    while index < bytes.len() {
        let ch = bytes[index] as char;
        if escape {
            escape = false;
            index += 1;
            continue;
        }
        if in_string {
            if ch == '\\' {
                escape = true;
            } else if ch == '"' {
                in_string = false;
            }
        } else {
            match ch {
                '"' => in_string = true,
                '[' => depth += 1,
                ']' => {
                    depth -= 1;
                    if depth <= 0 {
                        return (index + 1, index + 1);
                    }
                }
                '\n' if depth <= 0 => return (last_content, index),
                '#' if depth <= 0 => return (last_content, index),
                _ => {}
            }
        }
        if !ch.is_whitespace() {
            last_content = index + 1;
        }
        index += 1;
    }
    (last_content, bytes.len())
}

/// Insert a root-level key at the end of the root table.
///
/// A root key appended at EOF would land *inside* the last `[table]` and be
/// parsed as a member of it, so the insertion point is the first table header.
pub fn insert_root_key(text: &str, key: &str, literal: &str) -> String {
    let line = format!("{key} = {literal}\n");
    let mut offset = 0usize;
    let mut insert_at = None;
    for raw_line in text.split_inclusive('\n') {
        if raw_line.trim_start().starts_with('[') {
            insert_at = Some(offset);
            break;
        }
        offset += raw_line.len();
    }
    match insert_at {
        Some(at) => {
            let mut out = String::with_capacity(text.len() + line.len() + 1);
            out.push_str(&text[..at]);
            if !out.is_empty() && !out.ends_with('\n') {
                out.push('\n');
            }
            out.push_str(&line);
            out.push('\n');
            out.push_str(&text[at..]);
            out
        }
        None => {
            let mut out = text.to_string();
            if !out.is_empty() && !out.ends_with('\n') {
                out.push('\n');
            }
            out.push_str(&line);
            out
        }
    }
}

/// Replace a root-level array key, or report that it was absent.
pub fn upsert_root_array(text: &str, key: &str, literal: &str) -> (String, bool) {
    match locate_root_key(text, key) {
        Some((_, start, end)) => {
            let mut out = String::with_capacity(text.len() + literal.len());
            out.push_str(&text[..start]);
            out.push_str(&format!("{key} = {literal}"));
            out.push_str(&text[end..]);
            (out, true)
        }
        None => (text.to_string(), false),
    }
}

/// Remove a root-level key (used to restore "the key did not exist").
pub fn remove_root_key(text: &str, key: &str) -> String {
    match locate_root_key(text, key) {
        Some((_, start, end)) => {
            let mut out = String::with_capacity(text.len());
            out.push_str(&text[..start]);
            let rest = &text[end..];
            out.push_str(rest.strip_prefix('\n').unwrap_or(rest));
            // Collapse a doubled blank line left behind by the removal.
            while out.contains("\n\n\n") {
                out = out.replace("\n\n\n", "\n\n");
            }
            out
        }
        None => text.to_string(),
    }
}
