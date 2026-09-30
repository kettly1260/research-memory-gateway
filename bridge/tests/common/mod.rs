//! Shared test helpers: an isolated bridge home and a minimal HTTP stub gateway.

#![allow(dead_code)]
// Test fixtures legitimately start from defaults and then override a field.
#![allow(clippy::field_reassign_with_default)]

use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use research_memory_bridge::config::BridgeConfig;
use tempfile::TempDir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

/// One recorded request the stub server received.
#[derive(Debug, Clone)]
pub struct StubRequest {
    pub method: String,
    pub path: String,
    pub authorization: Option<String>,
    pub body: String,
}

impl StubRequest {
    pub fn json(&self) -> serde_json::Value {
        serde_json::from_str(&self.body).unwrap_or(serde_json::Value::Null)
    }

    pub fn event_ids(&self) -> Vec<String> {
        self.json()
            .get("events")
            .and_then(|value| value.as_array())
            .map(|events| {
                events
                    .iter()
                    .filter_map(|event| {
                        event
                            .get("event_id")
                            .and_then(|value| value.as_str())
                            .map(|text| text.to_string())
                    })
                    .collect()
            })
            .unwrap_or_default()
    }
}

/// A canned response the stub server replays, in order.
#[derive(Debug, Clone)]
pub struct StubResponse {
    pub status: u16,
    pub body: String,
}

impl StubResponse {
    pub fn json(status: u16, body: serde_json::Value) -> StubResponse {
        StubResponse {
            status,
            body: body.to_string(),
        }
    }

    pub fn ok(body: serde_json::Value) -> StubResponse {
        StubResponse::json(200, body)
    }
}

/// Minimal HTTP/1.1 stub gateway: one request per connection, canned responses.
pub struct StubGateway {
    addr: SocketAddr,
    responses: Arc<Mutex<Vec<StubResponse>>>,
    pub requests: Arc<Mutex<Vec<StubRequest>>>,
    _task: tokio::task::JoinHandle<()>,
}

impl StubGateway {
    pub async fn start(responses: Vec<StubResponse>) -> StubGateway {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind stub");
        let addr = listener.local_addr().expect("local addr");
        let responses = Arc::new(Mutex::new(responses));
        let requests: Arc<Mutex<Vec<StubRequest>>> = Arc::new(Mutex::new(Vec::new()));
        let responses_for_task = responses.clone();
        let requests_for_task = requests.clone();
        let task = tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    return;
                };
                let responses = responses_for_task.clone();
                let requests = requests_for_task.clone();
                tokio::spawn(async move {
                    if let Some(request) = read_request(&mut stream).await {
                        requests.lock().expect("requests lock").push(request);
                        let response = {
                            let mut queue = responses.lock().expect("responses lock");
                            if queue.is_empty() {
                                StubResponse::json(
                                    500,
                                    serde_json::json!({"error": "stub queue exhausted"}),
                                )
                            } else {
                                queue.remove(0)
                            }
                        };
                        let _ = write_response(&mut stream, &response).await;
                    }
                    let _ = stream.shutdown().await;
                });
            }
        });
        StubGateway {
            addr,
            responses,
            requests,
            _task: task,
        }
    }

    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }

    pub fn request_count(&self) -> usize {
        self.requests.lock().expect("requests lock").len()
    }

    pub fn request(&self, index: usize) -> StubRequest {
        self.requests
            .lock()
            .expect("requests lock")
            .get(index)
            .cloned()
            .expect("request index present")
    }

    pub fn push(&self, response: StubResponse) {
        self.responses
            .lock()
            .expect("responses lock")
            .push(response);
    }
}

async fn read_request(stream: &mut tokio::net::TcpStream) -> Option<StubRequest> {
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 4096];
    let header_end;
    loop {
        let read = stream.read(&mut chunk).await.ok()?;
        if read == 0 {
            return None;
        }
        buffer.extend_from_slice(&chunk[..read]);
        if let Some(position) = find_subsequence(&buffer, b"\r\n\r\n") {
            header_end = position + 4;
            break;
        }
        if buffer.len() > 4 * 1024 * 1024 {
            return None;
        }
    }
    let headers = String::from_utf8_lossy(&buffer[..header_end]).to_string();
    let mut lines = headers.split("\r\n");
    let request_line = lines.next().unwrap_or("");
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let path = parts.next().unwrap_or("").to_string();
    let mut authorization = None;
    let mut content_length = 0usize;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        let name = name.trim().to_ascii_lowercase();
        let value = value.trim().to_string();
        if name == "authorization" {
            authorization = Some(value);
        } else if name == "content-length" {
            content_length = value.parse().unwrap_or(0);
        }
    }
    let mut body = buffer[header_end..].to_vec();
    while body.len() < content_length {
        let read = stream.read(&mut chunk).await.ok()?;
        if read == 0 {
            break;
        }
        body.extend_from_slice(&chunk[..read]);
    }
    Some(StubRequest {
        method,
        path,
        authorization,
        body: String::from_utf8_lossy(&body).to_string(),
    })
}

async fn write_response(
    stream: &mut tokio::net::TcpStream,
    response: &StubResponse,
) -> std::io::Result<()> {
    let payload = response.body.as_bytes();
    let head = format!(
        "HTTP/1.1 {} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        response.status,
        status_text(response.status),
        payload.len()
    );
    stream.write_all(head.as_bytes()).await?;
    stream.write_all(payload).await?;
    stream.flush().await
}

fn status_text(status: u16) -> &'static str {
    match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        409 => "Conflict",
        413 => "Payload Too Large",
        500 => "Internal Server Error",
        503 => "Service Unavailable",
        _ => "Status",
    }
}

fn find_subsequence(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

/// An isolated bridge home so tests never touch the real user profile.
pub struct TestHome {
    pub dir: TempDir,
}

impl TestHome {
    pub fn new() -> TestHome {
        TestHome {
            dir: tempfile::tempdir().expect("temp dir"),
        }
    }

    pub fn path(&self) -> &Path {
        self.dir.path()
    }

    pub fn config_path(&self) -> PathBuf {
        self.dir.path().join("config.toml")
    }

    pub fn spool_path(&self) -> PathBuf {
        self.dir.path().join("spool.sqlite")
    }

    /// Config with a token env var that is guaranteed not to exist, so tests
    /// control the token explicitly instead of reading the ambient environment.
    pub fn config(&self) -> BridgeConfig {
        let mut config = BridgeConfig::default();
        config.token_env = "RMG_BRIDGE_TEST_TOKEN_UNSET".to_string();
        config.client_id = "test-client".to_string();
        config.capture_drain = false;
        config.batch_size = 10;
        config.spool_retention_days = 0;
        config.codex_sessions_dir = self.dir.path().join("codex-sessions").display().to_string();
        config.claude_projects_dir = self
            .dir
            .path()
            .join("claude-projects")
            .display()
            .to_string();
        config
    }

    pub fn write_config(&self, config: &BridgeConfig) {
        config.save(&self.config_path()).expect("save config");
    }
}

/// Load a fixture file that ships with the crate.
pub fn fixture(relative: &str) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join(relative);
    std::fs::read_to_string(&path)
        .unwrap_or_else(|err| panic!("cannot read fixture {}: {err}", path.display()))
}
