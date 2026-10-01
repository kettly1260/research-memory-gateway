//! Claude Code adapter.
//!
//! ## Realtime path: `hooks` in `~/.claude/settings.json`
//!
//! Claude Code runs configured commands on lifecycle events and passes the event
//! JSON **on stdin** (`hook_event_name` identifies the event).  Installed events:
//!
//! | Event | Mapping | Notes |
//! |---|---|---|
//! | `SessionStart` | `session_start` | carries `source`, `model`, `session_title` |
//! | `UserPromptSubmit` | `user_prompt` | `prompt` + `prompt_id` |
//! | `Stop` | `turn_end` | turn finished -- **not** a session end |
//! | `SessionEnd` | `session_end` | carries `reason` |
//! | `PreToolUse` / `PostToolUse` | `tool_call` / `tool_result` | only with `--with-tools` |
//!
//! Two properties of the hook contract drive the implementation:
//!
//! * **exit code 2 blocks the agent**, and plain stdout of `UserPromptSubmit` /
//!   `SessionStart` is injected into the model's context -- so `capture` must
//!   exit 0 and must never write to stdout;
//! * `Stop` means "Claude finished responding", which is a turn boundary, not a
//!   session boundary.  Treating it as `SessionEnd` would wrongly declare a live
//!   session finished, so it maps to `turn_end` instead.
//!
//! ## Reconciliation path: project transcripts
//!
//! `~/.claude/projects/<slug>/<session-id>.jsonl` holds the full transcript,
//! including the assistant messages that no hook exposes.  Records are
//! `{"type":"user"|"assistant", "uuid", "promptId", "sessionId", "timestamp",
//! "cwd", "message":{"role","content"}}`; tool results arrive as `user` records
//! whose content blocks are `tool_result`, and those are excluded from the
//! transcript.
//!
//! ## Identity across both paths
//!
//! The hook and the transcript share exactly one provider id for a user prompt:
//! `prompt_id` / `promptId`.  User-prompt events therefore anchor on it in both
//! paths, while assistant messages (transcript-only) anchor on their record
//! `uuid`.

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde_json::{json, Map, Value};

use super::{
    backup_file, count_transcript_files, read_json_object, write_json_object, AdapterStatus,
    AgentKind, CaptureOutcome, HookInstallReport,
};
use crate::config::BridgeConfig;
use crate::event::{EventType, NormalizedEvent, Role};

pub const SOURCE_SYSTEM: &str = "claude-code";

/// Bytes of the file head re-scanned to recover the session identity on an
/// incremental pass.
pub const HEAD_SCAN_BYTES: i64 = 64 * 1024;

/// Recover `(session_id, cwd)` from the head of a transcript, complete lines only.
pub fn scan_session_identity(bytes: &[u8]) -> Option<(String, String)> {
    let consumed = match bytes.iter().rposition(|byte| *byte == b'\n') {
        Some(index) => index + 1,
        None => 0,
    };
    let text = String::from_utf8_lossy(&bytes[..consumed]);
    let mut session_id = String::new();
    let mut cwd = String::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(record) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let Some(map) = record.as_object() else {
            continue;
        };
        if session_id.is_empty() {
            if let Some(value) = get_str(map, &["sessionId", "session_id"]) {
                session_id = value.to_string();
            }
        }
        if cwd.is_empty() {
            if let Some(value) = get_str(map, &["cwd"]) {
                cwd = value.to_string();
            }
        }
        if !session_id.is_empty() && !cwd.is_empty() {
            break;
        }
    }
    if session_id.is_empty() {
        return None;
    }
    Some((session_id, cwd))
}

/// Hook events the installer writes.  `SessionEnd` is included because Claude
/// Code *does* provide it, unlike Codex.
pub const HOOK_EVENTS: &[&str] = &["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"];
pub const TOOL_HOOK_EVENTS: &[&str] = &["PreToolUse", "PostToolUse"];

pub fn namespace_hash(config: &BridgeConfig) -> String {
    let label = if config.claude_namespace_label.trim().is_empty() {
        "default-v1"
    } else {
        config.claude_namespace_label.trim()
    };
    crate::normalize::account_namespace_hash(SOURCE_SYSTEM, label)
}

fn get_str<'a>(map: &'a Map<String, Value>, keys: &[&str]) -> Option<&'a str> {
    for key in keys {
        if let Some(Value::String(value)) = map.get(*key) {
            return Some(value.as_str());
        }
    }
    None
}

// ---------------------------------------------------------------------------
// Realtime hook payload
// ---------------------------------------------------------------------------

/// Parse a Claude Code hook payload (stdin JSON) into normalized events.
pub fn capture(payload: &str, config: &BridgeConfig) -> Result<CaptureOutcome> {
    let trimmed = payload.trim();
    if trimmed.is_empty() {
        return Ok(CaptureOutcome::empty().with_note("empty claude-code hook payload"));
    }
    let value: Value = match serde_json::from_str(trimmed) {
        Ok(value) => value,
        Err(err) => {
            return Ok(CaptureOutcome::empty()
                .with_note(format!("claude-code hook payload is not JSON: {err}")));
        }
    };
    let Some(map) = value.as_object() else {
        return Ok(CaptureOutcome::empty().with_note("claude-code hook payload is not an object"));
    };
    let hook = get_str(map, &["hook_event_name", "hookEventName"])
        .or_else(|| get_str(map, &["event"]))
        .unwrap_or("")
        .to_string();
    let session_id = get_str(map, &["session_id", "sessionId"])
        .unwrap_or("")
        .to_string();
    if session_id.is_empty() {
        return Ok(CaptureOutcome::empty().with_note(format!(
            "{hook} payload has no session_id; cannot anchor the session"
        )));
    }
    let timestamp = get_str(map, &["timestamp"])
        .map(|text| text.to_string())
        .unwrap_or_else(crate::normalize::now_rfc3339);
    let cwd = get_str(map, &["cwd"]).unwrap_or("");
    let transcript_path = get_str(map, &["transcript_path", "transcriptPath"]).unwrap_or("");
    let namespace = namespace_hash(config);

    let base = |event_type: EventType, content: &str| {
        let mut event = NormalizedEvent::new(SOURCE_SYSTEM, event_type, content)
            .with_account_namespace(namespace.clone())
            .with_session(session_id.clone())
            .with_conversation(session_id.clone())
            .with_timestamp(timestamp.clone())
            .with_metadata_entry("adapter", Value::String("claude-code-hook".to_string()))
            .with_metadata_entry("hook_event_name", Value::String(hook.clone()));
        if !cwd.is_empty() {
            event = event.with_metadata_entry("cwd", Value::String(cwd.to_string()));
        }
        if !transcript_path.is_empty() {
            event = event.with_metadata_entry(
                "transcript_path",
                Value::String(transcript_path.to_string()),
            );
        }
        if let Some(agent_type) = get_str(map, &["agent_type"]) {
            event = event.with_metadata_entry("agent_type", Value::String(agent_type.to_string()));
        }
        if let Some(agent_id) = get_str(map, &["agent_id"]) {
            event = event.with_metadata_entry("agent_id", Value::String(agent_id.to_string()));
        }
        if !config.project_label.trim().is_empty() {
            event = event.with_metadata_entry(
                "projects",
                Value::Array(vec![Value::String(config.project_label.trim().to_string())]),
            );
        }
        event
    };

    let mut outcome = CaptureOutcome::empty();
    match hook.as_str() {
        "SessionStart" => {
            let mut event = base(EventType::SessionStart, "");
            for key in ["source", "model", "session_title", "permission_mode"] {
                if let Some(value) = get_str(map, &[key]) {
                    event = event.with_metadata_entry(key, Value::String(value.to_string()));
                }
            }
            outcome.events.push(event);
        }
        "UserPromptSubmit" => {
            let prompt = get_str(map, &["prompt", "user_prompt", "message"]).unwrap_or("");
            if prompt.trim().is_empty() {
                outcome
                    .notes
                    .push("UserPromptSubmit payload has no prompt text".to_string());
            } else {
                let prompt_id = get_str(map, &["prompt_id", "promptId"]).unwrap_or("");
                let mut event = base(EventType::UserPrompt, prompt.trim());
                if !prompt_id.is_empty() {
                    // prompt_id is the one id shared with the transcript, so the
                    // hook copy and the transcript copy collapse into one block.
                    event = event.with_message(prompt_id);
                    event = event.with_turn(prompt_id);
                    event = event
                        .with_metadata_entry("id_source", Value::String("provider".to_string()));
                } else {
                    event = event
                        .with_metadata_entry("id_source", Value::String("synthesized".to_string()));
                }
                outcome.events.push(event);
            }
        }
        "Stop" => {
            // A turn boundary.  Deliberately NOT session_end: the session is
            // still alive and may continue with the next prompt.
            let mut event = base(EventType::TurnEnd, "");
            if let Some(active) = map.get("stop_hook_active").and_then(Value::as_bool) {
                event = event.with_metadata_entry("stop_hook_active", Value::Bool(active));
            }
            if let Some(id) = get_str(map, &["prompt_id", "promptId"]) {
                event = event.with_turn(id.to_string());
            }
            outcome.events.push(event);
        }
        "StopFailure" => {
            let mut event = base(EventType::TurnEnd, "");
            if let Some(kind) = get_str(map, &["error", "error_type"]) {
                event = event.with_metadata_entry("failure_type", Value::String(kind.to_string()));
            }
            outcome.events.push(event);
        }
        "SessionEnd" => {
            let mut event = base(EventType::SessionEnd, "");
            if let Some(reason) = get_str(map, &["reason"]) {
                event = event.with_metadata_entry("reason", Value::String(reason.to_string()));
            }
            outcome.events.push(event);
        }
        "PreToolUse" | "PostToolUse" | "PostToolUseFailure" => {
            if !config.tool_events {
                outcome.notes.push(format!(
                    "{hook} ignored; enable tool_events to capture tool telemetry"
                ));
            } else {
                let is_call = hook == "PreToolUse";
                let tool_name = get_str(map, &["tool_name"]).unwrap_or("");
                let tool_use_id = get_str(map, &["tool_use_id"]).unwrap_or("");
                let payload_value = if is_call {
                    map.get("tool_input").cloned().unwrap_or(Value::Null)
                } else {
                    map.get("tool_response").cloned().unwrap_or(Value::Null)
                };
                let content = match payload_value {
                    Value::String(text) => text,
                    Value::Null => String::new(),
                    other => other.to_string(),
                };
                let event_type = if is_call {
                    EventType::ToolCall
                } else {
                    EventType::ToolResult
                };
                let mut event = base(event_type, &content)
                    .with_role(Role::Tool)
                    .with_message(tool_use_id.to_string())
                    .with_metadata_entry("tool_name", Value::String(tool_name.to_string()))
                    .with_metadata_entry("call_id", Value::String(tool_use_id.to_string()));
                if hook == "PostToolUseFailure" {
                    event = event.with_metadata_entry("tool_failed", Value::Bool(true));
                }
                outcome.events.push(event);
            }
        }
        "" => {
            outcome
                .notes
                .push("claude-code hook payload has no hook_event_name".to_string());
        }
        other => {
            outcome.notes.push(format!(
                "claude-code hook event {other} is not captured (no conversation content)"
            ));
        }
    }
    Ok(outcome)
}

// ---------------------------------------------------------------------------
// Transcript reconciliation
// ---------------------------------------------------------------------------

/// Parse the *new* portion of one Claude Code transcript file.
pub fn parse_transcript(
    path: &Path,
    offset: i64,
    config: &BridgeConfig,
) -> Result<(Vec<NormalizedEvent>, i64)> {
    use std::io::{Read, Seek, SeekFrom};

    let mut file = std::fs::File::open(path)
        .with_context(|| format!("cannot open transcript {}", path.display()))?;
    let length = file.metadata()?.len() as i64;
    let start = offset.max(0).min(length);

    // Claude Code stamps `sessionId` on essentially every record, but recovering
    // it from the head keeps the incremental path correct even if a future
    // version only writes it on the first record -- and it keeps the derived
    // event ids identical between a cold parse and an incremental one.
    let mut head = vec![0u8; start.min(HEAD_SCAN_BYTES) as usize];
    file.seek(SeekFrom::Start(0))?;
    let read = file.read(&mut head)?;
    head.truncate(read);
    let mut session_id = String::new();
    let mut cwd = String::new();
    if let Some((found_session, found_cwd)) = scan_session_identity(&head) {
        session_id = found_session;
        cwd = found_cwd;
    }

    file.seek(SeekFrom::Start(start as u64))?;
    let mut buffer = Vec::new();
    file.read_to_end(&mut buffer)?;

    let consumed = match buffer.iter().rposition(|byte| *byte == b'\n') {
        Some(index) => index + 1,
        None => 0,
    };
    let text = String::from_utf8_lossy(&buffer[..consumed]);

    let namespace = namespace_hash(config);
    let mut events = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();

    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(record) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let Some(map) = record.as_object() else {
            continue;
        };
        let record_type = get_str(map, &["type"]).unwrap_or("");
        if record_type != "user" && record_type != "assistant" {
            continue;
        }
        if map.get("isMeta").and_then(Value::as_bool).unwrap_or(false) {
            continue;
        }
        if map
            .get("isApiErrorMessage")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            continue;
        }
        let Some(message) = map.get("message").and_then(Value::as_object) else {
            continue;
        };
        let role = get_str(message, &["role"]).unwrap_or(record_type);
        let uuid = get_str(map, &["uuid"]).unwrap_or("").to_string();
        let prompt_id = get_str(map, &["promptId", "prompt_id"]).unwrap_or("");
        if session_id.is_empty() {
            session_id = get_str(map, &["sessionId", "session_id"])
                .unwrap_or("")
                .to_string();
        }
        if cwd.is_empty() {
            cwd = get_str(map, &["cwd"]).unwrap_or("").to_string();
        }
        let timestamp = get_str(map, &["timestamp"]).unwrap_or("").to_string();

        let mut text_parts: Vec<String> = Vec::new();
        let mut tool_blocks: Vec<(bool, String, String, String)> = Vec::new();
        match message.get("content") {
            Some(Value::String(text)) => {
                if !text.trim().is_empty() {
                    text_parts.push(text.clone());
                }
            }
            Some(Value::Array(blocks)) => {
                for block in blocks {
                    let Some(block) = block.as_object() else {
                        continue;
                    };
                    match get_str(block, &["type"]).unwrap_or("") {
                        "text" => {
                            if let Some(text) = get_str(block, &["text"]) {
                                if !text.trim().is_empty() {
                                    text_parts.push(text.to_string());
                                }
                            }
                        }
                        "tool_use" => {
                            let name = get_str(block, &["name"]).unwrap_or("").to_string();
                            let id = get_str(block, &["id"]).unwrap_or("").to_string();
                            let input = block.get("input").cloned().unwrap_or(Value::Null);
                            let rendered = match input {
                                Value::String(text) => text,
                                Value::Null => String::new(),
                                other => other.to_string(),
                            };
                            tool_blocks.push((true, name, id, rendered));
                        }
                        "tool_result" => {
                            let id = get_str(block, &["tool_use_id"]).unwrap_or("").to_string();
                            let rendered =
                                match block.get("content").cloned().unwrap_or(Value::Null) {
                                    Value::String(text) => text,
                                    Value::Null => String::new(),
                                    other => other.to_string(),
                                };
                            tool_blocks.push((false, String::new(), id, rendered));
                        }
                        _ => {}
                    }
                }
            }
            _ => {}
        }

        // Tool-only records are telemetry, never conversation transcript.
        if !text_parts.is_empty() {
            let content = text_parts.join("\n\n");
            let event_type = match role {
                "user" => EventType::UserPrompt,
                "assistant" => EventType::AssistantMessage,
                _ => EventType::SystemMessage,
            };
            // User prompts anchor on promptId so the realtime hook and the
            // transcript collapse onto one archived message; assistant messages
            // exist only here, so their record uuid is the anchor.
            let anchor = if event_type == EventType::UserPrompt && !prompt_id.is_empty() {
                prompt_id.to_string()
            } else {
                uuid.clone()
            };
            if anchor.is_empty() {
                continue;
            }
            let key = format!("{event_type}:{anchor}");
            if !seen.insert(key) {
                continue;
            }
            let mut event = NormalizedEvent::new(SOURCE_SYSTEM, event_type, content)
                .with_account_namespace(namespace.clone())
                .with_session(session_id.clone())
                .with_conversation(session_id.clone())
                .with_message(anchor)
                .with_timestamp(timestamp.clone())
                .with_metadata_entry(
                    "adapter",
                    Value::String("claude-code-transcript".to_string()),
                )
                .with_metadata_entry("id_source", Value::String("provider".to_string()))
                .with_metadata_entry("transcript_path", Value::String(path.display().to_string()));
            if !prompt_id.is_empty() {
                event = event.with_turn(prompt_id.to_string());
            }
            if !cwd.is_empty() {
                event = event.with_metadata_entry("cwd", Value::String(cwd.clone()));
            }
            if map
                .get("isSidechain")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                event = event.with_metadata_entry("is_sidechain", Value::Bool(true));
            }
            if let Some(branch) = get_str(map, &["gitBranch"]) {
                if !branch.is_empty() {
                    event =
                        event.with_metadata_entry("git_branch", Value::String(branch.to_string()));
                }
            }
            if let Some(version) = get_str(map, &["version"]) {
                event =
                    event.with_metadata_entry("agent_version", Value::String(version.to_string()));
            }
            if !config.project_label.trim().is_empty() {
                event = event.with_metadata_entry(
                    "projects",
                    Value::Array(vec![Value::String(config.project_label.trim().to_string())]),
                );
            }
            events.push(event);
        }

        if config.tool_events {
            for (is_call, name, call_id, rendered) in tool_blocks {
                let event_type = if is_call {
                    EventType::ToolCall
                } else {
                    EventType::ToolResult
                };
                let key = format!("{event_type}:{call_id}:{uuid}");
                if !seen.insert(key) {
                    continue;
                }
                let mut event = NormalizedEvent::new(SOURCE_SYSTEM, event_type, rendered)
                    .with_account_namespace(namespace.clone())
                    .with_session(session_id.clone())
                    .with_conversation(session_id.clone())
                    .with_message(call_id.clone())
                    .with_role(Role::Tool)
                    .with_timestamp(timestamp.clone())
                    .with_metadata_entry(
                        "adapter",
                        Value::String("claude-code-transcript".to_string()),
                    )
                    .with_metadata_entry("id_source", Value::String("provider".to_string()))
                    .with_metadata_entry("tool_name", Value::String(name))
                    .with_metadata_entry("call_id", Value::String(call_id));
                if !config.project_label.trim().is_empty() {
                    event = event.with_metadata_entry(
                        "projects",
                        Value::Array(vec![Value::String(config.project_label.trim().to_string())]),
                    );
                }
                events.push(event);
            }
        }
    }

    Ok((events, start + consumed as i64))
}

/// All Claude Code transcript files under `root`.
pub fn discover_transcripts(root: &Path) -> Vec<PathBuf> {
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
                .map(|name| name.to_string_lossy().ends_with(".jsonl"))
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

fn hook_handler(agent: AgentKind, timeout: u64) -> Result<Value> {
    let exe = super::bridge_executable()?;
    Ok(json!({
        "type": "command",
        "command": exe.display().to_string(),
        "args": ["capture", "--agent", agent.as_str(), super::MANAGED_FLAG],
        "timeout": timeout,
    }))
}

/// True when a handler was installed by this bridge.
///
/// Matched on the marker argument rather than the executable path, so detection
/// survives a moved/renamed binary and can never claim a foreign hook.
fn is_our_handler(handler: &Value) -> bool {
    handler
        .get("type")
        .and_then(Value::as_str)
        .map(|kind| kind == "command")
        .unwrap_or(false)
        && handler
            .get("args")
            .and_then(Value::as_array)
            .map(|args| {
                args.iter()
                    .any(|item| item.as_str() == Some(super::MANAGED_FLAG))
            })
            .unwrap_or(false)
}

fn installed_events(settings: &Value) -> Vec<String> {
    let mut events = Vec::new();
    let Some(hooks) = settings.get("hooks").and_then(Value::as_object) else {
        return events;
    };
    for (event, groups) in hooks {
        let Some(groups) = groups.as_array() else {
            continue;
        };
        let found = groups.iter().any(|group| {
            group
                .get("hooks")
                .and_then(Value::as_array)
                .map(|handlers| handlers.iter().any(is_our_handler))
                .unwrap_or(false)
        });
        if found {
            events.push(event.clone());
        }
    }
    events.sort();
    events
}

pub fn status(config: &BridgeConfig) -> AdapterStatus {
    let config_path = AgentKind::ClaudeCode.config_path();
    let transcript_root = AgentKind::ClaudeCode.transcript_root(config);
    let mut installed = false;
    let mut config_exists = false;
    let mut command = String::new();
    if let Some(path) = &config_path {
        config_exists = path.exists();
        if let Ok(settings) = read_json_object(path) {
            let events = installed_events(&settings);
            installed = !events.is_empty();
            command = if installed {
                format!("hooks installed for: {}", events.join(", "))
            } else {
                String::new()
            };
        }
    }
    let transcript_files = transcript_root
        .as_deref()
        .map(|root| count_transcript_files(root, &|name| name.ends_with(".jsonl")))
        .unwrap_or(0);
    AdapterStatus {
        agent: AgentKind::ClaudeCode,
        supported: true,
        installed,
        config_path,
        config_exists,
        command,
        transcript_root,
        transcript_files,
    }
}

pub fn install_hooks(root: &Path, force: bool, with_tools: bool) -> Result<HookInstallReport> {
    let Some(path) = AgentKind::ClaudeCode.config_path() else {
        anyhow::bail!("cannot resolve ~/.claude/settings.json on this platform");
    };
    install_hooks_at(&path, root, force, with_tools)
}

/// Install into an explicit settings path (used by tests).
pub fn install_hooks_at(
    path: &Path,
    root: &Path,
    force: bool,
    with_tools: bool,
) -> Result<HookInstallReport> {
    let path = path.to_path_buf();
    let mut settings = read_json_object(&path)?;
    if !settings.is_object() {
        settings = Value::Object(Map::new());
    }
    let backup_path = backup_file(root, &path)?;

    let mut events: Vec<&str> = HOOK_EVENTS.to_vec();
    if with_tools {
        events.extend_from_slice(TOOL_HOOK_EVENTS);
    }

    let hooks = settings
        .as_object_mut()
        .expect("settings is an object")
        .entry("hooks".to_string())
        .or_insert_with(|| Value::Object(Map::new()));
    if !hooks.is_object() {
        *hooks = Value::Object(Map::new());
    }
    let hooks_map = hooks.as_object_mut().expect("hooks is an object");

    let mut added: Vec<String> = Vec::new();
    for event in events {
        let timeout = if event == "SessionEnd" { 5 } else { 10 };
        let handler = hook_handler(AgentKind::ClaudeCode, timeout)?;
        let groups = hooks_map
            .entry(event.to_string())
            .or_insert_with(|| Value::Array(Vec::new()));
        if !groups.is_array() {
            *groups = Value::Array(Vec::new());
        }
        let list = groups.as_array_mut().expect("groups is an array");
        let already = list.iter().any(|group| {
            group
                .get("hooks")
                .and_then(Value::as_array)
                .map(|handlers| handlers.iter().any(is_our_handler))
                .unwrap_or(false)
        });
        if already {
            continue;
        }
        list.push(json!({ "matcher": "", "hooks": [handler] }));
        added.push(event.to_string());
    }

    if added.is_empty() {
        return Ok(HookInstallReport {
            agent: AgentKind::ClaudeCode,
            config_path: Some(path),
            changed: false,
            backup_path,
            message: "Claude Code hooks are already installed".to_string(),
        });
    }

    let _ = force; // Claude Code hooks compose, so nothing is ever clobbered.
    write_json_object(&path, &settings)?;
    Ok(HookInstallReport {
        agent: AgentKind::ClaudeCode,
        config_path: Some(path),
        changed: true,
        backup_path,
        message: format!("installed Claude Code hooks for: {}", added.join(", ")),
    })
}

pub fn uninstall_hooks(root: &Path) -> Result<HookInstallReport> {
    let Some(path) = AgentKind::ClaudeCode.config_path() else {
        anyhow::bail!("cannot resolve ~/.claude/settings.json on this platform");
    };
    uninstall_hooks_at(&path, root)
}

/// Uninstall from an explicit settings path (used by tests).
pub fn uninstall_hooks_at(path: &Path, root: &Path) -> Result<HookInstallReport> {
    let path = path.to_path_buf();
    if !path.exists() {
        return Ok(HookInstallReport {
            agent: AgentKind::ClaudeCode,
            config_path: Some(path),
            changed: false,
            backup_path: None,
            message: "no Claude Code settings to update".to_string(),
        });
    }
    let mut settings = read_json_object(&path)?;
    let backup_path = backup_file(root, &path)?;
    let mut removed: Vec<String> = Vec::new();

    if let Some(hooks) = settings.get_mut("hooks").and_then(Value::as_object_mut) {
        let keys: Vec<String> = hooks.keys().cloned().collect();
        for key in keys {
            let Some(groups) = hooks.get_mut(&key).and_then(Value::as_array_mut) else {
                continue;
            };
            let before = groups.len();
            groups.retain_mut(|group| {
                let Some(handlers) = group.get_mut("hooks").and_then(Value::as_array_mut) else {
                    return true;
                };
                handlers.retain(|handler| !is_our_handler(handler));
                // Drop the matcher group only if it became empty; a group that
                // still holds someone else's handler is preserved intact.
                !handlers.is_empty()
            });
            if groups.len() != before {
                removed.push(key.clone());
            }
            if groups.is_empty() {
                hooks.remove(&key);
            }
        }
    }

    if removed.is_empty() {
        return Ok(HookInstallReport {
            agent: AgentKind::ClaudeCode,
            config_path: Some(path),
            changed: false,
            backup_path,
            message: "no bridge hooks found; settings left untouched".to_string(),
        });
    }

    write_json_object(&path, &settings)?;
    Ok(HookInstallReport {
        agent: AgentKind::ClaudeCode,
        config_path: Some(path),
        changed: true,
        backup_path,
        message: format!("removed Claude Code hooks for: {}", removed.join(", ")),
    })
}
