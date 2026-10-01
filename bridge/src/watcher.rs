//! Transcript reconciliation framework.
//!
//! The realtime hooks are the *low-latency* path; transcripts are the
//! *eventual-consistency* path.  Both feed the same spool, so an event captured
//! by both is rejected by the spool's primary key rather than archived twice.
//!
//! The framework is deliberately generic -- adding an agent means implementing
//! [`TranscriptSource`], not touching the watcher:
//!
//! ```text
//! discover()  -> candidate files
//! cursor_get() -> byte offset already consumed
//! parse()     -> (new events, new offset)   // always lands on a line boundary
//! enqueue_many() + cursor_set()             // one transaction per file
//! ```
//!
//! Both shipped sources are written against transcript formats that were
//! verified against real files on disk:
//!
//! * **Codex** -- `~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl`, the record
//!   shape already parsed by this repository's `CodexExportReader`;
//! * **Claude Code** -- `~/.claude/projects/<slug>/<session-id>.jsonl`, whose
//!   records carry `type`/`uuid`/`promptId`/`sessionId`/`message`.
//!
//! A partially written trailing line is never consumed: the cursor only advances
//! past the last newline, so an in-flight write is re-read on the next pass
//! instead of being lost.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use tracing::{debug, info, warn};

use crate::adapters::{claude_code, codex, AgentKind};
use crate::config::BridgeConfig;
use crate::event::NormalizedEvent;
use crate::normalize::{derive_event_id, EventIdInputs};
use crate::spool::{EnqueueOutcome, Spool};

/// A transcript format the watcher can reconcile.
pub trait TranscriptSource: Send + Sync {
    fn source_system(&self) -> &'static str;

    /// Candidate transcript files, oldest first.
    fn discover(&self, config: &BridgeConfig) -> Vec<PathBuf>;

    /// Parse the bytes after `offset`, returning new events and the new offset.
    fn parse(
        &self,
        path: &Path,
        offset: i64,
        config: &BridgeConfig,
    ) -> Result<(Vec<NormalizedEvent>, i64)>;
}

pub struct CodexTranscriptSource;

impl TranscriptSource for CodexTranscriptSource {
    fn source_system(&self) -> &'static str {
        codex::SOURCE_SYSTEM
    }

    fn discover(&self, config: &BridgeConfig) -> Vec<PathBuf> {
        codex::discover_rollouts(&config.codex_sessions_root())
    }

    fn parse(
        &self,
        path: &Path,
        offset: i64,
        config: &BridgeConfig,
    ) -> Result<(Vec<NormalizedEvent>, i64)> {
        codex::parse_rollout(path, offset, config)
    }
}

pub struct ClaudeTranscriptSource;

impl TranscriptSource for ClaudeTranscriptSource {
    fn source_system(&self) -> &'static str {
        claude_code::SOURCE_SYSTEM
    }

    fn discover(&self, config: &BridgeConfig) -> Vec<PathBuf> {
        claude_code::discover_transcripts(&config.claude_projects_root())
    }

    fn parse(
        &self,
        path: &Path,
        offset: i64,
        config: &BridgeConfig,
    ) -> Result<(Vec<NormalizedEvent>, i64)> {
        claude_code::parse_transcript(path, offset, config)
    }
}

pub fn source_for(agent: AgentKind) -> Option<Box<dyn TranscriptSource>> {
    match agent {
        AgentKind::Codex => Some(Box::new(CodexTranscriptSource)),
        AgentKind::ClaudeCode => Some(Box::new(ClaudeTranscriptSource)),
        AgentKind::Generic => None,
    }
}

/// Fill in `event_id` for events produced by a transcript parse or a hook
/// adapter.  Centralised so no adapter can accidentally enqueue an event whose
/// id would change between runs (which would break idempotency).
pub fn ensure_event_ids(events: &mut [NormalizedEvent]) {
    for event in events.iter_mut() {
        if event.event_id.len() >= 8 {
            continue;
        }
        event.event_id = derive_event_id(&EventIdInputs {
            schema_version: event.schema_version,
            source_system: &event.source_system,
            source_account_namespace: &event.source_account_namespace,
            conversation_id: &event.conversation_id,
            thread_id: &event.thread_id,
            branch_id: &event.branch_id,
            message_id: &event.message_id,
            turn_id: &event.turn_id,
            event_type: &event.event_type,
            content: &event.content,
        });
    }
}

/// Summary of one reconciliation pass.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WatchReport {
    pub files_scanned: usize,
    pub files_advanced: usize,
    pub events_parsed: usize,
    pub events_inserted: usize,
    pub events_duplicate: usize,
    pub errors: usize,
}

/// Run one reconciliation pass over every discovered transcript file.
pub fn watch_once(
    source: &dyn TranscriptSource,
    config: &BridgeConfig,
    spool: &Spool,
) -> Result<WatchReport> {
    let mut report = WatchReport::default();
    let files = source.discover(config);
    report.files_scanned = files.len();
    for path in files {
        let key = path.display().to_string();
        let offset = match spool.cursor_get(source.source_system(), &key) {
            Ok(offset) => offset,
            Err(err) => {
                warn!("cannot read cursor for {key}: {err}");
                report.errors += 1;
                continue;
            }
        };
        let (mut events, new_offset) = match source.parse(&path, offset, config) {
            Ok(result) => result,
            Err(err) => {
                debug!("cannot parse transcript {key}: {err}");
                report.errors += 1;
                continue;
            }
        };
        if new_offset == offset {
            continue;
        }
        ensure_event_ids(&mut events);
        events.retain(|event| event.validate().is_ok());
        report.events_parsed += events.len();
        if !events.is_empty() {
            match spool.enqueue_many(&events) {
                Ok(outcomes) => {
                    for outcome in outcomes {
                        match outcome {
                            EnqueueOutcome::Inserted => report.events_inserted += 1,
                            EnqueueOutcome::Duplicate => report.events_duplicate += 1,
                        }
                    }
                }
                Err(err) => {
                    warn!("cannot spool transcript events from {key}: {err}");
                    report.errors += 1;
                    // Do not advance the cursor: the events must not be skipped.
                    continue;
                }
            }
        }
        if let Err(err) = spool.cursor_set(source.source_system(), &key, new_offset) {
            warn!("cannot persist cursor for {key}: {err}");
            report.errors += 1;
            continue;
        }
        report.files_advanced += 1;
    }
    Ok(report)
}

/// Long-lived reconciliation loop.
pub async fn watch_loop(
    source: Arc<Box<dyn TranscriptSource>>,
    config: Arc<BridgeConfig>,
    spool: Arc<Spool>,
) {
    loop {
        match watch_once(source.as_ref().as_ref(), &config, &spool) {
            Ok(report) => {
                if report.events_inserted > 0 || report.errors > 0 {
                    info!(
                        "reconciliation: files={} advanced={} inserted={} duplicates={} errors={}",
                        report.files_scanned,
                        report.files_advanced,
                        report.events_inserted,
                        report.events_duplicate,
                        report.errors
                    );
                }
            }
            Err(err) => warn!("reconciliation pass failed: {err}"),
        }
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(config.watch_interval_seconds.max(2))) => {}
            _ = tokio::signal::ctrl_c() => {
                info!("watch interrupted; exiting");
                return;
            }
        }
    }
}

/// Explicit session finalisation for agents that cannot emit `SessionEnd`.
///
/// Codex's `notify` has no SessionEnd, so a turn completion must never be
/// mistaken for one.  This is the documented fallback: it records the observed
/// message count locally and marks the session for session-end reconciliation
/// on the next drain, without touching the transcript.
pub fn finalize_session(
    spool: &Spool,
    source_system: &str,
    session_id: &str,
    conversation_id: &str,
    last_message_id: &str,
    observed_message_count: i64,
) -> Result<()> {
    spool.session_upsert(
        source_system,
        session_id,
        conversation_id,
        last_message_id,
        observed_message_count,
    )?;
    spool.session_mark_ended(source_system, session_id)?;
    Ok(())
}
