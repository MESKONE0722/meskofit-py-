"""Open Food Facts client (port of internal/food/off.go) plus the HTTP/JSON helpers shared with usda.py."""
from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

from .types import Food, Hit, barcode_variants, clean_tags, fmt_num, round_n

# Identifies the app to Open Food Facts, as their API terms ask.
USER_AGENT = "MeskoFit/1.0 (self-hosted personal fitness app)"


class FoodError(Exception):
    """Base class for food-source failures."""


class NotFound(FoodError):
    """The database has no record for the query (Go: ErrNotFound)."""

    def __init__(self, msg: str = "not found"):
        super().__init__(msg)


class RateLimited(FoodError):
    """USDA rejected the request because the API key is out of requests."""

    def __init__(self, msg: str = "USDA rate limit reached — add your free API key in Settings"):
        super().__init__(msg)


class BadKey(FoodError):
    """The USDA API key was rejected."""

    def __init__(self, msg: str = "USDA rejected the API key — check it in Settings"):
        super().__init__(msg)


class StatusError(FoodError):
    """A non-2xx HTTP response (Go: httpError)."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def do_json(client: httpx.Client | None, method: str, url: str, content: bytes | None = None,
            headers: dict[str, str] | None = None) -> dict:
    """Send a request and decode a JSON object. Raises StatusError (>=300) or FoodError."""
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    own = client is None
    c = client or httpx.Client(timeout=20)
    try:
        resp = c.request(method, url, content=content, headers=h)
    except httpx.HTTPError as e:
        raise FoodError(str(e) or e.__class__.__name__) from e
    finally:
        if own:
            c.close()
    if resp.status_code >= 300:
        raise StatusError(resp.status_code, resp.content.decode("utf-8", "replace")[:300])
    try:
        out = json.loads(resp.content)
    except ValueError as e:
        raise FoodError(f"unexpected response: {e}") from e
    if not isinstance(out, dict):
        raise FoodError("unexpected response: json: cannot unmarshal into Go value of type object")
    return out


def get_json(client: httpx.Client | None, url: str) -> dict:
    return do_json(client, "GET", url)


OFF_FIELDS = (
    "code,product_name,product_name_en,generic_name,brands,quantity,serving_size,serving_quantity,"
    "serving_quantity_unit,nutrition_data_per,nutriments,image_front_url,image_front_small_url,image_url,"
    "ingredients_text,ingredients_text_en,allergens_tags,traces_tags,labels_tags,nutriscore_grade,nova_group,"
    "additives_tags,categories_tags,nutrient_levels,ingredients_analysis_tags,product_quantity,product_quantity_unit"
)


class OFF:
    """Open Food Facts client. Product reads need no key; be fair (~100 reads/min, ~10 searches/min)."""

    def __init__(self, http: httpx.Client | None,
                 product_url: str = "https://world.openfoodfacts.org",
                 search_url: str = "https://search.openfoodfacts.org"):
        self.http = http
        self.product_url = product_url
        self.search_url = search_url

    def product(self, barcode: str) -> Food:
        """Look up a barcode, trying the first three variants of the code. Raises NotFound."""
        last: Exception = NotFound()
        for i, code in enumerate(barcode_variants(barcode)):
            if i >= 3:  # as scanned, UPC-E expansion / trimmed, padded: enough
                break
            try:
                return self._product(code)
            except NotFound as e:
                last = e
        raise last

    def _product(self, code: str) -> Food:
        u = f"{self.product_url}/api/v2/product/{quote(code, safe='')}?fields={OFF_FIELDS}"
        try:
            resp = get_json(self.http, u)
        except StatusError as e:
            if e.status == 404:
                raise NotFound() from e
            raise FoodError(f"open food facts: {e}") from e
        except FoodError as e:
            raise FoodError(f"open food facts: {e}") from e
        if resp.get("status") != 1:
            raise NotFound()
        p = resp.get("product")
        p = p if isinstance(p, dict) else {}
        if not _s(p.get("code")):
            p = {**p, "code": _s(resp.get("code"))}
        return off_to_food(p)

    def search(self, q: str, limit: int) -> list[Hit]:
        """Full-text product search (packaged/branded foods)."""
        fields = "code,product_name,product_name_en,brands,nutriments,image_front_small_url,serving_size,serving_quantity,quantity"
        u = f"{self.search_url}/search?q={quote_plus(q)}&page_size={limit}&langs=en&fields={fields}"
        try:
            hits = get_json(self.http, u).get("hits")
        except FoodError as err:
            # Fall back to the legacy search endpoint.
            lu = (f"{self.product_url}/cgi/search.pl?search_terms={quote_plus(q)}&search_simple=1&action=process"
                  f"&json=1&page_size={limit}&fields={fields}")
            try:
                hits = get_json(self.http, lu).get("products")
            except FoodError:
                raise FoodError(f"open food facts search: {err}") from err
        out: list[Hit] = []
        for p in hits if isinstance(hits, list) else []:
            if not isinstance(p, dict):
                continue
            name = first_non_empty(_s(p.get("product_name_en")), _s(p.get("product_name")))
            code = _s(p.get("code"))
            if not name or not code:
                continue
            per100, _ = off_nutrients(p.get("nutriments"))
            out.append(Hit(source="off", sourceId=code, name=name, brand=brand_string(p.get("brands")),
                           imageUrl=_s(p.get("image_front_small_url")), per100=round_n(per100),
                           servingLabel=_s(p.get("serving_size"))))
        return out


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _tags(v: Any) -> list[str]:
    return [t for t in v if isinstance(t, str)] if isinstance(v, list) else []


def off_to_food(p: dict) -> Food:
    """Normalize an OFF product object."""
    code = _s(p.get("code"))
    f = Food(
        source="off", sourceId=code, barcode=code,
        name=first_non_empty(_s(p.get("product_name_en")), _s(p.get("product_name")), _s(p.get("generic_name")), "Unnamed product"),
        brand=brand_string(p.get("brands")),
        imageUrl=first_non_empty(_s(p.get("image_front_url")), _s(p.get("image_url")), _s(p.get("image_front_small_url"))),
        url="https://world.openfoodfacts.org/product/" + code,
        quantity=_s(p.get("quantity")),
    )
    f.per100, f.perServing = off_nutrients(p.get("nutriments"))

    serving_size = _s(p.get("serving_size"))
    unit = first_non_empty(_s(p.get("serving_quantity_unit")), _s(p.get("product_quantity_unit"))).lower()
    q = (_s(p.get("quantity")) + " " + serving_size).lower()
    f.liquid = (unit in ("ml", "l", "cl") or " ml" in q or "ml)" in q or "fl oz" in q or "fl. oz" in q)
    sq = to_float(p.get("serving_quantity"))
    if 0 < sq < 5000:
        f.servingGrams = sq
    if serving_size:
        f.servingLabel = serving_size
    if f.servingGrams > 0:
        f.portions.append({"label": first_non_empty(serving_size, "1 serving"), "grams": f.servingGrams})
    pq = to_float(p.get("product_quantity"))
    if 0 < pq < 5000 and pq != f.servingGrams:
        f.portions.append({"label": "Whole package (" + first_non_empty(_s(p.get("quantity")), fmt_num(pq) + " g") + ")", "grams": pq})
    f.ingredients = first_non_empty(_s(p.get("ingredients_text_en")), _s(p.get("ingredients_text")))
    f.allergens = clean_tags(_tags(p.get("allergens_tags")))
    f.traces = clean_tags(_tags(p.get("traces_tags")))
    f.labels = clean_tags(_tags(p.get("labels_tags")))
    f.additives = [a.upper() for a in clean_tags(_tags(p.get("additives_tags")))]
    f.analysis = clean_tags(_tags(p.get("ingredients_analysis_tags")))
    g = _s(p.get("nutriscore_grade")).lower()
    if len(g) == 1 and "a" <= g <= "e":
        f.nutriscore = g
    nova = to_float(p.get("nova_group"))
    if math.isfinite(nova) and 1 <= int(nova) <= 4:
        f.nova = int(nova)
    lv = p.get("nutrient_levels")
    if isinstance(lv, dict) and lv:
        f.nutrientLevels = {k: v for k, v in lv.items() if isinstance(v, str)}
    cats = clean_tags(_tags(p.get("categories_tags")))
    if cats:
        f.category = cats[-1]
    f.fill()
    return f


# (our key, OFF key, multiplier to our unit): OFF reports g, we keep mg / µg for the micros.
_SPECS = [
    ("protein", "proteins", 1), ("carbs", "carbohydrates", 1), ("fat", "fat", 1),
    ("satFat", "saturated-fat", 1), ("transFat", "trans-fat", 1), ("cholesterol", "cholesterol", 1000),
    ("sodium", "sodium", 1000), ("fiber", "fiber", 1), ("sugars", "sugars", 1),
    ("addedSugars", "added-sugars", 1), ("potassium", "potassium", 1000), ("calcium", "calcium", 1000),
    ("iron", "iron", 1000), ("vitC", "vitamin-c", 1000), ("vitD", "vitamin-d", 1e6),
]


def off_nutrients(m: Any) -> tuple[dict[str, float], dict[str, float]]:
    """Map OFF's nutriments object to (per100, perServing) nutrient dicts (empty when unknown)."""
    m = m if isinstance(m, dict) else {}
    per100: dict[str, float] = {}
    per_serving: dict[str, float] = {}
    for suffix, dst in (("_100g", per100), ("_serving", per_serving)):
        for key, off, mult in _SPECS:
            v = num(m, off + suffix)
            if v is not None:
                dst[key] = v * mult
        if "sodium" not in dst:
            v = num(m, "salt" + suffix)
            if v is not None:
                dst["sodium"] = v * 400  # salt g -> sodium mg
        v = num(m, "energy-kcal" + suffix)
        if v is not None:
            dst["kcal"] = v
        elif (v := num(m, "energy-kj" + suffix)) is not None:
            dst["kcal"] = v / 4.184
        elif (v := num(m, "energy" + suffix)) is not None:
            dst["kcal"] = v / 4.184
    return per100, per_serving


def num(m: dict, key: str) -> float | None:
    """A number from a JSON value that may be a number or a numeric string ("12,5")."""
    v = m.get(key)
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", ".").strip()
        if "_" in s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def to_float(v: Any) -> float:
    r = num({"v": v}, "v")
    return 0.0 if r is None else r


def brand_string(b: Any) -> str:
    """OFF's brands is a comma-separated string or a list; use the first brand."""
    if isinstance(b, str):
        return b.split(",")[0].strip()
    if isinstance(b, list) and b and isinstance(b[0], str):
        return b[0]
    return ""


def first_non_empty(*s: str) -> str:
    for v in s:
        if v.strip():
            return v.strip()
    return ""
