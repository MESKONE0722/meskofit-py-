from datetime import date

import pytest

from meskofit import calc


def test_bmi_and_category():
    assert calc.bmi(70, 175) == pytest.approx(22.86, abs=0.01)
    assert calc.category(22.9)[0] == "Healthy weight"
    assert calc.category(18.4)[0] == "Underweight"
    assert calc.category(30)[0] == "Obesity class I"
    assert calc.category(52)[0] == "Obesity class III"
    lo, hi = calc.healthy_range_kg(182.9)
    assert 61 < lo < 63 and 82 < hi < 84


def test_steady_deficit():
    pts = calc.project_deficit(100, 770, date(2026, 1, 1), 100, 50)  # 0.1 kg/day
    assert pts[-1].kg == pytest.approx(90, abs=0.01)
    assert calc.project_deficit(100, 770, date(2026, 1, 1), 1000, 80)[-1].kg == 80  # floor
    assert calc.reach_date(pts, 95) == date(2026, 2, 20)


def test_intake_levels_off():
    pts = calc.project_intake(210, 2200, "male", 183, 44, 1.2, date(2026, 1, 1), 3 * 365, 60)
    assert pts[-1].kg < 210
    drop_first = pts[0].kg - pts[52].kg
    drop_last = pts[-53].kg - pts[-1].kg
    assert drop_last < drop_first  # loss slows as you get lighter


def test_figure_widens_with_bmi():
    thin, wide = calc.figure_svg(18), calc.figure_svg(42)
    assert thin.startswith("<svg") and wide.startswith("<svg") and thin != wide
    assert "Healthy weight" in calc.figure_svg(22) and "Obesity class II" in calc.figure_svg(38)
    assert "▼" in calc.scale_html(31)


def test_muscle_map():
    svg = calc.muscle_svg(["chest"], ["triceps", "shoulders"])
    assert svg.startswith("<svg") and "#e11d48" in svg and "#f6a3b5" in svg
    assert calc.muscle_label("middle back") == "Upper back"
    assert "#e11d48" not in calc.muscle_svg([])
