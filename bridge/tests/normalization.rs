//! Text normalization and time helpers.

use research_memory_bridge::normalize::{
    format_rfc3339, normalize_text, parse_rfc3339, py_trim, timestamp_from_epoch, truncate_chars,
};

#[test]
fn normalization_is_conservative() {
    // CRLF -> LF, per-line trailing whitespace removed, edges trimmed.
    assert_eq!(normalize_text("a\r\nb   \r\nc  "), "a\nb\nc");
    // Interior whitespace and case are preserved: scientific content must never
    // become collapse-equal.
    assert_eq!(normalize_text("Fe3+  10  mM"), "Fe3+  10  mM");
    assert_ne!(normalize_text("NaCl"), normalize_text("nacl"));
}

#[test]
fn normalization_applies_unicode_nfc() {
    let decomposed = "e\u{0301}";
    let composed = "\u{00e9}";
    assert_ne!(decomposed, composed);
    assert_eq!(normalize_text(decomposed), normalize_text(composed));
}

#[test]
fn normalization_keeps_cjk_and_emoji_intact() {
    let text = "之前 Fe 的硝酸溶液怎么配的？🧪";
    assert_eq!(normalize_text(text), text);
}

#[test]
fn normalization_matches_python_whitespace_set() {
    // U+001C..U+001F are whitespace for Python's str.strip but not for Rust's
    // char::is_whitespace; the bridge must match Python so ids agree.
    assert_eq!(py_trim("\u{1c}\u{1d}value\u{1e}\u{1f}"), "value");
    assert_eq!(normalize_text("\u{1c}value\u{1f}"), "value");
}

#[test]
fn truncate_respects_char_boundaries() {
    let text = "中文内容";
    assert_eq!(truncate_chars(text, 2), "中文");
    assert_eq!(truncate_chars(text, 99), text);
}

#[test]
fn rfc3339_formatting_round_trips() {
    assert_eq!(format_rfc3339(0), "1970-01-01T00:00:00+00:00");
    assert_eq!(format_rfc3339(1_000_000_000), "2001-09-09T01:46:40+00:00");
    let sample = "2026-09-30T10:11:12+00:00";
    let epoch = parse_rfc3339(sample).expect("parse");
    assert_eq!(format_rfc3339(epoch), sample);
}

#[test]
fn epoch_conversion_handles_milliseconds() {
    let seconds = serde_json::json!(1_000_000_000_i64);
    let millis = serde_json::json!(1_000_000_000_000_i64);
    let text = serde_json::json!("1000000000");
    assert_eq!(timestamp_from_epoch(&seconds), "2001-09-09T01:46:40+00:00");
    assert_eq!(timestamp_from_epoch(&millis), "2001-09-09T01:46:40+00:00");
    assert_eq!(timestamp_from_epoch(&text), "2001-09-09T01:46:40+00:00");
    assert_eq!(timestamp_from_epoch(&serde_json::Value::Null), "");
}
