//! Client-side secret redaction.
//!
//! Defence in depth: the gateway runs a *second*, independent sanitizer before
//! anything reaches the archive.  Neither layer trusts the other.
//!
//! ```text
//! client sanitizer -> network -> server sanitizer -> archive
//! ```
//!
//! The rule that matters most here is *precision*.  A research memory gateway is
//! full of numbers, formulas and molecule names (`Fe(NO3)3`, `0.1 M HNO3`,
//! `IC50 = 12.5 uM`); an over-eager sanitizer that eats those would destroy the
//! very content the archive exists to keep.  Every pattern therefore requires a
//! secret-looking *label* or a high-entropy *token shape*, and the labeled-value
//! path additionally runs the same plausibility gate as the Python sanitizer.
//!
//! Patterns are kept in lock-step with
//! `src/research_memory_gateway/secret_scan.py`.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::{Map, Value};

pub const REDACTED: &str = "[REDACTED]";
pub const REDACTED_KEY: &str = "[REDACTED_KEY]";

/// Words that look like a label but never carry a secret value.
const NON_SECRET_WORDS: &[&str] = &[
    "configured",
    "enabled",
    "disabled",
    "missing",
    "unset",
    "required",
    "optional",
    "present",
    "available",
];

/// Key names whose values are always redacted in structured metadata.
const SENSITIVE_KEY_MARKERS: &[&str] = &[
    "password",
    "passwd",
    "pwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "authorization",
    "cookie",
    "session_key",
    "session_token",
    "connection_string",
    "private_key",
    "secret_access_key",
];

fn re_private_key() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        Regex::new(
            r"(?is)-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
        )
        .expect("private key regex")
    })
}

fn re_jwt() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        Regex::new(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
            .expect("jwt regex")
    })
}

fn re_github_pat() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"\bgithub_pat_[A-Za-z0-9_]{12,}\b").expect("gh pat regex"))
}

fn re_github_token() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"\bgh[pousr]_[A-Za-z0-9]{12,}\b").expect("gh token regex"))
}

fn re_openai_style() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"(?i)\bsk-[A-Za-z0-9_-]{10,}\b").expect("sk regex"))
}

fn re_aws_key() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b").expect("aws regex"))
}

fn re_bearer() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        Regex::new(r"(?i)\b(?P<label>bearer)\s+(?P<value>[A-Za-z0-9._~+/=-]{8,})")
            .expect("bearer regex")
    })
}

fn re_url_userinfo() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        Regex::new(
            r"(?P<prefix>[A-Za-z][A-Za-z0-9+.-]*://[^:/\s]+:)(?P<value>[^@\s/]+)(?P<suffix>@)",
        )
        .expect("url userinfo regex")
    })
}

/// Labeled assignments: `api_key = ...`, `password: "..."`, `token ...`.
///
/// Python uses a backreference to require matching quotes; the Rust `regex`
/// crate has none, so the three quoting shapes are written as explicit
/// alternatives producing the identical replacement.
fn re_labeled() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        Regex::new(
            r#"(?i)(?P<label>\b(?:api[ _-]?(?:key|token)|auth(?:entication)?[ _-]?token|token|access[ _-]?token|refresh[ _-]?token|password|passwd|pwd|secret[ _-]?access[ _-]?key|secret|authorization|cookie|session(?:[ _-]?(?:id|token|key))?|connection[ _-]?string|private[ _-]?key|aws[ _-]?secret[ _-]?access[ _-]?key)\b)(?P<sep>\s*(?:(?:is|=|:|：)\s*)?)(?:(?P<dq>")(?P<dv>[^\s,;，；"']{6,})"|(?P<sq>')(?P<sv>[^\s,;，；"']{6,})'|(?P<bv>[^\s,;，；"']{6,}))"#,
        )
        .expect("labeled regex")
    })
}

/// One redaction finding, for reporting (never contains the secret itself).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finding {
    pub kind: String,
    pub path: String,
}

/// Aggregated redaction outcome.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RedactionReport {
    pub findings: Vec<Finding>,
}

impl RedactionReport {
    pub fn detected(&self) -> bool {
        !self.findings.is_empty()
    }

    pub fn count(&self) -> usize {
        self.findings.len()
    }

    pub fn kinds(&self) -> Vec<String> {
        let mut kinds: Vec<String> = self.findings.iter().map(|f| f.kind.clone()).collect();
        kinds.sort();
        kinds.dedup();
        kinds
    }

    pub fn add(&mut self, kind: &str, path: &str) {
        self.findings.push(Finding {
            kind: kind.to_string(),
            path: path.to_string(),
        });
    }

    pub fn merge(&mut self, other: RedactionReport) {
        self.findings.extend(other.findings);
    }
}

/// Redact every secret-shaped token in `value`.
pub fn redact_text(value: &str, path: &str) -> (String, RedactionReport) {
    let mut report = RedactionReport::default();
    let mut text = value.to_string();

    let mut replace_all = |pattern: &Regex, kind: &str, text: &mut String| {
        if !pattern.is_match(text) {
            return;
        }
        report.add(kind, path);
        *text = pattern.replace_all(text, REDACTED).to_string();
    };

    replace_all(re_private_key(), "private_key", &mut text);
    replace_all(re_jwt(), "jwt", &mut text);
    replace_all(re_github_pat(), "github_pat", &mut text);
    replace_all(re_github_token(), "github_token", &mut text);
    replace_all(re_openai_style(), "openai_style_key", &mut text);
    replace_all(re_aws_key(), "aws_access_key", &mut text);

    if re_bearer().is_match(&text) {
        report.add("bearer_token", path);
        text = re_bearer()
            .replace_all(&text, "${label} [REDACTED]")
            .to_string();
    }

    if re_url_userinfo().is_match(&text) {
        report.add("url_password", path);
        text = re_url_userinfo()
            .replace_all(&text, "${prefix}[REDACTED]${suffix}")
            .to_string();
    }

    if re_labeled().is_match(&text) {
        let mut hits = 0usize;
        let replaced = re_labeled()
            .replace_all(&text, |caps: &regex::Captures<'_>| {
                let label = caps.name("label").map(|m| m.as_str()).unwrap_or_default();
                let sep = caps.name("sep").map(|m| m.as_str()).unwrap_or_default();
                let value = caps
                    .name("dv")
                    .or_else(|| caps.name("sv"))
                    .or_else(|| caps.name("bv"))
                    .map(|m| m.as_str())
                    .unwrap_or_default();
                if !looks_secret_value(value, sep) {
                    return caps
                        .get(0)
                        .map(|m| m.as_str().to_string())
                        .unwrap_or_default();
                }
                hits += 1;
                format!("{label}{sep}{REDACTED}")
            })
            .to_string();
        if hits > 0 {
            report.add("labeled_secret", path);
            text = replaced;
        }
    }

    (text, report)
}

/// Plausibility gate for labeled values, mirroring the Python sanitizer.
///
/// Prevents `secret 12` / `token enabled` style false positives while still
/// catching `password = hunter2xyz`.
fn looks_secret_value(value: &str, separator: &str) -> bool {
    let lowered = value.to_lowercase();
    if NON_SECRET_WORDS.contains(&lowered.as_str()) {
        return false;
    }
    if separator.contains('=') || separator.contains(':') || separator.contains('：') {
        return true;
    }
    if separator
        .to_lowercase()
        .split_whitespace()
        .any(|token| token == "is")
    {
        return true;
    }
    let char_count = value.chars().count();
    if char_count >= 16 {
        return true;
    }
    let has_digit = value.chars().any(|c| c.is_ascii_digit() || c.is_numeric());
    let has_non_alnum = value.chars().any(|c| !c.is_alphanumeric());
    has_digit && (has_non_alnum || char_count >= 8)
}

fn is_sensitive_key(key: &str) -> bool {
    let lowered = key.to_lowercase().replace(['-', ' '], "_");
    SENSITIVE_KEY_MARKERS
        .iter()
        .any(|marker| lowered.contains(marker))
}

/// Recursively redact a JSON value (used for event metadata).
pub fn redact_json(value: &Value, path: &str) -> (Value, RedactionReport) {
    let mut report = RedactionReport::default();
    let redacted = redact_value(value, path, &mut report);
    (redacted, report)
}

fn redact_value(value: &Value, path: &str, report: &mut RedactionReport) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            for (key, item) in map {
                let (clean_key, key_report) = redact_text(key, &format!("{path}.[key]"));
                report.merge(key_report);
                let child_path = format!("{path}.{clean_key}");
                if is_sensitive_key(key) && !is_emptyish(item) {
                    out.insert(clean_key, Value::String(REDACTED.to_string()));
                    report.add("sensitive_field", &child_path);
                } else {
                    out.insert(clean_key, redact_value(item, &child_path, report));
                }
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(
            items
                .iter()
                .enumerate()
                .map(|(index, item)| redact_value(item, &format!("{path}[{index}]"), report))
                .collect(),
        ),
        Value::String(text) => {
            let (clean, nested) = redact_text(text, path);
            report.merge(nested);
            Value::String(clean)
        }
        other => other.clone(),
    }
}

fn is_emptyish(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::String(text) => text.is_empty() || text == REDACTED,
        _ => false,
    }
}
