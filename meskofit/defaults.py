"""Pre-fill your profile so you never retype it (handy when the data folder is wiped, e.g. on Streamlit Cloud)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

LB = 0.45359237


def seed_profile(app: Any, path: Path | str) -> bool:
    """If there is no profile yet and `path` exists, create it from the file. Returns True when it seeded."""
    p = Path(path)
    if app.profile() is not None or not p.is_file():
        return False
    d = json.loads(p.read_text("utf-8"))
    today = date.today().isoformat()
    start_kg = d["startWeightLb"] * LB
    prof = {
        "setupDone": True, "name": d.get("name", ""), "level": d.get("level", "beginner"), "units": d.get("units", "imperial"),
        "age": d.get("age"), "heightCm": round(d["heightIn"] * 2.54, 2), "startWeightKg": round(start_kg, 2),
        "goalWeightKg": round(d["goalWeightLb"] * LB, 2), "startDate": today,
        "levelHistory": [{"date": today, "level": d.get("level", "beginner")}],
        "kcalLow": d.get("kcalLow"), "kcalHigh": d.get("kcalHigh"),
    }
    if d.get("sex"):
        prof["sex"] = d["sex"]
    app.db.kv_set("profile", {k: v for k, v in prof.items() if v is not None})
    app.db.exec(
        "INSERT INTO body_log(date, weight_kg, updated_at) VALUES(?,?,?) ON CONFLICT(date) DO NOTHING",
        today, round(start_kg, 2), __import__("meskofit.db", fromlist=["now"]).now())
    return True
