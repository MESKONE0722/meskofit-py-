"""Normalized food records shared by every food source (Open Food Facts, USDA, custom)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, fields
from typing import Any

# Nutrients maps a nutrient key to an amount. A missing key means "unknown", which
# is different from zero. Units: kcal; protein carbs fat satFat transFat fiber sugars
# addedSugars in g; cholesterol sodium potassium calcium iron vitC in mg; vitD in µg.
Nutrients = dict[str, float]

NUTRIENT_KEYS = [
    "kcal", "protein", "carbs", "fat", "satFat", "transFat", "cholesterol", "sodium",
    "fiber", "sugars", "addedSugars", "potassium", "calcium", "iron", "vitC", "vitD",
]


def _empty(v: Any) -> bool:
    """Go's `omitempty`: zero values, empty strings/lists/maps are left out of the JSON."""
    return v is None or v is False or v == 0 or v == "" or v == [] or v == {}


@dataclass
class Food:
    """The normalized food record. JSON keys match the web app (camelCase)."""

    source: str = ""  # off | usda | custom
    sourceId: str = ""
    name: str = ""
    id: int = 0  # local database id once cached
    barcode: str = ""
    brand: str = ""
    imageUrl: str = ""
    url: str = ""
    per100: Nutrients = field(default_factory=dict)  # per 100 g (or 100 ml for liquids)
    perServing: Nutrients = field(default_factory=dict)  # as printed on the label
    servingGrams: float = 0.0
    servingLabel: str = ""
    portions: list[dict] = field(default_factory=list)  # [{"label": "1 cup", "grams": 158}]
    liquid: bool = False
    ingredients: str = ""
    allergens: list[str] = field(default_factory=list)
    traces: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    additives: list[str] = field(default_factory=list)
    analysis: list[str] = field(default_factory=list)
    nutriscore: str = ""
    nova: int = 0
    nutrientLevels: dict[str, str] = field(default_factory=dict)
    quantity: str = ""
    category: str = ""
    dataType: str = ""
    favorite: bool = False
    custom: bool = False
    fetchedAt: str = ""

    # JSON key order matches the Go struct so responses are identical.
    _ORDER = [
        "id", "source", "sourceId", "barcode", "name", "brand", "imageUrl", "url", "per100", "perServing",
        "servingGrams", "servingLabel", "portions", "liquid", "ingredients", "allergens", "traces", "labels",
        "additives", "analysis", "nutriscore", "nova", "nutrientLevels", "quantity", "category", "dataType",
        "favorite", "custom", "fetchedAt",
    ]
    _ALWAYS = ("source", "sourceId", "name")

    def to_json(self) -> dict:
        out: dict[str, Any] = {}
        for k in self._ORDER:
            v = getattr(self, k)
            if k in self._ALWAYS or not _empty(v):
                out[k] = v
        return out

    @classmethod
    def from_json(cls, d: dict) -> "Food":
        names = {f.name for f in fields(cls)}
        f = cls(**{k: v for k, v in d.items() if k in names and v is not None})
        return f

    def has_energy(self) -> bool:
        return "kcal" in self.per100 or "kcal" in self.perServing

    def fill(self) -> None:
        """Derive whichever of per100/perServing is missing when the serving weight is
        known, and round everything to sane precision."""
        if self.servingGrams > 0:
            if not self.per100 and self.perServing:
                self.per100 = scale(self.perServing, 100 / self.servingGrams)
            elif not self.perServing and self.per100:
                self.perServing = scale(self.per100, self.servingGrams / 100)
        self.per100 = round_n(self.per100)
        self.perServing = round_n(self.perServing)
        if self.servingGrams > 0 and not self.servingLabel:
            unit = "ml" if self.liquid else "g"
            self.servingLabel = f"1 serving ({fmt_num(self.servingGrams)} {unit})"


@dataclass
class Hit:
    """A light search result; the full Food is fetched when tapped."""

    source: str
    sourceId: str
    name: str
    brand: str = ""
    imageUrl: str = ""
    per100: Nutrients = field(default_factory=dict)
    servingLabel: str = ""
    dataType: str = ""
    localId: int = 0

    def to_json(self) -> dict:
        out: dict[str, Any] = {}
        for k in ("source", "sourceId", "name", "brand", "imageUrl", "per100", "servingLabel", "dataType", "localId"):
            v = getattr(self, k)
            if k in ("source", "sourceId", "name") or not _empty(v):
                out[k] = v
        return out


def scale(n: Nutrients | None, k: float) -> Nutrients:
    """Multiply every nutrient by k."""
    if not n:
        return {}
    return {key: v * k for key, v in n.items()}


def _go_round(x: float) -> float:
    """math.Round: half away from zero (Python's round() is banker's rounding)."""
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def round_n(n: Nutrients | None) -> Nutrients:
    """Trim nutrient values to a precision that makes sense for display (in place)."""
    if n is None:
        return {}
    for k in list(n):
        v = n[k]
        if math.isnan(v) or math.isinf(v) or v < 0:
            del n[k]
        elif k == "kcal" or v >= 100:
            n[k] = _go_round(v * 10) / 10
        else:
            n[k] = _go_round(v * 1000) / 1000
    return n


def fmt_num(v: float) -> str:
    return str(int(v)) if v == math.trunc(v) else f"{v:.1f}"


def clean_tags(tags: list[str]) -> list[str]:
    """["en:sesame-seeds", "fr:lait"] -> ["sesame seeds", "lait"]."""
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        i = t.find(":")
        if 0 <= i <= 3:
            t = t[i + 1:]
        t = t.replace("-", " ").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def normalize_barcode(code: str) -> str:
    """Strip everything but digits."""
    return re.sub(r"\D", "", code)


def barcode_variants(code: str) -> list[str]:
    """The forms a code may be stored under: as scanned, without leading zeros, padded
    to EAN-13 / GTIN-14, and UPC-E expanded to UPC-A."""
    code = normalize_barcode(code)
    if not code:
        return []
    out: list[str] = []

    def add(s: str) -> None:
        if s and s not in out:
            out.append(s)

    add(code)
    if len(code) == 8:
        a = upce_to_upca(code)
        if a:
            add(a)
            add("0" + a)
    trimmed = code.lstrip("0")
    add(trimmed)
    for n in (12, 13, 14):
        if len(trimmed) <= n:
            add("0" * (n - len(trimmed)) + trimmed)
    return out


def same_barcode(a: str, b: str) -> bool:
    """Compare two codes ignoring leading zeros."""
    a = normalize_barcode(a).lstrip("0")
    b = normalize_barcode(b).lstrip("0")
    return a != "" and a == b


def upce_to_upca(e: str) -> str:
    """Expand an 8-digit UPC-E code (number system 0 or 1) to UPC-A."""
    if len(e) != 8 or e[0] not in "01":
        return ""
    ns, d, check = e[0], e[1:7], e[7]
    last = d[5]
    if last in "012":
        mfr, prod = d[0:2] + last + "00", "00" + d[2:5]
    elif last == "3":
        mfr, prod = d[0:3] + "00", "000" + d[3:5]
    elif last == "4":
        mfr, prod = d[0:4] + "0", "0000" + d[4:5]
    else:
        mfr, prod = d[0:5], "0000" + d[5:6]
    return ns + mfr + prod + check
