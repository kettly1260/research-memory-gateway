//! `research-memory-bridge` -- automatic conversation capture for
//! `research-memory-gateway`.
//!
//! The bridge is a *thin client*, never a second memory server.  It owns
//! exactly six jobs:
//!
//! 1. lifecycle hooks / transcript adapters,
//! 2. normalization onto the gateway's canonical event vocabulary,
//! 3. secret redaction,
//! 4. a durable local SQLite spool,
//! 5. retry with exponential backoff + jitter,
//! 6. batched HTTP upload.
//!
//! It deliberately contains **no** MCP server, WebUI, embedding, reranker,
//! vector database, LLM or wiki.  Retrieval stays in the gateway.
//!
//! Two hard boundaries shape every design decision here:
//!
//! * **fail-open for the agent** -- the capture hot path never waits for the
//!   network, and `capture` always exits `0`, so a dead gateway can never block
//!   Codex or Claude Code;
//! * **fail-closed for data integrity and auth** -- an invalid server URL, an
//!   unverifiable payload or an authentication failure is never reported as a
//!   successful ACK.

pub mod adapters;
pub mod config;
pub mod drain;
pub mod event;
pub mod logging;
pub mod normalize;
pub mod paths;
pub mod redact;
pub mod spool;
pub mod transport;
pub mod watcher;

pub use config::BridgeConfig;
pub use event::{EventType, NormalizedEvent, Role};
pub use spool::Spool;
