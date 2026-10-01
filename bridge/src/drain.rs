//! Drain loop: spool -> gateway, with batching, retry and dead-lettering.
//!
//! Two entry points:
//!
//! * [`drain_once`] -- one bounded pass, used by the detached short drain that
//!   `capture` spawns and by `drain --once`;
//! * [`drain_daemon`] -- a long-lived loop for hosts that prefer a resident
//!   worker over per-hook processes.
//!
//! Neither busy-loops.  When there is nothing due, the daemon sleeps until the
//! earliest scheduled retry (bounded by `drain_interval_seconds`), so an offline
//! gateway costs one wake-up per interval rather than a spin.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use tracing::{debug, error, info, warn};

use crate::config::BridgeConfig;
use crate::event::NormalizedEvent;
use crate::spool::{is_permanent_rejection, Spool, SpoolRow};
use crate::transport::{BatchOutcome, Transport, TransportError};

/// One pass summary, also used for `status`/`doctor` reporting.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DrainReport {
    pub batches: usize,
    pub accepted: usize,
    pub duplicates: usize,
    pub rejected_permanent: usize,
    pub rescheduled: usize,
    pub dead_lettered: usize,
    pub reclaimed: usize,
    pub pruned: usize,
    pub parked: usize,
    pub sessions_reconciled: usize,
    pub last_error: String,
    pub stopped_reason: String,
}

impl DrainReport {
    pub fn did_work(&self) -> bool {
        self.batches > 0 || self.reclaimed > 0 || self.pruned > 0 || self.sessions_reconciled > 0
    }

    pub fn acked(&self) -> usize {
        self.accepted + self.duplicates
    }
}

/// Run one bounded drain pass.
pub async fn drain_once(
    config: &BridgeConfig,
    spool: &Spool,
    transport: &Transport,
    max_batches: usize,
) -> DrainReport {
    let mut report = DrainReport::default();

    match spool.reclaim_stale_inflight(120) {
        Ok(count) => report.reclaimed = count,
        Err(err) => warn!("cannot reclaim stale inflight rows: {err}"),
    }
    match spool.prune_acked(config.spool_retention_days) {
        Ok(count) => report.pruned = count,
        Err(err) => warn!("cannot prune acked rows: {err}"),
    }
    match spool.enforce_pending_cap(config.max_pending_events) {
        Ok(count) if count > 0 => {
            warn!(
                "pending spool cap ({} events) exceeded; parked {count} oldest events in dead_letter",
                config.max_pending_events
            );
            report.parked = count;
        }
        Ok(_) => {}
        Err(err) => warn!("cannot enforce pending cap: {err}"),
    }

    let limit = max_batches.max(1);
    for _ in 0..limit {
        let rows = match spool.claim_batch(config.batch_size) {
            Ok(rows) => rows,
            Err(err) => {
                report.last_error = format!("claim_batch failed: {err}");
                error!("{}", report.last_error);
                break;
            }
        };
        if rows.is_empty() {
            report.stopped_reason = "spool_empty".to_string();
            break;
        }
        let events: Vec<NormalizedEvent> = rows.iter().map(|row| row.event.clone()).collect();
        report.batches += 1;
        match transport.post_batch(&events, &config.client_id).await {
            Ok(outcome) => {
                apply_outcome(config, spool, &rows, &outcome, &mut report);
            }
            Err(err) => {
                report.last_error = err.to_string();
                handle_transport_error(config, spool, &rows, &err, &mut report);
                if !err.retryable() {
                    break;
                }
            }
        }
    }
    if report.stopped_reason.is_empty() {
        report.stopped_reason = "max_batches".to_string();
    }

    reconcile_sessions(config, spool, transport, &mut report).await;

    if report.did_work() {
        let _ = spool.set_meta("last_drain_at", &crate::spool::now_rfc3339());
        let _ = spool.set_meta(
            "last_drain_summary",
            &format!(
                "batches={} acked={} dead_lettered={} rescheduled={}",
                report.batches,
                report.acked(),
                report.dead_lettered,
                report.rescheduled
            ),
        );
    }
    report
}

fn apply_outcome(
    config: &BridgeConfig,
    spool: &Spool,
    rows: &[SpoolRow],
    outcome: &BatchOutcome,
    report: &mut DrainReport,
) {
    let attempts: HashMap<&str, i64> = rows
        .iter()
        .map(|row| (row.event_id.as_str(), row.attempts))
        .collect();

    let acked = outcome.acked();
    if let Err(err) = spool.ack(&acked) {
        warn!("cannot ACK uploaded events: {err}");
    }
    report.accepted += outcome.accepted.len();
    report.duplicates += outcome.duplicates.len();

    let now = crate::spool::now_epoch();
    for rejected in &outcome.rejected {
        if rejected.event_id.is_empty() {
            // A batch-level rejection (bad version, oversized batch).  Nothing
            // was accepted, so the whole batch must be retried or parked.
            report.rejected_permanent += 1;
            continue;
        }
        let ids = vec![rejected.event_id.clone()];
        if is_permanent_rejection(&rejected.code) {
            report.rejected_permanent += 1;
            if let Err(err) =
                spool.dead_letter(&ids, &format!("{}: {}", rejected.code, rejected.message))
            {
                warn!("cannot dead-letter rejected event: {err}");
            }
            report.dead_lettered += 1;
        } else {
            let attempt = attempts
                .get(rejected.event_id.as_str())
                .copied()
                .unwrap_or(0) as u32
                + 1;
            let retry_at = now + config.backoff_seconds(attempt, &rejected.event_id) as i64;
            if let Err(err) = spool.reschedule(
                &ids,
                retry_at,
                &format!("{}: {}", rejected.code, rejected.message),
            ) {
                warn!("cannot reschedule rejected event: {err}");
            }
            report.rescheduled += 1;
        }
    }

    // Safety net: an event the gateway neither accepted, duplicated nor rejected
    // must not be left inflight forever.
    let mut mentioned: std::collections::HashSet<&str> = std::collections::HashSet::new();
    for id in outcome.accepted.iter().chain(outcome.duplicates.iter()) {
        mentioned.insert(id.as_str());
    }
    for rejected in &outcome.rejected {
        mentioned.insert(rejected.event_id.as_str());
    }
    let unaccounted: Vec<String> = rows
        .iter()
        .filter(|row| !mentioned.contains(row.event_id.as_str()))
        .map(|row| row.event_id.clone())
        .collect();
    if !unaccounted.is_empty() {
        warn!(
            "gateway response did not mention {} uploaded event(s); rescheduling",
            unaccounted.len()
        );
        let retry_at = now + config.base_backoff_seconds as i64;
        let _ = spool.reschedule(&unaccounted, retry_at, "unaccounted_in_response");
        report.rescheduled += unaccounted.len();
    }
}

fn handle_transport_error(
    config: &BridgeConfig,
    spool: &Spool,
    rows: &[SpoolRow],
    error: &TransportError,
    report: &mut DrainReport,
) {
    let now = crate::spool::now_epoch();
    let mut retry: Vec<String> = Vec::new();
    let mut park: Vec<String> = Vec::new();
    for row in rows {
        let attempt = (row.attempts + 1) as u32;
        if !error.retryable() || attempt >= config.max_attempts {
            park.push(row.event_id.clone());
        } else {
            retry.push(row.event_id.clone());
        }
    }
    if !retry.is_empty() {
        // Group by computed delay is unnecessary: each event carries its own
        // jittered deadline, so use the maximum to stay conservative.
        let delay = retry
            .iter()
            .map(|id| config.backoff_seconds(1, id))
            .max()
            .unwrap_or(config.base_backoff_seconds);
        if let Err(err) = spool.reschedule(&retry, now + delay as i64, &error.to_string()) {
            warn!("cannot reschedule batch after transport error: {err}");
        }
        report.rescheduled += retry.len();
    }
    if !park.is_empty() {
        if let Err(err) = spool.dead_letter(&park, &error.to_string()) {
            warn!("cannot dead-letter batch after transport error: {err}");
        }
        report.dead_lettered += park.len();
        error!(
            "parked {} event(s) in dead_letter after {} attempt(s): {error}",
            park.len(),
            config.max_attempts
        );
    }
    let _ = spool.set_meta("last_error", &error.to_string());
    let _ = spool.set_meta("last_error_at", &crate::spool::now_rfc3339());
}

async fn reconcile_sessions(
    config: &BridgeConfig,
    spool: &Spool,
    transport: &Transport,
    report: &mut DrainReport,
) {
    let pending = match spool.unreconciled_sessions(50) {
        Ok(items) => items,
        Err(err) => {
            warn!("cannot list unreconciled sessions: {err}");
            return;
        }
    };
    for session in pending {
        let payload = serde_json::json!({
            "schema_version": crate::event::SCHEMA_VERSION,
            "client_id": config.client_id,
            "source_system": session.source_system,
            "session_id": session.session_id,
            "conversation_id": session.conversation_id,
            "last_message_id": session.last_message_id,
            "observed_message_count": session.observed_message_count,
            "ended_at": crate::spool::now_rfc3339(),
        });
        match transport.post_session_end(&payload).await {
            Ok(result) => {
                if result
                    .get("reconciliation_required")
                    .and_then(|value| value.as_bool())
                    .unwrap_or(false)
                {
                    warn!(
                        "gateway reports missing events for session {} (missing={})",
                        session.session_id,
                        result
                            .get("missing_event_count")
                            .and_then(|value| value.as_i64())
                            .unwrap_or(0)
                    );
                }
                let _ = spool.mark_session_reconciled(&session.source_system, &session.session_id);
                report.sessions_reconciled += 1;
            }
            Err(err) => {
                debug!("session-end declaration deferred: {err}");
                break;
            }
        }
    }
}

/// Long-lived drain loop.  Sleeps between passes; never busy-loops.
pub async fn drain_daemon(
    config: Arc<BridgeConfig>,
    spool: Arc<Spool>,
    transport: Arc<Transport>,
) -> DrainReport {
    let mut total = DrainReport::default();
    loop {
        let report = drain_once(&config, &spool, &transport, 50).await;
        total.batches += report.batches;
        total.accepted += report.accepted;
        total.duplicates += report.duplicates;
        total.dead_lettered += report.dead_lettered;
        total.rescheduled += report.rescheduled;
        total.sessions_reconciled += report.sessions_reconciled;
        if !report.last_error.is_empty() {
            total.last_error = report.last_error.clone();
        }

        let sleep_seconds = if report.did_work() {
            1
        } else {
            let now = crate::spool::now_epoch();
            spool
                .next_retry_epoch()
                .ok()
                .flatten()
                .map(|at| (at - now).clamp(1, config.drain_interval_seconds as i64))
                .unwrap_or(config.drain_interval_seconds as i64)
        };
        info!("drain pass complete; sleeping {sleep_seconds}s");
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(sleep_seconds.max(1) as u64)) => {}
            _ = tokio::signal::ctrl_c() => {
                info!("drain daemon interrupted; exiting");
                return total;
            }
        }
    }
}
