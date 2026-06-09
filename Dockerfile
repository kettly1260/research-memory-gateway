FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY prompts ./prompts

RUN pip install --no-cache-dir -e .

EXPOSE 8787 8788
CMD ["research-memory-gateway", "--config", "/app/config.yaml", "--transport", "sse", "--host", "0.0.0.0", "--port", "8787"]
