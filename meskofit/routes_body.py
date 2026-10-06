"""Body measurements, fasting, the progress bundle, progress photos, export and backup."""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path
from typing import Any

from starlette.responses import FileResponse, Response

from .db import nz, now
from .routes_train import SESSION_COLS, _session, list_activities
from .web import HTTPError, Req, Router, bad_request, json_resp, not_found, valid_date

router = Router()

BODY_FIELDS = [("weightKg", "weight_kg"), ("waistCm", "waist_cm"), ("neckCm", "neck_cm"), ("hipCm", "hip_cm"),
               ("chestCm", "chest_cm"), ("armCm", "arm_cm"), ("thighCm", "thigh_cm"), ("bodyFatPct", "body_fat_pct")]


def _body_row(r) -> dict:
    e: dict[str, Any] = {"date": r[0]}
    for i, (k, _) in enumerate(BODY_FIELDS):
        e[k] = r[i + 1]
    if r[9]:
        e["note"] = r[9]
    return e


def list_body(app) -> list[dict]:
    rows = app.db.query("""SELECT date, weight_kg, waist_cm, neck_cm, hip_cm, chest_cm, arm_cm, thigh_cm, body_fat_pct,
        COALESCE(note,'') FROM body_log ORDER BY date""")
    return [_body_row(r) for r in rows]


@router.get("/api/body")
def get_body(app, req: Req):
    return list_body(app)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise bad_request("bad body entry")
    return float(v)


@router.put("/api/body/{date}")
def put_body(app, req: Req):
    date = req.params["date"]
    try:
        d = req.json_obj()
    except HTTPError:
        raise bad_request("bad body entry")
    if not valid_date(date):
        raise bad_request("bad body entry")
    vals = [_num(d.get(k)) for k, _ in BODY_FIELDS]
    note = d.get("note") if isinstance(d.get("note"), str) else ""
    for v in vals:
        if v is not None and (v <= 0 or v > 1000):
            raise bad_request("values must be positive")
    app.db.exec(
        """INSERT INTO body_log(date, weight_kg, waist_cm, neck_cm, hip_cm, chest_cm, arm_cm, thigh_cm, body_fat_pct, note, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(date) DO UPDATE SET weight_kg=excluded.weight_kg, waist_cm=excluded.waist_cm, neck_cm=excluded.neck_cm,
        hip_cm=excluded.hip_cm, chest_cm=excluded.chest_cm, arm_cm=excluded.arm_cm, thigh_cm=excluded.thigh_cm,
        body_fat_pct=excluded.body_fat_pct, note=excluded.note, updated_at=excluded.updated_at""",
        date, *vals, nz(note), now())
    out: dict[str, Any] = {"date": date}
    for (k, _), v in zip(BODY_FIELDS, vals):
        out[k] = v
    if note:
        out["note"] = note
    return out


@router.delete("/api/body/{date}")
def delete_body(app, req: Req):
    app.db.exec("DELETE FROM body_log WHERE date = ?", req.params["date"])
    return {"ok": True}


# ───────────────────────── fasting ─────────────────────────

def parse_time(s: Any) -> str | None:
    """RFC 3339 -> UTC 'Z' form, or None when it doesn't parse."""
    if not isinstance(s, str) or not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00") if s.endswith("Z") else s)
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_fasts(app, limit: int) -> list[dict]:
    rows = app.db.query("SELECT id, start_at, end_at, target_hours FROM fasts ORDER BY start_at DESC LIMIT ?", limit)
    return [{"id": r[0], "startAt": r[1], "endAt": r[2], "targetHours": r[3]} for r in rows]


@router.get("/api/fasts")
def get_fasts(app, req: Req):
    return list_fasts(app, 60)


def _opt_obj(req: Req) -> dict:
    try:
        v = req.json()
    except HTTPError:
        return {}
    return v if isinstance(v, dict) else {}


@router.post("/api/fasts/start")
def start_fast(app, req: Req):
    b = _opt_obj(req)
    th = b.get("targetHours")
    th = float(th) if isinstance(th, (int, float)) and not isinstance(th, bool) else 0.0
    if th <= 0 or th > 168:
        th = 16.0
    start = parse_time(b.get("startAt")) or now()
    with app.db.tx() as c:
        c.execute("UPDATE fasts SET end_at = ? WHERE end_at IS NULL", (start,))
        fid = c.execute("INSERT INTO fasts(start_at, target_hours) VALUES(?, ?)", (start, th)).lastrowid
    return 201, {"id": fid, "startAt": start, "endAt": None, "targetHours": th}


@router.post("/api/fasts/{id}/end")
def end_fast(app, req: Req):
    fid = req.path_id()
    end = parse_time(_opt_obj(req).get("endAt")) or now()
    app.db.exec("UPDATE fasts SET end_at = ? WHERE id = ?", end, fid)
    return {"ok": True, "endAt": end}


@router.patch("/api/fasts/{id}")
def patch_fast(app, req: Req):
    fid = req.path_id()
    b = req.json_obj()
    s = parse_time(b.get("startAt"))
    if s:
        app.db.exec("UPDATE fasts SET start_at = ? WHERE id = ?", s, fid)
    e = parse_time(b.get("endAt"))
    if e:
        app.db.exec("UPDATE fasts SET end_at = ? WHERE id = ?", e, fid)
    th = b.get("targetHours")
    if isinstance(th, (int, float)) and not isinstance(th, bool) and th > 0:
        app.db.exec("UPDATE fasts SET target_hours = ? WHERE id = ?", float(th), fid)
    return {"ok": True}


@router.delete("/api/fasts/{id}")
def delete_fast(app, req: Req):
    try:
        fid = req.path_id()
    except HTTPError:
        fid = 0
    app.db.exec("DELETE FROM fasts WHERE id = ?", fid)
    return {"ok": True}


# ───────────────────────── progress bundle ─────────────────────────

@router.get("/api/progress")
def progress(app, req: Req):
    frm = req.query.get("from")
    if not valid_date(frm):
        frm = "0000-01-01"
    body = list_body(app)
    sessions = []
    for r in app.db.query(f"SELECT {SESSION_COLS} FROM sessions s WHERE s.date >= ? ORDER BY s.date, s.id", frm):
        sessions.append(_session(r))
    nrows = app.db.query(
        """WITH meals AS (
            SELECT date, meal, SUM(COALESCE(json_extract(nutrients,'$.fat'),0)) AS fat FROM food_log WHERE date >= ? GROUP BY date, meal)
        SELECT l.date,
            SUM(COALESCE(json_extract(l.nutrients,'$.kcal'),0)), SUM(COALESCE(json_extract(l.nutrients,'$.protein'),0)),
            SUM(COALESCE(json_extract(l.nutrients,'$.carbs'),0)), SUM(COALESCE(json_extract(l.nutrients,'$.fat'),0)),
            SUM(COALESCE(json_extract(l.nutrients,'$.fiber'),0)), SUM(COALESCE(json_extract(l.nutrients,'$.sugars'),0)),
            SUM(COALESCE(json_extract(l.nutrients,'$.sodium'),0)), COUNT(*),
            (SELECT MAX(fat) FROM meals m WHERE m.date = l.date)
        FROM food_log l WHERE l.date >= ? GROUP BY l.date ORDER BY l.date""", frm, frm)
    nutrition = [{"date": r[0], "kcal": r[1], "protein": r[2], "carbs": r[3], "fat": r[4], "fiber": r[5], "sugars": r[6],
                  "sodium": r[7], "entries": r[8], "maxMealFat": r[9] or 0.0} for r in nrows]
    water = [{"date": r[0], "ml": r[1]} for r in app.db.query("SELECT date, ml FROM water_log WHERE date >= ? ORDER BY date", frm)]
    return {"body": body, "sessions": sessions, "nutrition": nutrition, "water": water,
            "fasts": list_fasts(app, 2000), "activities": list_activities(app, frm, "9999-12-31")}


# ───────────────────────── progress photos ─────────────────────────

PHOTO_NAME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[a-f0-9]{16}(_t)?\.jpg$")
MAX_UPLOAD = 30 << 20


def _photos_dir(app) -> Path:
    return Path(app.opt.data_dir) / "photos"


@router.get("/api/photos")
def list_photos(app, req: Req):
    rows = app.db.query("SELECT id, date, file, COALESCE(thumb, file), COALESCE(note,'') FROM photos ORDER BY date DESC, id DESC")
    out = []
    for r in rows:
        p = {"id": r[0], "date": r[1], "url": "/photos/" + r[2], "thumbUrl": "/photos/" + r[3]}
        if r[4]:
            p["note"] = r[4]
        out.append(p)
    return out


def is_jpeg(b: bytes | None) -> bool:
    return bool(b) and len(b) > 3 and b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF


def parse_multipart(req: Req) -> tuple[dict[str, str], dict[str, bytes]]:
    ctype = req.headers.get("content-type", "")
    if not ctype.lower().startswith("multipart/form-data"):
        raise bad_request("upload too large or malformed")
    if len(req.body) > MAX_UPLOAD:
        raise bad_request("upload too large or malformed")
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + req.body)
    if not msg.is_multipart():
        raise bad_request("upload too large or malformed")
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            files[name] = payload
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


@router.post("/api/photos")
def upload_photo(app, req: Req):
    fields, files = parse_multipart(req)
    date = fields.get("date", "")
    if not valid_date(date):
        date = datetime.now().strftime("%Y-%m-%d")
    img, thumb = files.get("image"), files.get("thumb")
    if not is_jpeg(img):
        raise bad_request("image must be a JPEG")
    base = f"{date}_{secrets.token_hex(8)}"
    d = _photos_dir(app)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{base}.jpg").write_bytes(img)
    thumb_name = f"{base}.jpg"
    if is_jpeg(thumb):
        thumb_name = f"{base}_t.jpg"
        (d / thumb_name).write_bytes(thumb)
    note = fields.get("note", "")
    pid = app.db.exec("INSERT INTO photos(date, file, thumb, note, created_at) VALUES(?,?,?,?,?)",
                      date, f"{base}.jpg", thumb_name, nz(note), now())
    out = {"id": pid, "date": date, "url": f"/photos/{base}.jpg", "thumbUrl": f"/photos/{thumb_name}"}
    if note:
        out["note"] = note
    return 201, out


@router.patch("/api/photos/{id}")
def patch_photo(app, req: Req):
    pid = int(req.params["id"]) if req.params.get("id", "").isdigit() else 0
    b = _opt_obj(req)
    if isinstance(b.get("date"), str) and valid_date(b["date"]):
        app.db.exec("UPDATE photos SET date = ? WHERE id = ?", b["date"], pid)
    if isinstance(b.get("note"), str):
        app.db.exec("UPDATE photos SET note = ? WHERE id = ?", nz(b["note"]), pid)
    return {"ok": True}


@router.delete("/api/photos/{id}")
def delete_photo(app, req: Req):
    pid = int(req.params["id"]) if req.params.get("id", "").isdigit() else 0
    r = app.db.one("SELECT file, COALESCE(thumb,'') FROM photos WHERE id = ?", pid)
    if r:
        for f in (r[0], r[1]):
            if PHOTO_NAME.match(f):
                try:
                    (_photos_dir(app) / f).unlink()
                except OSError:
                    pass
        app.db.exec("DELETE FROM photos WHERE id = ?", pid)
    return {"ok": True}


@router.get("/photos/{file}")
def serve_photo(app, req: Req):
    name = req.params["file"]
    p = _photos_dir(app) / name
    if not PHOTO_NAME.match(name) or not p.is_file():
        raise not_found("404 page not found")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"})


# ───────────────────────── export & backup ─────────────────────────

def table_json(app, q: str) -> list[dict]:
    try:
        with app.db.conn() as c:
            cur = c.execute(q)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        m = {}
        for c_, v in zip(cols, r):
            if isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            if isinstance(v, str) and c_ in ("data", "nutrients"):
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
            m[c_] = v
        out.append(m)
    return out


@router.get("/api/export")
def export(app, req: Req):
    st = app.settings()
    st.pop("usdaKey", None)
    if isinstance(st.get("ai"), dict):
        st["ai"].pop("apiKey", None)
    from .routes_core import current_plans

    doc = {
        "app": "MeskoFit", "version": app.opt.version, "exportedAt": now(),
        "profile": app.profile(), "settings": st, "plans": current_plans(app),
        "body": table_json(app, "SELECT * FROM body_log ORDER BY date"),
        "sessions": table_json(app, "SELECT * FROM sessions ORDER BY date, id"),
        "sets": table_json(app, "SELECT * FROM sets ORDER BY session_id, plan_ex_id, set_no"),
        "foodLog": table_json(app, "SELECT * FROM food_log ORDER BY date, id"),
        "myFoods": table_json(app, "SELECT * FROM foods WHERE custom = 1 OR favorite = 1"),
        "water": table_json(app, "SELECT * FROM water_log ORDER BY date"),
        "fasts": table_json(app, "SELECT * FROM fasts ORDER BY start_at"),
        "activities": table_json(app, "SELECT * FROM activities ORDER BY date"),
        "shots": app.db.kv_get("shots") or [],
        "photos": table_json(app, "SELECT id, date, file, note FROM photos ORDER BY date"),
    }
    resp = json_resp(doc)
    resp.headers["Content-Disposition"] = f'attachment; filename="meskofit-export-{datetime.now().strftime("%Y-%m-%d")}.json"'
    return resp


@router.get("/api/backup")
def backup(app, req: Req):
    tmp = Path(app.opt.data_dir) / f"backup-{os.getpid()}-{secrets.token_hex(4)}.db"
    try:
        app.db.backup_to(tmp)
        data = tmp.read_bytes()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return Response(data, 200, {"Content-Type": "application/vnd.sqlite3",
                                "Content-Disposition": f'attachment; filename="meskofit-{datetime.now().strftime("%Y-%m-%d")}.db"'})
