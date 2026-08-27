import sqlite3

from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.config import AppConfig
from research_memory_gateway.secret_scan import REDACTED, redact_secrets, redact_text
from research_memory_gateway.service import ResearchMemoryService


def test_secret_scanner_redacts_common_credential_shapes() -> None:
    secrets = [
        "Bearer abcdefghijklmnopqrstuvwxyz123456",
        "sk-proj-ABCDEFGHIJKLMN1234567890",
        "ghp_1234567890ABCDEFGHIJ",
        "github_pat_1234567890_ABCDEFGHIJ",
        "eyJabcdefghijk.eyJmnopqrstuv.wxyzABCDEFGHIJK",
        "AKIA1234567890ABCDEF",
        "password=hunter2-secret",
        "token abc12345-secret",
        "postgresql://user:super-secret-password@db.local/database",
    ]

    for raw in secrets:
        sanitized, report = redact_text(raw)
        assert raw not in sanitized
        assert REDACTED in sanitized
        assert report.detected is True


def test_secret_scanner_redacts_nested_sensitive_fields_and_embedded_text() -> None:
    raw_secret = "sk-test-SECRET-123456789"
    sanitized, report = redact_secrets(
        {
            "metadata": {
                "api_key": raw_secret,
                "note": f"Authorization: Bearer {raw_secret}",
            },
            "claim": f"connection_string=postgresql://user:{raw_secret}@db.local/x",
            "entity": raw_secret,
        }
    )

    dumped = repr(sanitized)
    assert raw_secret not in dumped
    assert REDACTED in dumped
    assert report.redacted_count >= 3


def test_secret_scanner_does_not_redact_configuration_words_without_secret_value() -> None:
    text = "The API token is configured through an environment variable and is not stored here."

    sanitized, report = redact_text(text)

    assert sanitized == text
    assert report.detected is False


def test_secret_scanner_redacts_secret_shaped_dictionary_keys() -> None:
    raw_secret = "sk-test-SECRET-123456789"

    sanitized, report = redact_secrets({"metadata": {raw_secret: "value"}})

    dumped = repr(sanitized)
    assert raw_secret not in dumped
    assert "[REDACTED_KEY]" in dumped
    assert raw_secret not in repr(report.as_dict())


def test_secret_shaped_metadata_key_never_reaches_sqlite(tmp_path) -> None:
    raw_secret = "sk-test-SECRET-123456789"
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "secret-key.db")
    service = ResearchMemoryService(config, SQLiteMemoryBackend(config.backend.sqlite_path))

    saved = service.save_research_memory(
        user_confirmed=True,
        memory={
            "project": "security",
            "topic": "metadata key redaction",
            "memory_type": "paper_note",
            "title": "Metadata key redaction",
            "summary": "A safe memory.",
            "metadata": {raw_secret: "value"},
        },
    )

    assert raw_secret not in repr(saved.model_dump(mode="json"))
    with sqlite3.connect(config.backend.sqlite_path) as connection:
        rows = connection.execute(
            "SELECT title, summary, data FROM memories UNION ALL "
            "SELECT title, summary, claims FROM memories_fts"
        ).fetchall()
        raw_database_text = "\n".join(str(value) for row in rows for value in row)
    assert raw_secret not in raw_database_text
