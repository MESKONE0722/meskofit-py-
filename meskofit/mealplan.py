"""Your weekly meal plan (about 1,700-1,800 kcal a day), with the portion of every ingredient.

Calories per meal are the ones printed in the plan. Protein, carbs and fat are estimates worked out from the
ingredient weights with common nutrition values, so they can differ a little from your actual brands.
"""
from __future__ import annotations

from typing import Any

KCAL_LOW, KCAL_HIGH = 1700, 1800

# per 100 g: kcal, protein, carbs, fat  (egg: per 50 g large egg, listed per 100 g)
FOODS: dict[str, tuple[float, float, float, float]] = {
    "egg": (143, 12.6, 0.7, 9.5),
    "egg white (liquid)": (52, 10.9, 0.7, 0.2),
    "spinach": (23, 2.9, 3.6, 0.4),
    "avocado": (160, 2.0, 8.5, 14.7),
    "olive oil": (884, 0, 0, 100),
    "chicken breast (cooked)": (165, 31, 0, 3.6),
    "romaine / spring mix": (17, 1.2, 3.3, 0.3),
    "cucumber": (15, 0.7, 3.6, 0.1),
    "bell pepper": (26, 1.0, 6.0, 0.3),
    "blue cheese": (353, 21, 2.3, 29),
    "pomegranate dressing": (180, 0, 16, 12),
    "plain nonfat Greek yogurt": (59, 10.2, 3.6, 0.4),
    "plain pecans": (691, 9.2, 13.9, 72),
    "salmon (cooked)": (206, 22, 0, 12),
    "zucchini": (17, 1.2, 3.1, 0.3),
    "green beans": (31, 1.8, 7.0, 0.2),
    "asparagus": (20, 2.2, 3.9, 0.1),
    "cabbage": (25, 1.3, 5.8, 0.1),
    "lean sirloin (cooked)": (205, 29, 0, 9.5),
    "mushrooms": (22, 3.1, 3.3, 0.3),
    "93% lean ground beef (cooked)": (190, 26, 0, 9),
    "rice (cooked)": (130, 2.7, 28, 0.3),
    "pasta (cooked)": (158, 5.8, 31, 0.9),
    "tomato sauce (no added sugar)": (29, 1.3, 6, 0.2),
}

EGG_G = 50  # one large egg


def meal(name: str, what: str, kcal: int, parts: list[tuple[str, float]], how: str = "") -> dict[str, Any]:
    items, tot = [], [0.0, 0.0, 0.0]
    for food, g in parts:
        k, p, c, f = FOODS[food]
        tot[0] += p * g / 100
        tot[1] += c * g / 100
        tot[2] += f * g / 100
        label = f"{round(g / EGG_G)} large eggs ({g:g} g)" if food == "egg" and g > EGG_G else (
            "1 large egg (50 g)" if food == "egg" else f"{g:g} g")
        items.append({"food": food, "grams": g, "portion": label})
    return {"name": name, "what": what, "kcal": kcal, "protein": round(tot[0]), "carbs": round(tot[1]), "fat": round(tot[2]),
            "items": items, "how": how}


BREAKFAST = meal("Breakfast", "Spinach eggs with avocado", 415, [
    ("egg", 100), ("egg white (liquid)", 100), ("spinach", 50), ("avocado", 100), ("olive oil", 5)],
    "Beat eggs and whites with pepper. Wilt the spinach in the oil, add the eggs and scramble until set. Avocado on the side.")

LUNCH = meal("Lunch", "Chicken, egg and avocado salad", 595, [
    ("chicken breast (cooked)", 170), ("egg", 50), ("romaine / spring mix", 100), ("cucumber", 100), ("bell pepper", 50),
    ("avocado", 50), ("blue cheese", 15), ("pomegranate dressing", 25)],
    "Chop the veg, add sliced egg, avocado, chicken and blue cheese. Weigh the dressing separately and toss just before eating.")

SNACK = meal("Snack", "Greek yogurt with pecans", 200, [("plain nonfat Greek yogurt", 170), ("plain pecans", 15)],
             "Chop the pecans and stir them in. Swap: 2 boiled eggs is about 155-160 kcal.")

DINNERS: list[dict[str, Any]] = [  # Monday first
    meal("Dinner", "Lemon salmon, zucchini, green beans", 500, [
        ("salmon (cooked)", 170), ("zucchini", 125), ("green beans", 125), ("olive oil", 8)],
        "Roast the veg with 5 g oil at 400 F, 15-25 min. Brush the salmon with 3 g oil, lemon, garlic; bake to 145 F."),
    meal("Dinner", "Paprika chicken, zucchini, asparagus, avocado", 510, [
        ("chicken breast (cooked)", 170), ("zucchini", 125), ("asparagus", 125), ("avocado", 50), ("olive oil", 8)],
        "Roast the veg with the oil, garlic and pepper at 400 F. Add paprika chicken (165 F) and the avocado."),
    meal("Dinner", "Chicken-and-rice bowl", 500, [
        ("chicken breast (cooked)", 150), ("rice (cooked)", 100), ("bell pepper", 100), ("zucchini", 100), ("olive oil", 7)],
        "Saute the pepper and zucchini in the oil, add chicken and garlic, serve over exactly 100 g cooked rice."),
    meal("Dinner", "Lemon-garlic chicken, cabbage, zucchini, avocado", 505, [
        ("chicken breast (cooked)", 170), ("cabbage", 125), ("zucchini", 125), ("avocado", 50), ("olive oil", 8)],
        "Saute the cabbage and zucchini in the oil, add garlic, chicken and lemon juice. Avocado on the side."),
    meal("Dinner", "Sirloin, mushrooms, green beans", 525, [
        ("lean sirloin (cooked)", 170), ("mushrooms", 125), ("green beans", 125), ("olive oil", 8)],
        "Sear the steak in 4 g oil to at least 145 F and rest 3 min. Saute the mushrooms in the other 4 g. Steam the beans."),
    meal("Dinner", "Lean beef pasta with zucchini", 550, [
        ("93% lean ground beef (cooked)", 150), ("pasta (cooked)", 100), ("tomato sauce (no added sugar)", 100), ("zucchini", 100)],
        "Brown the beef with no oil to 160 F and drain. Simmer with the zucchini and sauce, then mix in the pasta."),
    meal("Dinner", "Garlic chicken, green beans, asparagus, avocado", 510, [
        ("chicken breast (cooked)", 170), ("green beans", 125), ("asparagus", 125), ("avocado", 50), ("olive oil", 8)],
        "Roast the beans and asparagus with the oil and garlic. Serve with chicken (165 F) and avocado."),
]

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

TIPS = [
    "Weigh meat, fish, rice and pasta after cooking. Weigh vegetables raw or frozen before cooking. Avocado: flesh only.",
    "Weigh oil, dressing, nuts, cheese and sauce as served. Put the empty bowl on the scale and press tare before each ingredient.",
    "If your day runs high, trim an oil or avocado portion (5 g oil is about 45 kcal, 25 g avocado about 40 kcal) rather than the protein or vegetables.",
    "If you skip breakfast, move its food to another meal instead of dropping the whole day to about 1,300 kcal.",
    "Drinks: water, or unsweetened tea or coffee. A mini Coke is 90 kcal and 25 g sugar, so count it.",
    "This is lower-carb, not zero-carb. Review the calorie target and the big carb cut with your doctor, especially if you feel lightheaded.",
]

SHOPPING: list[tuple[str, str]] = [
    ("Chicken breast", "About 24 lb raw (10.9 kg). Portion and freeze."),
    ("Salmon", "About 2.5 lb raw (1.13 kg)"),
    ("Lean sirloin", "About 2 lb raw (0.91 kg)"),
    ("93% lean ground beef", "About 2 lb raw (0.91 kg)"),
    ("Large eggs", "90 eggs"),
    ("Liquid egg whites", "3,000 g"),
    ("Plain nonfat Greek yogurt", "5,100 g"),
    ("Blue cheese", "450 g"),
    ("Plain pecans", "450 g"),
    ("Pomegranate dressing", "750 g (15 packets of 50 g)"),
    ("Olive oil", "One 500 mL bottle"),
    ("Rice and pasta", "A small bag or box of each (400 g cooked of each a month)"),
    ("Tomato sauce, no added sugar", "400 g"),
    ("Seasonings", "Garlic powder, paprika, pepper, Italian seasoning, a little salt, lemons"),
    ("Romaine / spring mix (weekly)", "700 g a week"),
    ("Cucumber (weekly)", "700 g a week"),
    ("Bell pepper", "350 g fresh a week for salads, 100 g frozen a week for dinner"),
    ("Spinach", "350 g a week"),
    ("Zucchini", "700 g a week"),
    ("Green beans (frozen)", "375 g a week"),
    ("Asparagus (frozen)", "250 g a week"),
    ("Cabbage", "125 g a week"),
    ("Mushrooms", "125 g a week"),
    ("Avocado flesh", "1,200 g a week"),
]


def day_plan(weekday: int) -> dict[str, Any]:
    """The four meals for a weekday (0 = Monday) and the day's totals."""
    meals = [BREAKFAST, LUNCH, SNACK, DINNERS[weekday % 7]]
    tot = {k: sum(m[k] for m in meals) for k in ("kcal", "protein", "carbs", "fat")}
    return {"day": DAYS[weekday % 7], "meals": meals, "totals": tot, "kcalLow": KCAL_LOW, "kcalHigh": KCAL_HIGH}


def week_plan() -> list[dict[str, Any]]:
    return [day_plan(i) for i in range(7)]
