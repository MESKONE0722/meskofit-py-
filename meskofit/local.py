"""Call the API handlers in-process (no HTTP), for UIs such as the Streamlit version."""
from __future__ import annotations

import json
import re
from typing import Any

from .app import App, Options, all_routes
from .web import HTTPError, Query, Req


class Local:
    """``Local(app).get("/api/body")`` runs the same handler the web server would, and returns its JSON."""

    def __init__(self, app: App):
        self.app = app
        self._routes = []
        for method, path, fn in all_routes().routes:
            rx = re.compile("^" + re.sub(r"\{(\w+)(:path)?\}", lambda m: f"(?P<{m.group(1)}>[^/]+)", path) + "$")
            self._routes.append((method, rx, fn))

    def call(self, method: str, path: str, body: Any = None, **query: Any) -> Any:
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        for m, rx, fn in self._routes:
            mt = rx.match(path)
            if m == method and mt:
                req = Req(method, path, mt.groupdict(), Query(qs), {}, {}, json.dumps(body).encode() if body is not None else b"",
                          "127.0.0.1", False, "local")
                out = fn(self.app, req)
                if isinstance(out, tuple):
                    out = out[1]
                if hasattr(out, "body"):  # a Response: decode JSON bodies
                    try:
                        return json.loads(out.body)
                    except ValueError:
                        return out
                return out
        raise HTTPError(404, "unknown API endpoint")

    def get(self, path: str, **q: Any) -> Any:
        return self.call("GET", path, **q)

    def post(self, path: str, body: Any = None) -> Any:
        return self.call("POST", path, body)

    def put(self, path: str, body: Any = None) -> Any:
        return self.call("PUT", path, body)

    def patch(self, path: str, body: Any = None) -> Any:
        return self.call("PATCH", path, body)

    def delete(self, path: str) -> Any:
        return self.call("DELETE", path)


def open_local(data_dir: str, version: str = "streamlit") -> Local:
    return Local(App(Options(data_dir=data_dir, version=version, http_port=0, https_port=0)))
