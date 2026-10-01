//! Deterministic identity derivation -- the Rust mirror of
//! `src/research_memory_gateway/ingest/identity.py`.
//!
//! These two implementations MUST agree byte-for-byte, because they decide
//! whether a snapshot message and the hook event that captured the same message
//! collapse into one archived message.  `tests/contract_parity.rs` asserts
//! agreement against a fixture that is generated from the Python side, and the
//! Python suite asserts the same fixture from the other direction.
//!
//! Every hash input starts with a versioned domain separator so distinct
//! purposes can never share a hash space.

use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;

pub const EVENT_ID_DOMAIN: &str = "rmg-bridge-event-v1";
pub const CONTENT_IDENTITY_DOMAIN: &str = "rmg-bridge-content-v1";
pub const SESSION_KEY_DOMAIN: &str = "rmg-bridge-session-v1";
pub const EVENT_CONTENT_DOMAIN: &str = "rmg-bridge-event-content-v1";
pub const EVENT_ID_PREFIX: &str = "rmb1";

/// Bumped only when the derivation inputs change meaning; it is part of the
/// hash input so a bump can never silently reinterpret old ids.
pub const DERIVATION_VERSION: i64 = 1;

pub fn sha256_hex(payload: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(payload.as_bytes());
    hex::encode(hasher.finalize())
}

/// Python's `str.isspace()` is slightly wider than Rust's `char::is_whitespace`
/// (it also covers U+001C..U+001F).  Matching it exactly keeps the two
/// implementations from disagreeing on control-character-laden payloads.
#[inline]
fn is_py_space(ch: char) -> bool {
    ch.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&ch)
}

/// Python-compatible `str.strip()`.
pub fn py_trim(value: &str) -> &str {
    value.trim_matches(is_py_space)
}

/// Python-compatible `str.rstrip()`.
pub fn py_rtrim(value: &str) -> &str {
    value.trim_end_matches(is_py_space)
}

/// Conservative, versioned text normalization.
///
/// Deliberately conservative: Unicode NFC, CRLF/CR -> LF, per-line trailing
/// whitespace removal, whole-text edge trim.  No case folding, no whitespace
/// collapsing, no punctuation stripping -- scientific numbers, formulas and
/// molecule names must never become collapse-equal.
pub fn normalize_text(value: &str) -> String {
    let normalized: String = value.nfc().collect();
    let unified = normalized.replace("\r\n", "\n").replace('\r', "\n");
    let joined: String = unified
        .split('\n')
        .map(py_rtrim)
        .collect::<Vec<_>>()
        .join("\n");
    py_trim(&joined).to_string()
}

/// Stable content identity: hash of the normalized content only.
pub fn content_identity(content: &str) -> String {
    sha256_hex(&[CONTENT_IDENTITY_DOMAIN, &normalize_text(content)].join("\0"))
}

/// Fingerprint of a stored event, used by the gateway to detect
/// `event_id` conflicts (same id, different content).
pub fn event_content_hash(event_type: &str, role: &str, content: &str) -> String {
    sha256_hex(
        &[
            EVENT_CONTENT_DOMAIN,
            py_trim(event_type),
            &py_trim(role).to_lowercase(),
            &content_identity(content),
        ]
        .join("\0"),
    )
}

/// Inputs of [`derive_event_id`], grouped so call sites cannot transpose them.
#[derive(Debug, Clone, Default)]
pub struct EventIdInputs<'a> {
    pub schema_version: i64,
    pub source_system: &'a str,
    pub source_account_namespace: &'a str,
    pub conversation_id: &'a str,
    pub thread_id: &'a str,
    pub branch_id: &'a str,
    pub message_id: &'a str,
    pub turn_id: &'a str,
    pub event_type: &'a str,
    pub content: &'a str,
}

/// Derive the stable `event_id` for an event that has no provider-supplied id.
///
/// `message_id` is preferred; `turn_id` is the documented fallback.  If both are
/// empty the content identity alone anchors the event, which is stable across
/// replays but collapses two byte-identical consecutive messages in one turn --
/// adapters should therefore always pass a provider id when one exists.
pub fn derive_event_id(input: &EventIdInputs<'_>) -> String {
    let anchor = {
        let message = py_trim(input.message_id);
        if message.is_empty() {
            py_trim(input.turn_id).to_string()
        } else {
            message.to_string()
        }
    };
    let payload = [
        EVENT_ID_DOMAIN.to_string(),
        DERIVATION_VERSION.to_string(),
        input.schema_version.to_string(),
        py_trim(input.source_system).to_string(),
        py_trim(input.source_account_namespace).to_string(),
        py_trim(input.conversation_id).to_string(),
        py_trim(input.thread_id).to_string(),
        py_trim(input.branch_id).to_string(),
        anchor,
        py_trim(input.event_type).to_string(),
        content_identity(input.content),
    ]
    .join("\0");
    format!("{EVENT_ID_PREFIX}_{}", sha256_hex(&payload))
}

/// Identity of one conversation thread, independent of any single event.
pub fn session_key(
    source_system: &str,
    source_account_namespace: &str,
    session_id: &str,
    conversation_id: &str,
    thread_id: &str,
    branch_id: &str,
) -> String {
    sha256_hex(
        &[
            SESSION_KEY_DOMAIN.to_string(),
            py_trim(source_system).to_lowercase(),
            py_trim(source_account_namespace).to_string(),
            py_trim(session_id).to_string(),
            py_trim(conversation_id).to_string(),
            py_trim(thread_id).to_string(),
            py_trim(branch_id).to_string(),
        ]
        .join("\0"),
    )
}

/// Account namespace label -> deterministic hash.
///
/// Mirrors `identity.account_namespace_hash`: only the hash is ever persisted,
/// never a raw account email or token.
pub fn account_namespace_hash(source_system: &str, namespace_label: &str) -> String {
    sha256_hex(
        &[
            "rmg-account-ns-v1".to_string(),
            py_trim(source_system).to_lowercase(),
            py_trim(namespace_label).to_string(),
        ]
        .join("\0"),
    )
}

/// Default Codex namespace, matching the gateway's legacy Codex library so
/// hook-captured Codex events group with previously imported sessions.
pub fn codex_default_namespace() -> String {
    account_namespace_hash("codex", "legacy-default-v1")
}

/// Truncate on a character boundary (never splits a UTF-8 sequence).
pub fn truncate_chars(value: &str, limit: usize) -> String {
    if value.chars().count() <= limit {
        return value.to_string();
    }
    value.chars().take(limit).collect()
}

/// RFC3339 UTC timestamp for "now", without an external date dependency.
pub fn now_rfc3339() -> String {
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    format_rfc3339(seconds)
}

/// Format a Unix timestamp (seconds) as `YYYY-MM-DDTHH:MM:SS+00:00`.
pub fn format_rfc3339(seconds: i64) -> String {
    let days = seconds.div_euclid(86_400);
    let secs_of_day = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let hour = secs_of_day / 3600;
    let minute = (secs_of_day % 3600) / 60;
    let second = secs_of_day % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}+00:00")
}

/// Howard Hinnant's `civil_from_days` (proleptic Gregorian, days since epoch).
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let mut year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    if month <= 2 {
        year += 1;
    }
    (year, month, day)
}

/// Parse a small subset of RFC3339 into epoch seconds (used only for cursor and
/// retention arithmetic; opaque provider timestamps are never parsed).
pub fn parse_rfc3339(value: &str) -> Option<i64> {
    let bytes = value.as_bytes();
    if bytes.len() < 19 {
        return None;
    }
    let year: i64 = value.get(0..4)?.parse().ok()?;
    let month: i64 = value.get(5..7)?.parse().ok()?;
    let day: i64 = value.get(8..10)?.parse().ok()?;
    let hour: i64 = value.get(11..13)?.parse().ok()?;
    let minute: i64 = value.get(14..16)?.parse().ok()?;
    let second: i64 = value.get(17..19)?.parse().ok()?;
    Some(days_from_civil(year, month, day) * 86_400 + hour * 3600 + minute * 60 + second)
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = if month > 2 { month - 3 } else { month + 9 };
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Best-effort RFC3339 from a provider epoch value (seconds or milliseconds).
pub fn timestamp_from_epoch(value: &serde_json::Value) -> String {
    let raw = match value {
        serde_json::Value::Number(number) => number.as_f64(),
        serde_json::Value::String(text) => text.parse::<f64>().ok(),
        _ => None,
    };
    let Some(raw) = raw else {
        return String::new();
    };
    let seconds = if raw.abs() > 100_000_000_000.0 {
        (raw / 1000.0) as i64
    } else {
        raw as i64
    };
    format_rfc3339(seconds)
}
