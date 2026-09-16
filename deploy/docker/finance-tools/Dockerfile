FROM python:3.11-slim AS base
WORKDIR /app
RUN useradd --create-home --uid 10001 mcp
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
USER mcp
ENTRYPOINT ["python", "-m", "redteam.capability.servers"]

FROM base AS active
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nmap \
    && rm -rf /var/lib/apt/lists/*
USER mcp
