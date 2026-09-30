//! HTTP transport to the gateway's non-MCP ingest API.
//!
//! Authentication reuses the gateway's existing remote model -- a plain
//! `Authorization: Bearer <token>` header read from an environment variable.
//! There is no second credential system, no token file, and the token is never
//! logged, echoed by `status`/`doctor`, or included in error messages.
//!
//! Error classification is what makes retry safe:
//!
//! * `Unauthorized` -- fail-closed: the batch is never ACKed, so a fixed token
//!   re-uploads it;
//! * `Disabled` -- the operator has not enabled ingest; retry, do not ACK;
//! * `Http 5xx` / `Network` -- transient, retry with backoff;
//! * `Protocol` -- the response could not be understood; never treat as success.

use std::time::Duration;

use anyhow::Result;
use serde::{Deserialize, Serialize};

use crate::config::BridgeConfig;
use crate::event::{NormalizedEvent, SCHEMA_VERSION};

/// One rejected event, as reported by the gateway.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RejectedItem {
    #[serde(default)]
    pub event_id: String,
    pub code: String,
    #[serde(default)]
    pub message: String,
}

/// Gateway response envelope for single/batch/snapshot ingest.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct BatchOutcome {
    #[serde(default)]
    pub accepted: Vec<String>,
    #[serde(default)]
    pub duplicates: Vec<String>,
    #[serde(default)]
    pub rejected: Vec<RejectedItem>,
    #[serde(default)]
    pub session: Option<serde_json::Value>,
}

impl BatchOutcome {
    pub fn acked(&self) -> Vec<String> {
        let mut ids = self.accepted.clone();
        ids.extend(self.duplicates.clone());
        ids
    }
}

#[derive(Debug)]
pub enum TransportError {
    Unauthorized(String),
    Disabled(String),
    Http { status: u16, body: String },
    Network(String),
    Protocol(String),
}

impl std::fmt::Display for TransportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TransportError::Unauthorized(message) => write!(f, "unauthorized: {message}"),
            TransportError::Disabled(message) => write!(f, "ingest disabled: {message}"),
            TransportError::Http { status, body } => {
                write!(
                    f,
                    "http {status}: {}",
                    crate::redact::redact_text(body, "$.http").0
                )
            }
            TransportError::Network(message) => write!(f, "network: {message}"),
            TransportError::Protocol(message) => write!(f, "protocol: {message}"),
        }
    }
}

impl std::error::Error for TransportError {}

impl TransportError {
    /// Whether re-sending the same payload could ever succeed.
    pub fn retryable(&self) -> bool {
        match self {
            TransportError::Unauthorized(_) => true,
            TransportError::Disabled(_) => true,
            TransportError::Network(_) => true,
            TransportError::Protocol(_) => true,
            TransportError::Http { status, .. } => {
                *status >= 500 || *status == 408 || *status == 429
            }
        }
    }
}

/// Result of an authenticated (but non-mutating) probe, used by `doctor`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuthProbe {
    /// Token accepted and ingest enabled.
    Ok,
    /// Token accepted but ingest is switched off on the gateway.
    Disabled,
    /// Gateway reachable, token missing or rejected.
    Rejected(String),
}

pub struct Transport {
    client: reqwest::Client,
    config: BridgeConfig,
    token: Option<String>,
}

impl Transport {
    pub fn new(config: &BridgeConfig) -> Result<Transport> {
        let token = config.resolve_token();
        Transport::with_token(config, token)
    }

    /// Build a transport with an explicit token.
    ///
    /// Used by tests so they never have to mutate process-wide environment
    /// variables (which would race between test threads).
    pub fn with_token(config: &BridgeConfig, token: Option<String>) -> Result<Transport> {
        let client = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(config.connect_timeout_seconds.max(1)))
            .timeout(Duration::from_secs(config.request_timeout_seconds.max(1)))
            .user_agent(format!(
                "research-memory-bridge/{}",
                env!("CARGO_PKG_VERSION")
            ))
            .build()?;
        Ok(Transport {
            client,
            config: config.clone(),
            token,
        })
    }

    pub fn token_configured(&self) -> bool {
        self.token.is_some()
    }

    fn request(&self, method: reqwest::Method, url: &str) -> reqwest::RequestBuilder {
        let builder = self.client.request(method, url);
        match &self.token {
            Some(token) => builder.bearer_auth(token),
            None => builder,
        }
    }

    async fn send_json<T: serde::de::DeserializeOwned>(
        &self,
        method: reqwest::Method,
        url: &str,
        body: Option<serde_json::Value>,
    ) -> Result<T, TransportError> {
        let mut request = self.request(method, url);
        if let Some(payload) = body {
            request = request.json(&payload);
        }
        let response = request.send().await.map_err(|err| {
            if err.is_timeout() {
                TransportError::Network(format!("timeout: {err}"))
            } else {
                TransportError::Network(err.to_string())
            }
        })?;
        let status = response.status();
        let text = response.text().await.unwrap_or_default();
        if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
            return Err(TransportError::Unauthorized(
                "gateway rejected the bearer token".to_string(),
            ));
        }
        if status == reqwest::StatusCode::SERVICE_UNAVAILABLE {
            return Err(TransportError::Disabled(
                crate::redact::redact_text(&text, "$.body").0,
            ));
        }
        if !status.is_success() {
            return Err(TransportError::Http {
                status: status.as_u16(),
                body: text,
            });
        }
        serde_json::from_str::<T>(&text).map_err(|err| {
            TransportError::Protocol(format!(
                "cannot decode gateway response ({err}): {}",
                crate::normalize::truncate_chars(&text, 300)
            ))
        })
    }

    /// Upload one batch of normalized events.
    pub async fn post_batch(
        &self,
        events: &[NormalizedEvent],
        client_id: &str,
    ) -> Result<BatchOutcome, TransportError> {
        let payload = serde_json::json!({
            "schema_version": SCHEMA_VERSION,
            "client_id": client_id,
            "events": events,
        });
        self.send_json(
            reqwest::Method::POST,
            &self.config.batch_endpoint(),
            Some(payload),
        )
        .await
    }

    /// Declare a finished session so the gateway can detect missing events.
    pub async fn post_session_end(
        &self,
        payload: &serde_json::Value,
    ) -> Result<serde_json::Value, TransportError> {
        self.send_json(
            reqwest::Method::POST,
            &self.config.session_end_endpoint(),
            Some(payload.clone()),
        )
        .await
    }

    /// Reconcile a whole conversation (final consistency path).
    pub async fn post_snapshot(
        &self,
        payload: &serde_json::Value,
    ) -> Result<BatchOutcome, TransportError> {
        self.send_json(
            reqwest::Method::POST,
            &self.config.snapshot_endpoint(),
            Some(payload.clone()),
        )
        .await
    }

    /// Read gateway health (unauthenticated on the gateway side).
    pub async fn health(&self) -> Result<serde_json::Value, TransportError> {
        self.send_json::<serde_json::Value>(
            reqwest::Method::GET,
            &self.config.health_endpoint(),
            None,
        )
        .await
    }

    /// Verify that the configured token is actually accepted.
    ///
    /// Sends a syntactically valid but empty batch: it mutates nothing on the
    /// gateway yet still exercises authentication and the ingest-enabled gate.
    pub async fn probe_auth(&self, client_id: &str) -> Result<AuthProbe, TransportError> {
        let payload = serde_json::json!({
            "schema_version": SCHEMA_VERSION,
            "client_id": client_id,
            "events": [],
        });
        match self
            .send_json::<BatchOutcome>(
                reqwest::Method::POST,
                &self.config.batch_endpoint(),
                Some(payload),
            )
            .await
        {
            Ok(_) => Ok(AuthProbe::Ok),
            Err(TransportError::Disabled(_)) => Ok(AuthProbe::Disabled),
            Err(TransportError::Unauthorized(message)) => Ok(AuthProbe::Rejected(message)),
            Err(other) => Err(other),
        }
    }

    /// Gateway ingest counters (`GET /api/conversations/ingest/stats`).
    pub async fn ingest_stats(&self) -> Result<serde_json::Value, TransportError> {
        self.send_json::<serde_json::Value>(
            reqwest::Method::GET,
            &self.config.ingest_stats_endpoint(),
            None,
        )
        .await
    }
}
