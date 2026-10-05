"""The weekly meal plan, for the web app and the Streamlit version."""
from __future__ import annotations

from . import mealplan
from .web import Req, Router, bad_request

router = Router()


@router.get("/api/mealplan")
def get_mealplan(app, req: Req):
    d = req.query.get("weekday")
    if d != "":
        try:
            n = int(d)
        except ValueError:
            raise bad_request("weekday must be 0 (Monday) to 6 (Sunday)")
        if not 0 <= n <= 6:
            raise bad_request("weekday must be 0 (Monday) to 6 (Sunday)")
        return mealplan.day_plan(n)
    return {"days": mealplan.week_plan(), "tips": mealplan.TIPS, "shopping": [{"item": a, "amount": b} for a, b in mealplan.SHOPPING],
            "kcalLow": mealplan.KCAL_LOW, "kcalHigh": mealplan.KCAL_HIGH}
