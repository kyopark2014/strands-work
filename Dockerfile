# Stage 1: frontend build
FROM node:22-alpine AS frontend
WORKDIR /web
COPY application/web/package.json application/web/package-lock.json ./
RUN npm ci
COPY application/web/ .
RUN npm run build

# Stage 2: Python runtime
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade "setuptools>=83.0.0" \
    && pip install \
    fastapi \
    "python-multipart>=0.0.31" \
    "urllib3>=2.7.0" \
    uvicorn[standard] \
    boto3 \
    cryptography \
    langchain_aws \
    langchain-openai \
    "openai>=2.41.0" \
    aws-bedrock-token-generator \
    requests \
    "markdown>=3.7" \
    "mcp>=2.1.1"

COPY . .
COPY --from=frontend /web/dist /app/application/web/dist

# Notion MCP App viewer (self-contained HTML). Rebuild so the image works even
# if dist/ was missing from the build context.
RUN python3 /app/application/mcp_app_views/build.py

# Knowledge-graph + Wiki sync deps (import name: graphify ← PyPI graphifyy)
RUN pip install --no-cache-dir -r /app/graph/requirements.txt

RUN chmod +x /app/docker-entrypoint.sh \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/api/health

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "application.server:app", "--host", "0.0.0.0", "--port", "8501", "--no-server-header"]
