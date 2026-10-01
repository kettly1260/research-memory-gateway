//! Bounded file logging.
//!
//! Two properties matter for a hook-invoked binary:
//!
//! * **never write to stdout** -- Claude Code treats plain stdout of a
//!   `UserPromptSubmit`/`SessionStart` hook as *context injected into the
//!   model*, so a stray log line would pollute the agent's conversation;
//! * **never grow without bound** -- the log rotates at 5 MiB and keeps two
//!   generations, so a long-running `drain --daemon` cannot fill the disk.
//!
//! All diagnostics therefore go to `<home>/logs/bridge.log`, and `capture`
//! lowers the default level to `warn` to keep the hot path cheap.

use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use tracing_subscriber::fmt::MakeWriter;

pub const MAX_LOG_BYTES: u64 = 5 * 1024 * 1024;
pub const ROTATED_GENERATIONS: usize = 2;

/// Append-only writer that rotates `bridge.log` -> `bridge.log.1` -> `.2`.
#[derive(Debug)]
pub struct RotatingFile {
    path: PathBuf,
    file: File,
    written: u64,
    max_bytes: u64,
}

impl RotatingFile {
    pub fn open(path: &Path) -> io::Result<RotatingFile> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        let written = file.metadata().map(|m| m.len()).unwrap_or(0);
        Ok(RotatingFile {
            path: path.to_path_buf(),
            file,
            written,
            max_bytes: MAX_LOG_BYTES,
        })
    }

    pub fn with_max_bytes(mut self, max_bytes: u64) -> Self {
        self.max_bytes = max_bytes.max(1024);
        self
    }

    fn rotate(&mut self) -> io::Result<()> {
        // Drop generations from the oldest end so only N files exist.
        for index in (1..ROTATED_GENERATIONS).rev() {
            let from = self.path.with_extension(format!("log.{index}"));
            let to = self.path.with_extension(format!("log.{}", index + 1));
            if from.exists() {
                let _ = std::fs::rename(&from, &to);
            }
        }
        let first = self.path.with_extension("log.1");
        let _ = std::fs::rename(&self.path, &first);
        self.file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        self.written = 0;
        Ok(())
    }
}

impl Write for RotatingFile {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        if self.written + buf.len() as u64 > self.max_bytes {
            let _ = self.rotate();
        }
        let written = self.file.write(buf)?;
        self.written += written as u64;
        Ok(written)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}

/// `MakeWriter` adapter so `tracing_subscriber` can share one rotating file.
#[derive(Clone, Debug)]
pub struct SharedRotatingFile(Arc<Mutex<RotatingFile>>);

impl SharedRotatingFile {
    pub fn open(path: &Path) -> io::Result<SharedRotatingFile> {
        Ok(SharedRotatingFile(Arc::new(Mutex::new(
            RotatingFile::open(path)?,
        ))))
    }
}

impl<'a> MakeWriter<'a> for SharedRotatingFile {
    type Writer = SharedRotatingFileGuard;

    fn make_writer(&'a self) -> Self::Writer {
        SharedRotatingFileGuard(self.0.clone())
    }
}

pub struct SharedRotatingFileGuard(Arc<Mutex<RotatingFile>>);

impl Write for SharedRotatingFileGuard {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self.0.lock() {
            Ok(mut file) => file.write(buf),
            Err(poisoned) => poisoned.into_inner().write(buf),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        match self.0.lock() {
            Ok(mut file) => file.flush(),
            Err(poisoned) => poisoned.into_inner().flush(),
        }
    }
}

/// Initialise file logging.  Returns `false` when the log file could not be
/// opened, in which case the caller should keep going silently: a hook must
/// never fail because logging is unavailable.
pub fn init(root: &Path, level: &str) -> bool {
    let path = crate::paths::log_path(root);
    let writer = match SharedRotatingFile::open(&path) {
        Ok(writer) => writer,
        Err(_) => return false,
    };
    let filter = std::env::var("RUST_LOG")
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| level.to_string());
    let env_filter = tracing_subscriber::EnvFilter::try_new(&filter)
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    let result = tracing_subscriber::fmt()
        .with_env_filter(env_filter)
        .with_writer(writer)
        .with_ansi(false)
        .with_target(false)
        .try_init();
    result.is_ok()
}

/// Append a single line directly to the log without going through `tracing`.
///
/// Used on the capture hot path before the subscriber is installed, so that a
/// fatal capture error is still recorded.
pub fn append_line(root: &Path, line: &str) {
    let path = crate::paths::log_path(root);
    if let Ok(mut file) = RotatingFile::open(&path) {
        let _ = writeln!(file, "{}", line);
    }
}
