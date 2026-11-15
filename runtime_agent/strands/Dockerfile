FROM --platform=linux/arm64 python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
 curl \
 git \
 gnupg \
 build-essential \
 python3-dev \
 nodejs \
 npm \
 pandoc \
 poppler-utils \
 libreoffice-writer \
 libreoffice-calc \
 libreoffice-impress \
 fonts-liberation \
 # Hangul glyphs for matplotlib / ReportLab in execute_code
 fonts-nanum \
 fontconfig \
 tesseract-ocr \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --upgrade "setuptools>=83.0.0" \
 && pip install "urllib3>=2.7.0" boto3 botocore --upgrade
# MCP 2.x MCPServer (mcp.server.mcpserver).
RUN pip install "mcp>=2.1.1"
# Cap <0.9: v0.9.0 removed strands_tools.file_read/file_write (breaks tools/* wrappers).
RUN pip install "strands-agents[openai,otel] @ git+https://github.com/strands-agents/sdk-python.git@main#subdirectory=strands-py" \
    "strands-agents-tools>=0.8.1,<0.9.0"
# CVE-2026-16796 fixed in bedrock-agentcore>=1.18.1
RUN pip install "bedrock-agentcore>=1.18.1" uv
RUN pip install "mcp>=2.1.1"
RUN pip install Pillow pytz tavily-python requests beautifulsoup4
RUN pip install networkx
RUN pip install langchain-aws langchain-core langchain-text-splitters PyPDF2
RUN pip install python-docx openpyxl reportlab matplotlib pandas finance-datareader pypdf pdfplumber pymupdf pyyaml
RUN pip install colorama rich defusedxml lxml "markitdown[pptx]" python-pptx pdf2image pytesseract
RUN pip install python-dotenv
# Preinstall so aws documentation MCP skips uvx/PyPI cold start (see mcp_config.py)
RUN pip install awslabs.aws-documentation-mcp-server

# Strands runtime + graph embedding lib (hybrid wiki search on AgentCore)
COPY runtime_agent/strands/ .
COPY graph/lib/ graph/lib/

# Skills that use docx-js / pptxgenjs via bash/node (local install for require())
# pptx skill also uses react-icons + sharp for icon rasterization
# mcp-server-fetch-typescript: preinstall so web_fetch MCP skips npx/npm cold start
RUN npm install docx pptxgenjs react react-dom react-icons sharp mcp-server-fetch-typescript
ENV NODE_PATH=/app/node_modules
ENV PATH="/app/node_modules/.bin:${PATH}"

# OpenTelemetry (ADOT + Strands telemetry for AgentCore Evaluations)
RUN pip install aws-opentelemetry-distro>=0.10.0

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app

USER appuser

ENV PYTHONPATH=/app:/app/graph
ENV APP_CONFIG_PATH=/app/config.json
ENV ALLOW_MISSING_RAG_CONFIG=1
ENV SESSION_STORAGE_DIR=/mnt/workspace
ENV BYPASS_TOOL_CONSENT=true
ENV OTEL_PYTHON_DISTRO=aws_distro
ENV OTEL_PYTHON_CONFIGURATOR=aws_configurator
ENV OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
ARG OTEL_SERVICE_NAME=strands_runtime.DEFAULT
ENV OTEL_RESOURCE_ATTRIBUTES=service.name=${OTEL_SERVICE_NAME}

EXPOSE 8080

# AgentCore runtimes expose /ping for health probes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
 CMD curl --fail http://localhost:8080/ping || exit 1

CMD ["uv", "run", "opentelemetry-instrument", "uvicorn", "agent:app", "--host", "0.0.0.0", "--port", "8080"]
