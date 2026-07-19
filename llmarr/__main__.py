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
    from .auth import AuthMiddleware, effective_mode, ensure_token
    from .config import ConfigStore
    from .db import Database
    from .oauth import OAuthProvider
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
    mode = effective_mode(sc)

    oauth_provider = None
    if mode != "none":
        token = ensure_token(store)
        if mode == "oauth":
            oauth_provider = OAuthProvider(
                store, Database(), mcp_path=mcp.settings.streamable_http_path
            )
            oauth_provider.signing_key()  # generate + persist on first run
            oauth_provider.mount(app)
        app.add_middleware(AuthMiddleware, store=store, oauth_provider=oauth_provider)
        _print_banner(mode, host, port, mcp.settings.streamable_http_path, token, sc)
    else:
        print(
            "WARNING: LLMarr HTTP transport is running WITHOUT authentication "
            "(server.require_auth is false).",
            file=sys.stderr,
        )

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


def _print_banner(mode, host, port, path, token, sc) -> None:
    lines = [
        "",
        "=" * 70,
        f" LLMarr HTTP transport — auth mode: {mode.upper()}",
        f"   Local URL:  http://{host}:{port}{path}",
    ]
    public = sc.public_url.rstrip("/") if sc.public_url else None
    if mode == "oauth":
        base = public or "https://<your-public-url>"
        lines += [
            f"   Public:     {base}{path}",
            "   OAuth is ON — add this as a claude.ai custom connector; on the",
            "   authorize page enter the token below to approve access:",
            f"   Token:      {token}",
            "   (Claude Code / static clients may still use it as a bearer header.)",
        ]
    else:
        lines += [
            f"   Token:      {token}",
            "   Send it on every request:  Authorization: Bearer <token>",
        ]
    lines += ["   (rotate with the rotate_auth_token tool)", "=" * 70, ""]
    print("\n".join(lines), file=sys.stderr)


def main() -> None:
    transport = os.environ.get("LLMARR_TRANSPORT", "stdio")
    if transport == "stdio":
        from .server import mcp

        mcp.run()
    else:
        _run_http()


if __name__ == "__main__":
    main()
