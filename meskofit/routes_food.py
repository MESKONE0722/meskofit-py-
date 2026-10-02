"""Food routes: search, barcode, item cache, your foods, the food log and water (port of internal/app/h_food.go)."""
from __future__ import annotations

import dataclasses
import json
import re
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import now
from .food.off import FoodError, NotFound, RateLimited
from .food.types import Food, Hit, barcode_variants, normalize_barcode, round_n
from .web import HTTPError, Req, Router, valid_date

router = Router()

CACHE_TTL = timedelta(days=30)
FOOD_COLS = "id, data, favorite, custom, fetched_at"
LOG_COLS = """l.id, l.date, l.meal, l.food_id, l.name, COALESCE(l.brand,''), l.amount, l.unit, COALESCE(l.unit_label,''),
	l.grams, l.nutrients, COALESCE(l.source,''), l.created_at, COALESCE(json_extract(f.data, '$.imageUrl'), '')"""

# ───────────────────────── JSON helpers (match Go's encoder) ─────────────────────────


def _norm(v: Any) -> Any:
    """Integral floats become ints so they print like Go's (250 not 250.0)."""
    if isinstance(v, float):
        return int(v) if v == v and abs(v) < 1e21 and v == int(v) else v
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    return v


def _go_dumps(v: Any) -> str:
    """json.Marshal: sorted map keys (callers pre-sort structs), compact, <>& escaped."""
    s = json.dumps(_norm(v), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026") \
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _sorted(d: dict | None) -> dict | None:
    return None if d is None else {k: d[k] for k in sorted(d)}


def _nut(d: dict | None) -> dict:
    return _sorted(d) or {}


def food_out(f: Food) -> dict:
    """Food as the API returns it: struct key order, maps sorted like Go's."""
    o = f.to_json()
    for k in ("per100", "perServing", "nutrientLevels"):
        if k in o:
            o[k] = _sorted(o[k])
    return _norm(o)


def hit_out(h: Hit) -> dict:
    o = h.to_json()
    if "per100" in o:
        o["per100"] = _sorted(o["per100"])
    return _norm(o)


def hit_of(f: Food) -> Hit:
    return Hit(source=f.source, sourceId=f.sourceId, name=f.name, brand=f.brand, imageUrl=f.imageUrl,
               per100=f.per100, servingLabel=f.servingLabel, localId=f.id)


def _body(req: Req) -> Any:
    """Decoded JSON body; an empty body is Go's EOF error."""
    if not req.body.strip():
        raise HTTPError(400, "invalid JSON body: EOF")
    return req.json()


def _kind(v: Any) -> str:
    return ("bool" if isinstance(v, bool) else "number" if isinstance(v, (int, float)) else "string" if isinstance(v, str)
            else "array" if isinstance(v, list) else "object" if isinstance(v, dict) else "null")


def _bad(v: Any, where: str, typ: str) -> HTTPError:
    return HTTPError(400, f"invalid JSON body: json: cannot unmarshal {_kind(v)} into Go {where} of type {typ}")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


_STR = ("source", "sourceId", "barcode", "name", "brand", "imageUrl", "url", "servingLabel", "ingredients",
        "nutriscore", "quantity", "category", "dataType", "fetchedAt")
_STRLIST = ("allergens", "traces", "labels", "additives", "analysis")


def food_from_body(d: Any) -> Food:
    """Decode a request body into a Food, rejecting wrong types the way Go's decoder does."""
    if d is None:
        return Food()
    if not isinstance(d, dict):
        raise _bad(d, "value", "food.Food")
    for k, v in d.items():
        if v is None:
            continue
        w = f"struct field Food.{k}"
        if k in _STR and not isinstance(v, str):
            raise _bad(v, w, "string")
        if k in ("servingGrams",) and not _is_num(v):
            raise _bad(v, w, "float64")
        if k in ("id", "nova") and not _is_int(v):
            raise _bad(v, w, "int64" if k == "id" else "int")
        if k in ("liquid", "favorite", "custom") and not isinstance(v, bool):
            raise _bad(v, w, "bool")
        if k in ("per100", "perServing"):
            if not isinstance(v, dict):
                raise _bad(v, w, "food.Nutrients")
            for x in v.values():
                if x is not None and not _is_num(x):
                    raise _bad(x, w, "float64")
        if k in _STRLIST and not (isinstance(v, list) and all(x is None or isinstance(x, str) for x in v)):
            raise _bad(v, w, "[]string")
        if k == "nutrientLevels" and not (isinstance(v, dict) and all(x is None or isinstance(x, str) for x in v.values())):
            raise _bad(v, w, "map[string]string")
        if k == "portions":
            ok = isinstance(v, list) and all(
                x is None or (isinstance(x, dict) and (x.get("label") is None or isinstance(x.get("label"), str))
                              and (x.get("grams") is None or _is_num(x.get("grams")))) for x in v)
            if not ok:
                raise _bad(v, w, "[]food.Portion")
    f = Food.from_json(d)
    f.per100 = {k: float(x) for k, x in f.per100.items() if x is not None}
    f.perServing = {k: float(x) for k, x in f.perServing.items() if x is not None}
    f.portions = [{"label": x.get("label") or "", "grams": float(x.get("grams") or 0)} for x in f.portions if x is not None]
    return f


# ───────────────────────── food cache ─────────────────────────


def cache_food(app, f: Food) -> None:
    """Insert or refresh a food in the cache; sets f.id / f.favorite from the stored row."""
    f.fetchedAt = now()
    cp = dataclasses.replace(f, id=0, favorite=False)
    data = _go_dumps(food_out(cp))
    with app.db.tx() as c:
        row = c.execute(
            """INSERT INTO foods(source, source_id, barcode, name, brand, data, custom, fetched_at)
		VALUES(?,?,?,?,?,?,?,?)
		ON CONFLICT(source, source_id) DO UPDATE SET barcode=excluded.barcode, name=excluded.name, brand=excluded.brand,
			data=excluded.data, fetched_at=excluded.fetched_at
		RETURNING id, favorite""",
            (f.source, f.sourceId, _nulls(f.barcode), f.name, _nulls(f.brand), data, 1 if f.custom else 0, f.fetchedAt),
        ).fetchone()
    f.id, f.favorite = row[0], bool(row[1])


def _nulls(s: str) -> str | None:
    return s if s.strip() != "" else None


def scan_food(row: sqlite3.Row | None) -> Food | None:
    """A Food from a (id, data, favorite, custom, fetched_at) row, or None."""
    if row is None:
        return None
    try:
        d = json.loads(row["data"])
        if not isinstance(d, dict):
            return None
        f = Food.from_json(d)
    except (ValueError, TypeError):
        return None
    f.id, f.favorite, f.custom, f.fetchedAt = row["id"], bool(row["favorite"]), bool(row["custom"]), row["fetched_at"]
    return f


def food_by_id(app, fid: int) -> Food | None:
    return scan_food(app.db.one(f"SELECT {FOOD_COLS} FROM foods WHERE id = ?", fid))


def food_by_source(app, source: str, source_id: str) -> Food | None:
    return scan_food(app.db.one(f"SELECT {FOOD_COLS} FROM foods WHERE source = ? AND source_id = ?", source, source_id))


def fresh(f: Food) -> bool:
    """Cached less than 30 days ago (fetchedAt must be RFC 3339)."""
    try:
        t = datetime.fromisoformat(f.fetchedAt)
    except ValueError:
        return False
    if t.tzinfo is None or "T" not in f.fetchedAt:
        return False
    return datetime.now(timezone.utc) - t < CACHE_TTL


# ───────────────────────── barcode ─────────────────────────


def lookup_barcode(app, code: str, refresh: bool = False) -> Food:
    """custom -> fresh cache -> Open Food Facts -> USDA branded -> stale cache. Raises NotFound or a FoodError."""
    variants = barcode_variants(code)
    if not variants:
        raise NotFound()
    ph = ",".join("?" * len(variants))
    # 1. your own foods with this barcode always win
    f = scan_food(app.db.one(f"SELECT {FOOD_COLS} FROM foods WHERE custom = 1 AND barcode IN ({ph}) ORDER BY id DESC LIMIT 1", *variants))
    if f:
        return f
    # 2. cached lookups
    cached = scan_food(app.db.one(f"SELECT {FOOD_COLS} FROM foods WHERE custom = 0 AND barcode IN ({ph}) ORDER BY fetched_at DESC LIMIT 1", *variants))
    if cached and not refresh and fresh(cached):
        return cached
    # 3. Open Food Facts, then 4. USDA branded foods
    off_food: Food | None = None
    off_err: Exception | None = None
    try:
        off_food = app.off.product(code)
    except FoodError as e:
        off_err = e
    if off_food is not None and off_food.has_energy():
        _try_cache(app, off_food)
        return off_food
    usda_food: Food | None = None
    usda_err: Exception | None = None
    try:
        usda_food = app.usda.barcode(code)
    except FoodError as e:
        usda_err = e
    if usda_food is not None:
        if off_food is not None and not usda_food.imageUrl:
            usda_food.imageUrl = off_food.imageUrl
        _try_cache(app, usda_food)
        return usda_food
    if off_food is not None:  # found but without nutrition facts, still useful
        _try_cache(app, off_food)
        return off_food
    if cached is not None:
        return cached  # stale but better than nothing (e.g. offline)
    if isinstance(off_err, NotFound) and isinstance(usda_err, (NotFound, RateLimited)):
        raise NotFound()
    if not isinstance(off_err, NotFound):
        raise off_err  # type: ignore[misc]
    raise usda_err  # type: ignore[misc]


def _try_cache(app, f: Food) -> None:
    """Go ignores cache errors on the lookup path."""
    try:
        cache_food(app, f)
    except sqlite3.Error:
        pass


@router.get("/api/food/barcode/{code}")
def handle_barcode(app, req: Req):
    code = normalize_barcode(req.params.get("code", ""))
    if len(code) < 6 or len(code) > 14:
        raise HTTPError(400, "that doesn't look like a product barcode")
    try:
        f = lookup_barcode(app, code, req.query.get("refresh") == "1")
    except NotFound:
        return 404, {"code": code, "error": "not found"}
    except FoodError as e:
        raise HTTPError(502, str(e))
    return food_out(f)


# ───────────────────────── search ─────────────────────────


@router.get("/api/food/search")
def handle_food_search(app, req: Req):
    q = req.query.get("q").strip()
    if len(q.encode()) < 2:
        raise HTTPError(400, "type at least 2 characters")

    local: list[Hit] = []
    like = "%" + q.replace("%", "").replace("_", "") + "%"
    try:
        rows = app.db.query(
            f"""SELECT {FOOD_COLS} FROM foods
		WHERE (custom = 1 OR favorite = 1 OR id IN (SELECT DISTINCT food_id FROM food_log WHERE food_id IS NOT NULL))
		AND (name LIKE ? OR brand LIKE ?) ORDER BY custom DESC, favorite DESC, id DESC LIMIT 8""", like, like)
        for r in rows:
            f = scan_food(r)
            if f:
                local.append(hit_of(f))
    except sqlite3.Error:
        pass

    errs: dict[str, str] = {}
    digits = normalize_barcode(q)
    if len(digits) >= 8 and len(digits) == len(q.replace(" ", "")):
        branded: list[Hit] = []
        try:
            branded.append(hit_of(lookup_barcode(app, digits, False)))
        except Exception:  # noqa: BLE001 - Go ignores the error here
            pass
        return {"branded": [hit_out(h) for h in branded], "errors": errs, "generic": [], "local": [hit_out(h) for h in local]}

    with ThreadPoolExecutor(max_workers=2) as pool:
        fg = pool.submit(app.usda.generic, q, 25)
        fb = pool.submit(app.off.search, q, 24)
        generic: list[Hit] = []
        branded = []
        try:
            generic = fg.result()
        except Exception as e:  # noqa: BLE001
            errs["usda"] = str(e)
        try:
            branded = fb.result()
        except Exception as e:  # noqa: BLE001
            errs["off"] = str(e)
    return {"branded": [hit_out(h) for h in branded], "errors": errs, "generic": [hit_out(h) for h in generic],
            "local": [hit_out(h) for h in local]}


def _parse_go_int(s: str) -> int:
    """json.Unmarshal of a path segment into an int64; errors carry Go-like messages."""
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", s):
        n = int(s)
        if -(1 << 63) <= n < (1 << 63):
            return n
        raise FoodError(f"json: cannot unmarshal number {s} into Go value of type int64")
    if s == "":
        raise FoodError("unexpected end of JSON input")
    m = re.match(r"-?(0|[1-9][0-9]*)", s)
    if m and m.group(0):
        rest = s[m.end():]
        if rest[0] in ".eE" and re.fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?", s):
            raise FoodError(f"json: cannot unmarshal number {s} into Go value of type int64")
        raise FoodError(f"invalid character '{rest[0]}' after top-level value")
    raise FoodError(f"invalid character '{s[0]}' looking for beginning of value")


@router.get("/api/food/item/{source}/{id}")
def handle_food_item(app, req: Req):
    """The full record for a search hit, cached."""
    source, fid = req.params.get("source", ""), req.params.get("id", "")
    f: Food | None = None
    try:
        if source in ("local", "custom"):
            f = food_by_id(app, _parse_go_int(fid))
            if f is None:
                raise NotFound()
        elif source == "off":
            c = food_by_source(app, "off", fid)
            if c and fresh(c):
                f = c
            else:
                f = app.off.product(fid)
                cache_food(app, f)
        elif source == "usda":
            c = food_by_source(app, "usda", fid)
            if c and fresh(c) and c.portions:
                f = c
            else:
                try:
                    f = app.usda.details(fid)
                    cache_food(app, f)
                except (FoodError, sqlite3.Error):
                    c2 = food_by_source(app, "usda", fid)
                    if c2 is None:
                        raise
                    f = c2
        else:
            raise NotFound()
    except NotFound:
        raise HTTPError(404, "food not found")
    except (FoodError, sqlite3.Error) as e:
        raise HTTPError(502, str(e))
    return food_out(f)


# ───────────────────────── your foods ─────────────────────────


@router.get("/api/foods")
def handle_list_foods(app, req: Req):
    where = {"favorites": "favorite = 1", "all": "custom = 1 OR favorite = 1"}.get(req.query.get("kind"), "custom = 1")
    rows = app.db.query(f"SELECT {FOOD_COLS} FROM foods WHERE {where} ORDER BY name COLLATE NOCASE")
    return [food_out(f) for f in (scan_food(r) for r in rows) if f]


def sanitize_custom(f: Food) -> None:
    """Validate and normalize a user-entered food (raises 400)."""
    f.name = f.name.strip()
    if f.name == "":
        raise HTTPError(400, "name is required")
    if not f.perServing and not f.per100:
        raise HTTPError(400, "enter at least calories")
    f.source, f.custom = "custom", True
    f.barcode = normalize_barcode(f.barcode)
    f.fill()


@router.post("/api/foods")
def handle_create_food(app, req: Req):
    f = food_from_body(_body(req))
    sanitize_custom(f)
    f.sourceId = secrets.token_hex(8)
    cache_food(app, f)
    return 201, food_out(f)


@router.put("/api/foods/{id}")
def handle_update_food(app, req: Req):
    fid = req.path_id()
    cur = food_by_id(app, fid)
    if cur is None:
        raise HTTPError(404, "food not found")
    if not cur.custom:
        raise HTTPError(400, "only your own foods can be edited — save a copy instead")
    f = food_from_body(_body(req))
    sanitize_custom(f)
    f.sourceId = cur.sourceId
    cache_food(app, f)
    return food_out(f)


@router.delete("/api/foods/{id}")
def handle_delete_food(app, req: Req):
    fid = req.path_id()
    try:
        with app.db.tx() as c:
            try:
                c.execute("UPDATE food_log SET food_id = NULL WHERE food_id = ?", (fid,))
            except sqlite3.Error:
                pass
            c.execute("DELETE FROM foods WHERE id = ? AND custom = 1", (fid,))
    except sqlite3.Error:
        pass
    return {"ok": True}


@router.post("/api/foods/{id}/favorite")
def handle_favorite_food(app, req: Req):
    fid = req.path_id()
    fav = False
    try:
        body = req.json()
        if isinstance(body, dict) and isinstance(body.get("favorite"), bool):
            fav = body["favorite"]
    except HTTPError:
        pass
    try:
        app.db.exec("UPDATE foods SET favorite = ? WHERE id = ?", 1 if fav else 0, fid)
    except sqlite3.Error:
        pass
    return {"favorite": fav, "ok": True}


# ───────────────────────── food log ─────────────────────────


def scan_log(r) -> dict:
    """A food_log row (LOG_COLS order) as the API object."""
    try:
        nut = json.loads(r[10])
        nut = _nut(nut) if isinstance(nut, dict) else None
    except ValueError:
        nut = None
    o: dict[str, Any] = {"id": r[0], "date": r[1], "meal": r[2], "foodId": r[3], "name": r[4]}
    if r[5]:
        o["brand"] = r[5]
    o["amount"], o["unit"] = r[6], r[7]
    if r[8]:
        o["unitLabel"] = r[8]
    o["grams"], o["nutrients"] = r[9], nut
    if r[11]:
        o["source"] = r[11]
    o["createdAt"] = r[12]
    if r[13]:
        o["imageUrl"] = r[13]
    return _norm(o)


@router.get("/api/log/{date}")
def handle_get_log(app, req: Req):
    date = req.params.get("date", "")
    if not valid_date(date):
        raise HTTPError(400, "bad date")
    rows = app.db.query(f"SELECT {LOG_COLS} FROM food_log l LEFT JOIN foods f ON f.id = l.food_id WHERE l.date = ? ORDER BY l.id", date)
    w = app.db.one("SELECT ml FROM water_log WHERE date = ?", date)
    return {"date": date, "entries": [scan_log(r) for r in rows], "waterMl": _norm(float(w[0])) if w else 0}


def _entry(d: Any, where: str = "") -> dict:
    """Decode one log entry object (Go's logEntry) with Go-style type checks."""
    if d is None:
        d = {}
    if not isinstance(d, dict):
        raise _bad(d, "value", "app.logEntry")
    e: dict[str, Any] = {"date": "", "meal": "", "foodId": None, "name": "", "brand": "", "amount": 0.0, "unit": "",
                         "unitLabel": "", "grams": None, "nutrients": None, "source": ""}
    for k in e:
        v = d.get(k)
        if v is None:
            continue
        w = f"struct field logEntry.{k}"
        if k in ("date", "meal", "name", "brand", "unit", "unitLabel", "source"):
            if not isinstance(v, str):
                raise _bad(v, w, "string")
        elif k == "foodId":
            if not _is_int(v):
                raise _bad(v, w, "int64")
        elif k in ("amount", "grams"):
            if not _is_num(v):
                raise _bad(v, w, "float64")
            v = float(v)
        elif k == "nutrients":
            if not isinstance(v, dict) or any(x is not None and not _is_num(x) for x in v.values()):
                raise _bad(v, w, "food.Nutrients")
            v = {a: float(b or 0) for a, b in v.items()}
        e[k] = v
    return e


def insert_log(app, e: dict) -> dict:
    """Store one entry; returns the API object."""
    nut = round_n(e["nutrients"])
    ts = now()
    e["id"] = app.db.exec(
        """INSERT INTO food_log(date, meal, food_id, name, brand, amount, unit, unit_label, grams, nutrients, source, created_at)
		VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        e["date"], e["meal"], e["foodId"], e["name"], _nulls(e["brand"]), e["amount"], e["unit"], _nulls(e["unitLabel"]),
        e["grams"], _go_dumps(_nut(nut)), _nulls(e["source"]), ts)
    o: dict[str, Any] = {"id": e["id"], "date": e["date"], "meal": e["meal"], "foodId": e["foodId"], "name": e["name"]}
    if e["brand"]:
        o["brand"] = e["brand"]
    o["amount"], o["unit"] = e["amount"], e["unit"]
    if e["unitLabel"]:
        o["unitLabel"] = e["unitLabel"]
    o["grams"], o["nutrients"] = e["grams"], _nut(nut)
    if e["source"]:
        o["source"] = e["source"]
    o["createdAt"] = ts
    return _norm(o)


@router.post("/api/log")
def handle_add_log(app, req: Req):
    body = _body(req)
    if body is not None and not isinstance(body, dict):
        raise _bad(body, "value", "struct")
    body = body or {}
    raw = body.get("entries")
    if raw is not None and not isinstance(raw, list):
        raise _bad(raw, "struct field .entries", "[]app.logEntry")
    entries = [_entry(x) for x in raw or []]
    lst = entries or [_entry(body)]
    for e in lst:
        e["name"] = e["name"].strip()
        if not valid_date(e["date"]) or e["meal"] == "" or e["name"] == "" or e["amount"] <= 0 or e["unit"] == "":
            raise HTTPError(400, "date, meal, name, amount and unit are required")
        if e["nutrients"] is None:
            e["nutrients"] = {}
    out = [insert_log(app, e) for e in lst]
    return 201, (out if entries else out[0])


_PATCH_COLS = {"amount": "amount", "unit": "unit", "unitLabel": "unit_label", "grams": "grams",
               "meal": "meal", "date": "date", "name": "name"}


@router.patch("/api/log/{id}")
def handle_patch_log(app, req: Req):
    lid = req.path_id()
    body = _body(req)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise _bad(body, "value", "map[string]json.RawMessage")
    sets: list[str] = []
    args: list[Any] = []
    for k, col in _PATCH_COLS.items():
        if k not in body:
            continue
        v = body[k]
        if k == "date" and not (isinstance(v, str) and valid_date(v)):
            continue
        sets.append(f"{col} = ?")
        args.append(v)
    if "nutrients" in body:
        n = body["nutrients"]
        if n is None:
            sets.append("nutrients = ?")
            args.append("null")
        elif isinstance(n, dict) and all(x is None or _is_num(x) for x in n.values()):
            sets.append("nutrients = ?")
            args.append(_go_dumps(_nut(round_n({a: float(b or 0) for a, b in n.items()}))))
    if not sets:
        raise HTTPError(400, "nothing to update")
    args.append(lid)
    app.db.exec(f"UPDATE food_log SET {', '.join(sets)} WHERE id = ?", *args)
    r = app.db.one(f"SELECT {LOG_COLS} FROM food_log l LEFT JOIN foods f ON f.id = l.food_id WHERE l.id = ?", lid)
    if r is None:
        raise HTTPError(404, "entry not found")
    return scan_log(r)


@router.delete("/api/log/{id}")
def handle_delete_log(app, req: Req):
    lid = req.path_id()
    app.db.exec("DELETE FROM food_log WHERE id = ?", lid)
    return {"ok": True}


@router.post("/api/log/copy")
def handle_copy_log(app, req: Req):
    err = HTTPError(400, "from and to dates are required")
    try:
        body = _body(req)
    except HTTPError:
        raise err
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise err
    vals = {}
    for k in ("from", "to", "meal", "toMeal"):
        v = body.get(k)
        if v is not None and not isinstance(v, str):
            raise err
        vals[k] = v or ""
    if not valid_date(vals["from"]) or not valid_date(vals["to"]):
        raise err
    q = """INSERT INTO food_log(date, meal, food_id, name, brand, amount, unit, unit_label, grams, nutrients, source, created_at)
		SELECT ?, COALESCE(NULLIF(?, ''), meal), food_id, name, brand, amount, unit, unit_label, grams, nutrients, source, ?
		FROM food_log WHERE date = ?"""
    args: list[Any] = [vals["to"], vals["toMeal"], now(), vals["from"]]
    if vals["meal"] != "":
        q += " AND meal = ?"
        args.append(vals["meal"])
    with app.db.tx() as c:
        n = c.execute(q + " ORDER BY id", args).rowcount
    return {"copied": n}


@router.get("/api/foods/recent")
def handle_recent_foods(app, req: Req):
    """One-tap re-logging: the last way you logged each food."""
    rows = app.db.query(f"SELECT {LOG_COLS} FROM food_log l LEFT JOIN foods f ON f.id = l.food_id ORDER BY l.id DESC LIMIT 600")
    out: list[dict] = []
    index: dict[str, dict] = {}
    for r in rows:
        e = scan_log(r)
        key = ("n:" + (e["name"] + "|" + e.get("brand", "")).lower()) if e["foodId"] is None else f"f:{e['foodId']}"
        rc = index.get(key)
        if rc is not None:
            rc["count"] += 1
            continue
        rc = {"entry": e, "count": 1, "favorite": False}
        index[key] = rc
        out.append(rc)
    favs = {r[0] for r in app.db.query("SELECT id FROM foods WHERE favorite = 1")}
    for rc in out:
        if rc["entry"]["foodId"] is not None:
            rc["favorite"] = rc["entry"]["foodId"] in favs
    return out[:60]


@router.put("/api/water/{date}")
def handle_put_water(app, req: Req):
    date = req.params.get("date", "")
    err = HTTPError(400, "date and ml (0–20000) required")
    try:
        body = _body(req)
    except HTTPError:
        raise err
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise err
    ml = body.get("ml")
    if ml is None:
        ml = 0.0
    if not _is_num(ml) or not valid_date(date) or ml < 0 or ml > 20000:
        raise err
    ml = float(ml)
    app.db.exec("INSERT INTO water_log(date, ml) VALUES(?, ?) ON CONFLICT(date) DO UPDATE SET ml = excluded.ml", date, ml)
    return {"date": date, "waterMl": _norm(ml)}
