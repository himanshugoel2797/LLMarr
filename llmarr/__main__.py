"""Entry point: ``python -m llmarr`` / ``llmarr``.

Runs the MCP server over stdio by default (the transport MCP clients expect).
Set ``LLMARR_TRANSPORT=streamable-http`` (with ``LLMARR_HOST``/``LLMARR_PORT``)
to expose it over HTTP instead — useful when the server runs in its own
container and a remote MCP client connects to it.
"""

from __future__ import annotations

import os


def main() -> None:
    from .server import mcp

    transport = os.environ.get("LLMARR_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    else:
        # FastMCP reads host/port from its settings; pass through env if given.
        if os.environ.get("LLMARR_HOST"):
            mcp.settings.host = os.environ["LLMARR_HOST"]
        if os.environ.get("LLMARR_PORT"):
            mcp.settings.port = int(os.environ["LLMARR_PORT"])
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()
