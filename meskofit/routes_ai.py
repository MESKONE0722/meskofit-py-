"""AI photo analysis routes: meal photos, nutrition labels, and a provider connection test."""
from __future__ import annotations

import base64
import binascii
import dataclasses
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import ai
from .db import now
from .food.types import Food, round_n
from .web import HTTPError, Req, Router, bad_request

router = Router()

MEAL_SYSTEM = """You are a careful nutrition assistant inside a food-logging app. You look at photos of meals, identify the foods and estimate portion sizes. You answer with JSON only."""

MEAL_PROMPT = """Identify every distinct food or drink in this photo.
For each item return:
- name: short everyday name, e.g. "white rice", "grilled chicken thigh"
- search: a generic phrase to find it in the USDA food database, e.g. "rice white cooked", "chicken thigh roasted"
- grams: best estimate of the edible portion weight in grams. Use the plate (a dinner plate is about 26 cm / 10 in), utensils, hands and packaging for scale.
- kcal, protein, carbs, fat: your estimate for that portion (macros in grams)
- confidence: "high", "medium" or "low"
List sauces, cooking oil you can see, and drinks as separate items. People usually underestimate portions, so don't lowball.
If there is no food in the photo, return {"items": []}.
Also return "notes": one short sentence about anything you were unsure of."""

_num = {"type": "number"}
_str = {"type": "string"}

MEAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": _str,
                    "search": _str,
                    "grams": _num,
                    "kcal": _num,
                    "protein": _num,
                    "carbs": _num,
                    "fat": _num,
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["name", "search", "grams", "kcal", "protein", "carbs", "fat", "confidence"],
            },
        },
        "notes": _str,
    },
    "required": ["items", "notes"],
}

LABEL_SYSTEM = """You read Nutrition Facts labels from photos for a food-logging app. You copy numbers exactly as printed and answer with JSON only."""

LABEL_PROMPT = """This photo shows a packaged food's Nutrition Facts label (the package front may also be visible).
Read the values exactly as printed for ONE serving and return JSON with these keys:
productName (if visible), servingSize (text as printed, e.g. "2/3 cup (55g)"), servingGrams (grams or ml in one serving),
servingsPerContainer, calories, fat, satFat, transFat, cholesterol (mg), sodium (mg), carbs, fiber, sugars, addedSugars,
protein, ingredients (if visible).
Numbers only, without units. Leave out any key you cannot read."""

LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "productName": _str, "servingSize": _str,
        "servingGrams": _num, "servingsPerContainer": _num,
        "calories": _num, "fat": _num,
        "satFat": _num, "transFat": _num,
        "cholesterol": _num, "sodium": _num,
        "carbs": _num, "fiber": _num,
        "sugars": _num, "addedSugars": _num,
        "protein": _num, "ingredients": _str,
    },
    "required": ["servingSize", "calories", "fat", "carbs", "protein"],
}

AI_TIMEOUT = 4 * 60
LOOKUP_TIMEOUT = 15
BAD_FORMAT = "the model's answer wasn't in the expected format"


def is_jpeg(b: bytes) -> bool:
    return len(b) > 3 and b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF


def decode_image(s: Any) -> bytes:
    """Decode a base64 JPEG (optionally a data: URL); raises a 400 on anything else."""
    if not isinstance(s, str):
        s = ""
    if s.startswith("data:") and "," in s[1:]:
        s = s[s.index(",") + 1 :]
    try:
        b = base64.b64decode(s.replace("\r", "").replace("\n", ""), validate=True)
    except (binascii.Error, ValueError):
        raise bad_request("image must be base64 JPEG")
    if not is_jpeg(b):
        raise bad_request("image must be a JPEG")
    return b


def _fail(e: Exception) -> HTTPError:
    """Map an AI failure to the status the Go app uses: 412 when unconfigured, else 502."""
    return HTTPError(412 if isinstance(e, ai.ErrDisabled) else 502, str(e))


def _image_body(req: Req) -> bytes:
    return decode_image(req.json_obj().get("image"))


def cache_food(app: Any, f: Food) -> None:
    """Upsert a food into the local cache and fill in its id / favorite flag."""
    f.fetchedAt = now()
    cp = dataclasses.replace(f, id=0, favorite=False)
    with app.db.tx() as c:
        row = c.execute(
            """INSERT INTO foods(source, source_id, barcode, name, brand, data, custom, fetched_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(source, source_id) DO UPDATE SET barcode=excluded.barcode, name=excluded.name,
                brand=excluded.brand, data=excluded.data, fetched_at=excluded.fetched_at
            RETURNING id, favorite""",
            (f.source, f.sourceId, f.barcode.strip() and f.barcode or None, f.name, f.brand.strip() and f.brand or None,
             json.dumps(cp.to_json(), ensure_ascii=False, separators=(",", ":")), int(f.custom), f.fetchedAt),
        ).fetchone()
    f.id, f.favorite = row[0], bool(row[1])


def _f(v: Any) -> float:
    """Lenient number read (a missing/null key is 0); anything non-numeric is a format error."""
    if v is None:
        return 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("not a number")
    return float(v)


def _s(v: Any) -> str:
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("not a string")
    return v


@router.post("/api/ai/meal")
def handle_ai_meal(app: Any, req: Req) -> dict:
    img = _image_body(req)
    cfg = app.ai_config()
    started = time.monotonic()
    try:
        raw = app.ai.vision_json(cfg, MEAL_SYSTEM, MEAL_PROMPT, img, MEAL_SCHEMA, timeout=AI_TIMEOUT)
    except ai.AIError as e:
        raise _fail(e)
    items: list[dict] = []
    try:
        parsed = json.loads(raw)
        raw_items = parsed.get("items") or []
        if not isinstance(raw_items, list):
            raise ValueError("items")
        notes = _s(parsed.get("notes"))
        for it in raw_items:
            if not isinstance(it, dict):
                raise ValueError("item")
            name, grams = _s(it.get("name")).strip(), _f(it.get("grams"))
            search, conf = _s(it.get("search")).strip(), _s(it.get("confidence"))
            nutr = {k: _f(it.get(k)) for k in ("kcal", "protein", "carbs", "fat")}
            if name == "" or grams <= 0 or grams > 3000:
                continue
            items.append({"name": name, "search": search, "grams": grams, "confidence": conf, "ai": round_n(nutr)})
            if len(items) == 10:
                break
    except (ValueError, AttributeError):
        raise HTTPError(502, BAD_FORMAT)

    usda_err = ""
    if app.settings().get("aiMatchUsda") is True and items:

        def lookup(it: dict) -> tuple[Food | None, str]:
            try:
                foods = app.usda.search_foods(it["search"] or it["name"], 5)
            except Exception as e:  # noqa: BLE001 - a failed lookup only drops the match
                return None, str(e)
            return (foods[0] if foods else None), ""

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lookup, items))
        for it, (food, err) in zip(items, results):
            if err:
                usda_err = err
            if food is not None:
                try:
                    cache_food(app, food)
                except Exception:  # noqa: BLE001 - caching is best-effort, like the Go app
                    pass
                it["match"] = food.to_json()
    return {"items": items, "notes": notes, "model": cfg.model, "usdaError": usda_err,
            "seconds": time.monotonic() - started}


@router.post("/api/ai/label")
def handle_ai_label(app: Any, req: Req) -> dict:
    img = _image_body(req)
    try:
        raw = app.ai.vision_json(app.ai_config(), LABEL_SYSTEM, LABEL_PROMPT, img, LABEL_SCHEMA, timeout=AI_TIMEOUT)
    except ai.AIError as e:
        raise _fail(e)
    label = json.loads(raw)
    if not isinstance(label, dict):
        raise HTTPError(502, BAD_FORMAT)
    return label


@router.post("/api/ai/test")
def handle_ai_test(app: Any, req: Req) -> dict:
    body = req.json_obj()

    def s(k: str) -> str:
        v = body.get(k)
        return v if isinstance(v, str) else ""

    cfg = ai.Config(provider=s("provider"), base_url=s("baseUrl"), model=s("model"), api_key=s("apiKey"))
    if cfg.api_key == "":
        cfg.api_key = app.ai_config().api_key
    try:
        models = app.ai.models(cfg, timeout=15)
    except ai.AIError as e:
        return {"ok": False, "error": str(e)}
    found = any(m == cfg.model or m.removesuffix(":latest") == cfg.model for m in models)
    return {"ok": True, "models": models, "modelFound": found}
