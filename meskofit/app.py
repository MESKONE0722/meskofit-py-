"""The MeskoFit application object: storage, food/AI clients, bundled data, and the Starlette app."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import httpx

from . import settings
from .db import Database, now
from .web import Router

DATA = resources.files("meskofit") / "data"


@dataclass
class Options:
    data_dir: Path
    version: str = "dev"
    http_port: int = 8080
    https_port: int = 8443
    certs: Any = None  # certs.Manager, or None when HTTPS is off
    web_dir: Path | None = None  # built web app (dist); defaults to the bundled copy
    dev_proxy: str = ""  # optional Vite dev server URL
    verbose: bool = False


class App:
    def __init__(self, opt: Options):
        from . import ai
        from .food.off import OFF
        from .food.usda import USDA

        self.opt = opt
        self.db = Database(opt.data_dir)
        for d in ("photos", "cache/ex-img"):
            (Path(opt.data_dir) / d).mkdir(parents=True, exist_ok=True)
        self.http = httpx.Client(timeout=25, follow_redirects=True)
        self.ai = ai.Client()
        self.off = OFF(self.http)
        self.usda = USDA(self.http, self.usda_key)

        cat = json.loads((DATA / "catalog.json").read_text("utf-8"))
        self.catalog: dict[str, Any] = cat["exercises"]
        self.library: list[dict] = json.loads((DATA / "library.json").read_text("utf-8"))
        self.lib_by_id = {e["id"]: e for e in self.library}
        self.defaults: dict = json.loads((DATA / "plans.json").read_text("utf-8"))
        self.img_lock = threading.Lock()
        self.login_lock = threading.Lock()
        self.login_fail: dict[str, int] = {}

    # ── shared state helpers (profile / settings live in the kv table) ──
    def profile(self) -> dict | None:
        return self.db.kv_get("profile")

    def first_run(self) -> bool:
        return self.profile() is None

    def settings(self) -> dict:
        """Stored settings merged over defaults (secrets included)."""
        return settings.settings(self.db)

    def public_settings(self) -> dict:
        return settings.public_settings(self.db)

    def usda_key(self) -> str:
        return self.settings().get("usdaKey") or ""

    def ai_config(self):
        """The AI provider configuration as an ai.Config."""
        from . import ai

        m = self.settings().get("ai") or {}
        return ai.Config(provider=m.get("provider", ""), base_url=m.get("baseUrl", ""), model=m.get("model", ""), api_key=m.get("apiKey", ""))

    def close(self) -> None:
        self.http.close()


def all_routes() -> Router:
    """Every API route, gathered from the route modules."""
    from . import (
        auth,
        routes_ai,
        routes_body,
        routes_core,
        routes_food,
        routes_shots,
        routes_meals,
        routes_train,
    )

    r = Router()
    for mod in (routes_core, auth, routes_train, routes_food, routes_body, routes_ai, routes_shots, routes_meals):
        for method, path, fn in mod.router.routes:
            r.add(method, path, fn)
    return r
