//! `research-memory-bridge` command line interface.
//!
//! Command set:
//!
//! ```text
//! capture           hot path: spool one hook payload and exit   (fail-open)
//! drain             upload spooled events   (--once | --daemon)
//! watch             reconcile transcripts   (--once | loop)
//! status            local state, no network required
//! doctor            status + live server/auth/ingest checks
//! install-hooks     idempotent, backed-up hook installation
//! uninstall-hooks   removes only what install-hooks added
//! config            show effective config / `config init`
//! finalize-session  SessionEnd fallback for agents that cannot signal it
//! version
//! ```
//!
//! `capture` is intentionally synchronous: it must not pay for a tokio runtime
//! and must not wait for the network.  Everything else may spin one up.

use std::io::Read;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;

use anyhow::{anyhow, Result};
use clap::{Args, Parser, Subcommand};
use tracing::{debug, warn};

use research_memory_bridge::adapters::{self, AgentKind, PayloadSource};
use research_memory_bridge::config::{BridgeConfig, DEFAULT_TOKEN_ENV};
use research_memory_bridge::drain;
use research_memory_bridge::logging;
use research_memory_bridge::paths;
use research_memory_bridge::spool::Spool;
use research_memory_bridge::transport::{AuthProbe, Transport};
use research_memory_bridge::watcher;

#[derive(Parser, Debug)]
#[command(
    name = "research-memory-bridge",
    version,
    about = "Automatic conversation capture for research-memory-gateway",
    long_about = "Captures Codex / Claude Code lifecycle events into a durable local spool and \
                  uploads them to the research-memory-gateway HTTP ingest API.\n\n\
                  Writing: Agent -> bridge -> HTTP ingest -> gateway\n\
                  Reading: Agent -> Streamable HTTP MCP -> gateway"
)]
struct Cli {
    /// Override the bridge home directory (same as RESEARCH_MEMORY_BRIDGE_HOME).
    #[arg(long, global = true, value_name = "DIR")]
    home: Option<PathBuf>,

    /// Internal marker written into installed hook commands.
    ///
    /// It makes hook detection independent of the executable path (which
    /// contains spaces and backslashes on Windows and differs between the
    /// installed binary and a test harness), so `install-hooks` stays idempotent
    /// and `uninstall-hooks` can never remove someone else's hook.
    #[arg(long, global = true, hide = true)]
    bridge_managed: bool,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Spool one hook payload locally, then exit.  Never waits for the network.
    Capture(CaptureArgs),
    /// Upload spooled events to the gateway.
    Drain(DrainArgs),
    /// Reconcile agent transcripts into the spool.
    Watch(WatchArgs),
    /// Show local bridge state (no network access).
    Status(JsonArgs),
    /// Diagnose configuration, server reachability and authentication.
    Doctor(JsonArgs),
    /// Install lifecycle hooks for one agent.
    InstallHooks(InstallArgs),
    /// Remove the hooks installed by install-hooks.
    UninstallHooks(AgentArgs),
    /// Show or initialise the bridge configuration.
    Config(ConfigArgs),
    /// Record an explicit session end for agents with no SessionEnd hook.
    FinalizeSession(FinalizeArgs),
    /// Print the bridge version.
    Version,
}

#[derive(Args, Debug)]
struct CaptureArgs {
    /// Agent whose hook invoked us.
    #[arg(long, value_name = "AGENT")]
    agent: String,
    /// Provider event name (informational; adapters read the payload).
    #[arg(long, value_name = "EVENT")]
    event: Option<String>,
    /// Read the payload from stdin instead of a trailing argument.
    #[arg(long)]
    stdin: bool,
    /// Trailing payload argument, as appended by Codex's `notify`.
    #[arg(value_name = "PAYLOAD")]
    payload: Option<String>,
}

#[derive(Args, Debug)]
struct DrainArgs {
    /// Run a single bounded pass and exit.
    #[arg(long)]
    once: bool,
    /// Run until interrupted.
    #[arg(long)]
    daemon: bool,
    /// Maximum batches per pass.
    #[arg(long, default_value_t = 20)]
    max_batches: usize,
}

#[derive(Args, Debug)]
struct WatchArgs {
    #[arg(long, value_name = "AGENT")]
    agent: String,
    /// Run one reconciliation pass and exit.
    #[arg(long)]
    once: bool,
}

#[derive(Args, Debug)]
struct JsonArgs {
    /// Emit machine-readable JSON.
    #[arg(long)]
    json: bool,
}

#[derive(Args, Debug)]
struct AgentArgs {
    #[arg(long, value_name = "AGENT")]
    agent: String,
}

#[derive(Args, Debug)]
struct InstallArgs {
    #[arg(long, value_name = "AGENT")]
    agent: String,
    /// Replace a foreign Codex `notify` command (backed up, restorable).
    #[arg(long)]
    force: bool,
    /// Also install tool-lifecycle hooks.
    #[arg(long)]
    with_tools: bool,
    /// Show what would change without writing anything.
    #[arg(long)]
    dry_run: bool,
}

#[derive(Args, Debug)]
struct ConfigArgs {
    #[arg(long)]
    json: bool,
    #[command(subcommand)]
    action: Option<ConfigAction>,
}

#[derive(Subcommand, Debug)]
enum ConfigAction {
    /// Write a starter config.toml.
    Init(ConfigInitArgs),
}

#[derive(Args, Debug)]
struct ConfigInitArgs {
    #[arg(long, value_name = "URL")]
    server_url: String,
    /// Environment variable holding the bearer token (the token itself is never stored).
    #[arg(long, value_name = "NAME", default_value = DEFAULT_TOKEN_ENV)]
    token_env: String,
    /// Overwrite an existing config.toml.
    #[arg(long)]
    force: bool,
}

#[derive(Args, Debug)]
struct FinalizeArgs {
    #[arg(long, value_name = "AGENT", default_value = "codex")]
    agent: String,
    #[arg(long, value_name = "ID")]
    session_id: String,
    #[arg(long, value_name = "ID")]
    conversation_id: Option<String>,
    #[arg(long, value_name = "ID", default_value = "")]
    last_message_id: String,
    /// Number of messages the agent believes the session contained.
    #[arg(long, default_value_t = -1)]
    observed_message_count: i64,
}

fn main() {
    let cli = Cli::parse();
    if let Some(home) = &cli.home {
        std::env::set_var(paths::HOME_ENV, home);
    }
    let exit_code = match dispatch(cli.command) {
        Ok(code) => code,
        Err(err) => {
            eprintln!("research-memory-bridge: {err:#}");
            1
        }
    };
    std::process::exit(exit_code);
}

fn resolve_agent(value: &str) -> Result<AgentKind> {
    AgentKind::parse(value)
        .ok_or_else(|| anyhow!("unknown agent {value:?}; supported: codex, claude-code, generic"))
}

fn load_config(root: &std::path::Path) -> Result<BridgeConfig> {
    BridgeConfig::load_or_default(root)
}

fn open_spool(root: &std::path::Path) -> Result<Spool> {
    Spool::open(&paths::spool_path(root))
}

fn dispatch(command: Command) -> Result<i32> {
    let root = paths::home_dir(true)?;

    // `capture` must stay cheap and silent on stdout, so it gets a lower log
    // level and never builds a tokio runtime.
    let log_level = match &command {
        Command::Capture(_) => "warn",
        _ => "info",
    };
    logging::init(&root, log_level);

    match command {
        Command::Version => {
            println!("research-memory-bridge {}", env!("CARGO_PKG_VERSION"));
            Ok(0)
        }
        Command::Capture(args) => run_capture(&root, args),
        Command::Drain(args) => run_drain(&root, args),
        Command::Watch(args) => run_watch(&root, args),
        Command::Status(args) => run_status(&root, args.json),
        Command::Doctor(args) => run_doctor(&root, args.json),
        Command::InstallHooks(args) => run_install(&root, args),
        Command::UninstallHooks(args) => run_uninstall(&root, args),
        Command::Config(args) => run_config(&root, args),
        Command::FinalizeSession(args) => run_finalize(&root, args),
    }
}

// ---------------------------------------------------------------------------
// capture
// ---------------------------------------------------------------------------

fn read_payload(args: &CaptureArgs) -> Result<(String, PayloadSource)> {
    if args.stdin {
        let mut buffer = String::new();
        std::io::stdin().read_to_string(&mut buffer)?;
        return Ok((buffer, PayloadSource::Stdin));
    }
    if let Some(payload) = &args.payload {
        if !payload.trim().is_empty() {
            return Ok((payload.clone(), PayloadSource::Argv));
        }
    }
    // Codex appends the payload as the last argument; Claude Code uses stdin.
    // Auto-detect so a misconfigured hook still works instead of losing data.
    let mut buffer = String::new();
    if !atty_stdin() {
        std::io::stdin().read_to_string(&mut buffer)?;
    }
    if buffer.trim().is_empty() {
        return Err(anyhow!(
            "no payload received (pass it as the last argument or on stdin)"
        ));
    }
    Ok((buffer, PayloadSource::Stdin))
}

/// Detect a piped stdin so a misconfigured hook still receives its payload.
fn atty_stdin() -> bool {
    use std::io::IsTerminal;
    std::io::stdin().is_terminal()
}

/// Hot path.  Always exits 0: a capture failure must never block the agent.
fn run_capture(root: &std::path::Path, args: CaptureArgs) -> Result<i32> {
    let agent = match resolve_agent(&args.agent) {
        Ok(agent) => agent,
        Err(err) => {
            logging::append_line(root, &format!("ERROR capture: {err:#}"));
            return Ok(0);
        }
    };
    let config = match load_config(root) {
        Ok(config) => config,
        Err(err) => {
            logging::append_line(root, &format!("ERROR capture: invalid config: {err:#}"));
            return Ok(0);
        }
    };
    let (payload, source) = match read_payload(&args) {
        Ok(result) => result,
        Err(err) => {
            logging::append_line(root, &format!("ERROR capture: {err:#}"));
            return Ok(0);
        }
    };

    let outcome = match adapters::capture(agent, &payload, &config) {
        Ok(outcome) => outcome,
        Err(err) => {
            logging::append_line(root, &format!("ERROR capture parse failed: {err:#}"));
            return Ok(0);
        }
    };
    for note in &outcome.notes {
        logging::append_line(root, &format!("WARN capture: {note}"));
    }
    if outcome.events.is_empty() {
        logging::append_line(
            root,
            &format!(
                "INFO capture: agent={} event={:?} source={:?} produced no events",
                agent.as_str(),
                args.event,
                source
            ),
        );
        return Ok(0);
    }

    let spool = match open_spool(root) {
        Ok(spool) => spool,
        Err(err) => {
            logging::append_line(
                root,
                &format!("ERROR capture: cannot open spool: {err:#} -- EVENT NOT PERSISTED"),
            );
            return Ok(0);
        }
    };

    let mut inserted = 0usize;
    let mut duplicates = 0usize;
    for event in &outcome.events {
        match spool.enqueue(event) {
            Ok(research_memory_bridge::spool::EnqueueOutcome::Inserted) => inserted += 1,
            Ok(research_memory_bridge::spool::EnqueueOutcome::Duplicate) => duplicates += 1,
            Err(err) => {
                logging::append_line(
                    root,
                    &format!("ERROR capture: spool insert failed: {err:#} -- EVENT NOT PERSISTED"),
                );
                return Ok(0);
            }
        }
    }

    record_session_state(&spool, &outcome.events, agent);
    logging::append_line(
        root,
        &format!(
            "INFO capture: agent={} inserted={inserted} duplicates={duplicates}",
            agent.as_str()
        ),
    );

    if config.capture_drain && inserted > 0 {
        spawn_detached_drain(&spool);
    }
    Ok(0)
}

/// Keep the spool's per-session view exact so session-end reconciliation
/// compares the gateway against a number the bridge can actually justify.
fn record_session_state(
    spool: &Spool,
    events: &[research_memory_bridge::NormalizedEvent],
    _agent: AgentKind,
) {
    let mut sessions: Vec<(String, String, String)> = Vec::new();
    for event in events {
        let key = (event.source_system.clone(), event.session_id.clone());
        if event.session_id.is_empty() {
            continue;
        }
        if sessions
            .iter()
            .any(|(system, session, _)| *system == key.0 && *session == key.1)
        {
            continue;
        }
        sessions.push((
            key.0.clone(),
            key.1.clone(),
            event.effective_conversation_id(),
        ));
    }
    for (system, session, conversation) in sessions {
        let count = spool.count_session_messages(&system, &session).unwrap_or(0);
        let last = spool
            .last_session_message_id(&system, &session)
            .unwrap_or_default();
        if let Err(err) = spool.session_upsert(&system, &session, &conversation, &last, count) {
            warn!("cannot record session {session}: {err}");
        }
        let ended = events
            .iter()
            .any(|event| event.session_id == session && event.event_type == "session_end");
        if ended {
            if let Err(err) = spool.session_mark_ended(&system, &session) {
                warn!("cannot mark session {session} ended: {err}");
            }
        }
    }
}

/// Spawn `drain --once` detached, rate-limited so a burst of hooks cannot fork
/// a process per event.
fn spawn_detached_drain(spool: &Spool) {
    let now = research_memory_bridge::spool::now_epoch();
    if let Ok(Some(previous)) = spool.get_meta("drain_spawned_at") {
        if let Ok(previous) = previous.parse::<i64>() {
            if now - previous < 2 {
                debug!(
                    "skipping detached drain; one was started {}s ago",
                    now - previous
                );
                return;
            }
        }
    }
    let _ = spool.set_meta("drain_spawned_at", &now.to_string());

    let Ok(exe) = std::env::current_exe() else {
        return;
    };
    let mut command = std::process::Command::new(exe);
    command
        .arg("drain")
        .arg("--once")
        .arg("--max-batches")
        .arg("5")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if let Ok(home) = std::env::var(paths::HOME_ENV) {
        command.env(paths::HOME_ENV, home);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // DETACHED_PROCESS | CREATE_NO_WINDOW: survive the hook process and
        // never flash a console window.
        command.creation_flags(0x0000_0008 | 0x0800_0000);
    }
    match command.spawn() {
        Ok(_) => debug!("spawned detached drain"),
        Err(err) => warn!("cannot spawn detached drain: {err}"),
    }
}

// ---------------------------------------------------------------------------
// drain / watch
// ---------------------------------------------------------------------------

fn run_drain(root: &std::path::Path, args: DrainArgs) -> Result<i32> {
    let config = Arc::new(load_config(root)?);
    let spool = Arc::new(open_spool(root)?);
    let transport = Arc::new(Transport::new(&config)?);
    if !transport.token_configured() {
        warn!(
            "no bearer token found in ${}; the gateway will reject uploads unless it \
             allows unauthenticated loopback access",
            config.token_env
        );
    }
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    let daemon = args.daemon && !args.once;
    let report = runtime.block_on(async {
        if daemon {
            drain::drain_daemon(config.clone(), spool.clone(), transport.clone()).await
        } else {
            drain::drain_once(&config, &spool, &transport, args.max_batches).await
        }
    });
    println!(
        "drain: batches={} accepted={} duplicates={} rejected={} rescheduled={} dead_letter={} reconciled_sessions={} reason={}",
        report.batches,
        report.accepted,
        report.duplicates,
        report.rejected_permanent,
        report.rescheduled,
        report.dead_lettered,
        report.sessions_reconciled,
        report.stopped_reason
    );
    if !report.last_error.is_empty() {
        println!("last_error: {}", report.last_error);
        return Ok(1);
    }
    Ok(0)
}

fn run_watch(root: &std::path::Path, args: WatchArgs) -> Result<i32> {
    let agent = resolve_agent(&args.agent)?;
    let Some(source) = watcher::source_for(agent) else {
        return Err(anyhow!(
            "agent {} has no transcript format the bridge can verify; \
             transcript reconciliation is unavailable",
            agent.as_str()
        ));
    };
    let config = Arc::new(load_config(root)?);
    let spool = Arc::new(open_spool(root)?);
    if args.once {
        let report = watcher::watch_once(source.as_ref(), &config, &spool)?;
        println!(
            "watch: files={} advanced={} parsed={} inserted={} duplicates={} errors={}",
            report.files_scanned,
            report.files_advanced,
            report.events_parsed,
            report.events_inserted,
            report.events_duplicate,
            report.errors
        );
        return Ok(0);
    }
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    let boxed: Arc<Box<dyn watcher::TranscriptSource>> = Arc::new(source);
    runtime.block_on(async move {
        watcher::watch_loop(boxed, config.clone(), spool.clone()).await;
    });
    Ok(0)
}

// ---------------------------------------------------------------------------
// status / doctor
// ---------------------------------------------------------------------------

fn status_payload(root: &std::path::Path) -> Result<serde_json::Value> {
    let config = load_config(root)?;
    let spool = open_spool(root)?;
    let counts = spool.counts()?;
    let oldest = spool.oldest_pending_epoch()?;
    let now = research_memory_bridge::spool::now_epoch();
    let dead_letters = spool.dead_letter_sample(5)?;
    let adapters_status: Vec<serde_json::Value> = AgentKind::ALL
        .iter()
        .map(|agent| {
            let status = adapters::status(*agent, &config);
            serde_json::json!({
                "agent": agent.as_str(),
                "supported": status.supported,
                "installed": status.installed,
                "config_path": status.config_path.as_ref().map(|p| p.display().to_string()),
                "config_exists": status.config_exists,
                "command": status.command,
                "transcript_root": status.transcript_root.as_ref().map(|p| p.display().to_string()),
                "transcript_files": status.transcript_files,
            })
        })
        .collect();

    Ok(serde_json::json!({
        "version": env!("CARGO_PKG_VERSION"),
        "config": config.redacted_view(root),
        "spool": {
            "path": spool.path().display().to_string(),
            "pending": counts.pending,
            "inflight": counts.inflight,
            "acked": counts.acked,
            "dead_letter": counts.dead_letter,
            "total": counts.total(),
            "oldest_pending_seconds": oldest.map(|at| (now - at).max(0)),
        },
        "sessions_tracked": spool.session_count()?,
        "cursors": spool.cursor_count()?,
        "last_successful_upload": spool.get_meta("last_drain_at")?,
        "last_drain_summary": spool.get_meta("last_drain_summary")?,
        "last_error": spool.get_meta("last_error")?,
        "last_error_at": spool.get_meta("last_error_at")?,
        "dead_letter_sample": dead_letters
            .into_iter()
            .map(|(event_id, error, attempts)| serde_json::json!({
                "event_id": event_id,
                "last_error": error,
                "attempts": attempts,
            }))
            .collect::<Vec<_>>(),
        "adapters": adapters_status,
    }))
}

fn run_status(root: &std::path::Path, json: bool) -> Result<i32> {
    let payload = status_payload(root)?;
    if json {
        println!("{}", serde_json::to_string_pretty(&payload)?);
        return Ok(0);
    }
    let spool = &payload["spool"];
    let config = &payload["config"];
    println!(
        "research-memory-bridge {}",
        payload["version"].as_str().unwrap_or("?")
    );
    println!(
        "  home            {}",
        config["home"].as_str().unwrap_or("-")
    );
    println!(
        "  server_url      {}",
        config["server_url"].as_str().unwrap_or("-")
    );
    println!(
        "  token           {}",
        if config["token_configured"].as_bool().unwrap_or(false) {
            format!(
                "configured via ${}",
                config["token_env"].as_str().unwrap_or("-")
            )
        } else {
            format!(
                "MISSING (set ${})",
                config["token_env"].as_str().unwrap_or("-")
            )
        }
    );
    println!(
        "  client_id       {}",
        config["client_id"].as_str().unwrap_or("-")
    );
    println!(
        "  spool           {} (pending={} inflight={} acked={} dead_letter={})",
        spool["path"].as_str().unwrap_or("-"),
        spool["pending"],
        spool["inflight"],
        spool["acked"],
        spool["dead_letter"]
    );
    match spool["oldest_pending_seconds"].as_i64() {
        Some(seconds) => println!("  oldest pending  {seconds}s"),
        None => println!("  oldest pending  (none)"),
    }
    println!(
        "  last upload     {}",
        payload["last_successful_upload"]
            .as_str()
            .unwrap_or("(never)")
    );
    if let Some(error) = payload["last_error"].as_str() {
        if !error.is_empty() {
            println!("  last error      {error}");
        }
    }
    for adapter in payload["adapters"].as_array().into_iter().flatten() {
        println!(
            "  adapter {:<12} supported={:<5} installed={:<5} transcripts={}",
            adapter["agent"].as_str().unwrap_or("?"),
            adapter["supported"].as_bool().unwrap_or(false),
            adapter["installed"].as_bool().unwrap_or(false),
            adapter["transcript_files"]
        );
    }
    Ok(0)
}

fn run_doctor(root: &std::path::Path, json: bool) -> Result<i32> {
    let config = load_config(root)?;
    let spool = open_spool(root)?;
    let mut checks: Vec<(String, bool, String)> = Vec::new();

    checks.push((
        "config_valid".to_string(),
        true,
        format!("{}", paths::config_path(root).display()),
    ));
    checks.push((
        "spool_writable".to_string(),
        spool.path().exists(),
        spool.path().display().to_string(),
    ));
    let token_ok = config.token_configured();
    checks.push((
        "token_present".to_string(),
        token_ok,
        format!("${}", config.token_env),
    ));
    for agent in AgentKind::ALL {
        let status = adapters::status(agent, &config);
        if status.supported {
            checks.push((
                format!("hooks_installed:{}", agent.as_str()),
                status.installed,
                status
                    .config_path
                    .as_ref()
                    .map(|path| path.display().to_string())
                    .unwrap_or_default(),
            ));
        }
    }

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let (reachable, auth, ingest_enabled, detail) = runtime.block_on(async {
        let transport = match Transport::new(&config) {
            Ok(transport) => transport,
            Err(err) => return (false, "unknown".to_string(), false, err.to_string()),
        };
        match transport.health().await {
            Ok(health) => {
                let version = health
                    .get("version")
                    .and_then(|value| value.as_str())
                    .unwrap_or("?")
                    .to_string();
                let ingest_enabled = health
                    .get("conversation_ingest")
                    .and_then(|value| value.get("enabled"))
                    .and_then(|value| value.as_bool())
                    .unwrap_or(false);
                match transport.probe_auth(&config.client_id).await {
                    Ok(AuthProbe::Ok) => (
                        true,
                        "valid".to_string(),
                        ingest_enabled,
                        format!("gateway {version}"),
                    ),
                    Ok(AuthProbe::Disabled) => (
                        true,
                        "valid".to_string(),
                        false,
                        format!("gateway {version}; ingest disabled"),
                    ),
                    Ok(AuthProbe::Rejected(message)) => (
                        true,
                        format!("INVALID ({message})"),
                        ingest_enabled,
                        version,
                    ),
                    Err(err) => (true, format!("unknown ({err})"), ingest_enabled, version),
                }
            }
            Err(err) => (false, "unknown".to_string(), false, err.to_string()),
        }
    });

    checks.push(("server_reachable".to_string(), reachable, detail.clone()));
    checks.push(("auth".to_string(), auth == "valid", auth.clone()));
    checks.push((
        "gateway_ingest_enabled".to_string(),
        ingest_enabled,
        if ingest_enabled {
            "conversation_ingest.enabled = true".to_string()
        } else {
            "enable conversation_archive.enabled and conversation_ingest.enabled on the gateway"
                .to_string()
        },
    ));

    let counts = spool.counts()?;
    checks.push((
        "spool_backlog".to_string(),
        counts.dead_letter == 0,
        format!(
            "pending={} inflight={} dead_letter={}",
            counts.pending, counts.inflight, counts.dead_letter
        ),
    ));

    let ok = checks.iter().all(|(_, passed, _)| *passed);
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "ok": ok,
                "checks": checks.iter().map(|(name, passed, detail)| serde_json::json!({
                    "check": name,
                    "passed": passed,
                    "detail": detail,
                })).collect::<Vec<_>>(),
                "status": status_payload(root)?,
            }))?
        );
    } else {
        println!("research-memory-bridge doctor");
        for (name, passed, detail) in &checks {
            println!(
                "  [{}] {:<28} {}",
                if *passed { "ok" } else { "!!" },
                name,
                detail
            );
        }
        println!(
            "\n{}",
            if ok {
                "all checks passed"
            } else {
                "one or more checks failed; see above"
            }
        );
    }
    Ok(if ok { 0 } else { 1 })
}

// ---------------------------------------------------------------------------
// hooks / config / finalize
// ---------------------------------------------------------------------------

fn run_install(root: &std::path::Path, args: InstallArgs) -> Result<i32> {
    let agent = resolve_agent(&args.agent)?;
    let config = load_config(root)?;
    if args.dry_run {
        let status = adapters::status(agent, &config);
        println!(
            "dry-run: would install {} hooks\n  config: {}\n  currently installed: {}",
            agent.as_str(),
            status
                .config_path
                .as_ref()
                .map(|path| path.display().to_string())
                .unwrap_or_else(|| "(no config file for this agent)".to_string()),
            status.installed
        );
        return Ok(0);
    }
    let report = adapters::install_hooks(agent, root, args.force, args.with_tools)?;
    println!(
        "{}: {}",
        if report.changed {
            "installed"
        } else {
            "unchanged"
        },
        report.message
    );
    if let Some(backup) = &report.backup_path {
        println!("  backup: {}", backup.display());
    }
    if let Some(path) = &report.config_path {
        println!("  config: {}", path.display());
    }
    Ok(0)
}

fn run_uninstall(root: &std::path::Path, args: AgentArgs) -> Result<i32> {
    let agent = resolve_agent(&args.agent)?;
    let report = adapters::uninstall_hooks(agent, root)?;
    println!(
        "{}: {}",
        if report.changed {
            "uninstalled"
        } else {
            "unchanged"
        },
        report.message
    );
    if let Some(backup) = &report.backup_path {
        println!("  backup: {}", backup.display());
    }
    Ok(0)
}

fn run_config(root: &std::path::Path, args: ConfigArgs) -> Result<i32> {
    match args.action {
        Some(ConfigAction::Init(init)) => {
            let path = paths::config_path(root);
            if path.exists() && !init.force {
                return Err(anyhow!(
                    "{} already exists; pass --force to overwrite",
                    path.display()
                ));
            }
            let config = BridgeConfig {
                server_url: init.server_url.clone(),
                token_env: init.token_env.clone(),
                ..BridgeConfig::default()
            };
            config.validate()?;
            config.save(&path)?;
            println!("wrote {}", path.display());
            println!("  server_url = {}", config.server_url_trimmed());
            println!(
                "  token      = read from ${} (never stored in the config file)",
                config.token_env
            );
            Ok(0)
        }
        None => {
            let config = load_config(root)?;
            let view = config.redacted_view(root);
            if args.json {
                println!("{}", serde_json::to_string_pretty(&view)?);
            } else {
                println!("config: {}", paths::config_path(root).display());
                println!("{}", serde_json::to_string_pretty(&view)?);
            }
            Ok(0)
        }
    }
}

fn run_finalize(root: &std::path::Path, args: FinalizeArgs) -> Result<i32> {
    let agent = resolve_agent(&args.agent)?;
    let spool = open_spool(root)?;
    let conversation = args
        .conversation_id
        .clone()
        .unwrap_or_else(|| args.session_id.clone());
    let observed = if args.observed_message_count >= 0 {
        args.observed_message_count
    } else {
        spool.count_session_messages(agent.source_system(), &args.session_id)?
    };
    let last_message_id = if args.last_message_id.is_empty() {
        spool.last_session_message_id(agent.source_system(), &args.session_id)?
    } else {
        args.last_message_id.clone()
    };
    watcher::finalize_session(
        &spool,
        agent.source_system(),
        &args.session_id,
        &conversation,
        &last_message_id,
        observed,
    )?;
    println!(
        "finalized session {} (agent={} observed_message_count={} last_message_id={})",
        args.session_id,
        agent.as_str(),
        observed,
        if last_message_id.is_empty() {
            "-"
        } else {
            last_message_id.as_str()
        }
    );
    println!("session-end will be declared to the gateway on the next drain");
    Ok(0)
}
