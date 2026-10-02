# Candidate mobile MCP server (NOT finalized — see deploy/mcp/README.md
# "Mobile capability"). Runs mobile-next/mobile-mcp in Streamable HTTP mode
# using the upstream package's own `--listen` flag (documented at
# https://github.com/mobile-next/mobile-mcp). Needs host networking (see
# docker-compose.yml's `mobile-next-mcp` service) to reach adb/Xcode
# simulators already running on the host.
FROM node:22-slim
ARG MOBILE_MCP_VERSION=1.0.4
WORKDIR /app
RUN npm install --global "@mobilenext/mobile-mcp@${MOBILE_MCP_VERSION}" \
    && npm cache clean --force
RUN useradd --create-home --uid 10001 mcp
USER mcp
ENV MOBILECLI_HOME=/tmp/mobilecli
EXPOSE 3000
ENTRYPOINT ["mcp-server-mobile", "--listen", "0.0.0.0:3000"]
