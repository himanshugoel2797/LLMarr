"""Entry point: ``python -m llmarr`` / ``llmarr``.

Runs the MCP server over stdio by default (what most MCP clients spawn locally —
no auth needed). Set ``LLMARR_TRANSPORT=streamable-http`` to run it as a
persistent HTTP service in its own process; that transport is protected by a
single static bearer token (a persistent login) unless auth is disabled.
"""

from __future__ import annotations

import os
import sys


def _run_http() -> None:
    from .auth import BearerAuthMiddleware, ensure_token
    from .config import ConfigStore
    from .server import mcp

    if os.environ.get("LLMARR_HOST"):
        mcp.settings.host = os.environ["LLMARR_HOST"]
    if os.environ.get("LLMARR_PORT"):
        mcp.settings.port = int(os.environ["LLMARR_PORT"])

    store = ConfigStore()

    # Configure MCP's DNS-rebinding / Host-header protection for the deployment.
    # Behind a tunnel or reverse proxy the incoming Host is the external name, not
    # localhost, so trust the configured hostnames — or disable the check when
    # none are set (the bearer token is the real boundary).
    from mcp.server.transport_security import TransportSecuritySettings

    sc = store.config.server
    if sc.allowed_hosts:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=sc.allowed_hosts,
            allowed_origins=sc.allowed_origins,
        )
    else:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )

    app = mcp.streamable_http_app()

    host, port = mcp.settings.host, mcp.settings.port
    if store.config.server.require_auth:
        token = ensure_token(store)
        app.add_middleware(BearerAuthMiddleware, token=token)
        print(
            "\n".join(
                [
                    "",
                    "=" * 68,
                    " LLMarr HTTP transport — authentication is ON",
                    f"   URL:   http://{host}:{port}{mcp.settings.streamable_http_path}",
                    f"   Token: {token}",
                    "   Send it on every request:  Authorization: Bearer <token>",
                    "   (rotate with the rotate_auth_token tool)",
                    "=" * 68,
                    "",
                ]
            ),
            file=sys.stderr,
        )
    else:
        print(
            "WARNING: LLMarr HTTP transport is running WITHOUT authentication "
            "(server.require_auth is false).",
            file=sys.stderr,
        )

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    transport = os.environ.get("LLMARR_TRANSPORT", "stdio")
    if transport == "stdio":
        from .server import mcp

        mcp.run()
    else:
        _run_http()


if __name__ == "__main__":
    main()
