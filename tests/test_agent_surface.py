import anyio

from research_memory_gateway.agent_surface.capture import capture_memory
from research_memory_gateway.agent_surface.recall import get_project_state, recall_memory
from research_memory_gateway.agent_surface.tools import AGENT_TOOL_NAMES
from research_memory_gateway.agent_surface.verify import verify_memory
from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.config import AppConfig
from research_memory_gateway.models import MemoryStatus, MemoryTier, ResearchMemory
from research_memory_gateway.server import ADMIN_TOOL_NAMES, build_mcp
from research_memory_gateway.service import ResearchMemoryService


def make_service(tmp_path) -> ResearchMemoryService:
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "memory.db")
    backend = SQLiteMemoryBackend(config.backend.sqlite_path)
    return ResearchMemoryService(config, backend)


def tool_names(mcp) -> set[str]:
    return {tool.name for tool in anyio.run(mcp.list_tools)}


def test_research_memory_gets_stable_generated_claim_id() -> None:
    payload = {
        "memory_id": "mem_stable_claim",
        "project": "demo",
        "topic": "stable claim IDs",
        "memory_type": "paper_note",
        "title": "Stable claim",
        "summary": "Stable claim IDs are generated for legacy payloads.",
        "claims": [{"claim": "A durable claim"}],
    }
    first = ResearchMemory.model_validate(payload)
    second = ResearchMemory.model_validate(payload)

    assert first.claims[0].claim_id is not None
    assert first.claims[0].claim_id.startswith("claim_")
    assert first.claims[0].claim_id == second.claims[0].claim_id


def test_recall_memory_returns_compact_context(tmp_path) -> None:
    service = make_service(tmp_path)
    memory = ResearchMemory.model_validate(
        {
            "project": "origin-mcp",
            "topic": "repository path",
            "memory_type": "workflow_plan",
            "memory_tier": "ambient",
            "title": "Origin MCP repository path",
            "summary": "Origin MCP repository is located at G:\\LLM\\originlab-jx.",
            "metadata": {"plan_status": "active", "plan_type": "mcp_setup"},
        }
    )
    service.backend.save(memory)

    result = recall_memory(service, query="之前 Origin MCP 的仓库路径", limit=5)

    assert result["result_count"] == 1
    assert result["project"] == "origin-mcp"
    assert result["results"][0]["memory_id"] == memory.memory_id
    assert result["results"][0]["content"].startswith("Origin MCP repository")
    assert "evidence" not in result["results"][0]
    assert "memory_type" not in result["results"][0]


def test_recall_memory_rewrites_history_filler_for_keyword_mode(tmp_path) -> None:
    service = make_service(tmp_path)
    memory = ResearchMemory.model_validate(
        {
            "project": "Fe3-probe",
            "topic": "Fe3+ stock preparation",
            "memory_type": "material_system",
            "title": "Fe3+ nitrate stock preparation",
            "summary": "Fe3+ 储备液浓度为 10 mM，并使用 0.1 M HNO3 作为酸性介质。",
            "claims": [{"claim": "Fe3+ 储备液浓度为 10 mM，并使用 0.1 M HNO3 作为酸性介质。"}],
        }
    )
    service.backend.save(memory)

    result = recall_memory(service, query="之前 Fe 的硝酸溶液怎么配的？", project="Fe3-probe")

    assert result["result_count"] == 1
    assert result["results"][0]["memory_id"] == memory.memory_id
    assert "HNO3" in result["rewritten_query"]
    assert "储备液" in result["rewritten_query"]


def test_recall_returns_matched_claim_with_claim_verification(tmp_path) -> None:
    service = make_service(tmp_path)
    memory = ResearchMemory.model_validate(
        {
            "project": "Hg-probe",
            "topic": "sulfur-doped carbon dots",
            "memory_type": "paper_note",
            "title": "Sulfur-doped carbon dot Hg2+ note",
            "summary": "A reusable note about sulfur-doped carbon dots as Hg2+ fluorescence probes.",
            "evidence": [
                {
                    "evidence_id": "ev_soft_acid",
                    "type": "paper_excerpt",
                    "quote": "Sulfur-containing groups support Hg2+ binding.",
                }
            ],
            "claims": [
                {
                    "claim_id": "claim_supported",
                    "claim": "Sulfur-containing surface groups may improve Hg2+ affinity through soft acid-soft base interactions.",
                    "verification_status": "evidence_backed",
                    "evidence_ids": ["ev_soft_acid"],
                },
                {
                    "claim_id": "claim_unverified",
                    "claim": "The same material may also detect an unrelated analyte.",
                    "verification_status": "unverified",
                    "evidence_ids": [],
                },
            ],
        }
    )
    service.backend.save(memory)

    result = recall_memory(service, query="soft acid soft base interactions", project="Hg-probe")

    recalled = result["results"][0]
    assert recalled["content"].startswith("Sulfur-containing surface groups")
    assert recalled["verification"] == "evidence_backed"
    assert recalled["matched_claims"] == [
        {
            "claim_id": "claim_supported",
            "claim": "Sulfur-containing surface groups may improve Hg2+ affinity through soft acid-soft base interactions.",
            "verification": "evidence_backed",
            "confidence": "medium",
        }
    ]


def test_capture_memory_auto_saves_ambient_project_context(tmp_path) -> None:
    service = make_service(tmp_path)

    result = capture_memory(
        service,
        content="Origin MCP repository is located at G:\\LLM\\originlab-jx and this is the active workspace.",
        project="origin-mcp",
    )

    assert result["action"] == "saved"
    assert result["memory_tier"] == "ambient"
    saved = service.get_research_memory(result["memory_id"])
    assert saved.memory_tier == MemoryTier.ambient
    assert saved.metadata["plan_status"] == "active"
    assert saved.metadata["plan_type"] == "mcp_setup"


def test_capture_memory_queues_trusted_research_fact(tmp_path) -> None:
    service = make_service(tmp_path)

    result = capture_memory(
        service,
        content="本次 Pyr PL 测试使用新购买的 HPLC 级 DMSO，之前使用的是化学纯 DMSO。",
        project="Fe3-probe",
    )

    assert result["action"] == "queued"
    assert result["memory_tier"] == "trusted"
    proposal = service.get_memory_proposal(result["proposal_id"])
    assert proposal.suggested_memory.memory_tier == MemoryTier.trusted
    assert proposal.suggested_memory.claims[0].verification_status.value == "unverified"
    assert proposal.suggested_memory.evidence
    assert service.search_research_memory(query="HPLC DMSO", project="Fe3-probe") == []


def test_capture_scientific_measurements_generalize_beyond_fe_pl(tmp_path) -> None:
    service = make_service(tmp_path)
    cases = [
        "碳硼烷改性树脂的玻璃化转变温度为 312 °C。",
        "该材料介电常数为 2.8。",
        "热导率达到 1.25 W m-1 K-1。",
        "拉伸强度为 48 MPa。",
        "XRD 显示 2theta=25.4° 出现新峰。",
        "TGA 的 5% 失重温度为 486 °C。",
    ]

    for index, content in enumerate(cases):
        result = capture_memory(service, content=content, project=f"materials-{index}")
        assert result["action"] == "queued", content
        assert result["memory_tier"] == "trusted", content


def test_uncertain_durable_research_statement_fails_safe_to_trusted_review(tmp_path) -> None:
    service = make_service(tmp_path)

    result = capture_memory(
        service,
        content="该涂层经过热处理后表现出明显更高的抗氧化稳定性。",
        project="coating-research",
        importance="high",
    )

    assert result["action"] == "queued"
    assert result["memory_tier"] == "trusted"


def test_capture_redacts_secret_before_ambient_autosave_and_fts(tmp_path) -> None:
    service = make_service(tmp_path)
    raw_secret = "sk-test-SECRET-123456789"
    result = capture_memory(
        service,
        content=(
            "MCP config uses API token "
            f"{raw_secret} and repository path G:\\LLM\\memory."
        ),
        project="memory-gateway",
    )

    assert result["action"] == "saved"
    assert result["memory_tier"] == "ambient"
    assert result["secret_redaction"]["detected"] is True
    saved = service.get_research_memory(result["memory_id"])
    dumped = saved.model_dump_json()
    assert raw_secret not in dumped
    assert "[REDACTED]" in dumped
    assert service.search_research_memory(query="SECRET-123456789", project="memory-gateway") == []


def test_service_sanitizes_secrets_across_memory_fields(tmp_path) -> None:
    service = make_service(tmp_path)
    raw_secret = "ghp_1234567890ABCDEFGHIJ"
    saved = service.save_research_memory(
        user_confirmed=True,
        memory={
            "project": "security",
            "topic": "secret scanner",
            "memory_type": "paper_note",
            "title": f"Token {raw_secret}",
            "summary": f"Authorization Bearer {raw_secret}",
            "claims": [{"claim": f"password: {raw_secret}"}],
            "evidence": [{"quote": f"api_key={raw_secret}"}],
            "source_refs": [{"source_type": "note", "excerpt": f"token {raw_secret}"}],
            "entities": [{"name": raw_secret, "entity_type": "credential"}],
            "metadata": {"note": f"cookie={raw_secret}", "api_key": raw_secret},
        },
        confirmation={"source": "chat", "text": f"save token {raw_secret}", "confirmed_by": "user"},
    )

    dumped = saved.model_dump_json()
    assert raw_secret not in dumped
    assert "[REDACTED]" in dumped
    assert service.search_research_memory(query="1234567890ABCDEFGHIJ", project="security") == []


def test_capture_memory_user_confirmed_saves_trusted_research_fact(tmp_path) -> None:
    service = make_service(tmp_path)

    result = capture_memory(
        service,
        content="Fe3+ 储备液浓度为 10 mM，并使用 0.1 M HNO3 作为酸性介质。",
        project="Fe3-probe",
        user_confirmed=True,
    )

    assert result["action"] == "saved"
    assert result["memory_tier"] == "trusted"
    saved = service.get_research_memory(result["memory_id"])
    assert saved.memory_tier == MemoryTier.trusted
    assert saved.claims[0].verification_status.value == "unverified"
    assert saved.metadata["save_confirmation"]["confirmed_by"] == "user"


def test_capture_memory_deduplicates_exact_ambient_capture(tmp_path) -> None:
    service = make_service(tmp_path)
    content = "Memory gateway repository uses G:\\LLM\\memory as the active workspace path."

    first = capture_memory(service, content=content, project="memory-gateway")
    second = capture_memory(service, content=content, project="memory-gateway")

    assert first["action"] == "saved"
    assert second["action"] == "duplicate"
    assert second["memory_id"] == first["memory_id"]


def test_ambient_state_update_supersedes_previous_active_value(tmp_path) -> None:
    service = make_service(tmp_path)
    old = capture_memory(
        service,
        content="Origin MCP repository path is G:\\LLM\\originlab-old.",
        project="origin-mcp",
    )
    new = capture_memory(
        service,
        content="Origin MCP repository path changed to G:\\LLM\\originlab-jx.",
        project="origin-mcp",
    )

    old_memory = service.get_research_memory(old["memory_id"])
    new_memory = service.get_research_memory(new["memory_id"])
    assert old_memory.memory_status == MemoryStatus.archived
    assert old_memory.claims[0].verification_status.value == "superseded"
    assert old_memory.metadata["superseded_by"] == new_memory.memory_id
    assert old_memory.metadata["semantic_key"] == new_memory.metadata["semantic_key"]

    recalled = recall_memory(service, query="Origin MCP repository path", project="origin-mcp")
    assert recalled["result_count"] == 1
    assert recalled["results"][0]["memory_id"] == new_memory.memory_id
    assert "originlab-jx" in recalled["results"][0]["content"]


def test_ambient_state_update_supersedes_alpha_memory_without_semantic_key(tmp_path) -> None:
    service = make_service(tmp_path)
    legacy = ResearchMemory.model_validate(
        {
            "project": "origin-mcp",
            "topic": "repository path",
            "memory_type": "workflow_plan",
            "memory_tier": "ambient",
            "title": "Origin MCP repository path",
            "summary": "Origin MCP repository path is G:\\LLM\\originlab-old.",
            "claims": [{"claim": "Origin MCP repository path is G:\\LLM\\originlab-old."}],
            "tags": ["ambient", "agent-captured", "project-path"],
            "metadata": {"plan_status": "active", "plan_type": "mcp_setup"},
        }
    )
    service.backend.save(legacy)

    new = capture_memory(
        service,
        content="Origin MCP repository path changed to G:\\LLM\\originlab-jx.",
        project="origin-mcp",
    )

    legacy_after = service.get_research_memory(legacy.memory_id)
    assert legacy_after.memory_status == MemoryStatus.archived
    assert legacy_after.metadata["semantic_key"] == service.get_research_memory(new["memory_id"]).metadata["semantic_key"]


def test_capture_memory_deduplicates_pending_trusted_capture(tmp_path) -> None:
    service = make_service(tmp_path)
    content = "本次 PL 测试使用 20 mM HEPES 缓冲液。"

    first = capture_memory(service, content=content, project="Fe3-probe")
    second = capture_memory(service, content=content, project="Fe3-probe")

    assert first["action"] == "queued"
    assert second["action"] == "duplicate"
    assert second["proposal_id"] == first["proposal_id"]


def test_verify_memory_expands_claim_evidence_and_source_ref(tmp_path) -> None:
    service = make_service(tmp_path)
    captured = capture_memory(
        service,
        content="Fe3+ 储备液浓度为 10 mM，并使用 0.1 M HNO3 作为酸性介质。",
        project="Fe3-probe",
        source_context="current conversation",
        user_confirmed=True,
    )
    memory = service.get_research_memory(captured["memory_id"])

    result = verify_memory(
        service,
        memory_id=memory.memory_id,
        claim_id=memory.claims[0].claim_id,
    )

    assert result["memory_id"] == memory.memory_id
    assert result["verification"] == "unverified"
    assert result["claims"][0]["claim_id"] == memory.claims[0].claim_id
    assert result["claims"][0]["evidence"][0]["type"] == "conversation_assertion"
    assert result["source_refs"][0]["source_type"] == "conversation"


def test_conversation_source_ref_uses_real_anchor_fields_when_available(tmp_path) -> None:
    service = make_service(tmp_path)
    captured = capture_memory(
        service,
        content="拉伸强度为 48 MPa。",
        project="materials",
        source_context="current conversation",
        source_client="codex",
        conversation_id="conv_123",
        message_id="msg_456",
        session_id="session_789",
        source_timestamp="2026-08-27T19:00:00-07:00",
        user_confirmed=True,
    )
    memory = service.get_research_memory(captured["memory_id"])

    resolved = service.open_source_ref(memory.source_refs[0].model_dump(mode="json"))

    assert resolved["kind"] == "conversation_anchor"
    assert resolved["resolvable"] is False
    assert resolved["source_id"] == "conv_123"
    assert resolved["message_range"] == "msg_456"
    assert resolved["metadata"]["client"] == "codex"


def test_get_project_state_includes_pending_trusted_capture(tmp_path) -> None:
    service = make_service(tmp_path)
    capture_memory(
        service,
        content="Origin MCP repository is located at G:\\LLM\\originlab-jx and this is the active workspace.",
        project="origin-mcp",
    )
    queued = capture_memory(
        service,
        content="本次 PL 测试使用 20 mM HEPES 缓冲液。",
        project="origin-mcp",
    )

    state = get_project_state(service, project="origin-mcp")

    assert state["memory_count"] == 1
    assert state["pending_proposals"][0]["proposal_id"] == queued["proposal_id"]


def test_agent_surface_exposes_only_four_agent_tools(tmp_path) -> None:
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "agent.db")
    config.server.surface = "agent"

    names = tool_names(build_mcp(config))

    assert names == set(AGENT_TOOL_NAMES)


def test_admin_surface_keeps_legacy_tools_without_agent_tools(tmp_path) -> None:
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "admin.db")
    config.server.surface = "admin"

    names = tool_names(build_mcp(config))

    assert names == set(ADMIN_TOOL_NAMES)
    assert not names.intersection(AGENT_TOOL_NAMES)


def test_full_surface_exposes_agent_and_admin_tools(tmp_path) -> None:
    config = AppConfig()
    config.backend.sqlite_path = str(tmp_path / "full.db")
    config.server.surface = "full"

    names = tool_names(build_mcp(config))

    assert names == set(ADMIN_TOOL_NAMES).union(AGENT_TOOL_NAMES)
