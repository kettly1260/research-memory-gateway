//! Drain behaviour against a stub gateway: ACK handling, partial batch success,
//! retry classification and dead-lettering.

mod common;

use common::{StubGateway, StubResponse, TestHome};
use research_memory_bridge::drain::drain_once;
use research_memory_bridge::event::{EventType, NormalizedEvent};
use research_memory_bridge::spool::Spool;
use research_memory_bridge::transport::Transport;

fn event(id: &str, content: &str) -> NormalizedEvent {
    let mut event = NormalizedEvent::new("codex", EventType::UserPrompt, content)
        .with_session("s-1")
        .with_conversation("s-1");
    event.event_id = id.to_string();
    event
}

fn batch_response(
    accepted: &[&str],
    duplicates: &[&str],
    rejected: Vec<serde_json::Value>,
) -> serde_json::Value {
    serde_json::json!({
        "schema_version": 1,
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": rejected,
    })
}

#[tokio::test]
async fn successful_batch_is_acked_and_cleared() {
    let home = TestHome::new();
    let gateway = StubGateway::start(vec![StubResponse::ok(batch_response(
        &["rmb1_ok000001", "rmb1_ok000002"],
        &[],
        vec![],
    ))])
    .await;
    let mut config = home.config();
    config.server_url = gateway.url();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool
        .enqueue(&event("rmb1_ok000001", "one"))
        .expect("insert");
    spool
        .enqueue(&event("rmb1_ok000002", "two"))
        .expect("insert");

    let transport = Transport::with_token(&config, Some("test-token".into())).expect("transport");
    let report = drain_once(&config, &spool, &transport, 5).await;

    assert_eq!(report.batches, 1);
    assert_eq!(report.accepted, 2);
    assert_eq!(report.dead_lettered, 0);
    assert_eq!(report.rescheduled, 0);
    let counts = spool.counts().expect("counts");
    assert_eq!(counts.acked, 2);
    assert_eq!(counts.outstanding(), 0);

    // The bearer token is sent as an Authorization header, and never logged.
    let request = gateway.request(0);
    assert_eq!(request.method, "POST");
    assert!(
        request.path.ends_with("/api/conversations/events/batch"),
        "{}",
        request.path
    );
    assert_eq!(request.authorization.as_deref(), Some("Bearer test-token"));
    assert_eq!(request.event_ids(), vec!["rmb1_ok000001", "rmb1_ok000002"]);
}

#[tokio::test]
async fn duplicates_count_as_success() {
    let home = TestHome::new();
    let gateway = StubGateway::start(vec![StubResponse::ok(batch_response(
        &[],
        &["rmb1_dup00001"],
        vec![],
    ))])
    .await;
    let mut config = home.config();
    config.server_url = gateway.url();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_dup00001", "x")).expect("insert");

    let transport = Transport::with_token(&config, None).expect("transport");
    let report = drain_once(&config, &spool, &transport, 5).await;
    assert_eq!(report.duplicates, 1);
    assert_eq!(report.dead_lettered, 0);
    // A duplicate must NOT be retried: the gateway already has it.
    assert_eq!(spool.counts().expect("counts").acked, 1);
}

#[tokio::test]
async fn partial_batch_success_is_applied_per_event() {
    let home = TestHome::new();
    let gateway = StubGateway::start(vec![StubResponse::ok(batch_response(
        &["rmb1_part0001"],
        &[],
        vec![
            serde_json::json!({"event_id": "rmb1_part0002", "code": "content_too_large", "message": "too big"}),
            serde_json::json!({"event_id": "rmb1_part0003", "code": "internal_error", "message": "boom"}),
        ],
    ))])
    .await;
    let mut config = home.config();
    config.server_url = gateway.url();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    for (index, id) in ["rmb1_part0001", "rmb1_part0002", "rmb1_part0003"]
        .iter()
        .enumerate()
    {
        spool
            .enqueue(&event(id, &format!("body {index}")))
            .expect("insert");
    }

    let transport = Transport::with_token(&config, None).expect("transport");
    let report = drain_once(&config, &spool, &transport, 5).await;
    assert_eq!(report.accepted, 1);
    assert_eq!(
        report.dead_lettered, 1,
        "permanent rejection is dead-lettered"
    );
    assert_eq!(report.rescheduled, 1, "transient rejection is retried");

    let counts = spool.counts().expect("counts");
    assert_eq!(counts.acked, 1);
    assert_eq!(counts.dead_letter, 1);
    assert_eq!(counts.pending, 1, "the retryable event returns to pending");
}

#[tokio::test]
async fn unaccounted_events_are_rescheduled_not_lost() {
    let home = TestHome::new();
    // The gateway mentions only one of the two uploaded events.
    let gateway = StubGateway::start(vec![StubResponse::ok(batch_response(
        &["rmb1_unac0001"],
        &[],
        vec![],
    ))])
    .await;
    let mut config = home.config();
    config.server_url = gateway.url();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_unac0001", "a")).expect("insert");
    spool.enqueue(&event("rmb1_unac0002", "b")).expect("insert");

    let transport = Transport::with_token(&config, None).expect("transport");
    let report = drain_once(&config, &spool, &transport, 5).await;
    assert_eq!(report.accepted, 1);
    assert_eq!(report.rescheduled, 1);
    assert_eq!(spool.counts().expect("counts").pending, 1);
}

#[tokio::test]
async fn unauthorized_response_never_acks() {
    let home = TestHome::new();
    let gateway = StubGateway::start(vec![StubResponse {
        status: 401,
        body: "Unauthorized".to_string(),
    }])
    .await;
    let mut config = home.config();
    config.server_url = gateway.url();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_auth0001", "x")).expect("insert");

    let transport = Transport::with_token(&config, Some("wrong".into())).expect("transport");
    let report = drain_once(&config, &spool, &transport, 5).await;
    let counts = spool.counts().expect("counts");
    assert_eq!(
        counts.acked, 0,
        "auth failure must not be treated as success"
    );
    assert_eq!(counts.pending, 1);
    assert!(
        report.last_error.contains("unauthorized"),
        "{}",
        report.last_error
    );
}

#[tokio::test]
async fn server_error_is_retried_with_backoff() {
    let home = TestHome::new();
    let gateway = StubGateway::start(vec![StubResponse {
        status: 500,
        body: "{\"error\":\"boom\"}".to_string(),
    }])
    .await;
    let mut config = home.config();
    config.server_url = gateway.url();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_50000001", "x")).expect("insert");

    let transport = Transport::with_token(&config, None).expect("transport");
    let report = drain_once(&config, &spool, &transport, 5).await;
    assert_eq!(report.rescheduled, 1);
    assert_eq!(report.dead_lettered, 0);
    let counts = spool.counts().expect("counts");
    assert_eq!(counts.pending, 1);
    assert_eq!(counts.acked, 0);
    let next = spool.next_retry_epoch().expect("next").expect("some");
    let now = research_memory_bridge::spool::now_epoch();
    assert!(next > now, "retry must be scheduled in the future");
}

#[tokio::test]
async fn ingest_disabled_is_retried_not_dead_lettered() {
    let home = TestHome::new();
    let gateway = StubGateway::start(vec![StubResponse {
        status: 503,
        body: "{\"schema_version\":1,\"accepted\":[],\"duplicates\":[],\"rejected\":[{\"event_id\":\"\",\"code\":\"ingest_disabled\",\"message\":\"off\"}]}".to_string(),
    }])
    .await;
    let mut config = home.config();
    config.server_url = gateway.url();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_dis00001", "x")).expect("insert");

    let transport = Transport::with_token(&config, None).expect("transport");
    let report = drain_once(&config, &spool, &transport, 5).await;
    assert_eq!(report.rescheduled, 1);
    assert_eq!(report.dead_lettered, 0);
    assert_eq!(spool.counts().expect("counts").pending, 1);
}

#[tokio::test]
async fn max_attempts_parks_a_permanently_failing_event() {
    let home = TestHome::new();
    let mut config = home.config();
    config.max_attempts = 2;
    let responses: Vec<StubResponse> = (0..3)
        .map(|_| StubResponse {
            status: 500,
            body: "{\"error\":\"boom\"}".to_string(),
        })
        .collect();
    let gateway = StubGateway::start(responses).await;
    config.server_url = gateway.url();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_max00001", "x")).expect("insert");
    let transport = Transport::with_token(&config, None).expect("transport");

    for _ in 0..3 {
        // Force the retry to be due immediately.
        let claimed = spool.claim_batch(10).expect("claim");
        for row in claimed {
            spool
                .reschedule(&[row.event_id], 0, "forced due")
                .expect("reschedule");
        }
        drain_once(&config, &spool, &transport, 5).await;
        if spool.counts().expect("counts").dead_letter > 0 {
            break;
        }
    }
    let counts = spool.counts().expect("counts");
    assert_eq!(counts.dead_letter, 1, "attempts exhausted -> dead_letter");
    assert_eq!(counts.acked, 0);
}

#[tokio::test]
async fn network_failure_leaves_the_spool_intact() {
    let home = TestHome::new();
    let mut config = home.config();
    // Nothing is listening on this port.
    config.server_url = "http://127.0.0.1:1".to_string();
    config.connect_timeout_seconds = 2;
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_net00001", "x")).expect("insert");
    let transport = Transport::with_token(&config, None).expect("transport");
    let report = drain_once(&config, &spool, &transport, 2).await;
    assert_eq!(report.rescheduled, 1);
    assert_eq!(spool.counts().expect("counts").pending, 1);
    assert!(!report.last_error.is_empty());
}

#[tokio::test]
async fn empty_spool_does_not_call_the_gateway() {
    let home = TestHome::new();
    let gateway = StubGateway::start(vec![]).await;
    let mut config = home.config();
    config.server_url = gateway.url();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    let transport = Transport::with_token(&config, None).expect("transport");
    let report = drain_once(&config, &spool, &transport, 5).await;
    assert_eq!(report.batches, 0);
    assert_eq!(report.stopped_reason, "spool_empty");
    assert_eq!(gateway.request_count(), 0);
}

#[tokio::test]
async fn auth_probe_distinguishes_valid_invalid_and_disabled() {
    let home = TestHome::new();
    let config = home.config();

    let valid = StubGateway::start(vec![StubResponse::ok(batch_response(&[], &[], vec![]))]).await;
    let mut cfg = config.clone();
    cfg.server_url = valid.url();
    let transport = Transport::with_token(&cfg, Some("good".into())).expect("transport");
    assert_eq!(
        transport.probe_auth("c").await.expect("probe"),
        research_memory_bridge::transport::AuthProbe::Ok
    );
    // The probe must not mutate anything: it sends an empty event list.
    assert_eq!(valid.request(0).event_ids().len(), 0);

    let disabled = StubGateway::start(vec![StubResponse {
        status: 503,
        body: "{}".to_string(),
    }])
    .await;
    let mut cfg = config.clone();
    cfg.server_url = disabled.url();
    let transport = Transport::with_token(&cfg, None).expect("transport");
    assert_eq!(
        transport.probe_auth("c").await.expect("probe"),
        research_memory_bridge::transport::AuthProbe::Disabled
    );

    let invalid = StubGateway::start(vec![StubResponse {
        status: 401,
        body: "Unauthorized".to_string(),
    }])
    .await;
    let mut cfg = config.clone();
    cfg.server_url = invalid.url();
    let transport = Transport::with_token(&cfg, Some("bad".into())).expect("transport");
    match transport.probe_auth("c").await.expect("probe") {
        research_memory_bridge::transport::AuthProbe::Rejected(_) => {}
        other => panic!("expected rejection, got {other:?}"),
    }
}

#[tokio::test]
async fn batching_respects_the_configured_batch_size() {
    let home = TestHome::new();
    let mut config = home.config();
    config.batch_size = 2;
    let gateway = StubGateway::start(vec![
        StubResponse::ok(batch_response(
            &["rmb1_bat00001", "rmb1_bat00002"],
            &[],
            vec![],
        )),
        StubResponse::ok(batch_response(&["rmb1_bat00003"], &[], vec![])),
    ])
    .await;
    config.server_url = gateway.url();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    for id in ["rmb1_bat00001", "rmb1_bat00002", "rmb1_bat00003"] {
        spool.enqueue(&event(id, "x")).expect("insert");
    }
    let transport = Transport::with_token(&config, None).expect("transport");
    let report = drain_once(&config, &spool, &transport, 10).await;
    assert_eq!(report.batches, 2, "{report:?}");
    assert_eq!(report.accepted, 3);
    assert_eq!(gateway.request(0).event_ids().len(), 2);
    assert_eq!(gateway.request(1).event_ids().len(), 1);
}

#[tokio::test]
async fn session_end_is_declared_after_a_session_is_finalised() {
    let home = TestHome::new();
    let gateway = StubGateway::start(vec![
        // 1) the pending event batch
        StubResponse::ok(batch_response(&["rmb1_ses00001"], &[], vec![])),
        // 2) the session-end declaration
        StubResponse::ok(serde_json::json!({
            "schema_version": 1,
            "session_id": "s-1",
            "stored_message_count": 1,
            "observed_message_count": 1,
            "missing_event_count": 0,
            "reconciliation_required": false,
        })),
    ])
    .await;
    let mut config = home.config();
    config.server_url = gateway.url();
    let spool = Spool::open(&home.spool_path()).expect("spool");
    spool.enqueue(&event("rmb1_ses00001", "x")).expect("insert");
    spool
        .session_upsert("codex", "s-1", "s-1", "rmb1_ses00001", 1)
        .expect("upsert");
    spool.session_mark_ended("codex", "s-1").expect("ended");

    let transport = Transport::with_token(&config, None).expect("transport");
    let report = drain_once(&config, &spool, &transport, 5).await;
    assert_eq!(report.sessions_reconciled, 1, "{report:?}");
    assert_eq!(gateway.request_count(), 2);
    let request = gateway.request(1);
    assert!(
        request.path.ends_with("/api/conversations/session-end"),
        "{}",
        request.path
    );
    assert_eq!(request.json()["session_id"], "s-1");
    assert_eq!(request.json()["observed_message_count"], 1);
    // Declared once, not on every pass.
    assert!(spool.unreconciled_sessions(10).expect("pending").is_empty());
}
