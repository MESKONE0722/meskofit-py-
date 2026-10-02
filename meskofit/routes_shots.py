"""Injection log (for example weekly Mounjaro), kept in the kv table so the database format is unchanged."""
from __future__ import annotations

from .db import now
from .web import Req, Router, bad_request, valid_date

router = Router()
KEY = "shots"
DOSES = [2.5, 5.0, 7.5, 10.0, 12.5, 15.0]


def _load(app) -> list[dict]:
    v = app.db.kv_get(KEY)
    return v if isinstance(v, list) else []


@router.get("/api/shots")
def list_shots(app, req: Req):
    return sorted(_load(app), key=lambda s: (s["date"], s["id"]))


@router.post("/api/shots")
def add_shot(app, req: Req):
    b = req.json_obj()
    d, dose = b.get("date"), b.get("doseMg")
    if not valid_date(d) or isinstance(dose, bool) or not isinstance(dose, (int, float)) or not 0 < dose <= 100:
        raise bad_request("date (YYYY-MM-DD) and doseMg are required")
    shots = _load(app)
    shot = {"id": max([s["id"] for s in shots], default=0) + 1, "date": d, "doseMg": float(dose),
            "drug": str(b.get("drug") or "Mounjaro")[:40], "site": str(b.get("site") or "")[:40],
            "note": str(b.get("note") or "")[:300], "createdAt": now()}
    shots.append(shot)
    app.db.kv_set(KEY, shots)
    return 201, shot


@router.delete("/api/shots/{id}")
def delete_shot(app, req: Req):
    sid = req.path_id()
    app.db.kv_set(KEY, [s for s in _load(app) if s["id"] != sid])
    return {"ok": True}
