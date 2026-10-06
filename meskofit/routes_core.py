"""Settings, profile, bootstrap, plans, exercise catalog/library, server info, QR code, CA download."""
from __future__ import annotations

import json
import socket
from datetime import timezone
from pathlib import Path
from typing import Any

from starlette.responses import FileResponse, Response

from . import auth, certs, qr
from .routes_train import active_session
from .web import HTTPError, Req, Router, bad_request, json_resp, not_found

router = Router()


@router.get("/api/health")
def health(app, req: Req):
    return {"ok": True}


# ───────────────────────── settings ─────────────────────────

@router.get("/api/settings")
def get_settings(app, req: Req):
    return app.public_settings()


@router.put("/api/settings")
def put_settings(app, req: Req):
    patch = req.json()
    if not isinstance(patch, dict):
        raise bad_request("invalid JSON body: expected an object")
    stored = app.db.kv_get("settings")
    if not isinstance(stored, dict):
        stored = {}
    for k, v in patch.items():
        if k == "usdaKeySet":
            continue
        if k == "ai":
            if not isinstance(v, dict):
                continue
            cur = stored.get("ai")
            if not isinstance(cur, dict):
                cur = {}
            for kk, vv in v.items():
                if kk != "apiKeySet":
                    cur[kk] = vv
            stored["ai"] = cur
        else:
            stored[k] = v
    app.db.kv_set("settings", stored)
    return app.public_settings()


# ───────────────────────── profile & bootstrap ─────────────────────────

@router.put("/api/profile")
def put_profile(app, req: Req):
    try:
        p = req.json()
    except HTTPError:
        p = None
    if not isinstance(p, dict):
        raise bad_request("profile must be a JSON object")
    cur = app.profile() or {}
    cur.update(p)
    app.db.kv_set("profile", cur)
    return cur


@router.get("/api/bootstrap")
def bootstrap(app, req: Req):
    return {
        "version": app.opt.version,
        "profile": app.profile(),
        "settings": app.public_settings(),
        "plans": current_plans(app),
        "activeSession": active_session(app),
        "https": app.opt.certs is not None,
        "secure": req.https,
        "passwordSet": auth.auth_config(app) is not None,
    }


# ───────────────────────── plans & catalog ─────────────────────────

def current_plans(app) -> dict:
    levels = dict(app.defaults["levels"])
    custom: list[str] = []
    stored = app.db.kv_get("plans")
    if isinstance(stored, dict):
        for k, v in stored.items():
            if k in levels:
                levels[k] = v
                custom.append(k)
    return {"version": app.defaults.get("version", 1), "levels": levels, "customized": sorted(custom) or None}


@router.get("/api/plans")
def get_plans(app, req: Req):
    return current_plans(app)


@router.put("/api/plans/{level}")
def put_plan(app, req: Req):
    level = req.params["level"]
    if level not in app.defaults["levels"]:
        raise not_found("unknown level")
    body = req.json()
    days = body.get("days") if isinstance(body, dict) else None
    if not isinstance(days, list) or not days:
        raise bad_request("plan must have days")
    stored = app.db.kv_get("plans")
    if not isinstance(stored, dict):
        stored = {}
    stored[level] = body
    app.db.kv_set("plans", stored)
    return current_plans(app)


@router.post("/api/plans/{level}/reset")
def reset_plan(app, req: Req):
    stored = app.db.kv_get("plans")
    if not isinstance(stored, dict):
        stored = {}
    stored.pop(req.params["level"], None)
    app.db.kv_set("plans", stored)
    return current_plans(app)


def lib_to_catalog(key: str, le: dict) -> dict:
    cat, eq = le.get("category"), le.get("equipment", "")
    if cat == "stretching":
        kind = "stretch"
    elif cat == "cardio":
        kind = "cardio"
    elif eq in ("body only", ""):
        kind = "bodyweight"
    else:
        kind = "weight"
    primary = le.get("primary") or []
    lower = any(m in ("quadriceps", "hamstrings", "glutes", "calves", "adductors", "abductors") for m in primary)
    return {
        "key": key, "name": le["name"], "lib": le["id"], "img": le["id"], "frames": list(range(le.get("frames", 0))),
        "kind": kind, "equipment": eq, "primary": primary, "secondary": le.get("secondary") or [],
        "target": "/".join(primary), "flags": [], "alts": [], "lower": lower, "unilateral": False,
        "steps": le.get("steps") or [], "tips": [],
    }


@router.get("/api/catalog")
def catalog(app, req: Req):
    out = dict(app.catalog)
    for lv in current_plans(app)["levels"].values():
        refs = []
        for d in lv.get("days") or []:
            refs += [e.get("ex", "") for e in d.get("exercises") or []]
        refs += [e.get("ex", "") for e in (lv.get("core") or {}).get("exercises") or []]
        for ref in refs:
            if ref.startswith("lib:"):
                le = app.lib_by_id.get(ref[4:])
                if le:
                    out[ref] = lib_to_catalog(ref, le)
    resp = json_resp({"exercises": out})
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@router.get("/api/library")
def library_search(app, req: Req):
    q = req.query.get("q").strip().lower()
    muscle = req.query.get("muscle")
    words = q.split()
    hits = []
    for e in app.library:
        prim = e.get("primary") or []
        if muscle and muscle not in prim:
            continue
        name = e["name"].lower()
        score, ok = 0, True
        for wd in words:
            if wd in name:
                score += 2
                if name.startswith(wd):
                    score += 1
            elif wd in (e.get("equipment") or "").lower() or any(wd in m for m in prim):
                score += 1
            else:
                ok = False
                break
        if ok:
            hits.append((score, {"id": e["id"], "name": e["name"], "equipment": e.get("equipment", ""),
                                 "level": e.get("level", ""), "category": e.get("category", ""), "primary": prim,
                                 "frames": e.get("frames", 0)}))
    hits.sort(key=lambda h: -h[0])  # stable
    limit = req.query.int("limit", 40)  # ?limit= lets the library page through a whole muscle group
    limit = min(limit, 2000) if limit > 0 else 40
    return {"results": [h[1] for h in hits[:limit]]}


@router.get("/api/library/{id}")
def library_item(app, req: Req):
    le = app.lib_by_id.get(req.params["id"])
    if le is None:
        raise not_found("no such exercise")
    return lib_to_catalog("lib:" + le["id"], le)


# ───────────────────────── server info, QR, CA ─────────────────────────

def _port_suffix(p: int, default: int) -> str:
    return "" if p == default else f":{p}"


def server_urls(app) -> dict:
    u: dict[str, list] = {"http": [], "https": [], "tailscale": []}
    for ip in certs.local_ips():
        host = str(ip)
        if app.opt.http_port > 0:
            u["http"].append(f"http://{host}{_port_suffix(app.opt.http_port, 80)}")
        if app.opt.https_port > 0 and app.opt.certs is not None:
            u["https"].append(f"https://{host}{_port_suffix(app.opt.https_port, 443)}")
        if certs.is_tailscale(ip):
            u["tailscale"].append(host)
    return u


@router.get("/api/server")
def server_info(app, req: Req):
    info: dict[str, Any] = {
        "version": app.opt.version, "hostname": socket.gethostname(), "httpPort": app.opt.http_port,
        "httpsPort": app.opt.https_port, "urls": server_urls(app), "dataDir": str(app.opt.data_dir),
        "requestIP": req.client_ip,
    }
    c = app.opt.certs
    if c is not None:
        info["ca"] = {"fingerprint": c.fingerprint(), "constrained": c.constrained, "warnings": list(c.warnings) or None}
        names = c.leaf_names()
        if names:
            info["certNames"] = names
            na = c.leaf_not_after()
            if na is not None:
                info["certExpires"] = na.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return info


@router.get("/api/qr.svg")
def qr_svg(app, req: Req):
    text = req.query.get("data")
    if not text or len(text) > 512:
        raise bad_request("data required (max 512 chars)")
    try:
        svg = qr.svg(text)
    except ValueError as e:
        raise bad_request(str(e))
    return Response(svg, 200, {"Content-Type": "image/svg+xml", "Cache-Control": "public, max-age=3600"})


@router.get("/ca.crt")
def ca_cert(app, req: Req):
    if app.opt.certs is None:
        raise not_found("404 page not found")
    return Response(app.opt.certs.ca_der(), 200, {"Content-Type": "application/x-x509-ca-cert",
                                                  "Content-Disposition": 'attachment; filename="meskofit-ca.crt"'})


@router.get("/meskofit.mobileconfig")
def mobileconfig(app, req: Req):
    if app.opt.certs is None:
        raise not_found("404 page not found")
    return Response(app.opt.certs.mobileconfig(), 200, {"Content-Type": "application/x-apple-aspen-config",
                                                        "Content-Disposition": 'attachment; filename="py123.mobileconfig"'})


# ───────────────────────── exercise pictures ─────────────────────────

import re  # noqa: E402
from importlib import resources  # noqa: E402

_FRAME = re.compile(r"^[0-9]$")
_IMG_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


@router.get("/img/ex/{id}/{n}")
def exercise_image(app, req: Req):
    ex_id = req.params["id"]
    n = req.params["n"].removesuffix(".jpg")
    if ex_id not in app.lib_by_id or not _FRAME.match(n):
        raise not_found("404 page not found")
    rel = Path(ex_id) / f"{n}.jpg"
    bundled = Path(str(resources.files("meskofit") / "data" / "exercise-img")) / rel
    if bundled.is_file():
        return FileResponse(bundled, media_type="image/jpeg", headers=_IMG_HEADERS)
    cache = Path(app.opt.data_dir) / "cache" / "ex-img" / rel
    if cache.is_file():
        return FileResponse(cache, media_type="image/jpeg", headers=_IMG_HEADERS)
    from urllib.parse import quote

    with app.img_lock:
        try:
            r = app.http.get(f"https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/{quote(ex_id)}/{n}.jpg")
        except Exception:  # noqa: BLE001
            return Response("image unavailable offline\n", 502, media_type="text/plain")
        if r.status_code != 200:
            raise not_found("404 page not found")
        b = r.content[: 5 << 20]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b)
    return Response(b, 200, {"Content-Type": "image/jpeg", **_IMG_HEADERS})
