"""Pre-fill your profile so you never retype it.

Put a profile_defaults.json in the data folder (the Streamlit version reads the one next to streamlit_app.py).
When there is no profile yet, it is created from that file, the same way the Go server does it.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .db import now

KG_PER_LB = 0.45359237


def _num(d: dict, k: str) -> float:
    v = d.get(k)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def profile_from(d: dict[str, Any], today: str) -> dict[str, Any] | None:
    """The profile a defaults file describes, or None when age, height or starting weight is missing."""
    h = _num(d, "heightCm") or _num(d, "heightIn") * 2.54
    start = _num(d, "startWeightKg") or _num(d, "startWeightLb") * KG_PER_LB
    goal = _num(d, "goalWeightKg") or _num(d, "goalWeightLb") * KG_PER_LB
    age = _num(d, "age")
    if age <= 0 or h <= 0 or start <= 0:
        return None
    level = d.get("level") or "beginner"
    rate = d.get("goalRate")
    p: dict[str, Any] = {
        "setupDone": True, "units": d.get("units") or "imperial", "age": age, "ageAsOf": today, "heightCm": round(h, 2),
        "activity": d.get("activity") or "light", "goalRate": float(rate) if isinstance(rate, (int, float)) else -1.0,
        "level": level, "limits": dict(d.get("limits") or {}), "startWeightKg": round(start, 2), "startDate": today,
        "levelHistory": [{"level": level, "date": today}],
    }
    if d.get("name"):
        p["name"] = d["name"]
    if d.get("sex") in ("male", "female"):
        p["sex"] = d["sex"]
    if goal > 0:
        p["goalWeightKg"] = round(goal, 2)
    if _num(d, "kcalLow") > 0 and _num(d, "kcalHigh") > 0:
        p["kcalLow"], p["kcalHigh"] = d["kcalLow"], d["kcalHigh"]
    return p


def seed_profile(app: Any, path: Path | str) -> bool:
    """If there is no profile yet and `path` exists, create it from the file. Returns True when it seeded."""
    f = Path(path)
    if app.profile() is not None or not f.is_file():
        return False
    d = json.loads(f.read_text("utf-8"))
    today = date.today().isoformat()
    p = profile_from(d, today)
    if p is None:
        raise ValueError(f"{f}: age, height and starting weight are required")
    app.db.kv_set("profile", p)
    app.db.exec("INSERT INTO body_log(date, weight_kg, updated_at) VALUES(?,?,?) ON CONFLICT(date) DO NOTHING",
                today, p["startWeightKg"], now())
    targets = d.get("targets")
    if isinstance(targets, dict) and targets:
        stored = app.db.kv_get("settings") or {}
        if not stored.get("targets"):
            stored["targets"] = targets
            app.db.kv_set("settings", stored)
    return True
