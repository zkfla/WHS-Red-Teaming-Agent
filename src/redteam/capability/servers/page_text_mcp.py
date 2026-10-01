"""Operator-configured MCP exposure for the internal public page reader."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, ValidationError

from redteam.capability.servers.page_text import (
    PageTextConfig,
    PageTextError,
    PageTextErrorCode,
    PageTextReader,
    PageTextRequest,
    PageTextResult,
)

REMOTE_TOOL_NAME = "fetch_page_text"
PROPOSED_OPERATION = "page_read"
PROFILE = "page_text_readonly"
MODULE = "redteam.capability.servers.page_text_mcp"


def load_page_text_config(
    path: str | Path, expected_sha256: str | None = None,
) -> tuple[PageTextConfig, str]:
    """Read one bounded operator file; pin its bytes across bootstrap and server startup."""
    try:
        with Path(path).open("rb") as source:
            data = source.read(65537)
            if len(data) > 65536:
                raise ValueError("page text configuration exceeds 65536 bytes")
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("page text configuration changed after bootstrap")
        return PageTextConfig.model_validate_json(data), digest
    except (OSError, ValidationError, UnicodeError):
        raise ValueError("invalid page text configuration") from None


def create_server(config: PageTextConfig, *, reader: PageTextReader | None = None) -> MCPServer:
    """Expose only bounded page text; policy and network dependencies are construction-time only."""
    page_reader = reader or PageTextReader(config)
    server = MCPServer("page-text", log_level="WARNING")

    async def fetch_page_text(url: str, max_chars: int | None = None) -> PageTextResult:
        """Read text from an operator-allowlisted public HTTPS origin."""
        try:
            request = PageTextRequest(url=url, max_chars=max_chars)
        except ValidationError:
            raise ToolError(f"page_text: {PageTextErrorCode.INVALID_INPUT.value}") from None
        try:
            return await page_reader.fetch_page_text(request)
        except PageTextError as exc:
            raise ToolError(str(exc)) from None

    # The advertised request limit comes from the same PageTextConfig that
    # enforces it at execution. No caller argument can change this bound.
    fetch_page_text.__annotations__["max_chars"] = Annotated[
        int | None, Field(ge=1, le=config.max_return_chars)
    ]
    server.tool()(fetch_page_text)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    config, _ = load_page_text_config(args.config, args.sha256)
    create_server(config).run(transport="stdio")


if __name__ == "__main__":
    main()
