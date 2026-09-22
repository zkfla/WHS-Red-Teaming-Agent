FROM projectdiscovery/nuclei:v3.4.10@sha256:aeb5ea2db32a252b8135707d2ad0e89b90e19a18ea7816d38759bc51efb46b97 AS nuclei-engine

FROM python:3.11-slim AS base
WORKDIR /app
RUN useradd --create-home --uid 10001 mcp
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
USER mcp
ENTRYPOINT ["python", "-m", "redteam.capability.servers"]

FROM base AS nuclei
COPY --from=nuclei-engine /usr/local/bin/nuclei /usr/local/bin/nuclei

FROM base AS active
USER root
RUN apt-get update && apt-get install -y --no-install-recommends nmap \
    && rm -rf /var/lib/apt/lists/*
USER mcp
