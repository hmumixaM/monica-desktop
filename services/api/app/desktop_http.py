"""Loopback boundary for the desktop HTTP server; normal deployments are unchanged."""
from ipaddress import ip_address
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


def loopback(value: str) -> bool:
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


class DesktopBoundaryMiddleware:
    def __init__(self, app, *, origin: str):
        self.app = app
        self.origin = origin

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope["headers"]}
        try:
            host = urlsplit("http://" + headers.get("host", ""))
            valid_host = (host.hostname == "127.0.0.1" and not host.username and not host.password
                          and not host.path and not host.query and not host.fragment)
            _ = host.port
        except ValueError:
            valid_host = False
        peer = (scope.get("client") or ("", 0))[0]
        origin = headers.get("origin")
        allowed = (valid_host and loopback(peer)
                   and (origin is None or origin == self.origin)
                   and headers.get("sec-fetch-site") != "cross-site")
        if not allowed:
            return await JSONResponse({"detail": "Desktop requests require the configured loopback origin"}, status_code=403)(scope, receive, send)
        return await self.app(scope, receive, send)
