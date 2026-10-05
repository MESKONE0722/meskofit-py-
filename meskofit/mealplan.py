"""Your weekly meal plan (about 1,700-1,800 kcal a day) with the portion of every ingredient.

The plan lives in data/mealplan.json, shared word for word with the Go server. Calories per meal are the ones
printed in your plan; protein, carbs and fat were estimated from the ingredient weights with common nutrition
values, so they can differ a little from your brands.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from importlib import resources
from typing import Any


@lru_cache(maxsize=1)
def _doc() -> dict[str, Any]:
    return json.loads((resources.files("meskofit") / "data" / "mealplan.json").read_text("utf-8"))


def doc() -> dict[str, Any]:
    """The whole plan: seven days, tips and the monthly shopping list."""
    return copy.deepcopy(_doc())


KCAL_LOW: int = _doc()["kcalLow"]
KCAL_HIGH: int = _doc()["kcalHigh"]
TIPS: list[str] = _doc()["tips"]
SHOPPING: list[dict[str, str]] = _doc()["shopping"]
DAYS = [d["day"] for d in _doc()["days"]]


def day_plan(weekday: int) -> dict[str, Any]:
    """The four meals for a weekday (0 = Monday) and the day's totals."""
    d = copy.deepcopy(_doc()["days"][weekday % 7])
    return {**d, "kcalLow": KCAL_LOW, "kcalHigh": KCAL_HIGH}


def week_plan() -> list[dict[str, Any]]:
    return [day_plan(i) for i in range(7)]
