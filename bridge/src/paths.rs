//! Cross-platform resolution of the bridge's persistent directories.
//!
//! Nothing here assumes a POSIX shell: no `~`, no `/tmp`, no `chmod`.  The
//! default root is the platform-standard per-user data directory
//! (`%APPDATA%` on Windows, `~/.local/share` on Linux, `~/Library/Application
//! Support` on macOS), overridable with `RESEARCH_MEMORY_BRIDGE_HOME` so tests
//! and portable installs never have to touch the real user profile.
//!
//! Layout::
//!
//! ```text
//! <root>/
//!   config.toml
//!   spool.sqlite        (+ -wal / -shm)
//!   logs/bridge.log
//! ```

use std::path::{Path, PathBuf};

pub const APP_DIR_NAME: &str = "research-memory-bridge";
pub const HOME_ENV: &str = "RESEARCH_MEMORY_BRIDGE_HOME";
pub const CONFIG_FILE_NAME: &str = "config.toml";
pub const SPOOL_FILE_NAME: &str = "spool.sqlite";
pub const LOG_DIR_NAME: &str = "logs";
pub const LOG_FILE_NAME: &str = "bridge.log";
pub const BACKUP_DIR_NAME: &str = "backups";

/// Resolve the bridge home directory, creating it when `create` is set.
pub fn home_dir(create: bool) -> std::io::Result<PathBuf> {
    let root = resolve_home();
    if create {
        std::fs::create_dir_all(&root)?;
    }
    Ok(root)
}

/// Pure resolution (no filesystem side effects) -- handy for `config --json`.
pub fn resolve_home() -> PathBuf {
    if let Ok(explicit) = std::env::var(HOME_ENV) {
        let trimmed = explicit.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    if let Some(data) = dirs::data_dir() {
        return data.join(APP_DIR_NAME);
    }
    if let Some(home) = dirs::home_dir() {
        return home.join(format!(".{APP_DIR_NAME}"));
    }
    // Last resort: a relative directory next to the executable's cwd.  Reached
    // only when the platform provides no home directory at all.
    PathBuf::from(format!(".{APP_DIR_NAME}"))
}

pub fn config_path(root: &Path) -> PathBuf {
    root.join(CONFIG_FILE_NAME)
}

pub fn spool_path(root: &Path) -> PathBuf {
    root.join(SPOOL_FILE_NAME)
}

pub fn log_dir(root: &Path) -> PathBuf {
    root.join(LOG_DIR_NAME)
}

pub fn log_path(root: &Path) -> PathBuf {
    log_dir(root).join(LOG_FILE_NAME)
}

pub fn backup_dir(root: &Path) -> PathBuf {
    root.join(BACKUP_DIR_NAME)
}

/// Directory used for one-shot backups of files the bridge modifies
/// (`~/.codex/config.toml`, `~/.claude/settings.json`, ...).
pub fn ensure_backup_dir(root: &Path) -> std::io::Result<PathBuf> {
    let dir = backup_dir(root);
    std::fs::create_dir_all(&dir)?;
    Ok(dir)
}

/// Expand a leading `~` in a user-supplied path.
///
/// Windows has no shell-level tilde expansion for directly spawned processes,
/// so hook installers must expand it themselves.
pub fn expand_tilde(value: &str) -> PathBuf {
    let trimmed = value.trim();
    if trimmed == "~" {
        return dirs::home_dir().unwrap_or_else(|| PathBuf::from("~"));
    }
    if let Some(rest) = trimmed
        .strip_prefix("~/")
        .or_else(|| trimmed.strip_prefix("~\\"))
    {
        if let Some(home) = dirs::home_dir() {
            return home.join(rest);
        }
    }
    PathBuf::from(trimmed)
}
