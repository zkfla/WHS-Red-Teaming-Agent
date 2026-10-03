"""Start the pinned apktool MCP server without its stdout startup report."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))
from apktool_mcp_server import mcp

if __name__ == "__main__":
    mcp.run()
