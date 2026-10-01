//! Durable local spool.
//!
//! The spool is what makes `capture` safe to run from a lifecycle hook: the
//! hook only has to commit one SQLite transaction, and everything after that
//! (network, retry, batching) happens in a different process.
//!
//! Guarantees:
//!
//! * **WAL + busy timeout** -- a detached drain and a concurrent hook can both
//!   touch the file without lock storms;
//! * **`PRIMARY KEY (event_id)`** -- replaying a hook can only ever produce a
//!   `Duplicate`, never a second copy of the message;
//! * **`inflight` is recoverable** -- a crash mid-upload leaves rows in
//!   `inflight`, which [`Spool::reclaim_stale_inflight`] returns to `pending`;
//! * **bounded** -- ACKed rows are pruned by retention and the pending set is
//!   capped, so the spool cannot grow without limit;
//! * **no busy-loop** -- every retry gets a `next_retry_at`, and callers sleep
//!   until the earliest one instead of spinning.

use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};

use crate::event::{EventType, NormalizedEvent, Role};

pub const STATUS_PENDING: &str = "pending";
pub const STATUS_INFLIGHT: &str = "inflight";
pub const STATUS_ACKED: &str = "acked";
pub const STATUS_DEAD_LETTER: &str = "dead_letter";

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS events (
    event_id                 TEXT PRIMARY KEY,
    schema_version           INTEGER NOT NULL,
    source_system            TEXT NOT NULL,
    source_account_namespace TEXT NOT NULL DEFAULT '',
    session_id               TEXT NOT NULL DEFAULT '',
    conversation_id          TEXT NOT NULL DEFAULT '',
    thread_id                TEXT NOT NULL DEFAULT '',
    branch_id                TEXT NOT NULL DEFAULT '',
    message_id               TEXT NOT NULL DEFAULT '',
    turn_id                  TEXT NOT NULL DEFAULT '',
    event_type               TEXT NOT NULL,
    role                     TEXT NOT NULL DEFAULT '',
    content                  TEXT NOT NULL DEFAULT '',
    timestamp                TEXT NOT NULL DEFAULT '',
    metadata_json            TEXT NOT NULL DEFAULT '{}',
    created_at               TEXT NOT NULL,
    status                   TEXT NOT NULL,
    attempts                 INTEGER NOT NULL DEFAULT 0,
    next_retry_at            INTEGER NOT NULL DEFAULT 0,
    last_error               TEXT NOT NULL DEFAULT '',
    inflight_at              INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_spool_status ON events(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_spool_thread
    ON events(source_system, conversation_id, thread_id, branch_id);
CREATE INDEX IF NOT EXISTS idx_spool_created ON events(created_at);

CREATE TABLE IF NOT EXISTS cursors (
    source_system TEXT NOT NULL,
    source_path   TEXT NOT NULL,
    cursor        INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (source_system, source_path)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id             TEXT NOT NULL,
    source_system          TEXT NOT NULL,
    conversation_id        TEXT NOT NULL DEFAULT '',
    last_message_id        TEXT NOT NULL DEFAULT '',
    observed_message_count INTEGER NOT NULL DEFAULT 0,
    ended                  INTEGER NOT NULL DEFAULT 0,
    updated_at             TEXT NOT NULL,
    PRIMARY KEY (source_system, session_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"#;

/// Outcome of a spool insert.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EnqueueOutcome {
    Inserted,
    Duplicate,
}

/// One spooled event as claimed for upload.
#[derive(Debug, Clone)]
pub struct SpoolRow {
    pub event_id: String,
    pub attempts: i64,
    pub event: NormalizedEvent,
}

/// Spool counters for `status` / `doctor`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SpoolCounts {
    pub pending: i64,
    pub inflight: i64,
    pub acked: i64,
    pub dead_letter: i64,
}

impl SpoolCounts {
    pub fn outstanding(&self) -> i64 {
        self.pending + self.inflight
    }

    pub fn total(&self) -> i64 {
        self.pending + self.inflight + self.acked + self.dead_letter
    }
}

/// A session the bridge believes has finished but never reconciled.
#[derive(Debug, Clone)]
pub struct PendingSession {
    pub source_system: String,
    pub session_id: String,
    pub conversation_id: String,
    pub last_message_id: String,
    pub observed_message_count: i64,
}

pub fn now_epoch() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

pub fn now_rfc3339() -> String {
    crate::normalize::now_rfc3339()
}

/// SQLite-backed durable spool.  Cheap to construct; connections are opened per
/// operation so the type is `Send + Sync` and safe to share with tokio tasks.
#[derive(Debug, Clone)]
pub struct Spool {
    path: PathBuf,
    busy_timeout_ms: u64,
}

impl Spool {
    pub fn open(path: &Path) -> Result<Spool> {
        let spool = Spool {
            path: path.to_path_buf(),
            busy_timeout_ms: 5000,
        };
        spool.initialize()?;
        Ok(spool)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn connect(&self) -> Result<Connection> {
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent).ok();
        }
        let conn = Connection::open(&self.path)
            .with_context(|| format!("cannot open spool {}", self.path.display()))?;
        conn.busy_timeout(std::time::Duration::from_millis(self.busy_timeout_ms))?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        Ok(conn)
    }

    pub fn initialize(&self) -> Result<()> {
        let conn = self.connect()?;
        conn.execute_batch(SCHEMA)
            .context("cannot create spool schema")?;
        Ok(())
    }

    // -- capture hot path ----------------------------------------------------

    /// Insert one event.  Returns `Duplicate` when the id is already spooled.
    pub fn enqueue(&self, event: &NormalizedEvent) -> Result<EnqueueOutcome> {
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let outcome = insert_event(&tx, event)?;
        tx.commit()?;
        Ok(outcome)
    }

    /// Insert a batch of events in one transaction (used by `watch`/snapshot).
    pub fn enqueue_many(&self, events: &[NormalizedEvent]) -> Result<Vec<EnqueueOutcome>> {
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut outcomes = Vec::with_capacity(events.len());
        for event in events {
            outcomes.push(insert_event(&tx, event)?);
        }
        tx.commit()?;
        Ok(outcomes)
    }

    // -- drain path ----------------------------------------------------------

    /// Atomically claim up to `limit` due events and mark them `inflight`.
    pub fn claim_batch(&self, limit: usize) -> Result<Vec<SpoolRow>> {
        if limit == 0 {
            return Ok(Vec::new());
        }
        let now = now_epoch();
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let rows: Vec<SpoolRow> = {
            let mut stmt = tx.prepare(
                "SELECT event_id, schema_version, source_system, source_account_namespace,
                        session_id, conversation_id, thread_id, branch_id, message_id,
                        turn_id, event_type, role, content, timestamp, metadata_json, attempts
                 FROM events
                 WHERE status = ?1 AND next_retry_at <= ?2
                 ORDER BY created_at, rowid
                 LIMIT ?3",
            )?;
            let mapped = stmt.query_map(params![STATUS_PENDING, now, limit as i64], |row| {
                let metadata_json: String = row.get(14)?;
                let metadata = serde_json::from_str(&metadata_json)
                    .unwrap_or_else(|_| serde_json::Value::Object(serde_json::Map::new()));
                Ok(SpoolRow {
                    event_id: row.get(0)?,
                    attempts: row.get(15)?,
                    event: NormalizedEvent {
                        event_id: row.get(0)?,
                        schema_version: row.get(1)?,
                        source_system: row.get(2)?,
                        source_account_namespace: row.get(3)?,
                        session_id: row.get(4)?,
                        conversation_id: row.get(5)?,
                        thread_id: row.get(6)?,
                        branch_id: row.get(7)?,
                        message_id: row.get(8)?,
                        turn_id: row.get(9)?,
                        event_type: row.get(10)?,
                        role: row.get(11)?,
                        content: row.get(12)?,
                        timestamp: row.get(13)?,
                        metadata,
                    },
                })
            })?;
            mapped.collect::<rusqlite::Result<Vec<_>>>()?
        };
        for row in &rows {
            tx.execute(
                "UPDATE events SET status = ?1, attempts = attempts + 1, inflight_at = ?2
                 WHERE event_id = ?3",
                params![STATUS_INFLIGHT, now, row.event_id],
            )?;
        }
        tx.commit()?;
        Ok(rows)
    }

    /// Mark events as successfully uploaded.  `duplicates` count as success:
    /// the gateway already has them, so re-sending would be pointless.
    pub fn ack(&self, event_ids: &[String]) -> Result<usize> {
        if event_ids.is_empty() {
            return Ok(0);
        }
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut updated = 0usize;
        {
            let mut stmt = tx.prepare(
                "UPDATE events SET status = ?1, last_error = '', inflight_at = 0 WHERE event_id = ?2",
            )?;
            for event_id in event_ids {
                updated += stmt.execute(params![STATUS_ACKED, event_id])?;
            }
        }
        tx.commit()?;
        Ok(updated)
    }

    /// Return events to `pending` with a retry deadline, or park them.
    pub fn reschedule(&self, event_ids: &[String], retry_at: i64, error: &str) -> Result<usize> {
        if event_ids.is_empty() {
            return Ok(0);
        }
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut updated = 0usize;
        {
            let mut stmt = tx.prepare(
                "UPDATE events SET status = ?1, next_retry_at = ?2, last_error = ?3, inflight_at = 0
                 WHERE event_id = ?4",
            )?;
            for event_id in event_ids {
                updated += stmt.execute(params![STATUS_PENDING, retry_at, error, event_id])?;
            }
        }
        tx.commit()?;
        Ok(updated)
    }

    /// Park events permanently; they stay inspectable but are never retried.
    pub fn dead_letter(&self, event_ids: &[String], error: &str) -> Result<usize> {
        if event_ids.is_empty() {
            return Ok(0);
        }
        let mut conn = self.connect()?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut updated = 0usize;
        {
            let mut stmt = tx.prepare(
                "UPDATE events SET status = ?1, last_error = ?2, inflight_at = 0 WHERE event_id = ?3",
            )?;
            for event_id in event_ids {
                updated += stmt.execute(params![STATUS_DEAD_LETTER, error, event_id])?;
            }
        }
        tx.commit()?;
        Ok(updated)
    }

    /// Recover rows left `inflight` by a crashed drain.
    ///
    /// `older_than_seconds` of `0` treats every inflight row as stale, which is
    /// what tests and explicit recovery runs want; the drain daemon uses a
    /// generous window so it never steals work from a concurrent upload.
    pub fn reclaim_stale_inflight(&self, older_than_seconds: i64) -> Result<usize> {
        let cutoff = now_epoch() - older_than_seconds.max(0);
        let conn = self.connect()?;
        let updated = conn.execute(
            "UPDATE events SET status = ?1, inflight_at = 0,
                    last_error = CASE WHEN last_error = '' THEN 'reclaimed_after_crash' ELSE last_error END
             WHERE status = ?2 AND inflight_at <= ?3",
            params![STATUS_PENDING, STATUS_INFLIGHT, cutoff],
        )?;
        Ok(updated)
    }

    /// Attempts recorded for one event (0 when unknown).
    pub fn attempts_for(&self, event_id: &str) -> Result<i64> {
        let conn = self.connect()?;
        let attempts = conn
            .query_row(
                "SELECT attempts FROM events WHERE event_id = ?1",
                params![event_id],
                |row| row.get::<_, i64>(0),
            )
            .optional()?;
        Ok(attempts.unwrap_or(0))
    }

    // -- observability -------------------------------------------------------

    pub fn counts(&self) -> Result<SpoolCounts> {
        let conn = self.connect()?;
        let mut counts = SpoolCounts::default();
        let mut stmt = conn.prepare("SELECT status, COUNT(*) FROM events GROUP BY status")?;
        let rows = stmt.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })?;
        for row in rows {
            let (status, count) = row?;
            match status.as_str() {
                STATUS_PENDING => counts.pending = count,
                STATUS_INFLIGHT => counts.inflight = count,
                STATUS_ACKED => counts.acked = count,
                STATUS_DEAD_LETTER => counts.dead_letter = count,
                _ => {}
            }
        }
        Ok(counts)
    }

    /// Epoch seconds of the oldest not-yet-uploaded event.
    pub fn oldest_pending_epoch(&self) -> Result<Option<i64>> {
        let conn = self.connect()?;
        let value = conn
            .query_row(
                "SELECT MIN(created_at) FROM events WHERE status IN (?1, ?2)",
                params![STATUS_PENDING, STATUS_INFLIGHT],
                |row| row.get::<_, Option<String>>(0),
            )
            .optional()?
            .flatten();
        Ok(value.as_deref().and_then(crate::normalize::parse_rfc3339))
    }

    /// Epoch seconds of the earliest scheduled retry, for sleep scheduling.
    pub fn next_retry_epoch(&self) -> Result<Option<i64>> {
        let conn = self.connect()?;
        let value = conn
            .query_row(
                "SELECT MIN(next_retry_at) FROM events WHERE status = ?1 AND next_retry_at > 0",
                params![STATUS_PENDING],
                |row| row.get::<_, Option<i64>>(0),
            )
            .optional()?
            .flatten();
        Ok(value)
    }

    pub fn has_due_pending(&self) -> Result<bool> {
        let conn = self.connect()?;
        let count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM events WHERE status = ?1 AND next_retry_at <= ?2",
            params![STATUS_PENDING, now_epoch()],
            |row| row.get(0),
        )?;
        Ok(count > 0)
    }

    pub fn dead_letter_sample(&self, limit: usize) -> Result<Vec<(String, String, i64)>> {
        let conn = self.connect()?;
        let mut stmt = conn.prepare(
            "SELECT event_id, last_error, attempts FROM events WHERE status = ?1
             ORDER BY created_at DESC LIMIT ?2",
        )?;
        let rows = stmt.query_map(params![STATUS_DEAD_LETTER, limit as i64], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })?;
        Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
    }

    pub fn set_meta(&self, key: &str, value: &str) -> Result<()> {
        let conn = self.connect()?;
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?1, ?2)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![key, value],
        )?;
        Ok(())
    }

    pub fn get_meta(&self, key: &str) -> Result<Option<String>> {
        let conn = self.connect()?;
        let value = conn
            .query_row(
                "SELECT value FROM meta WHERE key = ?1",
                params![key],
                |row| row.get::<_, String>(0),
            )
            .optional()?;
        Ok(value)
    }

    // -- cursors (transcript reconciliation) ---------------------------------

    pub fn cursor_get(&self, source_system: &str, source_path: &str) -> Result<i64> {
        let conn = self.connect()?;
        let value = conn
            .query_row(
                "SELECT cursor FROM cursors WHERE source_system = ?1 AND source_path = ?2",
                params![source_system, source_path],
                |row| row.get::<_, i64>(0),
            )
            .optional()?;
        Ok(value.unwrap_or(0))
    }

    pub fn cursor_set(&self, source_system: &str, source_path: &str, cursor: i64) -> Result<()> {
        let conn = self.connect()?;
        conn.execute(
            "INSERT INTO cursors(source_system, source_path, cursor, updated_at)
             VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(source_system, source_path) DO UPDATE SET
                 cursor = excluded.cursor, updated_at = excluded.updated_at",
            params![source_system, source_path, cursor, now_rfc3339()],
        )?;
        Ok(())
    }

    pub fn cursor_count(&self) -> Result<i64> {
        let conn = self.connect()?;
        Ok(conn.query_row("SELECT COUNT(*) FROM cursors", [], |row| row.get(0))?)
    }

    // -- sessions ------------------------------------------------------------

    pub fn session_upsert(
        &self,
        source_system: &str,
        session_id: &str,
        conversation_id: &str,
        last_message_id: &str,
        observed_message_count: i64,
    ) -> Result<()> {
        let conn = self.connect()?;
        conn.execute(
            "INSERT INTO sessions(session_id, source_system, conversation_id, last_message_id,
                                  observed_message_count, ended, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, 0, ?6)
             ON CONFLICT(source_system, session_id) DO UPDATE SET
                 conversation_id = CASE WHEN excluded.conversation_id != ''
                                        THEN excluded.conversation_id
                                        ELSE sessions.conversation_id END,
                 last_message_id = CASE WHEN excluded.last_message_id != ''
                                        THEN excluded.last_message_id
                                        ELSE sessions.last_message_id END,
                 observed_message_count = MAX(sessions.observed_message_count,
                                              excluded.observed_message_count),
                 ended = 0,
                 updated_at = excluded.updated_at",
            params![
                session_id,
                source_system,
                conversation_id,
                last_message_id,
                observed_message_count,
                now_rfc3339()
            ],
        )?;
        Ok(())
    }

    pub fn session_mark_ended(&self, source_system: &str, session_id: &str) -> Result<()> {
        let conn = self.connect()?;
        conn.execute(
            "UPDATE sessions SET ended = 1, updated_at = ?3
             WHERE source_system = ?1 AND session_id = ?2",
            params![source_system, session_id, now_rfc3339()],
        )?;
        Ok(())
    }

    /// Message events currently spooled for one session.
    ///
    /// Used instead of a client-side counter so the observed count reported at
    /// session end is exactly what the bridge actually holds -- an honest number
    /// the gateway can compare against its own stored count.
    pub fn count_session_messages(&self, source_system: &str, session_id: &str) -> Result<i64> {
        let conn = self.connect()?;
        let count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM events
             WHERE source_system = ?1 AND session_id = ?2
               AND event_type IN ('user_prompt', 'assistant_message')",
            params![source_system, session_id],
            |row| row.get(0),
        )?;
        Ok(count)
    }

    /// Newest message id spooled for one session (for session-end reporting).
    pub fn last_session_message_id(&self, source_system: &str, session_id: &str) -> Result<String> {
        let conn = self.connect()?;
        let value = conn
            .query_row(
                "SELECT message_id FROM events
                 WHERE source_system = ?1 AND session_id = ?2 AND message_id != ''
                 ORDER BY created_at DESC, rowid DESC LIMIT 1",
                params![source_system, session_id],
                |row| row.get::<_, String>(0),
            )
            .optional()?;
        Ok(value.unwrap_or_default())
    }

    pub fn session_count(&self) -> Result<i64> {
        let conn = self.connect()?;
        Ok(conn.query_row("SELECT COUNT(*) FROM sessions", [], |row| row.get(0))?)
    }

    /// Sessions that ended but have not been declared to the gateway yet.
    pub fn unreconciled_sessions(&self, limit: usize) -> Result<Vec<PendingSession>> {
        let conn = self.connect()?;
        let mut stmt = conn.prepare(
            "SELECT source_system, session_id, conversation_id, last_message_id,
                    observed_message_count
             FROM sessions
             WHERE ended = 1
               AND COALESCE((SELECT value FROM meta WHERE key = 'session_end:' || source_system || ':' || session_id), '') = ''
             ORDER BY updated_at
             LIMIT ?1",
        )?;
        let rows = stmt.query_map(params![limit as i64], |row| {
            Ok(PendingSession {
                source_system: row.get(0)?,
                session_id: row.get(1)?,
                conversation_id: row.get(2)?,
                last_message_id: row.get(3)?,
                observed_message_count: row.get(4)?,
            })
        })?;
        Ok(rows.collect::<rusqlite::Result<Vec<_>>>()?)
    }

    pub fn mark_session_reconciled(&self, source_system: &str, session_id: &str) -> Result<()> {
        self.set_meta(
            &format!("session_end:{source_system}:{session_id}"),
            &now_rfc3339(),
        )
    }

    // -- bounds --------------------------------------------------------------

    /// Delete ACKed rows older than `retention_days`.
    pub fn prune_acked(&self, retention_days: u32) -> Result<usize> {
        if retention_days == 0 {
            return Ok(0);
        }
        let cutoff =
            crate::normalize::format_rfc3339(now_epoch() - (retention_days as i64) * 86_400);
        let conn = self.connect()?;
        let deleted = conn.execute(
            "DELETE FROM events WHERE status = ?1 AND created_at < ?2",
            params![STATUS_ACKED, cutoff],
        )?;
        Ok(deleted)
    }

    /// Keep the pending set bounded: the oldest rows beyond `max_pending` are
    /// parked in `dead_letter` (never silently dropped) so the spool cannot grow
    /// without limit when the gateway is unreachable for a long time.
    pub fn enforce_pending_cap(&self, max_pending: usize) -> Result<usize> {
        if max_pending == 0 {
            return Ok(0);
        }
        let conn = self.connect()?;
        let pending: i64 = conn.query_row(
            "SELECT COUNT(*) FROM events WHERE status = ?1",
            params![STATUS_PENDING],
            |row| row.get(0),
        )?;
        let excess = pending - max_pending as i64;
        if excess <= 0 {
            return Ok(0);
        }
        let parked = conn.execute(
            "UPDATE events SET status = ?1, last_error = 'pending_cap_exceeded'
             WHERE event_id IN (
                 SELECT event_id FROM events WHERE status = ?2
                 ORDER BY created_at, rowid LIMIT ?3
             )",
            params![STATUS_DEAD_LETTER, STATUS_PENDING, excess],
        )?;
        Ok(parked)
    }

    /// Drop cursor rows whose transcript file no longer exists.
    pub fn prune_missing_cursors(&self, source_system: &str) -> Result<usize> {
        let conn = self.connect()?;
        let mut stmt = conn.prepare("SELECT source_path FROM cursors WHERE source_system = ?1")?;
        let paths = stmt
            .query_map(params![source_system], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        drop(stmt);
        let mut removed = 0usize;
        for path in paths {
            if !Path::new(&path).exists() {
                removed += conn.execute(
                    "DELETE FROM cursors WHERE source_system = ?1 AND source_path = ?2",
                    params![source_system, path],
                )?;
            }
        }
        Ok(removed)
    }
}

fn insert_event(tx: &rusqlite::Transaction<'_>, event: &NormalizedEvent) -> Result<EnqueueOutcome> {
    let exists: Option<String> = tx
        .query_row(
            "SELECT event_id FROM events WHERE event_id = ?1",
            params![event.event_id],
            |row| row.get(0),
        )
        .optional()?;
    if exists.is_some() {
        return Ok(EnqueueOutcome::Duplicate);
    }
    let metadata_json = serde_json::to_string(&event.metadata).unwrap_or_else(|_| "{}".to_string());
    tx.execute(
        "INSERT INTO events(
            event_id, schema_version, source_system, source_account_namespace,
            session_id, conversation_id, thread_id, branch_id, message_id, turn_id,
            event_type, role, content, timestamp, metadata_json, created_at,
            status, attempts, next_retry_at, last_error, inflight_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15,
                   ?16, ?17, 0, 0, '', 0)",
        params![
            event.event_id,
            event.schema_version,
            event.source_system,
            event.source_account_namespace,
            event.session_id,
            event.conversation_id,
            event.thread_id,
            event.branch_id,
            event.message_id,
            event.turn_id,
            event.event_type,
            event.role,
            event.content,
            event.timestamp,
            metadata_json,
            now_rfc3339(),
            STATUS_PENDING,
        ],
    )?;
    Ok(EnqueueOutcome::Inserted)
}

/// True when a rejection code will never succeed on retry.
pub fn is_permanent_rejection(code: &str) -> bool {
    matches!(
        code,
        "unsupported_schema_version"
            | "invalid_payload"
            | "missing_required_field"
            | "invalid_event_type"
            | "invalid_role"
            | "content_too_large"
            | "metadata_too_large"
            | "event_id_conflict"
            | "session_id_mismatch"
            | "ingest_disabled"
    )
}

/// Guard against adapters emitting a role/type combination the schema rejects.
pub fn role_for_event_type(event_type: EventType) -> Role {
    Role::from_event_type(event_type)
}
