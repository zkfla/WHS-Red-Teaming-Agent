# Candidate analysis MCP server (NOT finalized — see deploy/mcp/README.md
# "Analysis capability"). Packages apktool + zinja-coder/apktool-mcp-server
# (https://github.com/zinja-coder/apktool-mcp-server), which only speaks MCP
# stdio. Build once (`docker compose build apktool-mcp`); invoke per-call via
# `docker compose run --rm -i apktool-mcp` — this image is not meant to be
# started with `docker compose up`.
#
# Pin the upstream commit whose MCP input schemas are checked by the
# mobile/analysis manifest. Update the pin and manifest together.
FROM python:3.13-slim
ARG APKTOOL_VERSION=2.9.3
ARG APKTOOL_MCP_SERVER_REF=12bb33134af905923a0a58643dcc35fd694ad6f6
ARG FASTMCP_VERSION=4.0.10
ARG MCP_SDK_VERSION=2.2.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jre-headless \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/apktool \
        "https://raw.githubusercontent.com/iBotPeaches/Apktool/master/scripts/linux/apktool" \
    && curl -fsSL -o /usr/local/bin/apktool.jar \
        "https://github.com/iBotPeaches/Apktool/releases/download/v${APKTOOL_VERSION}/apktool_${APKTOOL_VERSION}.jar" \
    && chmod +x /usr/local/bin/apktool /usr/local/bin/apktool.jar

RUN pip install --no-cache-dir uv

# The pinned server requires Python >=3.13. Keep uv on the image interpreter
# so dependency resolution cannot silently move to a newer unsupported ABI.
ENV UV_PYTHON=3.13 \
    UV_PYTHON_DOWNLOADS=never \
    FASTMCP_CHECK_FOR_UPDATES=off \
    FASTMCP_SHOW_SERVER_BANNER=false

WORKDIR /app
COPY entrypoint.py /app/entrypoint.py
RUN git init server \
    && git -C server remote add origin https://github.com/zinja-coder/apktool-mcp-server.git \
    && git -C server fetch --depth 1 origin "${APKTOOL_MCP_SERVER_REF}" \
    && git -C server checkout --detach FETCH_HEAD \
    && cd server \
    && uv sync --no-dev \
    && uv pip install --python .venv/bin/python \
        "fastmcp==${FASTMCP_VERSION}" \
        "mcp==${MCP_SDK_VERSION}"

RUN useradd --create-home --uid 10001 mcp \
    && chown -R mcp:mcp /app
USER mcp
WORKDIR /app/server
VOLUME ["/work"]
ENTRYPOINT ["/app/server/.venv/bin/python", "/app/entrypoint.py"]
