# Candidate analysis MCP server (NOT finalized — see deploy/mcp/README.md
# "Analysis capability"). Packages apktool + zinja-coder/apktool-mcp-server
# (https://github.com/zinja-coder/apktool-mcp-server), which only speaks MCP
# stdio. Build once (`docker compose build apktool-mcp`); invoke per-call via
# `docker compose run --rm -i apktool-mcp` — this image is not meant to be
# started with `docker compose up`.
#
# Versions/refs below are a best-effort pin; adjust once the team finalizes
# this candidate.
FROM python:3.11-slim
ARG APKTOOL_VERSION=2.9.3
ARG APKTOOL_MCP_SERVER_REF=master

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

# uv provisions its own interpreter under $UV_PYTHON_INSTALL_DIR when no
# system Python satisfies pyproject.toml's requirement. Left at its default
# (under /root), the non-root `mcp` user below can't traverse into /root to
# reach it at runtime ("Permission denied" resolving .venv/bin/python) --
# redirect it to a world-traversable path instead.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python

WORKDIR /app
RUN git clone --depth 1 --branch "${APKTOOL_MCP_SERVER_REF}" \
        https://github.com/zinja-coder/apktool-mcp-server.git server \
    && cd server && uv sync --no-dev || uv pip install -r requirements.txt || true

RUN useradd --create-home --uid 10001 mcp \
    && chown -R mcp:mcp /app /opt/uv-python
USER mcp
WORKDIR /app/server
VOLUME ["/work"]
ENTRYPOINT ["uv", "run", "apktool_mcp_server.py"]
