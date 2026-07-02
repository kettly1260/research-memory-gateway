from __future__ import annotations

from typing import Any


PLAN_REQUIRED_MEMORY_TYPES = {"experiment_plan", "workflow_plan"}
PLAN_STATUS_KEYS = {"draft", "accepted", "active", "superseded"}
ACTIONABLE_PLAN_STATUSES = {"accepted", "active"}
PLAN_TYPE_KEYS = {
    "agent_memory_policy",
    "mcp_setup",
    "research_workflow",
    "writing_workflow",
    "deployment_workflow",
    "project_governance",
}


MEMORY_TYPES = [
    {
        "key": "literature_review",
        "label_en": "Literature Review",
        "label_zh": "文献综述",
        "description_zh": "可复用的文献调研、路线比较或研究空白总结。",
        "description_en": "Reusable literature surveys, route comparisons, or research gap summaries.",
    },
    {
        "key": "paper_note",
        "label_en": "Paper Note",
        "label_zh": "论文笔记",
        "description_zh": "单篇或少量论文的结构化笔记和可核查结论。",
        "description_en": "Structured notes and checkable conclusions from one or a few papers.",
    },
    {
        "key": "synthesis_route",
        "label_en": "Synthesis Route",
        "label_zh": "合成路线",
        "description_zh": "材料、分子或前驱体的合成路线、条件和风险。",
        "description_en": "Synthesis routes, conditions, and risks for materials, molecules, or precursors.",
    },
    {
        "key": "experiment_plan",
        "label_en": "Experiment Plan",
        "label_zh": "实验规划",
        "description_zh": "实验设计、对照组、参数、表征和预期判断标准。",
        "description_en": "Experimental design, controls, parameters, characterization, and expected criteria.",
        "requires_plan_status": True,
    },
    {
        "key": "mechanism_hypothesis",
        "label_en": "Mechanism Hypothesis",
        "label_zh": "机制假设",
        "description_zh": "机理假说、证据链、反证条件和验证思路。",
        "description_en": "Mechanistic hypotheses, evidence chains, falsification criteria, and validation ideas.",
    },
    {
        "key": "material_system",
        "label_en": "Material System",
        "label_zh": "材料体系",
        "description_zh": "材料体系、结构-性能关系、性能指标和关键变量。",
        "description_en": "Material systems, structure-property relations, metrics, and key variables.",
    },
    {
        "key": "presentation_outline",
        "label_en": "Presentation Outline",
        "label_zh": "汇报提纲",
        "description_zh": "PPT、报告、组会、答辩或论文叙事结构。",
        "description_en": "Narrative structures for slides, reports, group meetings, defenses, or papers.",
    },
    {
        "key": "research_decision",
        "label_en": "Research Decision",
        "label_zh": "研究决策",
        "description_zh": "已经做出的研究取舍、路线选择或项目决策。",
        "description_en": "Settled research tradeoffs, route choices, or project decisions.",
    },
    {
        "key": "workflow_plan",
        "label_en": "Workflow Plan",
        "label_zh": "工作流规划",
        "description_zh": "agent 规则、MCP 配置、部署、写作流程或项目治理规划。",
        "description_en": "Plans for agent policy, MCP setup, deployment, writing workflow, or project governance.",
        "requires_plan_status": True,
    },
]


PLAN_STATUSES = [
    {
        "key": "draft",
        "label_en": "Draft",
        "label_zh": "草案",
        "actionable": False,
        "description_zh": "讨论中的规划，只能作为上下文，不能直接作为行动依据。",
        "description_en": "A plan under discussion; usable as context but not as an execution basis.",
    },
    {
        "key": "accepted",
        "label_en": "Accepted",
        "label_zh": "已确认",
        "actionable": True,
        "description_zh": "用户已经确认，可作为后续行动依据。",
        "description_en": "Confirmed by the user and usable as a basis for future action.",
    },
    {
        "key": "active",
        "label_en": "Active",
        "label_zh": "执行中",
        "actionable": True,
        "description_zh": "正在执行或持续生效的规划。",
        "description_en": "A plan currently being executed or continuously in effect.",
    },
    {
        "key": "superseded",
        "label_en": "Superseded",
        "label_zh": "已被取代",
        "actionable": False,
        "description_zh": "被新规划取代，只能作为历史记录。",
        "description_en": "Replaced by a newer plan and retained as history only.",
    },
]


PLAN_TYPES = [
    {"key": "agent_memory_policy", "label_en": "Agent Memory Policy", "label_zh": "Agent 记忆策略"},
    {"key": "mcp_setup", "label_en": "MCP Setup", "label_zh": "MCP 配置"},
    {"key": "research_workflow", "label_en": "Research Workflow", "label_zh": "科研工作流"},
    {"key": "writing_workflow", "label_en": "Writing Workflow", "label_zh": "写作工作流"},
    {"key": "deployment_workflow", "label_en": "Deployment Workflow", "label_zh": "部署工作流"},
    {"key": "project_governance", "label_en": "Project Governance", "label_zh": "项目治理"},
]


PROPOSAL_STATUSES = [
    {"key": "pending", "label_en": "Pending", "label_zh": "待审"},
    {"key": "approved", "label_en": "Approved", "label_zh": "已批准"},
    {"key": "rejected", "label_en": "Rejected", "label_zh": "已驳回"},
    {"key": "needs_edit", "label_en": "Needs Edit", "label_zh": "需修改"},
    {"key": "saved", "label_en": "Saved", "label_zh": "已保存"},
    {"key": "expired", "label_en": "Expired", "label_zh": "已过期"},
]


def get_memory_taxonomy() -> dict[str, Any]:
    return {
        "memory_types": MEMORY_TYPES,
        "plan_statuses": PLAN_STATUSES,
        "plan_types": PLAN_TYPES,
        "proposal_statuses": PROPOSAL_STATUSES,
        "rules": {
            "plan_required_memory_types": sorted(PLAN_REQUIRED_MEMORY_TYPES),
            "actionable_plan_statuses": sorted(ACTIONABLE_PLAN_STATUSES),
        },
    }


def validate_plan_metadata(memory_type: str, metadata: dict[str, Any]) -> None:
    plan_status = metadata.get("plan_status")
    plan_type = metadata.get("plan_type")

    if memory_type in PLAN_REQUIRED_MEMORY_TYPES and plan_status not in PLAN_STATUS_KEYS:
        allowed = ", ".join(sorted(PLAN_STATUS_KEYS))
        raise ValueError(
            f"{memory_type} requires metadata.plan_status; allowed values: {allowed}"
        )

    if plan_status is not None and plan_status not in PLAN_STATUS_KEYS:
        allowed = ", ".join(sorted(PLAN_STATUS_KEYS))
        raise ValueError(f"metadata.plan_status must be one of: {allowed}")

    if plan_type is not None and plan_type not in PLAN_TYPE_KEYS:
        allowed = ", ".join(sorted(PLAN_TYPE_KEYS))
        raise ValueError(f"metadata.plan_type must be one of: {allowed}")
