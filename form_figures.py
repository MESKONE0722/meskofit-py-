"""Animated wrong-vs-right form figures for the Streamlit mockup.

The figures themselves are drawn by `formfigures.js`, the same engine the web app uses (built from
web/src/lib/figures.ts in the main repo). `meskofit/data/formindex.json` says which animation each exercise
gets, and holds the plain-language cues; both are generated from the web app's rules so they never drift.
"""
import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BUNDLE = (_HERE / "formfigures.js").read_text("utf-8")
_INDEX = json.loads((_HERE / "meskofit" / "data" / "formindex.json").read_text("utf-8"))


def form_for(e: dict) -> dict | None:
    """{'anim', 'wrong', 'right'} for an exercise: its own notes where it has them, else cues for the movement."""
    own = e.get("form") or {}
    anim = own.get("anim")
    cues_own = bool(own.get("wrong") or own.get("right"))
    if not anim:
        anim = None if ("anim" in own and cues_own) else (_INDEX["anims"].get(e.get("key", "")) or None)
    stretch = str(e.get("key", "")).startswith("st-") or "stretch" in str(e.get("name", "")).lower()
    generic = _INDEX["cues"].get(anim) if anim else (_INDEX["cues"].get("stretch") if stretch else None)
    wrong = own.get("wrong") or (generic or {}).get("wrong") or []
    right = own.get("right") or (generic or {}).get("right") or []
    if not anim and not wrong and not right:
        return None
    return {"anim": anim, "wrong": wrong, "right": right}


def figures_html(anim: str, equipment: str = "", key: str = "") -> str:
    return f"""<style>
body{{margin:0;font-family:system-ui,sans-serif;background:transparent}}
.row{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.box{{position:relative;border-radius:12px;background:#eceeef;overflow:hidden}}
.box svg{{display:block;width:100%}}
.tag{{position:absolute;top:6px;left:6px;color:#fff;font-size:12px;font-weight:700;border-radius:99px;padding:2px 9px}}
</style>
<div class="row">
<div class="box" style="box-shadow:inset 0 0 0 1.5px #dc2626"><span class="tag" style="background:#dc2626">✕ Wrong</span><svg id="a" viewBox="0 -8 200 168"></svg></div>
<div class="box" style="box-shadow:inset 0 0 0 1.5px #16a34a"><span class="tag" style="background:#16a34a">✓ Right</span><svg id="b" viewBox="0 -8 200 168"></svg></div>
</div>
<script>{_BUNDLE}</script>
<script>
const ANIM={json.dumps(anim)}, held=FormFigures.heldFor({json.dumps(equipment)},{json.dumps(key)});
const A=document.getElementById('a'), B=document.getElementById('b');
function draw(u){{A.innerHTML=FormFigures.figureAt(ANIM,'wrong',u,held);B.innerHTML=FormFigures.figureAt(ANIM,'right',u,held);}}
if(matchMedia('(prefers-reduced-motion: reduce)').matches)draw(1);
else{{const t0=performance.now();(function tick(t){{const s=((t-t0)/3200)%1,c=s<.1?0:s<.45?(s-.1)/.35:s<.6?1:s<.95?1-(s-.6)/.35:0;draw(.5-.5*Math.cos(Math.PI*c));requestAnimationFrame(tick);}})(t0);}}
</script>"""
