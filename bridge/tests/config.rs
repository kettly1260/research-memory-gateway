//! Configuration loading, validation and backoff policy.

// Validation tests deliberately start from defaults and override one field.
#![allow(clippy::field_reassign_with_default)]

mod common;

use common::TestHome;
use research_memory_bridge::config::{default_client_id, BridgeConfig, DEFAULT_TOKEN_ENV};

#[test]
fn defaults_are_valid_and_conservative() {
    let config = BridgeConfig::default();
    config.validate().expect("defaults must be valid");
    assert_eq!(config.token_env, DEFAULT_TOKEN_ENV);
    assert!(config.batch_size > 0 && config.batch_size <= 500);
    assert!(config.base_backoff_seconds >= 1, "must not busy-retry");
    assert!(config.max_backoff_seconds >= config.base_backoff_seconds);
    // Tool telemetry is noisy; it is opt-in.
    assert!(!config.tool_events);
}

#[test]
fn round_trips_through_toml() {
    let home = TestHome::new();
    let mut config = home.config();
    config.server_url = "https://gateway.example.invalid:8787/".to_string();
    config.batch_size = 42;
    config.project_label = "research-memory-gateway".to_string();
    config.save(&home.config_path()).expect("save");

    let loaded = BridgeConfig::load(&home.config_path()).expect("load");
    assert_eq!(loaded.server_url, "https://gateway.example.invalid:8787/");
    assert_eq!(loaded.batch_size, 42);
    assert_eq!(loaded.project_label, "research-memory-gateway");
    assert_eq!(
        loaded.server_url_trimmed(),
        "https://gateway.example.invalid:8787"
    );
}

#[test]
fn missing_config_falls_back_to_defaults() {
    let home = TestHome::new();
    let config = BridgeConfig::load_or_default(home.path()).expect("load");
    assert_eq!(config.server_url, BridgeConfig::default().server_url);
}

#[test]
fn rejects_invalid_server_urls() {
    for url in [
        "",
        "   ",
        "ftp://gateway:8787",
        "gateway:8787",
        "http://",
        "https://",
        "http://:8787",
    ] {
        let mut config = BridgeConfig::default();
        config.server_url = url.to_string();
        assert!(
            config.validate().is_err(),
            "{url:?} should be rejected (fail-closed on an invalid URL)"
        );
    }
    for url in [
        "http://127.0.0.1:8787",
        "http://192.168.22.102:8787",
        "https://gateway.example.invalid/base",
    ] {
        let mut config = BridgeConfig::default();
        config.server_url = url.to_string();
        config
            .validate()
            .unwrap_or_else(|err| panic!("{url} rejected: {err}"));
    }
}

#[test]
fn rejects_a_retry_policy_that_could_busy_loop() {
    let mut config = BridgeConfig::default();
    config.base_backoff_seconds = 0;
    assert!(config.validate().is_err());

    let mut config = BridgeConfig::default();
    config.max_backoff_seconds = 1;
    config.base_backoff_seconds = 10;
    assert!(config.validate().is_err());

    let mut config = BridgeConfig::default();
    config.max_attempts = 0;
    assert!(config.validate().is_err());

    let mut config = BridgeConfig::default();
    config.batch_size = 501;
    assert!(config.validate().is_err());

    let mut config = BridgeConfig::default();
    config.request_timeout_seconds = 0;
    assert!(config.validate().is_err());

    let mut config = BridgeConfig::default();
    config.token_env = "  ".to_string();
    assert!(config.validate().is_err());
}

#[test]
fn unknown_config_keys_are_rejected_rather_than_ignored() {
    let home = TestHome::new();
    std::fs::write(
        home.config_path(),
        "server_url = \"http://127.0.0.1:8787\"\nnot_a_real_option = 1\n",
    )
    .expect("write");
    assert!(
        BridgeConfig::load(&home.config_path()).is_err(),
        "a typo in config.toml must not be silently ignored"
    );
}

#[test]
fn backoff_grows_and_is_capped() {
    let mut config = BridgeConfig::default();
    config.base_backoff_seconds = 4;
    config.max_backoff_seconds = 60;
    let first = config.backoff_seconds(1, "event-a");
    let third = config.backoff_seconds(3, "event-a");
    let huge = config.backoff_seconds(50, "event-a");
    assert!((2..=4).contains(&first), "first={first}");
    assert!((8..=16).contains(&third), "third={third}");
    assert!(huge <= 60, "huge={huge}");
    assert!(first <= third);
}

#[test]
fn backoff_jitter_spreads_retries_across_events() {
    let mut config = BridgeConfig::default();
    config.base_backoff_seconds = 60;
    config.max_backoff_seconds = 600;
    let values: std::collections::HashSet<u64> = (0..64)
        .map(|index| config.backoff_seconds(4, &format!("event-{index}")))
        .collect();
    assert!(
        values.len() > 1,
        "jitter must actually vary between events, got {values:?}"
    );
    for value in values {
        assert!((30..=480).contains(&value), "value out of range: {value}");
    }
}

#[test]
fn backoff_is_deterministic_for_one_event() {
    let config = BridgeConfig::default();
    assert_eq!(
        config.backoff_seconds(3, "same-event"),
        config.backoff_seconds(3, "same-event")
    );
}

#[test]
fn token_is_never_exposed_by_the_redacted_view() {
    let home = TestHome::new();
    let config = home.config();
    let view = config.redacted_view(home.path()).to_string();
    assert!(view.contains("token_env"), "{view}");
    assert!(view.contains("token_configured"), "{view}");
    // The literal token value must never appear in any serialized view.
    assert!(!view.contains("sk-"), "{view}");
    assert!(!view.contains("Bearer"), "{view}");
}

#[test]
fn endpoint_helpers_are_consistent() {
    let mut config = BridgeConfig::default();
    config.server_url = "http://gateway:8787/".to_string();
    assert_eq!(
        config.batch_endpoint(),
        "http://gateway:8787/api/conversations/events/batch"
    );
    assert_eq!(
        config.single_endpoint(),
        "http://gateway:8787/api/conversations/events"
    );
    assert_eq!(
        config.session_end_endpoint(),
        "http://gateway:8787/api/conversations/session-end"
    );
    assert_eq!(
        config.snapshot_endpoint(),
        "http://gateway:8787/api/conversations/snapshot"
    );
    assert_eq!(config.health_endpoint(), "http://gateway:8787/health");
    assert_eq!(
        config.ingest_stats_endpoint(),
        "http://gateway:8787/api/conversations/ingest/stats"
    );
}

#[test]
fn client_id_is_stable_and_non_empty() {
    let first = default_client_id();
    let second = default_client_id();
    assert_eq!(first, second);
    assert!(!first.is_empty());
}

#[test]
fn transcript_roots_honour_the_config_override() {
    let home = TestHome::new();
    let config = home.config();
    assert!(config.codex_sessions_root().ends_with("codex-sessions"));
    assert!(config.claude_projects_root().ends_with("claude-projects"));
}
