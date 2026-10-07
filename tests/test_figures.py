import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from form_figures import figures_html, form_for  # noqa: E402

DATA = ROOT / "meskofit" / "data"


def test_every_exercise_gets_a_form_check():
    cat = json.loads((DATA / "catalog.json").read_text("utf-8"))["exercises"]
    lib = json.loads((DATA / "library.json").read_text("utf-8")) if (DATA / "library.json").exists() else []
    idx = json.loads((DATA / "formindex.json").read_text("utf-8"))
    assert set(cat) <= set(idx["anims"])
    missing = []
    for k, e in cat.items():
        f = form_for(e)
        if not f or not (f["wrong"] and f["right"]):
            missing.append(k)
    assert not missing
    for entry in lib:
        f = form_for({"key": "lib:" + entry["id"], "name": entry["name"]})
        if f and f["anim"]:
            assert f["anim"] in idx["cues"], entry["id"]
    animated = [k for k, e in cat.items() if (form_for(e) or {}).get("anim")]
    assert len(animated) >= 90  # almost every curated exercise moves; only stretches like neck tilts don't
    assert "<svg" in figures_html("hinge", "kettlebells", "kb-swing")
