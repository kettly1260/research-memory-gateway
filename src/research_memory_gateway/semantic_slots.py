import re
from dataclasses import dataclass

_PATH_ROLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "repository_path",
        re.compile(r"\b(?:repository|repo)(?:\s+path)?\b|仓库(?:路径|目录)?", re.IGNORECASE),
    ),
    (
        "workspace_path",
        re.compile(r"\bworkspace(?:\s+path)?\b|工作区(?:路径|目录)?", re.IGNORECASE),
    ),
    (
        "config_path",
        re.compile(
            r"\bconfig(?:uration)?(?:\s+file)?(?:\s+path)?\b|配置(?:文件)?(?:路径|目录)?",
            re.IGNORECASE,
        ),
    ),
    (
        "executable_path",
        re.compile(
            r"\b(?:executable|binary)(?:\s+path)?\b|(?:可执行文件|程序)(?:路径)?",
            re.IGNORECASE,
        ),
    ),
    ("directory_path", re.compile(r"\b(?:directory|folder)\b|目录", re.IGNORECASE)),
    ("project_path", re.compile(r"\bpath\b|路径", re.IGNORECASE)),
)

_STATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("current_branch", re.compile(r"\bbranch\b|分支", re.IGNORECASE)),
    ("selected_model", re.compile(r"\bmodel\b|模型", re.IGNORECASE)),
    ("active_config", re.compile(r"\bconfig(?:uration)?\b|配置", re.IGNORECASE)),
    ("current_workflow", re.compile(r"\bworkflow\b|工作流|流程", re.IGNORECASE)),
)

_STATE_ASSIGNMENT_MARKERS = (
    " is ",
    " was ",
    " = ",
    " changed ",
    " changed to ",
    " using ",
    " use ",
    " selected ",
    " active ",
    " current ",
    "为",
    "是",
    "改为",
    "更换",
    "使用",
    "采用",
    "当前",
    "执行中",
)

_ENGLISH_PREFIX_MODIFIERS = {
    "a",
    "an",
    "the",
    "current",
    "active",
    "selected",
    "old",
    "new",
    "previous",
    "former",
    "original",
    "updated",
    "latest",
    "existing",
    "prior",
}

_CHINESE_PREFIX_MODIFIERS = (
    "原来的",
    "以前的",
    "之前的",
    "当前的",
    "现在的",
    "旧的",
    "新的",
    "原",
    "旧",
    "新",
    "当前",
    "现在",
    "该",
    "这个",
)

_POST_PROPERTY_QUALIFIER_RE = re.compile(r"^\s*(?:for|of)\b", re.IGNORECASE)
_POST_PROPERTY_ASSIGNMENT_RE = re.compile(
    r"\s+(?:is|was|changed(?:\s+to)?|uses?|using|selected|active|current)\b|\s*=",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AmbientSemanticSlot:
    role: str
    subject: str | None = None

    def key(self, project: str) -> str:
        project_key = _slug(project) or project.strip().lower()
        if self.subject and self.subject != project_key:
            return f"{project_key}.{self.subject}.{self.role}"
        return f"{project_key}.{self.role}"


def infer_ambient_semantic_slot(content: str, *, project: str) -> AmbientSemanticSlot | None:
    """Infer only high-confidence mutable Ambient state slots."""

    text = " ".join(content.strip().split())
    if not text:
        return None
    lowered = f" {text.lower()} "

    path_match = re.search(r"\b[A-Za-z]:[\\/]", text)
    if path_match:
        property_text = text[: path_match.start()]
        for role, pattern in _PATH_ROLE_PATTERNS:
            match = pattern.search(property_text)
            if match is None:
                continue
            subject, ambiguous = _subject_around_property(property_text, match)
            if ambiguous:
                return None
            return AmbientSemanticSlot(role=role, subject=_dedupe_project_subject(subject, project))

    if any(marker in lowered for marker in ("project state", "project status", "项目状态", "当前进度")):
        return AmbientSemanticSlot(role="project_state")

    if not any(marker in lowered for marker in _STATE_ASSIGNMENT_MARKERS):
        return None

    for role, pattern in _STATE_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        subject, ambiguous = _subject_around_property(text, match)
        if ambiguous:
            return None
        return AmbientSemanticSlot(role=role, subject=_dedupe_project_subject(subject, project))

    return None


def semantic_slots_match(existing_content: str, incoming_content: str, *, project: str) -> bool:
    existing = infer_ambient_semantic_slot(existing_content, project=project)
    incoming = infer_ambient_semantic_slot(incoming_content, project=project)
    if existing is None or incoming is None:
        return False
    return existing.key(project) == incoming.key(project)


def _extract_subject(prefix: str) -> str | None:
    candidate = re.split(r"[。.!?！？;；:\n]", prefix)[-1].strip(" ,，()[]{}'\"")
    if not candidate:
        return None

    for modifier in _CHINESE_PREFIX_MODIFIERS:
        if candidate.startswith(modifier):
            candidate = candidate[len(modifier) :].strip()
    candidate = re.sub(r"\s*的\s*$", "", candidate).strip()

    english_tokens = re.findall(r"[A-Za-z0-9_+.-]+", candidate)
    chinese_tokens = re.findall(r"[\u4e00-\u9fff]+", candidate)
    if english_tokens and not chinese_tokens:
        tokens = english_tokens[-6:]
        while tokens and tokens[0].lower().rstrip("'s") in _ENGLISH_PREFIX_MODIFIERS:
            tokens.pop(0)
        subject = _slug(" ".join(token.rstrip("'s") for token in tokens if token))
        return subject or None

    if chinese_tokens:
        chinese = chinese_tokens[-1]
        for modifier in _CHINESE_PREFIX_MODIFIERS:
            chinese = chinese.removeprefix(modifier)
        chinese = chinese.removesuffix("的")
        subject = _slug(chinese)
        return subject or None

    subject = _slug(candidate)
    return subject or None


def _subject_around_property(text: str, match: re.Match[str]) -> tuple[str | None, bool]:
    """Return a normalized subject and whether an explicit qualifier was ambiguous.

    Subject-before-property and property-for/of-subject are both accepted.  If the
    sentence explicitly contains a post-property qualifier but we cannot parse it,
    fail closed instead of emitting a coarse project-level slot that could archive a
    different valid state.
    """

    suffix = text[match.end() :]
    suffix_subject = _extract_post_property_subject(suffix)
    if suffix_subject is not None:
        return suffix_subject, False
    if _POST_PROPERTY_QUALIFIER_RE.match(suffix):
        return None, True
    return _extract_subject(text[: match.start()]), False


def _extract_post_property_subject(suffix: str) -> str | None:
    qualifier = _POST_PROPERTY_QUALIFIER_RE.match(suffix)
    if qualifier is None:
        return None
    remainder = suffix[qualifier.end() :].strip()
    assignment = _POST_PROPERTY_ASSIGNMENT_RE.search(f" {remainder}")
    if assignment is None:
        return None
    subject_text = f" {remainder}"[: assignment.start()].strip()
    if not subject_text:
        return None
    subject = _extract_subject(subject_text)
    return subject or None


def _dedupe_project_subject(subject: str | None, project: str) -> str | None:
    if not subject:
        return None
    project_key = _slug(project)
    return None if project_key and subject == project_key else subject


def _slug(value: str) -> str:
    normalized = value.strip().lower().replace("'s", "")
    normalized = re.sub(r"[^\w\u4e00-\u9fff+.-]+", "-", normalized)
    return normalized.strip("-._")
