FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HERMES_MCP_JOB_STORE_PATH=/var/lib/hermes-mcp/jobs.sqlite3

RUN addgroup --system hermes-mcp \
    && adduser --system --ingroup hermes-mcp --home /home/hermes-mcp hermes-mcp

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir .

RUN mkdir -p /var/lib/hermes-mcp \
    && chown hermes-mcp:hermes-mcp /var/lib/hermes-mcp \
    && chmod 700 /var/lib/hermes-mcp

USER hermes-mcp

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/.well-known/oauth-authorization-server', timeout=4).read()"]

CMD ["hermes-mcp", "serve"]
