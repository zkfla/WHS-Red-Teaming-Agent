# Placeholder image for the harmless example MCP server.
#
# The framework's ExampleMCPAdapter currently runs in-process (see
# src/redteam/capability/adapters/mcp/example.py) and does not call this
# container yet — this Dockerfile is here so Capability Team has a real
# place to build out an actual example MCP server binding to a port,
# once the transport (HTTP/stdio/etc.) is decided.

FROM python:3.11-slim

WORKDIR /app

# TODO(capability-team): replace with the real example MCP server
# implementation and its dependencies.
COPY server.py .

EXPOSE 8765

CMD ["python", "server.py"]
