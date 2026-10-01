//! Secret redaction: coverage of the required secret classes, and -- just as
//! important -- proof that ordinary scientific content survives untouched.

use serde_json::json;

use research_memory_bridge::redact::{redact_json, redact_text, REDACTED};

fn redacted(input: &str) -> String {
    redact_text(input, "$.test").0
}

#[test]
fn redacts_authorization_headers_and_bearer_tokens() {
    let out = redacted("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345");
    assert!(!out.contains("abcdefghijklmnopqrstuvwxyz012345"), "{out}");
    assert!(out.contains(REDACTED), "{out}");

    let out = redacted("sent header bearer eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM");
    assert!(!out.contains("SflKxwRJSM"), "{out}");
}

#[test]
fn redacts_openai_and_anthropic_style_keys() {
    for key in [
        "sk-proj-abcdefghijklmnop123456",
        "sk-ant-api03-abcdefghijklmnop",
        "sk-abcdefghijklmnopqrst",
    ] {
        let out = redacted(&format!("the key is {key} in config"));
        assert!(!out.contains(key), "{out}");
        assert!(out.contains(REDACTED), "{out}");
    }
}

#[test]
fn redacts_github_tokens() {
    let pat = "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz";
    let classic = "ghp_abcdefghijklmnopqrstuvwxyz0123";
    let out = redacted(&format!("tokens {pat} and {classic}"));
    assert!(!out.contains("github_pat_11ABCDEFG"), "{out}");
    assert!(!out.contains("ghp_abcdefghijklmnop"), "{out}");
}

#[test]
fn redacts_aws_access_keys_and_private_keys() {
    let out = redacted("aws AKIAIOSFODNN7EXAMPLE and ASIAIOSFODNN7EXAMPLE");
    assert!(!out.contains("AKIAIOSFODNN7EXAMPLE"), "{out}");
    assert!(!out.contains("ASIAIOSFODNN7EXAMPLE"), "{out}");

    let pem =
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n-----END OPENSSH PRIVATE KEY-----";
    let out = redacted(&format!("key:\n{pem}\ndone"));
    assert!(!out.contains("b3BlbnNzaC1rZXk="), "{out}");
    assert!(out.contains(REDACTED));
}

#[test]
fn redacts_jwts() {
    let jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U";
    let out = redacted(&format!("session {jwt} end"));
    assert!(!out.contains("dozjgNryP4J3jVmNHl0w5N"), "{out}");
}

#[test]
fn redacts_secret_env_assignments_and_password_like_assignments() {
    for input in [
        "export API_KEY=abcdef123456",
        "ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnop",
        "password = hunter2xyz",
        "passwd: \"s3cr3t-p4ss\"",
        "PASSWORD=hunter2xyz",
        "api_token: 9f8e7d6c5b4a39281706",
        "access_token = eyJhbGciOi.payloadpart.sigpart",
        "secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    ] {
        let out = redacted(input);
        assert!(out.contains(REDACTED), "not redacted: {input} -> {out}");
    }
}

#[test]
fn redacts_cookies_session_tokens_and_connection_strings() {
    for input in [
        "cookie: sessionid=abcdef1234567890",
        "session_token=abcdefghijklmnopqrst",
        "connection_string=postgres://user:pass@host/db",
        "https://user:hunter2@example.com/path",
    ] {
        let out = redacted(input);
        assert!(out.contains(REDACTED), "not redacted: {input} -> {out}");
    }
}

#[test]
fn does_not_touch_ordinary_scientific_content() {
    // The whole point of the precision gate: numbers, formulas, molecule names
    // and lab values must survive intact.
    for input in [
        "Fe3+ 储备液浓度为 10 mM，介质为 0.1 M HNO3。",
        "IC50 = 12.5 µM",
        "pH = 7.4",
        "ΔG° = -23.4 kJ/mol",
        "NaCl 0.9% (w/v)",
        "Fe(NO3)3·9H2O 404.00 g/mol",
        "离心 12000 rpm，4 °C，10 min",
        "摩尔比 1:2:3",
        "token 12",
        "secret enabled",
        "The password policy requires 12 characters",
    ] {
        let out = redacted(input);
        assert_eq!(out, input, "over-eager redaction of {input:?} -> {out:?}");
    }
}

#[test]
fn does_not_redact_short_labeled_values() {
    let out = redacted("token abc");
    assert_eq!(out, "token abc");
    let out = redacted("secret 12");
    assert_eq!(out, "secret 12");
}

#[test]
fn redacts_sensitive_metadata_keys_and_reports_findings() {
    let value = json!({
        "api_key": "abcdef123456",
        "password": "hunter2xyz",
        "note": "Fe3+ stock 10 mM",
        "nested": {"session_token": "abcdefghijklmnop"},
        "list": ["Bearer abcdefghijklmnopqrst"]
    });
    let (cleaned, report) = redact_json(&value, "$.metadata");
    assert!(report.detected());
    assert!(report.count() >= 4, "{:?}", report.findings);
    assert_eq!(cleaned["api_key"], REDACTED);
    assert_eq!(cleaned["password"], REDACTED);
    assert_eq!(cleaned["nested"]["session_token"], REDACTED);
    assert_eq!(cleaned["note"], "Fe3+ stock 10 mM");
    let serialized = cleaned.to_string();
    assert!(!serialized.contains("hunter2xyz"), "{serialized}");
    assert!(!serialized.contains("abcdefghijklmnop"), "{serialized}");
    // Findings must never contain the secret itself.
    for finding in &report.findings {
        assert!(!finding.path.contains("hunter2xyz"));
        assert!(!finding.kind.contains("hunter2xyz"));
    }
}

#[test]
fn redaction_is_idempotent() {
    let input = "api_key=abcdef123456 and password: \"hunter2xyz\"";
    let once = redacted(input);
    let twice = redacted(&once);
    assert_eq!(once, twice, "redaction must be stable across passes");
}

#[test]
fn empty_and_plain_text_is_untouched() {
    assert_eq!(redacted(""), "");
    assert_eq!(redacted("just a normal sentence"), "just a normal sentence");
}
