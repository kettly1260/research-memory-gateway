# Build Frontend
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY src/research_memory_gateway/webui/frontend/package*.json ./
RUN npm install
COPY src/research_memory_gateway/webui/frontend/ ./
RUN npm run build

# Build Backend
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY prompts ./prompts

# Copy built frontend assets to the correct static folder
COPY --from=frontend-builder /app/frontend/dist ./src/research_memory_gateway/webui/static/dist

RUN pip install --no-cache-dir -e .

EXPOSE 8787 8788
CMD ["research-memory-gateway", "--config", "/app/config.yaml", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8787"]
