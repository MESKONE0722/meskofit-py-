import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from form_figures import ANIMS, figures_html  # noqa: E402


def test_every_catalog_anim_has_figures():
    cat = json.loads((Path(__file__).resolve().parent.parent / "meskofit" / "data" / "catalog.json").read_text("utf-8"))["exercises"]
    used = {e["form"]["anim"] for e in cat.values() if e.get("form", {}).get("anim")}
    assert used and used <= set(ANIMS)
    for a in used:
        assert "<svg" in figures_html(a, True)
    assert figures_html(None) is None and figures_html("nope") is None
