"""Research Memory Gateway."""

__version__ = "0.2.6"

# MCP Python SDK v2 compatibility bridge:
# In MCP v2, model fields were migrated from camelCase to snake_case.
# We attach property aliases for readOnlyHint, destructiveHint, inputSchema, structuredContent
# so existing caller code and tests remain 100% compatible.
try:
    import mcp_types

    if not hasattr(mcp_types.ToolAnnotations, "readOnlyHint"):
        mcp_types.ToolAnnotations.readOnlyHint = property(lambda self: self.read_only_hint)  # type: ignore[attr-defined]
    if not hasattr(mcp_types.ToolAnnotations, "destructiveHint"):
        mcp_types.ToolAnnotations.destructiveHint = property(lambda self: self.destructive_hint)  # type: ignore[attr-defined]
    if not hasattr(mcp_types.Tool, "inputSchema"):
        mcp_types.Tool.inputSchema = property(lambda self: self.input_schema)  # type: ignore[attr-defined]
    if hasattr(mcp_types, "CallToolResult") and not hasattr(mcp_types.CallToolResult, "structuredContent"):
        mcp_types.CallToolResult.structuredContent = property(lambda self: self.structured_content)  # type: ignore[attr-defined]
except ImportError:
    pass
