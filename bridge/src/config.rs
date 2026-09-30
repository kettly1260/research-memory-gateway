//! Bridge configuration (`config.toml`) and its validation rules.
//!
//! The token is **never** stored here.  Only the *name* of the environment
//! variable that holds it is persisted (`token_env`), so a config file can be
//! committed, backed up or pasted into a bug report without leaking a secret.
//!
//! Validation is deliberately fail-closed on the things that would silently
//! corrupt data: an invalid server URL, a nonsensical batch size, or a retry
//! policy that could busy-loop.  Everything else has a safe default.

use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};

use crate::paths;

/// Fallback env var, matching the gateway's `server.auth_token_env` default.
pub const DEFAULT_TOKEN_ENV: &str = "RESEARCH_MEMORY_TOKEN";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct BridgeConfig {
    /// Gateway base URL, e.g. `http://192.168.22.102:8787`.
    pub server_url: String,
    /// Name of the environment variable holding the bearer token.
    pub token_env: String,
    /// Stable identifier for this machine, sent as `client_id`.
    pub client_id: String,
    /// Maximum events per batch request.
    pub batch_size: usize,
    pub request_timeout_seconds: u64,
    pub connect_timeout_seconds: u64,
    /// Attempts before an event is parked in `dead_letter`.
    pub max_attempts: u32,
    pub base_backoff_seconds: u64,
    pub max_backoff_seconds: u64,
    /// Spawn a detached `drain --once` after a successful capture so uploads do
    /// not delay the agent hook.  Set to `false` to rely on `drain --daemon`.
    pub capture_drain: bool,
    /// Wall-clock ceiling for the detached drain; it exits either way.
    pub capture_drain_timeout_ms: u64,
    /// ACKed spool rows older than this are deleted.
    pub spool_retention_days: u32,
    /// Hard cap on pending rows; the oldest beyond the cap are parked in
    /// `dead_letter` (loudly logged) so the spool cannot grow without bound.
    pub max_pending_events: usize,
    /// Sleep between daemon drain passes when the spool is empty.
    pub drain_interval_seconds: u64,
    pub watch_interval_seconds: u64,
    /// Override the Codex session transcript root (default `~/.codex/sessions`).
    pub codex_sessions_dir: String,
    /// Override the Claude Code transcript root (default `~/.claude/projects`).
    pub claude_projects_dir: String,
    /// Optional project label attached to every captured event's metadata.
    pub project_label: String,
    /// Account namespace label for Claude Code.  Only its hash is ever sent, so
    /// a raw account email must never be placed here.
    pub claude_namespace_label: String,
    /// Capture tool call/result telemetry.  Off by default: tool payloads are
    /// noisy and add little to conversation recall.
    pub tool_events: bool,
    pub log_level: String,
}

impl Default for BridgeConfig {
    fn default() -> Self {
        BridgeConfig {
            server_url: "http://127.0.0.1:8787".to_string(),
            token_env: DEFAULT_TOKEN_ENV.to_string(),
            client_id: default_client_id(),
            batch_size: 100,
            request_timeout_seconds: 30,
            connect_timeout_seconds: 10,
            max_attempts: 12,
            base_backoff_seconds: 5,
            max_backoff_seconds: 3600,
            capture_drain: true,
            capture_drain_timeout_ms: 1500,
            spool_retention_days: 30,
            max_pending_events: 200_000,
            drain_interval_seconds: 15,
            watch_interval_seconds: 20,
            codex_sessions_dir: String::new(),
            claude_projects_dir: String::new(),
            project_label: String::new(),
            claude_namespace_label: "default-v1".to_string(),
            tool_events: false,
            log_level: "info".to_string(),
        }
    }
}

impl BridgeConfig {
    /// Load `config.toml`, falling back to defaults when the file is absent.
    pub fn load_or_default(root: &Path) -> Result<BridgeConfig> {
        let path = paths::config_path(root);
        if !path.exists() {
            return Ok(BridgeConfig::default());
        }
        BridgeConfig::load(&path)
    }

    pub fn load(path: &Path) -> Result<BridgeConfig> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("cannot read config {}", path.display()))?;
        let config: BridgeConfig = toml::from_str(&text)
            .with_context(|| format!("cannot parse config {}", path.display()))?;
        config.validate()?;
        Ok(config)
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let text = toml::to_string_pretty(self).context("cannot serialize config")?;
        // Write via a sibling temp file so a crash cannot leave a truncated
        // config behind.
        let tmp = path.with_extension("toml.tmp");
        std::fs::write(&tmp, text)?;
        std::fs::rename(&tmp, path)?;
        Ok(())
    }

    /// Fail-closed validation of everything that could silently corrupt data.
    pub fn validate(&self) -> Result<()> {
        let url = self.server_url.trim();
        if url.is_empty() {
            return Err(anyhow!("server_url is empty"));
        }
        let lowered = url.to_ascii_lowercase();
        if !(lowered.starts_with("http://") || lowered.starts_with("https://")) {
            return Err(anyhow!(
                "server_url must start with http:// or https:// (got {url})"
            ));
        }
        let after_scheme = url
            .split_once("://")
            .map(|(_, rest)| rest)
            .unwrap_or_default();
        let host = after_scheme
            .split(['/', '?', '#'])
            .next()
            .unwrap_or_default();
        if host.is_empty() || host.starts_with(':') {
            return Err(anyhow!("server_url has no host: {url}"));
        }
        if self.batch_size == 0 || self.batch_size > 500 {
            return Err(anyhow!(
                "batch_size must be between 1 and 500 (got {})",
                self.batch_size
            ));
        }
        if self.max_attempts == 0 {
            return Err(anyhow!("max_attempts must be at least 1"));
        }
        if self.base_backoff_seconds == 0 {
            return Err(anyhow!(
                "base_backoff_seconds must be at least 1 to avoid a busy retry loop"
            ));
        }
        if self.max_backoff_seconds < self.base_backoff_seconds {
            return Err(anyhow!(
                "max_backoff_seconds must be >= base_backoff_seconds"
            ));
        }
        if self.request_timeout_seconds == 0 {
            return Err(anyhow!("request_timeout_seconds must be at least 1"));
        }
        if self.token_env.trim().is_empty() {
            return Err(anyhow!("token_env must not be empty"));
        }
        Ok(())
    }

    /// Resolve the bearer token from the environment.  Never persisted, never
    /// logged, never echoed by `config --json` or `status`.
    pub fn resolve_token(&self) -> Option<String> {
        for name in [self.token_env.as_str(), DEFAULT_TOKEN_ENV] {
            if name.trim().is_empty() {
                continue;
            }
            if let Ok(value) = std::env::var(name) {
                let trimmed = value.trim().to_string();
                if !trimmed.is_empty() {
                    return Some(trimmed);
                }
            }
        }
        None
    }

    /// True when the token is present, without revealing it.
    pub fn token_configured(&self) -> bool {
        self.resolve_token().is_some()
    }

    pub fn server_url_trimmed(&self) -> &str {
        self.server_url.trim().trim_end_matches('/')
    }

    pub fn batch_endpoint(&self) -> String {
        format!(
            "{}/api/conversations/events/batch",
            self.server_url_trimmed()
        )
    }

    pub fn single_endpoint(&self) -> String {
        format!("{}/api/conversations/events", self.server_url_trimmed())
    }

    pub fn session_end_endpoint(&self) -> String {
        format!(
            "{}/api/conversations/session-end",
            self.server_url_trimmed()
        )
    }

    pub fn snapshot_endpoint(&self) -> String {
        format!("{}/api/conversations/snapshot", self.server_url_trimmed())
    }

    pub fn health_endpoint(&self) -> String {
        format!("{}/health", self.server_url_trimmed())
    }

    pub fn ingest_stats_endpoint(&self) -> String {
        format!(
            "{}/api/conversations/ingest/stats",
            self.server_url_trimmed()
        )
    }

    pub fn codex_sessions_root(&self) -> PathBuf {
        if self.codex_sessions_dir.trim().is_empty() {
            dirs::home_dir()
                .unwrap_or_else(|| PathBuf::from("."))
                .join(".codex")
                .join("sessions")
        } else {
            paths::expand_tilde(&self.codex_sessions_dir)
        }
    }

    pub fn claude_projects_root(&self) -> PathBuf {
        if self.claude_projects_dir.trim().is_empty() {
            dirs::home_dir()
                .unwrap_or_else(|| PathBuf::from("."))
                .join(".claude")
                .join("projects")
        } else {
            paths::expand_tilde(&self.claude_projects_dir)
        }
    }

    /// Exponential backoff with full jitter, capped at `max_backoff_seconds`.
    ///
    /// `attempt` is 1-based.  Jitter uses a cheap deterministic hash of the
    /// event id so retries spread out without pulling in an RNG dependency.
    pub fn backoff_seconds(&self, attempt: u32, jitter_seed: &str) -> u64 {
        let exponent = attempt.saturating_sub(1).min(20);
        let base = self
            .base_backoff_seconds
            .saturating_mul(1u64 << exponent.min(32))
            .min(self.max_backoff_seconds);
        if base <= 1 {
            return base.max(1);
        }
        let spread = crate::normalize::sha256_hex(jitter_seed);
        let bucket = u64::from_str_radix(&spread[..8], 16).unwrap_or(0);
        // Full jitter: uniform in [base/2, base].
        let half = base / 2;
        half + (bucket % (base - half).max(1))
    }

    /// Redacted view for `config --json` / `status --json`.
    pub fn redacted_view(&self, root: &Path) -> serde_json::Value {
        serde_json::json!({
            "server_url": self.server_url_trimmed(),
            "token_env": self.token_env,
            "token_configured": self.token_configured(),
            "client_id": self.client_id,
            "batch_size": self.batch_size,
            "request_timeout_seconds": self.request_timeout_seconds,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "max_attempts": self.max_attempts,
            "base_backoff_seconds": self.base_backoff_seconds,
            "max_backoff_seconds": self.max_backoff_seconds,
            "capture_drain": self.capture_drain,
            "spool_retention_days": self.spool_retention_days,
            "max_pending_events": self.max_pending_events,
            "tool_events": self.tool_events,
            "project_label": self.project_label,
            "claude_namespace_label": self.claude_namespace_label,
            "home": root.display().to_string(),
            "config_path": paths::config_path(root).display().to_string(),
            "spool_path": paths::spool_path(root).display().to_string(),
            "log_path": paths::log_path(root).display().to_string(),
        })
    }
}

/// Stable per-machine client id derived from environment, not random, so it
/// survives restarts and lets the gateway group a client's history.
pub fn default_client_id() -> String {
    let host = std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "unknown-host".to_string());
    let user = std::env::var("USERNAME")
        .or_else(|_| std::env::var("USER"))
        .unwrap_or_else(|_| "unknown-user".to_string());
    format!("{host}-{user}")
}
