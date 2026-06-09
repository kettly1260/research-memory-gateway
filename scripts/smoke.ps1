$ErrorActionPreference = "Stop"

python -c "from research_memory_gateway.config import AppConfig; from research_memory_gateway.server import build_mcp; build_mcp(AppConfig()); print('smoke-ok')"
