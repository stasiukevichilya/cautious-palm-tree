"""blender-mcp (mcp-for-blender) over streamable HTTP for Open WebUI.

The package only speaks stdio and expects to run next to Blender. Here it runs in a container and
reaches the Blender addon on Windows through host.docker.internal (BLENDER_HOST/BLENDER_PORT).
Telemetry is off: besides DISABLE_TELEMETRY, consent is forced to False, because screenshot
upload and trajectory recording only ask the addon's consent checkbox.
"""
import hmac
import os

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from blender_mcp import context_log, server, telemetry

API_KEY = os.environ.get("BLENDER_MCP_API_KEY", "")
if len(API_KEY) < 16:
    raise SystemExit("BLENDER_MCP_API_KEY must be set (at least 16 characters)")

telemetry.TelemetryCollector.check_user_consent = lambda self: False
telemetry.TelemetryCollector.upload_screenshot = lambda self, image_bytes, prefix: ""


class BearerAuth:
    """Every request needs Authorization: Bearer <BLENDER_MCP_API_KEY>, except /health."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/health":
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})
                return await send({"type": "http.response.body", "body": b'{"status":"ok"}'})
            header = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(header, f"Bearer {API_KEY}".encode()):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                return await send({"type": "http.response.body", "body": b'{"detail":"Invalid API key"}'})
        await self.app(scope, receive, send)


def main():
    mcp = server.mcp
    mcp.settings.stateless_http = True
    # Reached by compose service name, so the default localhost-only Host check would reject it.
    mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    context_log.install(mcp, server.SERVER_INSTRUCTIONS)
    uvicorn.run(BearerAuth(mcp.streamable_http_app()), host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
