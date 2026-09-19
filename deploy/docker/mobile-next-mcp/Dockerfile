# Candidate mobile MCP server (NOT finalized — see deploy/mcp/README.md
# "Mobile capability"). Runs mobile-next/mobile-mcp in Streamable HTTP mode
# using the upstream package's own `--listen` flag (documented at
# https://github.com/mobile-next/mobile-mcp). Needs host networking (see
# docker-compose.yml's `mobile-next-mcp` service) to reach adb/Xcode
# simulators already running on the host.
FROM node:20-slim
WORKDIR /app
RUN useradd --create-home --uid 10001 mcp
USER mcp
EXPOSE 3000
ENTRYPOINT ["npx", "-y", "@mobilenext/mobile-mcp@latest", "--listen", "3000"]
