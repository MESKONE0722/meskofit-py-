"""USDA FoodData Central client (port of internal/food/usda.go)."""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import quote_plus

import httpx

from .off import BadKey, FoodError, NotFound, RateLimited, StatusError, do_json, first_non_empty
from .types import Food, Hit, fmt_num, normalize_barcode, same_barcode, barcode_variants

SEARCH_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)"]


class USDA:
    """FoodData Central client. A free api.data.gov key lifts DEMO_KEY's tiny limit to 1,000/hour."""

    def __init__(self, http: httpx.Client | None, key_func: Callable[[], str] | None = None,
                 base_url: str = "https://api.nal.usda.gov/fdc/v1"):
        self.http = http
        self.base_url = base_url
        self.key_func = key_func

    def key(self) -> str:
        if self.key_func is not None:
            k = (self.key_func() or "").strip()
            if k:
                return k
        return "DEMO_KEY"

    def generic(self, q: str, limit: int) -> list[Hit]:
        """Search whole/generic foods (Foundation, SR Legacy, FNDDS)."""
        out: list[Hit] = []
        for u in self._search(q, SEARCH_TYPES, limit):
            f = usda_to_food(u)
            if not f.has_energy():
                continue
            out.append(Hit(source="usda", sourceId=f.sourceId, name=f.name, brand=f.brand, per100=f.per100,
                           dataType=f.dataType, servingLabel=first_portion_label(f)))
        return out

    def search_foods(self, q: str, limit: int) -> list[Food]:
        """Fully parsed foods (used for AI meal matching)."""
        foods = (usda_to_food(u) for u in self._search(q, SEARCH_TYPES, limit))
        return [f for f in foods if f.has_energy()]

    def barcode(self, code: str) -> Food:
        """Find a branded food by its GTIN/UPC. Raises NotFound."""
        for v in barcode_variants(code)[:2]:
            for u in self._search(v, ["Branded"], 10):
                gtin = u.get("gtinUpc") if isinstance(u.get("gtinUpc"), str) else ""
                if same_barcode(gtin, code) or same_barcode(gtin, v):
                    return self.details(str(_int(u.get("fdcId"))))
        raise NotFound()

    def details(self, fdc_id: str) -> Food:
        """One food with its household portions."""
        if not re.fullmatch(r"[+-]?[0-9]+", fdc_id or "") or not -(1 << 63) <= int(fdc_id) < (1 << 63):
            raise NotFound()
        url = f"{self.base_url}/food/{fdc_id}?api_key={quote_plus(self.key())}"
        try:
            f = do_json(self.http, "GET", url)
        except (StatusError, FoodError) as e:
            raise map_usda_err(e) from e
        if _int(f.get("fdcId")) == 0:
            raise NotFound()
        return usda_to_food(f)

    def _search(self, q: str, types: list[str], limit: int) -> list[dict]:
        body = json.dumps({"dataType": types, "pageSize": limit, "query": q}, separators=(",", ":")).encode()
        url = f"{self.base_url}/foods/search?api_key={quote_plus(self.key())}"
        try:
            resp = do_json(self.http, "POST", url, content=body, headers={"Content-Type": "application/json"})
        except (StatusError, FoodError) as e:
            raise map_usda_err(e) from e
        foods = resp.get("foods")
        return [x for x in foods if isinstance(x, dict)] if isinstance(foods, list) else []


def map_usda_err(err: FoodError) -> FoodError:
    """Translate HTTP failures into the typed errors the routes understand."""
    if isinstance(err, StatusError):
        if err.status == 429 or "OVER_RATE_LIMIT" in err.body:
            return RateLimited()
        if err.status == 403 or "API_KEY" in err.body:
            return BadKey()
        if err.status == 404:
            return NotFound()
    return FoodError(f"USDA: {err}")


USDA_BY_ID = {
    1008: "kcal", 1003: "protein", 1004: "fat", 1005: "carbs", 2000: "sugars", 1079: "fiber",
    1093: "sodium", 1258: "satFat", 1257: "transFat", 1253: "cholesterol", 1235: "addedSugars",
    1092: "potassium", 1087: "calcium", 1089: "iron", 1162: "vitC", 1114: "vitD",
}
USDA_BY_NUMBER = {
    "208": "kcal", "203": "protein", "204": "fat", "205": "carbs", "269": "sugars", "291": "fiber",
    "307": "sodium", "606": "satFat", "605": "transFat", "601": "cholesterol", "539": "addedSugars",
    "306": "potassium", "301": "calcium", "303": "iron", "401": "vitC", "328": "vitD",
}
# Fallback ids when the primary is missing (Foundation foods often only have Atwater energy).
USDA_FALLBACK_ID = {2048: "kcal", 2047: "kcal", 1050: "carbs", 1063: "sugars"}
USDA_FALLBACK_NUMBER = {"958": "kcal", "957": "kcal", "205.2": "carbs", "269.3": "sugars"}

TARGET_UNIT = {
    "kcal": "kcal", "protein": "g", "fat": "g", "carbs": "g", "sugars": "g", "fiber": "g", "satFat": "g",
    "transFat": "g", "addedSugars": "g", "sodium": "mg", "cholesterol": "mg", "potassium": "mg",
    "calcium": "mg", "iron": "mg", "vitC": "mg", "vitD": "ug",
}


def convert_unit(v: float, frm: str, to: str) -> tuple[float, bool]:
    """Convert between g/mg/ug (and kJ -> kcal); (0, False) when the units don't match."""
    frm = frm.replace("µ", "u").lower()
    if frm == "" or frm == to:
        return v, True
    scale = {"g": 1.0, "mg": 1e-3, "ug": 1e-6}
    if frm in scale and to in scale:
        return v * scale[frm] / scale[to], True
    if frm == "kj" and to == "kcal":
        return v / 4.184, True
    return 0.0, False


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _int(v: Any) -> int:
    n = _num(v)
    return int(n) if n is not None else 0


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


def usda_nutrients(lst: Any) -> dict[str, float]:
    """Pick our nutrient keys out of any of the three foodNutrients shapes FDC returns."""
    out: dict[str, float] = {}
    fallback: dict[str, float] = {}
    kj = 0.0
    for n in lst if isinstance(lst, list) else []:
        if not isinstance(n, dict):
            continue
        nid, number, unit = _int(n.get("nutrientId")), _s(n.get("nutrientNumber")), _s(n.get("unitName"))
        val = _num(n.get("value"))
        nested = n.get("nutrient")
        if isinstance(nested, dict):
            nid, number, unit = _int(nested.get("id")), _s(nested.get("number")), _s(nested.get("unitName"))
        if number == "":
            number = _s(n.get("number"))
        if val is None:
            val = _num(n.get("amount"))
        if val is None:
            continue
        if nid == 1062 or number == "268":
            kj = val
            continue
        key, primary = USDA_BY_ID.get(nid, ""), True
        if not key:
            key = USDA_BY_NUMBER.get(number, "")
        if not key:
            key, primary = USDA_FALLBACK_ID.get(nid, ""), False
            if not key:
                key = USDA_FALLBACK_NUMBER.get(number, "")
        if not key:
            continue
        v, ok = convert_unit(val, unit, TARGET_UNIT[key])
        if not ok:
            continue
        if primary:
            out[key] = v
        elif key not in fallback or nid == 2048 or number == "958":
            fallback[key] = v
    for k, v in fallback.items():
        out.setdefault(k, v)
    if "kcal" not in out and kj > 0:
        out["kcal"] = kj / 4.184
    return out


_LABEL_MAP = {
    "calories": "kcal", "protein": "protein", "fat": "fat", "carbohydrates": "carbs", "sugars": "sugars",
    "fiber": "fiber", "sodium": "sodium", "saturatedFat": "satFat", "transFat": "transFat",
    "cholesterol": "cholesterol", "potassium": "potassium", "calcium": "calcium", "iron": "iron",
    "addedSugar": "addedSugars",
}


def _eq_fold(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def usda_to_food(u: dict) -> Food:
    """Normalize a USDA food object (search, abridged or full details)."""
    fid = str(_int(u.get("fdcId")))
    data_type = _s(u.get("dataType"))
    f = Food(source="usda", sourceId=fid, name=_s(u.get("description")), dataType=data_type,
             url=f"https://fdc.nal.usda.gov/food-details/{fid}/nutrients")
    if data_type == "Branded":
        f.name = title_case(_s(u.get("description")))
        f.brand = title_case(first_non_empty(_s(u.get("brandName")), _s(u.get("brandOwner"))))
        f.barcode = normalize_barcode(_s(u.get("gtinUpc"))).lstrip("0")
        if f.barcode and len(f.barcode) < 12:
            f.barcode = "0" * (12 - len(f.barcode)) + f.barcode
        f.ingredients = _s(u.get("ingredients"))
        f.category = _s(u.get("brandedFoodCategory"))
    c = u.get("foodCategory")
    if isinstance(c, str):
        if not f.category:
            f.category = c
    elif isinstance(c, dict):
        d = c.get("description")
        if isinstance(d, str) and not f.category:
            f.category = d
    f.per100 = usda_nutrients(u.get("foodNutrients"))

    unit = _s(u.get("servingSizeUnit")).lower()
    if unit in ("ml", "mlt"):
        f.liquid = True
    ss = _num(u.get("servingSize")) or 0.0
    if ss > 0 and unit in ("g", "grm", "ml", "mlt"):
        f.servingGrams = ss
        label = first_non_empty(_s(u.get("householdServingFullText")), "1 serving")
        f.portions.append({"label": label, "grams": ss})
        f.servingLabel = f"{label} ({fmt_num(ss)}{' ml' if f.liquid else ' g'})"
    ln = u.get("labelNutrients")
    if isinstance(ln, dict) and ln:
        ps: dict[str, float] = {}
        for k, v in ln.items():
            key = _LABEL_MAP.get(k)
            if key:
                ps[key] = (_num(v.get("value")) or 0.0) if isinstance(v, dict) else 0.0
        if ps and f.servingGrams > 0:
            f.perServing = ps

    seen: set[str] = set()
    for m in u.get("foodMeasures") or []:
        if not isinstance(m, dict):
            continue
        label = _s(m.get("disseminationText")).strip()
        gw = _num(m.get("gramWeight")) or 0.0
        if gw <= 0 or not label:
            continue
        if _eq_fold(label, "Quantity not specified"):
            label = "Typical portion"
        if label not in seen:
            seen.add(label)
            f.portions.append({"label": label, "grams": gw})
    for p in u.get("foodPortions") or []:
        if not isinstance(p, dict):
            continue
        gw = _num(p.get("gramWeight")) or 0.0
        if gw <= 0:
            continue
        desc = _s(p.get("portionDescription"))
        label = desc.strip()
        if not label or _eq_fold(label, "Quantity not specified"):
            amount = _num(p.get("amount")) or 0.0
            if amount == 0:
                amount = 1.0
            unit_name = ""
            mu = p.get("measureUnit")
            if isinstance(mu, dict) and not _eq_fold(_s(mu.get("name")), "undetermined"):
                unit_name = _s(mu.get("name"))
            mod = _s(p.get("modifier"))
            if re.fullmatch(r"[+-]?[0-9]+", mod):  # FNDDS uses numeric codes here
                mod = ""
            label = (fmt_num(amount) + " " + (unit_name + " " + mod).strip()).strip()
            if label == fmt_num(amount):
                label = "Typical portion" if _eq_fold(desc, "Quantity not specified") else fmt_num(amount) + " serving"
        if label not in seen:
            seen.add(label)
            f.portions.append({"label": label, "grams": gw})
    f.fill()
    return f


def first_portion_label(f: Food) -> str:
    if f.servingLabel:
        return f.servingLabel
    if f.portions:
        return f"{f.portions[0]['label']} ({fmt_num(f.portions[0]['grams'])} g)"
    return ""


def title_case(s: str) -> str:
    """"CHOBANI, GREEK YOGURT" -> "Chobani, Greek Yogurt"; mixed-case text is left alone."""
    s = s.strip()
    if not s or s.upper() != s:
        return s
    out = list(s.lower())
    start = True
    for i, r in enumerate(out):
        if start and r.isalpha():
            up = r.upper()
            out[i] = up if len(up) == 1 else r
        start = not r.isalpha() and r != "'"
    return "".join(out)
