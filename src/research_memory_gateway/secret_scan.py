from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import ResearchMemory

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_MARKERS = (
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
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
            flags=re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{12,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{12,}\b")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b", flags=re.IGNORECASE)),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
)

_BEARER_RE = re.compile(
    r"\b(?P<label>bearer)\s+(?P<value>[A-Za-z0-9._~+/=-]{8,})",
    flags=re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9+.-]*://[^:/\s]+:)(?P<value>[^@\s/]+)(?P<suffix>@)"
)
_LABELED_SECRET_RE = re.compile(
    r"(?P<label>\b(?:api[ _-]?(?:key|token)|auth(?:entication)?[ _-]?token|token|"
    r"access[ _-]?token|refresh[ _-]?token|"
    r"password|passwd|pwd|secret|authorization|cookie|session(?:[ _-]?(?:id|token|key))?|"
    r"connection[ _-]?string|private[ _-]?key|aws[ _-]?secret[ _-]?access[ _-]?key)\b)"
    r"(?P<sep>\s*(?:(?:is|=|:|：)\s*)?)"
    r"(?P<quote>[\"']?)(?P<value>[^\s,;，；\"']{6,})(?P=quote)",
    flags=re.IGNORECASE,
)

_NON_SECRET_WORDS = {
    "configured",
    "enabled",
    "disabled",
    "missing",
    "unset",
    "required",
    "optional",
    "present",
    "available",
}


@dataclass
class SecretFinding:
    kind: str
    path: str


@dataclass
class SecretScanReport:
    findings: list[SecretFinding] = field(default_factory=list)

    @property
    def redacted_count(self) -> int:
        return len(self.findings)

    @property
    def detected(self) -> bool:
        return bool(self.findings)

    def add(self, kind: str, path: str) -> None:
        self.findings.append(SecretFinding(kind=kind, path=path))

    def as_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "redacted_count": self.redacted_count,
            "kinds": sorted({item.kind for item in self.findings}),
            "paths": sorted({item.path for item in self.findings}),
        }


def sanitize_memory(memory: ResearchMemory) -> tuple[ResearchMemory, SecretScanReport]:
    sanitized, report = redact_secrets(memory.model_dump(mode="json"))
    return ResearchMemory.model_validate(sanitized), report


def redact_secrets(value: Any, *, path: str = "$", report: SecretScanReport | None = None) -> tuple[Any, SecretScanReport]:
    active_report = report or SecretScanReport()
    sanitized = _redact_value(value, path=path, report=active_report)
    return sanitized, active_report


def redact_text(value: str, *, path: str = "$") -> tuple[str, SecretScanReport]:
    report = SecretScanReport()
    return _redact_text(value, path=path, report=report), report


def _redact_value(value: Any, *, path: str, report: SecretScanReport) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            raw_key = str(key)
            sanitized_key = _redact_text(raw_key, path=f"{path}.[key]", report=report)
            if sanitized_key != raw_key:
                sanitized_key = _unique_redacted_key(sanitized)
            child_path = f"{path}.{sanitized_key}"
            if _is_sensitive_key(raw_key) and item not in (None, "", REDACTED):
                sanitized[sanitized_key] = REDACTED
                report.add("sensitive_field", child_path)
            else:
                sanitized[sanitized_key] = _redact_value(item, path=child_path, report=report)
        return sanitized
    if isinstance(value, list):
        return [
            _redact_value(item, path=f"{path}[{index}]", report=report)
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return tuple(
            _redact_value(item, path=f"{path}[{index}]", report=report)
            for index, item in enumerate(value)
        )
    if isinstance(value, str):
        return _redact_text(value, path=path, report=report)
    return value


def _redact_text(value: str, *, path: str, report: SecretScanReport) -> str:
    sanitized = value

    def redact_named(match: re.Match[str], kind: str) -> str:
        report.add(kind, path)
        return REDACTED

    for kind, pattern in _PATTERNS:
        sanitized = pattern.sub(lambda match, marker=kind: redact_named(match, marker), sanitized)

    def bearer_repl(match: re.Match[str]) -> str:
        report.add("bearer_token", path)
        return f"{match.group('label')} {REDACTED}"

    sanitized = _BEARER_RE.sub(bearer_repl, sanitized)

    def url_repl(match: re.Match[str]) -> str:
        report.add("url_password", path)
        return f"{match.group('prefix')}{REDACTED}{match.group('suffix')}"

    sanitized = _URL_USERINFO_RE.sub(url_repl, sanitized)

    def labeled_repl(match: re.Match[str]) -> str:
        raw_value = match.group("value")
        separator = match.group("sep")
        if not _looks_secret_value(raw_value, separator=separator):
            return match.group(0)
        report.add("labeled_secret", path)
        return f"{match.group('label')}{separator}{REDACTED}"

    return _LABELED_SECRET_RE.sub(labeled_repl, sanitized)


def _looks_secret_value(value: str, *, separator: str) -> bool:
    lowered = value.lower()
    if lowered in _NON_SECRET_WORDS:
        return False
    if any(marker in separator for marker in ("=", ":", "：")) or "is" in separator.lower().split():
        return True
    if len(value) >= 16:
        return True
    return any(char.isdigit() for char in value) and (
        any(not char.isalnum() for char in value) or len(value) >= 8
    )


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_").replace(" ", "_")
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _unique_redacted_key(existing: dict[str, Any]) -> str:
    base = "[REDACTED_KEY]"
    if base not in existing:
        return base
    index = 2
    while f"{base}_{index}" in existing:
        index += 1
    return f"{base}_{index}"
