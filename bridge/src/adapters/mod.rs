//! Agent adapters: provider lifecycle events -> [`NormalizedEvent`].
//!
//! Each adapter is written against the *verified* provider contract, never a
//! guessed one:
//!
//! | Agent | Realtime mechanism | Verified payload |
//! |---|---|---|
//! | Codex | `notify` in `~/.codex/config.toml`, fired on `agent-turn-complete` | JSON **appended as the last CLI argument** (not stdin) |
//! | Claude Code | `hooks` in `~/.claude/settings.json` | JSON on **stdin**, keyed by `hook_event_name` |
//! | Generic | documented JSON on stdin/argv | see [`generic`] |
//!
//! Nothing here invokes a model, opens a network socket or touches the gateway;
//! adapters only *translate*.  Upload is the drain's job.
//!
//! **What Codex does and does not give us.**  Codex's `notify` currently fires
//! only for `agent-turn-complete` -- there is no `SessionStart`, no `SessionEnd`
//! and no tool lifecycle.  A turn completion is therefore mapped to the turn's
//! messages, and is explicitly *not* treated as a session end; `finalize-session`
//! exists as the documented fallback for agents that cannot signal SessionEnd.

pub mod claude_code;
pub mod codex;
pub mod generic;

use std::path::PathBuf;

use anyhow::Result;

use crate::config::BridgeConfig;
use crate::event::NormalizedEvent;

/// Which agent an adapter speaks for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentKind {
    Codex,
    ClaudeCode,
    Generic,
}

impl AgentKind {
    pub const ALL: [AgentKind; 3] = [AgentKind::Codex, AgentKind::ClaudeCode, AgentKind::Generic];

    /// CLI spelling (`--agent codex`, `--agent claude-code`, `--agent generic`).
    pub fn as_str(self) -> &'static str {
        match self {
            AgentKind::Codex => "codex",
            AgentKind::ClaudeCode => "claude-code",
            AgentKind::Generic => "generic",
        }
    }

    /// Value used for `source_system` on the wire.
    pub fn source_system(self) -> &'static str {
        match self {
            AgentKind::Codex => "codex",
            AgentKind::ClaudeCode => "claude-code",
            AgentKind::Generic => "generic",
        }
    }

    pub fn parse(value: &str) -> Option<AgentKind> {
        let normalized = value.trim().to_ascii_lowercase().replace('_', "-");
        match normalized.as_str() {
            "codex" | "openai-codex" => Some(AgentKind::Codex),
            "claude-code" | "claude" | "claudecode" => Some(AgentKind::ClaudeCode),
            "generic" | "stdin" | "custom" => Some(AgentKind::Generic),
            _ => None,
        }
    }

    /// Where this agent keeps its configuration file.
    pub fn config_path(self) -> Option<PathBuf> {
        match self {
            AgentKind::Codex => Some(dirs::home_dir()?.join(".codex").join("config.toml")),
            AgentKind::ClaudeCode => Some(dirs::home_dir()?.join(".claude").join("settings.json")),
            AgentKind::Generic => None,
        }
    }

    /// Root of this agent's transcript tree, when it has one.
    pub fn transcript_root(self, config: &BridgeConfig) -> Option<PathBuf> {
        match self {
            AgentKind::Codex => Some(config.codex_sessions_root()),
            AgentKind::ClaudeCode => Some(config.claude_projects_root()),
            AgentKind::Generic => None,
        }
    }
}

/// How a capture payload reached us.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PayloadSource {
    /// Trailing CLI argument (Codex `notify` appends the payload).
    Argv,
    /// Standard input (Claude Code hooks).
    Stdin,
}

/// Result of parsing one capture payload.
#[derive(Debug, Clone)]
pub struct CaptureOutcome {
    pub events: Vec<NormalizedEvent>,
    /// Non-fatal notes (unsupported event names, skipped records, ...).
    pub notes: Vec<String>,
}

impl CaptureOutcome {
    pub fn empty() -> CaptureOutcome {
        CaptureOutcome {
            events: Vec::new(),
            notes: Vec::new(),
        }
    }

    pub fn with_note(mut self, note: impl Into<String>) -> CaptureOutcome {
        self.notes.push(note.into());
        self
    }
}

/// Parse a raw hook payload into normalized events.
///
/// Implementations must be *total*: an unrecognised payload returns an empty
/// outcome with a note, never an error that could make a hook fail.
pub fn capture(agent: AgentKind, payload: &str, config: &BridgeConfig) -> Result<CaptureOutcome> {
    let mut outcome = match agent {
        AgentKind::Codex => codex::capture(payload, config)?,
        AgentKind::ClaudeCode => claude_code::capture(payload, config)?,
        AgentKind::Generic => generic::capture(payload, config)?,
    };
    // Single choke point: an event must never reach the spool without a stable
    // id, because idempotency depends entirely on it.
    crate::watcher::ensure_event_ids(&mut outcome.events);
    outcome.events.retain(|event| match event.validate() {
        Ok(()) => true,
        Err(reason) => {
            outcome.notes.push(format!("event dropped: {reason}"));
            false
        }
    });
    Ok(outcome)
}

/// Report produced by `install-hooks` / `uninstall-hooks`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HookInstallReport {
    pub agent: AgentKind,
    pub config_path: Option<PathBuf>,
    pub changed: bool,
    pub backup_path: Option<PathBuf>,
    pub message: String,
}

/// Whether the bridge's hooks are currently installed for one agent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AdapterStatus {
    pub agent: AgentKind,
    pub supported: bool,
    pub installed: bool,
    pub config_path: Option<PathBuf>,
    pub config_exists: bool,
    pub command: String,
    pub transcript_root: Option<PathBuf>,
    pub transcript_files: usize,
}

/// Argument that marks a hook command as installed by this bridge.
///
/// Detection must not depend on the executable path: on Windows it contains
/// spaces and backslashes, and a test harness runs a differently-named binary.
pub const MANAGED_FLAG: &str = "--bridge-managed";

/// Path of the bridge executable, used when writing hook commands.
///
/// Resolved from `std::env::current_exe()` so `install-hooks` always records the
/// binary that is actually running.
pub fn bridge_executable() -> Result<PathBuf> {
    Ok(std::env::current_exe()?)
}

pub fn status(agent: AgentKind, config: &BridgeConfig) -> AdapterStatus {
    match agent {
        AgentKind::Codex => codex::status(config),
        AgentKind::ClaudeCode => claude_code::status(config),
        AgentKind::Generic => AdapterStatus {
            agent,
            supported: false,
            installed: false,
            config_path: None,
            config_exists: false,
            command: String::new(),
            transcript_root: None,
            transcript_files: 0,
        },
    }
}

pub fn install_hooks(
    agent: AgentKind,
    root: &std::path::Path,
    force: bool,
    with_tools: bool,
) -> Result<HookInstallReport> {
    match agent {
        AgentKind::Codex => codex::install_hooks(root, force),
        AgentKind::ClaudeCode => claude_code::install_hooks(root, force, with_tools),
        AgentKind::Generic => Ok(HookInstallReport {
            agent,
            config_path: None,
            changed: false,
            backup_path: None,
            message: "generic adapter has no hook configuration to install".to_string(),
        }),
    }
}

pub fn uninstall_hooks(agent: AgentKind, root: &std::path::Path) -> Result<HookInstallReport> {
    match agent {
        AgentKind::Codex => codex::uninstall_hooks(root),
        AgentKind::ClaudeCode => claude_code::uninstall_hooks(root),
        AgentKind::Generic => Ok(HookInstallReport {
            agent,
            config_path: None,
            changed: false,
            backup_path: None,
            message: "generic adapter has no hook configuration to uninstall".to_string(),
        }),
    }
}

/// Copy a file into the bridge's backup directory before modifying it.
pub(crate) fn backup_file(
    root: &std::path::Path,
    path: &std::path::Path,
) -> Result<Option<PathBuf>> {
    if !path.exists() {
        return Ok(None);
    }
    let dir = crate::paths::ensure_backup_dir(root)?;
    let stem = path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| "config".to_string());
    let stamp = crate::normalize::now_rfc3339().replace([':', '-'], "");
    let target = dir.join(format!("{stamp}-{stem}"));
    std::fs::copy(path, &target)?;
    Ok(Some(target))
}

/// Read a JSON object from a file, tolerating a missing file.
pub(crate) fn read_json_object(path: &std::path::Path) -> Result<serde_json::Value> {
    if !path.exists() {
        return Ok(serde_json::Value::Object(serde_json::Map::new()));
    }
    let text = std::fs::read_to_string(path)?;
    if text.trim().is_empty() {
        return Ok(serde_json::Value::Object(serde_json::Map::new()));
    }
    Ok(serde_json::from_str(&text)?)
}

/// Write a JSON object atomically (temp sibling + rename).
pub(crate) fn write_json_object(path: &std::path::Path, value: &serde_json::Value) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let text = serde_json::to_string_pretty(value)?;
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, format!("{text}\n"))?;
    std::fs::rename(&tmp, path)?;
    Ok(())
}

/// Count files under `root` whose name matches `predicate`, without following
/// symlinks and without failing on unreadable directories.
pub(crate) fn count_transcript_files(
    root: &std::path::Path,
    predicate: &dyn Fn(&str) -> bool,
) -> usize {
    if !root.is_dir() {
        return 0;
    }
    let mut count = 0usize;
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = match std::fs::read_dir(&dir) {
            Ok(entries) => entries,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path
                .file_name()
                .map(|name| predicate(&name.to_string_lossy()))
                .unwrap_or(false)
            {
                count += 1;
            }
        }
    }
    count
}
