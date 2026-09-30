//! Hook installation: idempotency, preservation of existing configuration,
//! backups, and uninstall that removes only what the bridge added.

mod common;

use common::TestHome;
use research_memory_bridge::adapters::{claude_code, codex, MANAGED_FLAG};

const CODEX_CONFIG: &str = r#"# Codex configuration -- user comments must survive hook installation
model = "gpt-6.1-sol"
model_reasoning_effort = "xhigh"

# Turn-ended notification for another tool
notify = ["C:\\tools\\other-notifier.exe", "turn-ended"]

[model_providers.custom]
name = "custom"
base_url = "https://example.invalid/v1"
"#;

const CODEX_CONFIG_NO_NOTIFY: &str = r#"# no notify key yet
model = "gpt-6.1-sol"

[mcp_servers.codegraph]
command = "cmd"
"#;

const CLAUDE_SETTINGS: &str = r#"{
  "model": "opus[1m]",
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {"type": "command", "command": "/usr/local/bin/audit.sh", "args": []}
        ]
      }
    ]
  },
  "permissions": {"allow": ["Bash(ls)"]}
}
"#;

// ---------------------------------------------------------------------------
// Codex: surgical TOML editing
// ---------------------------------------------------------------------------

#[test]
fn codex_install_preserves_comments_and_other_keys() {
    let home = TestHome::new();
    let path = home.path().join("config.toml");
    std::fs::write(&path, CODEX_CONFIG).expect("seed config");

    let report = codex::install_hooks_at(&path, home.path(), true).expect("install");
    assert!(report.changed);
    assert!(report.backup_path.is_some(), "a backup must be taken");

    let updated = std::fs::read_to_string(&path).expect("read");
    assert!(updated.contains("# Codex configuration"), "{updated}");
    assert!(updated.contains("# Turn-ended notification"), "{updated}");
    assert!(updated.contains("model_reasoning_effort"), "{updated}");
    assert!(updated.contains("[model_providers.custom]"), "{updated}");
    assert!(updated.contains(MANAGED_FLAG), "{updated}");
    assert!(!updated.contains("other-notifier.exe"), "{updated}");

    // The notify value must still be a valid single-line TOML array.
    let parsed = codex::read_root_array(&updated, "notify").expect("parse notify");
    assert!(parsed.iter().any(|item| item == "capture"));
    assert!(parsed.iter().any(|item| item == "codex"));
}

#[test]
fn codex_install_is_idempotent() {
    let home = TestHome::new();
    let path = home.path().join("config.toml");
    std::fs::write(&path, CODEX_CONFIG).expect("seed config");
    codex::install_hooks_at(&path, home.path(), true).expect("install");
    let first = std::fs::read_to_string(&path).expect("read");

    let second = codex::install_hooks_at(&path, home.path(), true).expect("install again");
    assert!(!second.changed, "a second install must be a no-op");
    assert_eq!(first, std::fs::read_to_string(&path).expect("read"));
    let notify = codex::read_root_array(&first, "notify").expect("notify");
    assert_eq!(
        notify.iter().filter(|item| *item == "capture").count(),
        1,
        "the hook must not be duplicated: {notify:?}"
    );
}

#[test]
fn codex_install_refuses_to_clobber_a_foreign_notify_without_force() {
    let home = TestHome::new();
    let path = home.path().join("config.toml");
    std::fs::write(&path, CODEX_CONFIG).expect("seed config");

    let report = codex::install_hooks_at(&path, home.path(), false).expect("install");
    assert!(!report.changed);
    assert!(report.message.contains("--force"), "{}", report.message);
    let unchanged = std::fs::read_to_string(&path).expect("read");
    assert_eq!(unchanged, CODEX_CONFIG, "config must be untouched");
}

#[test]
fn codex_install_appends_notify_when_the_key_is_absent() {
    let home = TestHome::new();
    let path = home.path().join("config.toml");
    std::fs::write(&path, CODEX_CONFIG_NO_NOTIFY).expect("seed config");
    let report = codex::install_hooks_at(&path, home.path(), false).expect("install");
    assert!(report.changed);
    let updated = std::fs::read_to_string(&path).expect("read");
    assert!(updated.contains("# no notify key yet"), "{updated}");
    assert!(updated.contains("notify = ["), "{updated}");
    assert!(codex::read_root_array(&updated, "notify").is_some());
}

#[test]
fn codex_uninstall_restores_the_previous_notify() {
    let home = TestHome::new();
    let path = home.path().join("config.toml");
    std::fs::write(&path, CODEX_CONFIG).expect("seed config");
    codex::install_hooks_at(&path, home.path(), true).expect("install");

    let report = codex::uninstall_hooks_at(&path, home.path()).expect("uninstall");
    assert!(report.changed);
    let restored = std::fs::read_to_string(&path).expect("read");
    assert!(restored.contains("other-notifier.exe"), "{restored}");
    assert!(!restored.contains(MANAGED_FLAG), "{restored}");
    assert!(restored.contains("# Codex configuration"), "{restored}");
    let notify = codex::read_root_array(&restored, "notify").expect("notify");
    assert_eq!(notify.len(), 2);
}

#[test]
fn codex_uninstall_removes_the_key_when_it_did_not_exist() {
    let home = TestHome::new();
    let path = home.path().join("config.toml");
    std::fs::write(&path, CODEX_CONFIG_NO_NOTIFY).expect("seed config");
    codex::install_hooks_at(&path, home.path(), false).expect("install");
    codex::uninstall_hooks_at(&path, home.path()).expect("uninstall");
    let restored = std::fs::read_to_string(&path).expect("read");
    // The seed comment literally contains the word "notify", so assert on the
    // parsed key rather than a substring match.
    assert!(
        codex::read_root_array(&restored, "notify").is_none(),
        "notify key should have been removed:\n{restored}"
    );
    assert!(restored.contains("# no notify key yet"), "{restored}");
    assert!(restored.contains("[mcp_servers.codegraph]"), "{restored}");
}

#[test]
fn codex_uninstall_leaves_a_foreign_notify_alone() {
    let home = TestHome::new();
    let path = home.path().join("config.toml");
    std::fs::write(&path, CODEX_CONFIG).expect("seed config");
    let report = codex::uninstall_hooks_at(&path, home.path()).expect("uninstall");
    assert!(!report.changed);
    assert_eq!(std::fs::read_to_string(&path).expect("read"), CODEX_CONFIG);
}

#[test]
fn toml_helpers_handle_multiline_arrays_and_string_escapes() {
    let text =
        "# header\nnotify = [\n  \"C:\\\\a b\\\\bridge.exe\",\n  \"capture\",\n]\nmodel = \"x\"\n";
    let parsed = codex::read_root_array(text, "notify").expect("parse");
    assert_eq!(parsed.len(), 2);
    assert_eq!(parsed[0], "C:\\a b\\bridge.exe");

    let (updated, replaced) = codex::upsert_root_array(text, "notify", "[\"new\"]");
    assert!(replaced);
    assert!(updated.contains("# header"));
    assert!(updated.contains("model = \"x\""));
    assert!(updated.contains("notify = [\"new\"]"));

    let removed = codex::remove_root_key(text, "notify");
    assert!(!removed.contains("notify"));
    assert!(removed.contains("model = \"x\""));
}

#[test]
fn toml_helpers_ignore_keys_inside_tables() {
    // `notify` nested in a table must not be mistaken for the root key.
    let text = "[some_table]\nnotify = [\"inner\"]\n";
    assert!(codex::read_root_array(text, "notify").is_none());
    let (updated, replaced) = codex::upsert_root_array(text, "notify", "[\"outer\"]");
    assert!(!replaced);
    assert_eq!(updated, text);
}

#[test]
fn inserting_a_root_key_places_it_before_the_first_table() {
    let text = "# comment\nmodel = \"gpt\"\n\n[tui]\ntheme = \"dark\"\n";
    let updated = codex::insert_root_key(text, "notify", "[\"x\"]");
    let notify_at = updated.find("notify = [\"x\"]").expect("inserted");
    let table_at = updated.find("[tui]").expect("table");
    assert!(
        notify_at < table_at,
        "a root key must precede the first table header:\n{updated}"
    );
    assert_eq!(
        codex::read_root_array(&updated, "notify").expect("parse"),
        vec!["x".to_string()]
    );

    // With no tables at all it simply appends.
    let updated = codex::insert_root_key("model = \"gpt\"\n", "notify", "[\"y\"]");
    assert_eq!(
        codex::read_root_array(&updated, "notify").expect("parse"),
        vec!["y".to_string()]
    );
}

// ---------------------------------------------------------------------------
// Claude Code: settings.json composition
// ---------------------------------------------------------------------------

#[test]
fn claude_install_preserves_existing_hooks_and_other_settings() {
    let home = TestHome::new();
    let path = home.path().join("settings.json");
    std::fs::write(&path, CLAUDE_SETTINGS).expect("seed settings");

    let report = claude_code::install_hooks_at(&path, home.path(), false, false).expect("install");
    assert!(report.changed);
    assert!(report.backup_path.is_some());

    let settings: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).expect("read")).expect("json");
    assert_eq!(settings["model"], "opus[1m]");
    assert_eq!(settings["permissions"]["allow"][0], "Bash(ls)");
    // Someone else's PreToolUse hook survives untouched.
    let pre = settings["hooks"]["PreToolUse"].as_array().expect("pre");
    assert!(pre
        .iter()
        .any(|group| group["hooks"][0]["command"] == "/usr/local/bin/audit.sh"));

    for event in ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"] {
        let groups = settings["hooks"][event].as_array().expect("groups");
        assert!(
            groups.iter().any(|group| {
                group["hooks"]
                    .as_array()
                    .map(|handlers| {
                        handlers.iter().any(|handler| {
                            handler["args"]
                                .as_array()
                                .map(|args| args.iter().any(|item| item == MANAGED_FLAG))
                                .unwrap_or(false)
                        })
                    })
                    .unwrap_or(false)
            }),
            "missing bridge hook for {event}"
        );
    }
    // Tool hooks are opt-in.
    assert!(settings["hooks"].get("PreToolUse").is_some());
    assert!(
        settings["hooks"]["PreToolUse"]
            .as_array()
            .expect("pre")
            .len()
            == 1,
        "tool hooks must not be installed without --with-tools"
    );
}

#[test]
fn claude_install_with_tools_adds_tool_events() {
    let home = TestHome::new();
    let path = home.path().join("settings.json");
    std::fs::write(&path, CLAUDE_SETTINGS).expect("seed settings");
    claude_code::install_hooks_at(&path, home.path(), false, true).expect("install");
    let settings: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).expect("read")).expect("json");
    for event in ["PreToolUse", "PostToolUse"] {
        assert!(!settings["hooks"][event]
            .as_array()
            .expect("groups")
            .is_empty());
    }
}

#[test]
fn claude_install_is_idempotent() {
    let home = TestHome::new();
    let path = home.path().join("settings.json");
    std::fs::write(&path, CLAUDE_SETTINGS).expect("seed settings");
    claude_code::install_hooks_at(&path, home.path(), false, false).expect("install");
    let first = std::fs::read_to_string(&path).expect("read");
    let second = claude_code::install_hooks_at(&path, home.path(), false, false).expect("install");
    assert!(!second.changed);
    assert_eq!(first, std::fs::read_to_string(&path).expect("read"));
    let settings: serde_json::Value = serde_json::from_str(&first).expect("json");
    assert_eq!(settings["hooks"]["Stop"].as_array().expect("stop").len(), 1);
}

#[test]
fn claude_uninstall_removes_only_our_hooks() {
    let home = TestHome::new();
    let path = home.path().join("settings.json");
    std::fs::write(&path, CLAUDE_SETTINGS).expect("seed settings");
    claude_code::install_hooks_at(&path, home.path(), false, true).expect("install");

    let report = claude_code::uninstall_hooks_at(&path, home.path()).expect("uninstall");
    assert!(report.changed);
    let settings: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).expect("read")).expect("json");
    let serialized = settings.to_string();
    assert!(!serialized.contains(MANAGED_FLAG), "{serialized}");
    // The user's own hook and every unrelated key survive.
    assert!(
        serialized.contains("/usr/local/bin/audit.sh"),
        "{serialized}"
    );
    assert_eq!(settings["model"], "opus[1m]");
    assert_eq!(settings["permissions"]["allow"][0], "Bash(ls)");
    assert!(settings["hooks"].get("SessionStart").is_none());
    assert!(settings["hooks"].get("Stop").is_none());
    assert!(settings["hooks"].get("PreToolUse").is_some());
}

#[test]
fn claude_uninstall_on_a_clean_settings_file_is_a_noop() {
    let home = TestHome::new();
    let path = home.path().join("settings.json");
    std::fs::write(&path, CLAUDE_SETTINGS).expect("seed settings");
    let report = claude_code::uninstall_hooks_at(&path, home.path()).expect("uninstall");
    assert!(!report.changed);
    assert_eq!(
        std::fs::read_to_string(&path).expect("read"),
        CLAUDE_SETTINGS
    );
}

#[test]
fn claude_install_creates_settings_when_absent() {
    let home = TestHome::new();
    let path = home.path().join("settings.json");
    let report = claude_code::install_hooks_at(&path, home.path(), false, false).expect("install");
    assert!(report.changed);
    assert!(path.exists());
    let settings: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&path).expect("read")).expect("json");
    assert!(settings["hooks"]["SessionEnd"].is_array());
}

#[test]
fn install_writes_a_backup_before_modifying() {
    let home = TestHome::new();
    let path = home.path().join("settings.json");
    std::fs::write(&path, CLAUDE_SETTINGS).expect("seed settings");
    let report = claude_code::install_hooks_at(&path, home.path(), false, false).expect("install");
    let backup = report.backup_path.expect("backup path");
    assert!(backup.exists(), "backup {} missing", backup.display());
    assert_eq!(
        std::fs::read_to_string(&backup).expect("read backup"),
        CLAUDE_SETTINGS
    );
    assert!(backup.starts_with(home.path().join("backups")));
}
