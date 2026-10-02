"""Assemble the ASGI application: API routes, exercise/photo files, the bundled web app, middleware."""
from __future__ import annotations

import logging
import time
from importlib import resources
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .app import App, all_routes
from .auth import AuthMiddleware
from .web import error_resp, make_endpoint

log = logging.getLogger("meskofit")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(self), microphone=()",
}


class SecurityHeaders:
    def __init__(self, app: ASGIApp, verbose: bool = False):
        self.app = app
        self.verbose = verbose

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.monotonic()
        status = 0

        async def send2(msg):
            nonlocal status
            if msg["type"] == "http.response.start":
                status = msg["status"]
                headers = list(msg.get("headers", []))
                for k, v in SECURITY_HEADERS.items():
                    headers.append((k.lower().encode(), v.encode()))
                msg = {**msg, "headers": headers}
            await send(msg)

        await self.app(scope, receive, send2)
        if self.verbose and (scope["path"].startswith("/api/") or status >= 400):
            log.info("%s %s %d %dms", scope["method"], scope["path"], status, (time.monotonic() - start) * 1000)


class SelectiveGZip:
    """gzip for text responses, but never for photos, exercise pictures, the DB backup or range requests."""

    def __init__(self, app: ASGIApp):
        self.app = app
        self.gz = GZipMiddleware(app, minimum_size=500, compresslevel=5)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            p = scope["path"]
            if p.startswith(("/photos/", "/img/")) or p == "/api/backup" or any(k == b"range" for k, _ in scope["headers"]):
                await self.app(scope, receive, send)
                return
        await self.gz(scope, receive, send)


def web_dir(app: App) -> Path | None:
    if app.opt.web_dir:
        return Path(app.opt.web_dir)
    d = Path(str(resources.files("meskofit") / "web" / "dist"))
    return d if (d / "index.html").is_file() else None


def build(app: App) -> ASGIApp:
    routes = []
    for method, path, fn in all_routes().routes:
        routes.append(Route(path, make_endpoint(app, fn), methods=[method] + (["HEAD"] if method == "GET" else [])))

    root = web_dir(app)
    proxy = httpx.AsyncClient(base_url=app.opt.dev_proxy, timeout=30) if app.opt.dev_proxy else None

    async def api_404(request: Request) -> Response:
        return error_resp(404, "unknown API endpoint")

    async def spa(request: Request) -> Response:
        if proxy is not None:
            return await _proxy(proxy, request)
        if root is None:
            return PlainTextResponse("web app not built\n", 404)
        rel = request.url.path.lstrip("/")
        target = (root / rel).resolve() if rel else root / "index.html"
        inside = str(target).startswith(str(root.resolve()))
        if not rel or not inside or not target.is_file() or rel == "index.html":
            return FileResponse(root / "index.html", media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
        headers = {}
        if rel.startswith("assets/"):
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif rel == "sw.js" or rel.endswith(".webmanifest"):
            headers["Cache-Control"] = "no-cache"
        else:
            headers["Cache-Control"] = "public, max-age=86400"
        media = "application/manifest+json" if rel.endswith(".webmanifest") else None
        return FileResponse(target, media_type=media, headers=headers)

    routes.append(Route("/api/{rest:path}", api_404, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]))
    routes.append(Route("/{rest:path}", spa, methods=["GET", "HEAD"]))

    star = Starlette(routes=routes)
    asgi: ASGIApp = AuthMiddleware(star, app)
    asgi = SelectiveGZip(asgi)
    asgi = SecurityHeaders(asgi, app.opt.verbose)
    return asgi


async def _proxy(client: httpx.AsyncClient, request: Request) -> Response:
    try:
        r = await client.request(request.method, request.url.path, params=request.query_params,
                                 headers={k: v for k, v in request.headers.items() if k.lower() not in ("host", "accept-encoding")},
                                 content=await request.body())
    except httpx.HTTPError as e:
        return PlainTextResponse(f"dev server unavailable: {e}\n", 502)
    skip = {"content-encoding", "transfer-encoding", "content-length", "connection"}
    return Response(r.content, r.status_code, {k: v for k, v in r.headers.items() if k.lower() not in skip})
