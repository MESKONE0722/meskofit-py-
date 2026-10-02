"""Workout sessions, logged sets, exercise history and extra cardio activities."""
from __future__ import annotations

import json
from typing import Any

from .db import nz, now
from .web import HTTPError, Req, Router, bad_request, not_found, valid_date

router = Router()

SESSION_COLS = """s.id, s.day_id, s.day_name, s.level, s.date, s.started_at, s.finished_at, s.data,
    (SELECT COUNT(*) FROM sets x WHERE x.session_id = s.id AND x.done = 1),
    (SELECT COALESCE(SUM(COALESCE(x.weight_kg,0) * COALESCE(x.reps,0)),0) FROM sets x WHERE x.session_id = s.id AND x.done = 1)"""


def _session(row) -> dict:
    return {
        "id": row[0],
        "dayId": row[1],
        "dayName": row[2],
        "level": row[3],
        "date": row[4],
        "startedAt": row[5],
        "finishedAt": row[6],
        "data": json.loads(row[7]),
        "setsDone": row[8],
        "volumeKg": row[9],
    }


def load_sets(app, sid: int) -> list[dict]:
    rows = app.db.query(
        """SELECT plan_ex_id, ex_key, set_no, weight_kg, reps, secs, done, logged_at
           FROM sets WHERE session_id = ? ORDER BY plan_ex_id, set_no""",
        sid,
    )
    return [
        {
            "planExId": r[0], "exKey": r[1], "setNo": r[2], "weightKg": r[3], "reps": r[4], "secs": r[5],
            "done": bool(r[6]), **({"loggedAt": r[7]} if r[7] else {}),
        }
        for r in rows
    ]


def get_session(app, sid: int) -> dict | None:
    row = app.db.one(f"SELECT {SESSION_COLS} FROM sessions s WHERE s.id = ?", sid)
    if row is None:
        return None
    s = _session(row)
    s["sets"] = load_sets(app, sid)
    return s


def active_session(app) -> dict | None:
    row = app.db.one("SELECT id FROM sessions WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1")
    return get_session(app, row[0]) if row else None


def _without_empty_sets(s: dict) -> dict:
    """Go omits `sets` when empty (omitempty) on list endpoints; single-session responses keep it."""
    return s


@router.get("/api/sessions")
def list_sessions(app, req: Req):
    q = req.query
    where, args = ["1=1"], []
    if valid_date(q.get("from")):
        where.append("s.date >= ?")
        args.append(q.get("from"))
    if valid_date(q.get("to")):
        where.append("s.date <= ?")
        args.append(q.get("to"))
    if q.get("dayId"):
        where.append("s.day_id = ?")
        args.append(q.get("dayId"))
    limit = 200
    n = q.int("limit", 0)
    if 0 < n <= 2000:
        limit = n
    rows = app.db.query(
        f"SELECT {SESSION_COLS} FROM sessions s WHERE {' AND '.join(where)} ORDER BY s.date DESC, s.id DESC LIMIT {limit}",
        *args,
    )
    return [_session(r) for r in rows]


@router.get("/api/sessions/active")
def get_active(app, req: Req):
    return active_session(app)


@router.post("/api/sessions")
def create_session(app, req: Req):
    body = req.json_obj()
    day_id, day_name, level = body.get("dayId") or "", body.get("dayName") or "", body.get("level") or ""
    date = body.get("date") or ""
    if not day_id or not valid_date(date):
        raise bad_request("dayId and date (YYYY-MM-DD) are required")
    if not body.get("force"):
        act = active_session(app)
        if act is not None:
            return 409, {"error": "a workout is already in progress", "active": act}
    data = body.get("data")
    data_s = json.dumps(data, separators=(",", ":")) if data not in (None, "") and isinstance(data, (dict, list)) else "{}"
    sid = app.db.exec(
        """INSERT INTO sessions(day_id, day_name, level, date, started_at, data, updated_at)
           VALUES(?,?,?,?,?,?,?)""",
        day_id, day_name, level, date, now(), data_s, now(),
    )
    return 201, get_session(app, sid)


@router.get("/api/sessions/{id}")
def get_one(app, req: Req):
    s = get_session(app, req.path_id())
    if s is None:
        raise not_found("no such workout")
    return s


@router.patch("/api/sessions/{id}")
def patch_session(app, req: Req):
    sid = req.path_id()
    body = req.json_obj()
    with app.db.tx() as c:
        row = c.execute("SELECT data FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            raise not_found("no such workout")
        if "data" in body:
            patch = body["data"]
            if not isinstance(patch, dict):
                raise bad_request("data must be an object")
            try:
                base = json.loads(row[0]) or {}
            except ValueError:
                base = {}
            base.update(patch)
            c.execute("UPDATE sessions SET data = ? WHERE id = ?", (json.dumps(base, separators=(",", ":")), sid))
        if "finishedAt" in body:
            fin = body["finishedAt"]
            c.execute("UPDATE sessions SET finished_at = ? WHERE id = ?", (fin if isinstance(fin, str) else None, sid))
        if "startedAt" in body:
            t = _parse_time(body["startedAt"])
            if t:
                c.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (t, sid))
        if "date" in body and valid_date(body["date"]):
            c.execute("UPDATE sessions SET date = ? WHERE id = ?", (body["date"], sid))
        c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now(), sid))
    return get_session(app, sid)


def _parse_time(s: Any) -> str | None:
    from datetime import datetime, timezone

    if not isinstance(s, str):
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.delete("/api/sessions/{id}")
def delete_session(app, req: Req):
    app.db.exec("DELETE FROM sessions WHERE id = ?", req.path_id())
    return {"ok": True}


def _opt_num(v: Any, as_int: bool = False):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if as_int else float(v)
    return None


@router.put("/api/sessions/{id}/sets")
def put_sets(app, req: Req):
    """Upsert sets; with replace=true the payload becomes the full set list (offline sync)."""
    sid = req.path_id()
    body = req.json_obj()
    sets = body.get("sets") or []
    if not isinstance(sets, list):
        raise bad_request("invalid JSON body: sets must be a list")
    with app.db.tx() as c:
        if c.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (sid,)).fetchone()[0] == 0:
            raise not_found("no such workout")
        if body.get("replace"):
            c.execute("DELETE FROM sets WHERE session_id = ?", (sid,))
        for s in sets:
            if not isinstance(s, dict):
                continue
            plan_ex, ex_key, set_no = s.get("planExId") or "", s.get("exKey") or "", s.get("setNo") or 0
            if not plan_ex or not ex_key or not isinstance(set_no, int) or set_no < 1:
                continue
            ts = s.get("loggedAt") or now()
            c.execute(
                """INSERT INTO sets(session_id, plan_ex_id, ex_key, set_no, weight_kg, reps, secs, done, logged_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id, plan_ex_id, set_no) DO UPDATE SET ex_key=excluded.ex_key,
                   weight_kg=excluded.weight_kg, reps=excluded.reps, secs=excluded.secs, done=excluded.done,
                   logged_at=excluded.logged_at""",
                (sid, plan_ex, ex_key, set_no, _opt_num(s.get("weightKg")), _opt_num(s.get("reps"), True),
                 _opt_num(s.get("secs"), True), 1 if s.get("done") else 0, ts),
            )
        c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now(), sid))
    return get_session(app, sid)


# ───────────────────────── history ─────────────────────────


def e1rm(w: float, reps: int) -> float:
    """Epley estimate of a one-rep max; sets above 15 reps are capped since the formula overshoots."""
    if w <= 0 or reps <= 0:
        return 0.0
    if reps == 1:
        return w
    return w * (1 + min(reps, 15) / 30)


def exercise_sessions(app, key: str, exclude: int, limit: int) -> list[dict]:
    """Every session that logged the key, newest first."""
    rows = app.db.query(
        """SELECT s.id, s.date, s.day_id, s.level, st.set_no, st.weight_kg, st.reps, st.secs
           FROM sets st JOIN sessions s ON s.id = st.session_id
           WHERE st.ex_key = ? AND st.done = 1 AND s.id != ?
           ORDER BY s.date DESC, s.id DESC, st.set_no""",
        key, exclude,
    )
    out: list[dict] = []
    for r in rows:
        if not out or out[-1]["sessionId"] != r[0]:
            if len(out) >= limit:
                break
            out.append({"sessionId": r[0], "date": r[1], "dayId": r[2], "level": r[3], "sets": []})
        out[-1]["sets"].append({"setNo": r[4], "weightKg": r[5], "reps": r[6], "secs": r[7]})
    return out


@router.get("/api/history/last")
def history_last(app, req: Req):
    """Per exercise key: the most recent sets (same workout day preferred) and all-time bests."""
    q = req.query
    day = q.get("day")
    try:
        exclude = int(q.get("exclude"))
    except ValueError:
        exclude = 0
    out: dict[str, Any] = {}
    for key in q.get("keys").split(","):
        key = key.strip()
        if not key:
            continue
        hist = exercise_sessions(app, key, exclude, 400)
        last = next((h for h in hist if day == "" or h["dayId"] == day), None)
        if last is None and hist:
            last = hist[0]
        recent = [h for h in hist if day == "" or h["dayId"] == day][:3]
        best = {"e1rmKg": 0.0, "weightKg": 0.0, "reps": 0, "secs": 0, "date": ""}
        for h in hist:
            for s in h["sets"]:
                wt, reps, secs = s["weightKg"] or 0.0, s["reps"] or 0, s["secs"] or 0
                v = e1rm(wt, reps)
                if v > best["e1rmKg"]:
                    best["e1rmKg"], best["date"] = v, h["date"]
                best["weightKg"] = max(best["weightKg"], wt)
                best["reps"] = max(best["reps"], reps)
                best["secs"] = max(best["secs"], secs)
        out[key] = {"last": last, "recent": recent, "best": best, "sessions": len(hist)}
    return out


@router.get("/api/history/exercise/{key}")
def exercise_history(app, req: Req):
    hist = exercise_sessions(app, req.params["key"], 0, 500)
    hist.sort(key=lambda h: h["date"])  # stable, oldest first
    return hist


# ───────────────────────── activities (extra cardio, walks) ─────────────────────────


def list_activities(app, frm: str, to: str) -> list[dict]:
    rows = app.db.query(
        """SELECT id, date, kind, minutes, COALESCE(note,''), created_at FROM activities
           WHERE date >= ? AND date <= ? ORDER BY date DESC, id DESC""",
        frm, to,
    )
    out = []
    for r in rows:
        x = {"id": r[0], "date": r[1], "kind": r[2], "minutes": r[3]}
        if r[4]:
            x["note"] = r[4]
        x["createdAt"] = r[5]
        out.append(x)
    return out


@router.get("/api/activities")
def get_activities(app, req: Req):
    frm, to = req.query.get("from"), req.query.get("to")
    return list_activities(app, frm if valid_date(frm) else "0000-01-01", to if valid_date(to) else "9999-12-31")


@router.post("/api/activities")
def add_activity(app, req: Req):
    x = req.json_obj()
    minutes = x.get("minutes") if isinstance(x.get("minutes"), (int, float)) and not isinstance(x.get("minutes"), bool) else 0
    if not valid_date(x.get("date") or "") or minutes <= 0 or minutes > 1440 or not x.get("kind"):
        raise bad_request("date, kind and minutes are required")
    note = x.get("note") or ""
    created = now()
    aid = app.db.exec(
        "INSERT INTO activities(date, kind, minutes, note, created_at) VALUES(?,?,?,?,?)",
        x["date"], x["kind"], float(minutes), nz(note), created,
    )
    out = {"id": aid, "date": x["date"], "kind": x["kind"], "minutes": minutes}
    if note:
        out["note"] = note
    out["createdAt"] = created
    return 201, out


@router.delete("/api/activities/{id}")
def delete_activity(app, req: Req):
    app.db.exec("DELETE FROM activities WHERE id = ?", req.path_id())
    return {"ok": True}
