"""Settings stored in the kv table, merged over defaults. Secrets never leave the server."""
from __future__ import annotations

import copy
from typing import Any

DEFAULTS: dict[str, Any] = {
    "waterGoalMl": 3000,
    "mealFatCap": 15,
    "fastingHours": 16,
    "restSec": 0,
    "sound": True,
    "meals": ["Breakfast", "Lunch", "Dinner", "Snacks"],
    "targets": {},
    "watchlist": [
        {"label": "Pork", "on": True, "terms": ["pork", "bacon", "ham", "lard", "prosciutto", "pancetta",
                                                "pepperoni", "salami", "chorizo", "gelatin", "gelatine", "pig", "swine", "porcine"]},
        {"label": "Dairy", "on": True, "terms": ["milk", "cream", "cheese", "butter", "whey", "casein",
                                                 "caseinate", "lactose", "yogurt", "yoghurt", "buttermilk", "ghee", "curd", "milkfat", "milk fat", "dairy"]},
    ],
    "ai": {"provider": "", "baseUrl": "", "model": "", "apiKey": ""},
    "aiMatchUsda": True,
    "usdaKey": "",
}


def settings(db) -> dict[str, Any]:
    """Stored settings merged over defaults (secrets included)."""
    s = copy.deepcopy(DEFAULTS)
    stored = db.kv_get("settings")
    if isinstance(stored, dict):
        for k, v in stored.items():
            if k == "ai" and isinstance(v, dict):
                s["ai"].update(v)
            else:
                s[k] = v
    return s


def public_settings(db) -> dict[str, Any]:
    """Settings with secrets blanked, plus usdaKeySet / ai.apiKeySet flags."""
    s = settings(db)
    key = s.get("usdaKey") or ""
    s["usdaKey"] = ""
    s["usdaKeySet"] = key != ""
    if isinstance(s.get("ai"), dict):
        ai = dict(s["ai"])
        k = ai.get("apiKey") or ""
        ai["apiKey"] = ""
        ai["apiKeySet"] = k != ""
        s["ai"] = ai
    return s
