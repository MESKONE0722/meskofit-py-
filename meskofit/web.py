"""Tiny HTTP layer over Starlette: sync handlers, JSON helpers, the error shape the web app expects."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("meskofit")

MAX_BODY = 25 << 20


class HTTPError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status
        self.msg = msg


def bad_request(msg: str) -> HTTPError:
    return HTTPError(400, msg)


def not_found(msg: str = "not found") -> HTTPError:
    return HTTPError(404, msg)


class Query:
    """Read-only view of the query string. get() returns the first value or the default."""

    def __init__(self, qs: str):
        self._d = parse_qs(qs, keep_blank_values=True)

    def get(self, key: str, default: str = "") -> str:
        v = self._d.get(key)
        return v[0] if v else default

    def getall(self, key: str) -> list[str]:
        return self._d.get(key, [])

    def int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return default

    def __contains__(self, key: str) -> bool:
        return key in self._d


@dataclass
class Req:
    """What a handler sees. Everything is already read, so handlers can be plain sync functions."""

    method: str
    path: str
    params: dict[str, str]
    query: Query
    headers: dict[str, str]  # lower-cased keys
    cookies: dict[str, str]
    body: bytes
    client_ip: str
    https: bool
    host: str

    def json(self) -> Any:
        if len(self.body) > MAX_BODY:
            raise bad_request("body too large")
        try:
            return json.loads(self.body or b"null")
        except ValueError as e:
            raise bad_request(f"invalid JSON body: {e}")

    def json_obj(self) -> dict:
        v = self.json()
        if not isinstance(v, dict):
            raise bad_request("body must be a JSON object")
        return v

    def path_id(self, name: str = "id") -> int:
        try:
            n = int(self.params.get(name, ""))
        except ValueError:
            n = 0
        if n <= 0:
            raise bad_request("bad id")
        return n


def _ints(o: Any) -> Any:
    """Go prints whole floats as integers (250, not 250.0); do the same so both servers agree."""
    if isinstance(o, float):
        return int(o) if o.is_integer() and abs(o) < 1e15 else o
    if isinstance(o, dict):
        return {k: _ints(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_ints(v) for v in o]
    return o


def json_resp(obj: Any, status: int = 200) -> Response:
    # Go's encoder doesn't escape <, >, & and ends with a newline; keep the same bytes.
    body = json.dumps(_ints(obj), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    return Response(body, status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"})


def error_resp(status: int, msg: str, **extra: Any) -> Response:
    return json_resp({"error": msg, **extra}, status)


def to_response(v: Any) -> Response:
    if isinstance(v, Response):
        return v
    if isinstance(v, tuple):
        status, obj = v
        return json_resp(obj, status)
    return json_resp(v)


Handler = Callable[[Any, Req], Any]


class Router:
    """Collects (method, path, handler) so modules can register routes without importing each other."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Handler]] = []

    def add(self, method: str, path: str, fn: Handler) -> None:
        self.routes.append((method, path, fn))

    def get(self, path: str):
        return self._deco("GET", path)

    def post(self, path: str):
        return self._deco("POST", path)

    def put(self, path: str):
        return self._deco("PUT", path)

    def patch(self, path: str):
        return self._deco("PATCH", path)

    def delete(self, path: str):
        return self._deco("DELETE", path)

    def _deco(self, method: str, path: str):
        def deco(fn: Handler) -> Handler:
            self.add(method, path, fn)
            return fn

        return deco


async def read_req(request: Request) -> Req:
    body = b""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        chunks = []
        size = 0
        async for c in request.stream():
            size += len(c)
            if size > MAX_BODY + 1:
                break
            chunks.append(c)
        body = b"".join(chunks)
    client = request.client.host if request.client else ""
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    return Req(
        method=request.method,
        path=request.url.path,
        params={k: v for k, v in request.path_params.items()},
        query=Query(request.url.query),
        headers={k.lower(): v for k, v in request.headers.items()},
        cookies=dict(request.cookies),
        body=body,
        client_ip=client,
        https=https,
        host=request.headers.get("host", ""),
    )


def make_endpoint(app: Any, fn: Handler):
    async def endpoint(request: Request) -> Response:
        req = await read_req(request)
        try:
            return to_response(await run_in_threadpool(fn, app, req))
        except HTTPError as e:
            return error_resp(e.status, e.msg)
        except Exception as e:  # noqa: BLE001 - last resort, mirrors the Go recover middleware
            log.exception("error serving %s %s", req.method, req.path)
            return error_resp(500, str(e) or "internal error")

    return endpoint


_date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_date(s: str) -> bool:
    if not isinstance(s, str) or not _date_re.match(s):
        return False
    import datetime

    try:
        datetime.date.fromisoformat(s)
        return True
    except ValueError:
        return False
