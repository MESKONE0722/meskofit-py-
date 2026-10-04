"""Body-mass index, weight projections and the body figure used by the interface."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

KCAL_PER_KG = 7700.0  # about 3500 kcal per pound
LB = 0.45359237

# WHO adult BMI bands: (upper bound, label, colour)
BANDS = [
    (16.0, "Severely underweight", "#3b82f6"),
    (18.5, "Underweight", "#38bdf8"),
    (25.0, "Healthy weight", "#22c55e"),
    (30.0, "Overweight", "#eab308"),
    (35.0, "Obesity class I", "#f97316"),
    (40.0, "Obesity class II", "#ef4444"),
    (1e9, "Obesity class III", "#b91c1c"),
]

ACTIVITY = {
    "Mostly sitting": 1.2,
    "Light (walks, 1–3 workouts a week)": 1.375,
    "Moderate (3–5 workouts a week)": 1.55,
    "Very active": 1.725,
}


def bmi(kg: float, cm: float) -> float:
    return kg / (cm / 100) ** 2 if kg > 0 and cm > 0 else 0.0


def category(b: float) -> tuple[str, str]:
    for hi, label, color in BANDS:
        if b < hi:
            return label, color
    return BANDS[-1][1], BANDS[-1][2]


def healthy_range_kg(cm: float) -> tuple[float, float]:
    m2 = (cm / 100) ** 2
    return 18.5 * m2, 24.9 * m2


def weight_at_bmi(b: float, cm: float) -> float:
    return b * (cm / 100) ** 2


def mifflin_bmr(sex: str, kg: float, cm: float, age: float) -> float:
    return 10 * kg + 6.25 * cm - 5 * age + (5 if sex == "male" else -161)


@dataclass
class Point:
    day: date
    kg: float


def project_deficit(start_kg: float, deficit_kcal_per_day: float, start: date, days: int, floor_kg: float) -> list[Point]:
    """Steady loss: the same daily deficit all the way. Stops at ``floor_kg``."""
    out, kg = [], start_kg
    per_day = deficit_kcal_per_day / KCAL_PER_KG
    for d in range(0, days + 1, 7 if days > 120 else 1):
        out.append(Point(start + timedelta(days=d), max(floor_kg, start_kg - per_day * d)))
    return out


def project_intake(start_kg: float, intake_kcal: float, sex: str, cm: float, age: float, activity: float,
                   start: date, days: int, floor_kg: float) -> list[Point]:
    """Eat the same calories every day. Burn drops as you get lighter, so loss slows and levels off."""
    out, kg = [Point(start, start_kg)], start_kg
    for d in range(1, days + 1):
        tdee = mifflin_bmr(sex, kg, cm, age + d / 365.25) * activity
        kg = max(floor_kg, kg + (intake_kcal - tdee) / KCAL_PER_KG)
        if d % (7 if days > 120 else 1) == 0 or d == days:
            out.append(Point(start + timedelta(days=d), kg))
    return out


def reach_date(points: list[Point], target_kg: float) -> date | None:
    for p in points:
        if p.kg <= target_kg + 1e-9:
            return p.day
    return None


# ───────────────────────── body figure ─────────────────────────

def _smooth(pts: list[tuple[float, float]]) -> str:
    """Closed Catmull-Rom spline through points, as an SVG path."""
    n = len(pts)
    d = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
    for i in range(n):
        p0, p1, p2, p3 = pts[(i - 1) % n], pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += f" C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}"
    return d + "Z"


def figure_svg(b: float) -> str:
    """A plain front-view silhouette whose build follows the BMI, coloured by category."""
    label, color = category(b)
    s = max(0.0, min(1.0, (b - 15.0) / 30.0))  # 0 at BMI 15, 1 at BMI 45
    cx = 110
    half = {  # half-widths by height, thinnest to widest
        "neck": 9 + 3 * s, "shoulder": 34 + 22 * s, "chest": 30 + 34 * s, "waist": 24 + 46 * s,
        "hip": 31 + 38 * s, "thigh": 29 + 25 * s, "knee": 19 + 12 * s, "ankle": 14 + 6 * s,
    }
    ys = {"neck": 62, "shoulder": 80, "chest": 118, "waist": 168, "hip": 208, "thigh": 252, "knee": 296, "ankle": 344}
    gap = 3
    right = [(cx + half[k], ys[k]) for k in ("neck", "shoulder", "chest", "waist", "hip", "thigh", "knee", "ankle")]
    right += [(cx + half["ankle"] + 5, 356), (cx + gap + 5, 357)]  # foot
    inner = [(cx + gap + 2 + 8 * s * 0.3, 296), (cx + gap, 232)]  # inner knee, crotch
    right_side = right + inner
    left_side = [(2 * cx - x, y) for x, y in reversed(right_side)]
    body = _smooth(right_side + left_side)
    arm_w = 11 + 9 * s
    ax = half["shoulder"] - 2
    arms = "".join(
        f'<line x1="{cx + sx * ax:.1f}" y1="92" x2="{cx + sx * (ax + 8 + 12 * s):.1f}" y2="206" stroke="{color}" stroke-opacity=".78" '
        f'stroke-width="{arm_w:.1f}" stroke-linecap="round"/>' for sx in (-1, 1))
    return (
        f'<svg viewBox="0 0 220 370" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Body figure for BMI {b:.1f}: {label}" '
        f'style="max-width:210px;width:100%;height:auto">'
        f'{arms}<path d="{body}" fill="{color}"/><circle cx="{cx}" cy="36" r="22" fill="{color}"/></svg>'
    )


def scale_html(b: float) -> str:
    """A horizontal BMI scale with a marker."""
    lo, hi = 15.0, 45.0
    pos = max(0.0, min(1.0, (b - lo) / (hi - lo))) * 100
    segs = ""
    prev = lo
    for top, label, color in BANDS:
        t = min(top, hi)
        if t > prev:
            segs += f'<div style="flex:{t - prev};background:{color}" title="{label}"></div>'
            prev = t
    return (
        '<div style="position:relative;margin:26px 0 6px">'
        f'<div style="position:absolute;left:{pos:.1f}%;top:-22px;transform:translateX(-50%);font-size:12px;font-weight:700">▼</div>'
        f'<div style="display:flex;height:12px;border-radius:6px;overflow:hidden">{segs}</div>'
        '<div style="display:flex;justify-content:space-between;font-size:11px;opacity:.65;margin-top:3px">'
        '<span>15</span><span>18.5</span><span>25</span><span>30</span><span>35</span><span>40+</span></div></div>'
    )


# ───────────────────────── muscle map ─────────────────────────

_MUSCLES: dict | None = None


def _muscle_data() -> dict:
    global _MUSCLES
    if _MUSCLES is None:
        import json
        from importlib import resources

        _MUSCLES = json.loads((resources.files("meskofit") / "data" / "muscles.json").read_text("utf-8"))
    return _MUSCLES


def muscle_label(m: str) -> str:
    return _muscle_data()["labels"].get(m, m.title())


def _shape_svg(s: dict, fill: str) -> str:
    el = (f'<ellipse cx="{s["ellipse"][0]}" cy="{s["ellipse"][1]}" rx="{s["ellipse"][2]}" ry="{s["ellipse"][3]}" fill="{fill}"/>'
          if s.get("ellipse") else f'<path d="{s["d"]}" fill="{fill}"/>')
    return el + (f'<g transform="translate(120,0) scale(-1,1)">{el}</g>' if s["side"] == "L" else "")


def muscle_svg(primary: list[str], secondary: list[str] | None = None, height: int = 230) -> str:
    """Front and back body figures with the main muscles in red and helper muscles in a lighter tint."""
    data = _muscle_data()
    p = set(primary)
    sec = set(secondary or []) - p
    base, neutral, main, assist = "#d9dfdc", "#c4cbc8", "#e11d48", "#f6a3b5"

    def fig(shapes: list[dict], x: int) -> str:
        parts = []
        for s in shapes:
            m = s["m"]
            fill = neutral if not m else main if m in p else assist if m in sec else base
            parts.append(_shape_svg(s, fill))
        return f'<g transform="translate({x},0)">{"".join(parts)}</g>'

    label = ", ".join(muscle_label(m) for m in primary)
    return (f'<svg viewBox="0 0 260 282" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Muscles worked: {label}" '
            f'style="height:{height}px;width:auto;max-width:100%;display:block;margin:0 auto">'
            f'{fig(data["front"], 4)}{fig(data["back"], 136)}'
            '<text x="64" y="278" font-size="9" text-anchor="middle" fill="#6b7a75" font-family="sans-serif">FRONT</text>'
            '<text x="196" y="278" font-size="9" text-anchor="middle" fill="#6b7a75" font-family="sans-serif">BACK</text></svg>')


def muscle_icon_svg(primary: list[str], secondary: list[str] | None = None, height: int = 58) -> str:
    """One small body figure (front, or back if the main muscles are on the back) for cards and filters."""
    data = _muscle_data()
    p, sec = set(primary), set(secondary or []) - set(primary)
    front_hit = any(s["m"] in p for s in data["front"])
    shapes = data["front"] if front_hit or not any(s["m"] in p for s in data["back"]) else data["back"]
    base, neutral, main, assist = "#d9dfdc", "#c4cbc8", "#e11d48", "#f6a3b5"
    parts = "".join(_shape_svg(s, neutral if not s["m"] else main if s["m"] in p else assist if s["m"] in sec else base) for s in shapes)
    return f'<svg viewBox="0 0 128 266" xmlns="http://www.w3.org/2000/svg" style="height:{height}px;width:auto"><g transform="translate(4,0)">{parts}</g></svg>'
