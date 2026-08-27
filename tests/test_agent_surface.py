import anyio

from research_memory_gateway.agent_surface.capture import capture_memory
from research_memory_gateway.agent_surface.recall import get_project_state, recall_memory
from research_memory_gateway.agent_surface.tools import AGENT_TOOL_NAMES
from research_memory_gateway.agent_surface.verify import verify_memory
from research_memory_gateway.backends import SQLiteMemoryBackend
from research_memory_gateway.config import AppConfig
from research_memory_gateway.models import MemoryTier, ResearchMemory
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
