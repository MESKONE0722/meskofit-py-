"""MeskoFit on Streamlit: the same data and logic as the web app, with a Streamlit interface.

Run:  streamlit run streamlit_app.py
Data lives in ./meskofit-data (or $MESKOFIT_DATA). Set a password with $MESKOFIT_PASSWORD or the
Streamlit secret `password`. On Streamlit Community Cloud the disk is wiped on restart: use the
downloads on the More tab, or host it somewhere with a persistent disk.
"""
from __future__ import annotations

import hmac
import json
import os
from datetime import date, datetime, timedelta, timezone
from importlib import resources
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from meskofit import calc, mealplan
from meskofit.defaults import seed_profile
from meskofit.local import Local, open_local
from meskofit.routes_body import backup
from meskofit.routes_shots import DOSES
from meskofit.web import HTTPError

LB = calc.LB
TEAL, INK, MUTED = "#0f766e", "#17211e", "#6b7a75"
st.set_page_config(page_title="MeskoFit", page_icon="💪", layout="centered", initial_sidebar_state="collapsed")

st.markdown("""
<style>
.block-container{padding-top:1.4rem;padding-bottom:5rem;max-width:760px}
header[data-testid="stHeader"]{background:transparent}
[data-testid="stSidebar"],[data-testid="collapsedControl"],[data-testid="stSidebarCollapsedControl"]{display:none}
h1{font-weight:800;letter-spacing:-.02em;margin-bottom:.2rem}
h2,h3{letter-spacing:-.01em}
.stTabs [data-baseweb="tab-list"]{gap:2px;border-bottom:1px solid #d6dedb;overflow-x:auto}
.stTabs [data-baseweb="tab"]{padding:.55rem .8rem;font-weight:600;white-space:nowrap}
div[data-testid="stMetric"]{background:#fff;border:1px solid #dfe6e3;border-radius:14px;padding:.7rem .9rem;box-shadow:0 1px 2px rgba(0,0,0,.03)}
div[data-testid="stMetricLabel"] p{font-size:.78rem;color:#6b7a75;font-weight:600}
div[data-testid="stMetricValue"]{font-weight:800}
div[data-testid="stExpander"]{background:#fff;border:1px solid #dfe6e3;border-radius:14px;overflow:hidden}
div[data-testid="stExpander"] summary{font-weight:600}
div[data-testid="stForm"]{background:#fff;border:1px solid #dfe6e3;border-radius:14px;padding:1rem}
div[data-testid="stVerticalBlockBorderWrapper"]:has(> div > div[data-testid="stVerticalBlock"] .mf-card){background:#fff}
.stButton>button,.stDownloadButton>button,div[data-testid="stFormSubmitButton"]>button{border-radius:12px;font-weight:700;min-height:2.7rem}
.stTextInput input,.stNumberInput input,.stDateInput input,div[data-baseweb="select"]>div{border-radius:10px}
.st-key-libgrid [data-testid="stHorizontalBlock"],[class*="st-key-row"] [data-testid="stHorizontalBlock"]{flex-wrap:nowrap!important;gap:.6rem}
.st-key-libgrid [data-testid="stColumn"],[class*="st-key-row"] [data-testid="stColumn"]{min-width:0!important;flex:1 1 0!important;width:auto!important}
.st-key-libgrid img{aspect-ratio:4/3;object-fit:cover;width:100%;border-radius:10px}
.st-key-row_macros [data-testid="stMetricValue"],.st-key-row_coach [data-testid="stMetricValue"]{font-size:1.35rem!important}
.st-key-row_macros [data-testid="stMetric"]{padding:.5rem .6rem}
.mf-card2{background:#fff;border:1px solid #dfe6e3;border-radius:14px;padding:.5rem .6rem .6rem;margin-bottom:.15rem}
.mf-cardname{font-weight:700;font-size:.9rem;line-height:1.2;margin:.35rem 0 .1rem}
.mf-chip{display:inline-block;padding:.2rem .7rem;border-radius:999px;color:#fff;font-weight:700;font-size:.85rem}
.mf-big{font-size:2.6rem;font-weight:800;line-height:1;letter-spacing:-.03em}
.mf-sub{color:#6b7a75;font-size:.88rem}
.mf-hero{background:linear-gradient(135deg,#0f766e,#115e59);color:#fff;border-radius:18px;padding:1rem 1.2rem;margin:.4rem 0 1rem}
.mf-hero .mf-sub{color:#c7e6e1}
</style>
""", unsafe_allow_html=True)


# ───────────────────────── plumbing ─────────────────────────

@st.cache_resource
def api() -> Local:
    return open_local(os.environ.get("MESKOFIT_DATA", "meskofit-data"))


def password() -> str:
    try:
        return st.secrets.get("password", "") or os.environ.get("MESKOFIT_PASSWORD", "")
    except Exception:  # noqa: BLE001 - no secrets file
        return os.environ.get("MESKOFIT_PASSWORD", "")


def gate() -> None:
    pw = password()
    if not pw or st.session_state.get("ok"):
        return
    st.title("MeskoFit")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Open", type="primary") and entered:
            if hmac.compare_digest(entered, pw):
                st.session_state.ok = True
                st.rerun()
            st.error("Wrong password")
    st.stop()


def safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except HTTPError as e:
        st.error(e.msg)
    return None


def jpeg_b64(raw: bytes) -> str:
    """Any phone photo -> a modest base64 JPEG the AI routes accept."""
    import base64
    import io
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    im.thumbnail((1280, 1280))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return base64.b64encode(out.getvalue()).decode()


def num(text: str | None) -> float | None:
    """Parse a typed number; blank or junk gives None."""
    try:
        v = float((text or "").replace(",", "").strip())
        return v if v == v else None
    except ValueError:
        return None


def field(label: str, key: str, hint: str = "") -> float | None:
    return num(st.text_input(label, key=key, placeholder=hint))


_counter = "_fresh"


def bump(name: str) -> None:
    st.session_state.setdefault(_counter, {})
    st.session_state[_counter][name] = st.session_state[_counter].get(name, 0) + 1


def fresh_expander(label: str, name: str, expanded: bool = False):
    """An expander that closes again after you submit (bump(name)) by getting a new identity."""
    n = st.session_state.get(_counter, {}).get(name, 0)
    return st.expander(label + "​" * n, expanded=expanded)


def target(ex: dict) -> str:
    """The prescription for an exercise: reps, a hold time, or a free note."""
    if ex.get("reps"):
        return f"{ex['sets']} × {ex['reps']}"
    if ex.get("secs"):
        return f"{ex['sets']} × {ex['secs']} sec"
    return f"{ex['sets']} sets"


_IMG_BASE = Path(str(resources.files("meskofit") / "data" / "exercise-img"))


def img_src(lib_id: str, n: int) -> str | None:
    """Bundled photo if we have it, else the free-exercise-db copy online."""
    local = _IMG_BASE / lib_id / f"{n}.jpg"
    if local.is_file():
        return str(local)
    return f"https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/{lib_id}/{n}.jpg"


def youtube_url(name: str) -> str:
    from urllib.parse import quote_plus

    return f"https://www.youtube.com/results?search_query={quote_plus(name + ' exercise proper form')}"


def exercise_guide(e: dict, key: str) -> None:
    """Muscle map, photos and step-by-step instructions for one exercise."""
    prim, sec = e.get("primary") or [], e.get("secondary") or []
    if prim:
        st.markdown(f'<div style="text-align:center">{calc.muscle_svg(prim, sec, 210)}</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="mf-sub" style="text-align:center;margin:.1rem 0 .6rem"><span style="color:#e11d48">●</span> main &nbsp; '
            '<span style="color:#f6a3b5">●</span> helps</div>', unsafe_allow_html=True)
    files = [img_src(e["lib"], i) for i in (e.get("frames") or [0, 1])] if e.get("lib") else []
    if files:
        st.image(files, caption=["Start", "Finish"][: len(files)] if len(files) == 2 else None, width=165)
    if prim:
        txt = "**Main:** " + ", ".join(calc.muscle_label(m) for m in prim)
        if sec:
            txt += "  \n**Also:** " + ", ".join(calc.muscle_label(m) for m in sec if m not in prim)
        st.markdown(txt)
    if e.get("steps"):
        st.markdown("**How to do it**")
        st.markdown("\n".join(f"{i}. {t}" for i, t in enumerate(e["steps"], 1)))
    if e.get("tips"):
        st.markdown("**Tips**")
        st.markdown("\n".join(f"- {t}" for t in e["tips"]))
    form = e.get("form") or {}
    if form.get("wrong") or form.get("right"):
        st.markdown("**Form check**")
        st.markdown("\n".join([f"- ❌ {t}" for t in form.get("wrong", [])] + [f"- ✅ {t}" for t in form.get("right", [])]))
        st.caption("The animated wrong-vs-right figures are in the PC app; this mockup shows the cues.")


def card_html(inner: str) -> None:
    st.markdown(f'<div class="mf-card">{inner}</div>', unsafe_allow_html=True)


gate()
A = api()
seed_profile(A.app, Path(__file__).parent / "profile_defaults.json")
boot = A.get("/api/bootstrap")
profile = boot["profile"] or {}
today = date.today()
today_s = today.isoformat()

# ───────────────────────── first-run setup ─────────────────────────

if not profile.get("setupDone"):
    st.title("💪 MeskoFit")
    st.caption("A few details so your plan, BMI and graphs fit you. Everything stays in your own data folder.")
    units = st.segmented_control("Units", ["Imperial (lb, ft, in)", "Metric (kg, cm)"], default=None, key="su_units")
    if units is None:
        st.info("Pick the units you want to use and the boxes appear.")
        st.stop()
    imp = units.startswith("Imperial")
    with st.form("setup"):
        name = st.text_input("What should the app call you? (optional)", placeholder="Your name")
        c1, c2 = st.columns(2)
        sex = c1.selectbox("Sex (for calorie estimates)", ["male", "female"], index=None, placeholder="Choose")
        age = num(c2.text_input("Age", placeholder="years"))
        if imp:
            h1, h2 = st.columns(2)
            ft, inch = num(h1.text_input("Height: feet", placeholder="ft")), num(h2.text_input("inches", placeholder="in"))
            hcm = ((ft or 0) * 12 + (inch or 0)) * 2.54 if ft else None
        else:
            hcm = num(st.text_input("Height (cm)", placeholder="cm"))
        w1, w2 = st.columns(2)
        wt = num(w1.text_input("Current weight (lb)" if imp else "Current weight (kg)", placeholder="lb" if imp else "kg"))
        gw = num(w2.text_input("Goal weight (lb)" if imp else "Goal weight (kg)", placeholder="lb" if imp else "kg"))
        level = st.selectbox("Workout level", ["beginner", "intermediate", "expert"], index=None, placeholder="Choose")
        if st.form_submit_button("Save and start", type="primary"):
            f = (lambda v: v * LB) if imp else (lambda v: v)
            if not (sex and age and hcm and wt and level):
                st.error("Please fill in sex, age, height, current weight and level.")
            else:
                A.put("/api/profile", {"setupDone": True, "name": name.strip(), "level": level, "sex": sex, "heightCm": hcm,
                                       "age": int(age), "units": "imperial" if imp else "metric", "startWeightKg": f(wt),
                                       **({"goalWeightKg": f(gw)} if gw else {}), "startDate": today_s,
                                       "levelHistory": [{"date": today_s, "level": level}]})
                A.put(f"/api/body/{today_s}", {"weightKg": f(wt)})
                st.rerun()
    st.stop()

imperial = profile.get("units", "imperial") == "imperial"
wl = "lb" if imperial else "kg"
to_disp = (lambda kg: round(kg / LB, 1)) if imperial else (lambda kg: round(kg, 1))
to_kg = (lambda v: v * LB) if imperial else (lambda v: v)
hcm = profile.get("heightCm") or 0
level = profile.get("level") or (profile.get("levelHistory") or [{}])[-1].get("level", "beginner")
plans = boot["plans"]["levels"]
plan = plans.get(level, plans["beginner"])
cat = A.get("/api/catalog")["exercises"]
body = A.get("/api/body")
weights = [(e["date"], e["weightKg"]) for e in body if e.get("weightKg")]
cur_kg = weights[-1][1] if weights else profile.get("startWeightKg")
start_kg = profile.get("startWeightKg") or (weights[0][1] if weights else cur_kg)
goal_kg = profile.get("goalWeightKg")

# ───────────────────────── header ─────────────────────────

hello = f"Hi {profile['name']}" if profile.get("name") else "MeskoFit"
bm = calc.bmi(cur_kg, hcm) if cur_kg and hcm else 0
lost = (start_kg - cur_kg) if (start_kg and cur_kg) else 0
st.markdown(
    f'<div class="mf-hero"><div style="font-weight:800;font-size:1.3rem">💪 {hello}</div>'
    f'<div class="mf-sub">{today.strftime("%A, %B %-d")}</div>'
    f'<div style="display:flex;gap:1.6rem;margin-top:.7rem;flex-wrap:wrap">'
    + (f'<div><div class="mf-sub">Weight</div><div style="font-size:1.5rem;font-weight:800">{to_disp(cur_kg)} {wl}</div></div>' if cur_kg else "")
    + (f'<div><div class="mf-sub">BMI</div><div style="font-size:1.5rem;font-weight:800">{bm:.1f}</div></div>' if bm else "")
    + (f'<div><div class="mf-sub">{"Lost" if lost >= 0 else "Gained"} so far</div><div style="font-size:1.5rem;font-weight:800">{abs(to_disp(lost))} {wl}</div></div>' if cur_kg and start_kg else "")
    + (f'<div><div class="mf-sub">To goal</div><div style="font-size:1.5rem;font-weight:800">{abs(to_disp(cur_kg - goal_kg))} {wl}</div></div>' if cur_kg and goal_kg else "")
    + "</div></div>", unsafe_allow_html=True)

ai_cfg = A.get("/api/settings").get("ai") or {}
ai_on = bool(ai_cfg.get("provider") and (ai_cfg.get("model") or "").strip())
t_train, t_food, t_body, t_goal, t_shot, t_prog, t_coach, t_more = st.tabs(
    ["Train", "Food", "Body & BMI", "Goal", "Shots", "Progress", "Coach", "More"])


def weight_on(day: str, after_ok: bool = True) -> float | None:
    """Weigh-in on or before a day (else the next one after)."""
    before = [w for d, w in weights if d <= day]
    if before:
        return before[-1]
    after = [w for d, w in weights if d > day]
    return after[0] if (after and after_ok) else None


# ───────────────────────── exercise library ─────────────────────────

def favs() -> list[str]:
    return list(A.get("/api/settings").get("favExercises") or [])


def toggle_fav(lib_id: str) -> None:
    f = favs()
    f = [x for x in f if x != lib_id] if lib_id in f else f + [lib_id]
    A.put("/api/settings", {"favExercises": f})


def history_key(lib_id: str) -> str:
    return next((k for k, v in cat.items() if v.get("lib") == lib_id), "lib:" + lib_id)


def exercise_detail(lib_id: str) -> None:
    e = A.get(f"/api/library/{lib_id}")
    ck = history_key(lib_id)
    if ck in cat:  # the curated entry has tips and the plan's names
        e = {**e, **{k: v for k, v in cat[ck].items() if k in ("tips", "steps", "name")}}
    if st.button("← Back to library"):
        st.session_state.lib_open = None
        st.rerun()
    st.markdown(f"### {e['name']}")
    isfav = lib_id in favs()
    with st.container(key="row_actions"):
        c1, c2 = st.columns(2)
        if c1.button("★ Saved" if isfav else "☆ Save", key=f"fav{lib_id}", width="stretch"):
            toggle_fav(lib_id)
            st.rerun()
        c2.link_button("▶ YouTube demo", youtube_url(e["name"]), width="stretch")
    about, hist_tab, prog_tab = st.tabs(["About", "History", "Progress"])
    with about:
        st.caption(" · ".join(x for x in [(e.get("equipment") or "bodyweight").title(), (e.get("kind") or "").title()] if x))
        exercise_guide(e, "detail")
    hist = A.get(f"/api/history/exercise/{ck}")
    with hist_tab:
        if not hist:
            st.info("You haven't logged this exercise yet.")
        for h in hist[::-1][:12]:
            st.markdown(f"**{h['date']}**  \n" + " · ".join(
                f"{to_disp(s['weightKg'])} {wl} × {s['reps']}" if s.get("weightKg") and s.get("reps") else
                f"{s['secs']} sec" if s.get("secs") else f"{s.get('reps') or '-'} reps" for s in h["sets"]))
    with prog_tab:
        pts = []
        for h in hist:
            best = max((s["weightKg"] * (1 + min(s["reps"], 15) / 30) if s.get("reps") and s["reps"] > 1 else (s["weightKg"] or 0)
                        for s in h["sets"] if s.get("weightKg")), default=0)
            if best:
                pts.append({"date": h["date"], f"Estimated best ({wl})": to_disp(best)})
        if len(pts) >= 2:
            st.line_chart(pd.DataFrame(pts).set_index("date"), color=TEAL)
            st.caption("Your estimated one-rep max for each session (from your best set).")
        elif pts:
            st.metric("Estimated one-rep max", f"{pts[0][f'Estimated best ({wl})']} {wl}")
            st.caption("Log this exercise again to see a graph.")
        else:
            st.info("Log some weights to see your progress here.")


def library_view() -> None:
    st.markdown("### Exercise library")
    if st.session_state.get("lib_open"):
        exercise_detail(st.session_state.lib_open)
        return
    labels = calc._muscle_data()["labels"]
    mus = st.pills("Muscle", sorted(labels, key=lambda m: labels[m]), format_func=calc.muscle_label, key="lmus", selection_mode="single")
    qq = st.text_input("Search", placeholder="Search 870+ exercises", key="lq", label_visibility="collapsed")
    sig = (mus, qq.strip())
    if st.session_state.get("lsig") != sig:
        st.session_state.lsig, st.session_state.lpage = sig, 0
    fav_ids = favs()
    if mus or qq.strip():
        res = A.get("/api/library", q=qq.strip().replace(" ", "+"), **({"muscle": mus} if mus else {}))["results"]
        title = None
    elif fav_ids:
        res = [{"id": i, "name": A.get(f"/api/library/{i}")["name"], "equipment": "", "primary": A.get(f"/api/library/{i}")["primary"]} for i in fav_ids]
        title = "★ Your saved exercises"
    else:
        st.caption("Tap a muscle above or search to browse. Save ★ exercises you like and they show up here.")
        return
    if title:
        st.markdown(f"**{title}**")
    if not res:
        st.info("No matches.")
        return
    per = 8
    pages = (len(res) - 1) // per + 1
    page = min(st.session_state.get("lpage", 0), pages - 1)
    chunk = res[page * per:(page + 1) * per]
    with st.container(key="libgrid"):
        for i in range(0, len(chunk), 2):
            cols = st.columns(2)
            for col, r in zip(cols, chunk[i:i + 2]):
                with col:
                    src = img_src(r["id"], 0)
                    st.image(src, width="stretch")
                    icon = calc.muscle_icon_svg(r.get("primary") or [], None, 48)
                    mlabel = ", ".join(calc.muscle_label(m) for m in (r.get("primary") or [])[:2])
                    st.markdown(f'<div style="display:flex;gap:.45rem;align-items:center"><div>{icon}</div><div><div class="mf-cardname">{r["name"]}</div>'
                                f'<div class="mf-sub">{mlabel}</div></div></div>', unsafe_allow_html=True)
                    if st.button("Open", key=f"open{r['id']}", width="stretch"):
                        st.session_state.lib_open = r["id"]
                        st.rerun()
    if pages > 1:
        with st.container(key="row_pager"):
            p1, p2, p3 = st.columns([1, 1, 1])
            if p1.button("‹ Prev", disabled=page == 0, width="stretch"):
                st.session_state.lpage = page - 1
                st.rerun()
            p2.markdown(f'<div style="text-align:center;padding-top:.55rem" class="mf-sub">{page + 1} / {pages}</div>', unsafe_allow_html=True)
            if p3.button("Next ›", disabled=page >= pages - 1, width="stretch"):
                st.session_state.lpage = page + 1
                st.rerun()


# ───────────────────────── coaching helpers (no AI, just your own history) ─────────────────────────

def rep_range(ex) -> tuple[int, int] | None:
    """'10-12' -> (10, 12); '12' -> (12, 12); '10/side' -> (10, 10); None for timed work."""
    import re
    m = re.match(r"\s*(\d+)(?:\s*-\s*(\d+))?", str(ex.get("reps") or ""))
    return (int(m.group(1)), int(m.group(2) or m.group(1))) if m else None


def e1rm(w: float, reps: int) -> float:
    return w if reps == 1 else w * (1 + min(reps, 15) / 30) if w > 0 and reps > 0 else 0.0


def suggestion(ex, kind: str, info: dict) -> str | None:
    """Where to start today: repeat or bump last time's weight once every set reached the top of the range."""
    rr = rep_range(ex)
    if kind != "weight" or not rr:
        return None
    last = (info.get("last") or {}).get("sets") or []
    lw = [s for s in last if s.get("weightKg") and s.get("reps")]
    if not lw:
        return (f"First time: start light. Pick a weight you could lift about {rr[1] + 3} times, "
                f"then do {rr[0]}-{rr[1]} with 2-3 reps still in the tank.")
    wt = max(s["weightKg"] for s in lw)
    top = [s for s in lw if s["weightKg"] == wt]
    if all(s["reps"] >= rr[1] for s in top) and len(top) >= int(ex["sets"]):
        step = 5 if imperial else 2.5
        return f"Suggested: <b>{to_disp(wt) + step:g} {wl}</b> (+{step:g}). You hit {rr[1]} reps on every set last time."
    return f"Suggested: <b>{to_disp(wt):g} {wl}</b> again. Aim for {rr[1]} reps on every set, then go up."


def is_pr(ex_sets: list[dict], info: dict) -> bool:
    """A done set whose estimated one-rep max beats everything logged before this session."""
    prior = (info.get("best") or {}).get("e1rmKg") or 0
    if not info.get("sessions") or not prior:
        return False
    return any(e1rm(s["weightKg"], s["reps"]) > prior * 1.001 for s in ex_sets
               if s.get("done") and s.get("weightKg") and s.get("reps"))


def block_timer(secs: int, start_label: str) -> None:
    """A start / pause / reset countdown that beeps and vibrates at zero."""
    html = f"""
<div style="font-family:system-ui,sans-serif;display:flex;align-items:center;gap:10px">
 <div id="t" style="font-size:34px;font-weight:800;color:#0f766e;min-width:92px">{secs // 60}:{secs % 60:02d}</div>
 <button id="b" style="flex:1;padding:12px;border:0;border-radius:12px;background:#0f766e;color:#fff;font-size:16px;font-weight:700">{start_label}</button>
 <button id="r" style="padding:12px;border:1px solid #0f766e;border-radius:12px;background:#fff;color:#0f766e;font-size:16px;font-weight:700">Reset</button>
</div>
<script>
const total={secs}; let left=total, h=null;
const t=document.getElementById('t'), b=document.getElementById('b'), r=document.getElementById('r');
const show=()=>t.textContent=Math.floor(left/60)+':'+String(left%60).padStart(2,'0');
function beep(){{try{{const c=new (window.AudioContext||window.webkitAudioContext)();for(let i=0;i<3;i++){{const o=c.createOscillator();o.frequency.value=880;o.connect(c.destination);o.start(c.currentTime+i*.3);o.stop(c.currentTime+i*.3+.15);}}}}catch(e){{}}
 if(navigator.vibrate)navigator.vibrate([200,100,200]);}}
function stop(){{clearInterval(h);h=null;}}
b.onclick=()=>{{ if(h){{stop();b.textContent='Resume';return;}}
 if(left<=0)left=total; b.textContent='Pause';
 h=setInterval(()=>{{left--;show();if(left<=0){{stop();t.textContent='Done!';b.textContent='Again';beep();}}}},1000);}};
r.onclick=()=>{{stop();left=total;show();b.textContent='{start_label}';}};
</script>"""
    if hasattr(st, "iframe"):
        st.iframe(html, height=70)
    else:
        import streamlit.components.v1 as components
        components.html(html, height=70)


STAIRS_MIN = {"beginner": 5, "intermediate": 8, "expert": 10}


def warmup_card() -> None:
    """Your plan opens every workout with stairs and ends with treadmill; here that is home stairs and a walk."""
    acts = A.get("/api/activities", **{"from": today_s, "to": today_s})
    done = {a["kind"]: a["minutes"] for a in acts}
    mins = STAIRS_MIN.get(level, 5)
    with st.expander(("✅ " if "Stairs" in done else "🪜 ") + "Daily stairs warm-up" + (f" · {done['Stairs']:g} min logged" if "Stairs" in done else f" · {mins} min"),
                     expanded="Stairs" not in done):
        st.caption("Your plan starts every workout with 10 minutes on the stair machine (5 minimum). "
                   "Climbing your home stairs does the same job. Hold the rail, take them at a pace where you can still talk, "
                   "and rest whenever you need to; the clock just counts your climbing time.")
        opts = sorted({5, mins, 10})
        m = st.segmented_control("Minutes", opts, default=mins, key="stairs_min", format_func=lambda v: f"{v} min") or mins
        block_timer(int(m) * 60, "Start climbing")
        st.markdown(f'<div class="mf-sub">💡 Start at 5 minutes and add about 1 minute a week up to 10. '
                    f'If a knee hurts above 4 out of 10, stop, or step up and down a single bottom step instead. '
                    f'Take the way down slowly.</div>', unsafe_allow_html=True)
        if "Stairs" not in done and st.button("Log stairs done", key="log_stairs", type="primary", width="stretch"):
            A.post("/api/activities", {"date": today_s, "kind": "Stairs", "minutes": float(m)})
            st.rerun()
    with st.expander(("✅ " if "Treadmill" in done else "🚶 ") + "Finisher: 5 min treadmill walk"
                     + (f" · {done['Treadmill']:g} min logged" if "Treadmill" in done else "")):
        st.caption("Your plan ends every workout with 5 minutes: walk, jog, then run the last minute. "
                   "With sore knees, keep it a brisk walk and make the last minute your fastest walk instead of a run. "
                   "Add the jog back when your knees and weight allow.")
        block_timer(300, "Start walking")
        if "Treadmill" not in done and st.button("Log treadmill done", key="log_tread", width="stretch"):
            A.post("/api/activities", {"date": today_s, "kind": "Treadmill", "minutes": 5.0})
            st.rerun()


def rest_timer() -> None:
    secs = st.segmented_control("Rest timer", [45, 60, 90, 120, 180], default=90, key="rest_len",
                                format_func=lambda v: f"{v}s" if v < 90 else f"{v // 60}:{v % 60:02d}")
    secs = secs or 90
    html = f"""
<div style="font-family:system-ui,sans-serif;display:flex;align-items:center;gap:12px">
 <div id="t" style="font-size:34px;font-weight:800;color:#0f766e;min-width:92px">{secs // 60}:{secs % 60:02d}</div>
 <button id="b" style="flex:1;padding:12px;border:0;border-radius:12px;background:#0f766e;color:#fff;font-size:16px;font-weight:700">Start rest</button>
</div>
<script>
let left={secs}, h=null; const t=document.getElementById('t'), b=document.getElementById('b');
const show=()=>t.textContent=Math.floor(left/60)+':'+String(left%60).padStart(2,'0');
function beep(){{try{{const c=new (window.AudioContext||window.webkitAudioContext)();for(let i=0;i<3;i++){{const o=c.createOscillator();o.frequency.value=880;o.connect(c.destination);o.start(c.currentTime+i*.3);o.stop(c.currentTime+i*.3+.15);}}}}catch(e){{}}
 if(navigator.vibrate)navigator.vibrate([200,100,200]);}}
b.onclick=()=>{{ if(h){{clearInterval(h);h=null;left={secs};show();b.textContent='Start rest';return;}}
 left={secs};show();b.textContent='Skip';
 h=setInterval(()=>{{left--;show();if(left<=0){{clearInterval(h);h=null;t.textContent='Go!';b.textContent='Start rest';beep();}}}},1000);}};
</script>"""
    if hasattr(st, "iframe"):
        st.iframe(html, height=70)
    else:  # older Streamlit
        import streamlit.components.v1 as components
        components.html(html, height=70)


# ═════════════ TRAIN ═════════════
with t_train:
    warmup_card()
    active = A.get("/api/sessions/active")
    if active is None:
        st.caption(f"{plan['label']} plan · {plan['weeklyGoal']} workouts a week · {plan['effort']}")
        days = plan["days"] + ([plan["core"]] if plan.get("core") else [])
        pick = st.selectbox("Choose a workout", days, format_func=lambda d: d["name"], index=None, placeholder="Choose a workout")
        if pick:
            if pick.get("note"):
                st.caption(pick["note"])
            day_prim = sorted({m for ex in pick["exercises"] for m in cat.get(ex["ex"], {}).get("primary", [])})
            day_sec = sorted({m for ex in pick["exercises"] for m in cat.get(ex["ex"], {}).get("secondary", [])} - set(day_prim))
            if day_prim:
                st.markdown(f'<div style="text-align:center">{calc.muscle_svg(day_prim, day_sec, 210)}</div>', unsafe_allow_html=True)
                st.caption("Muscles this workout hits: " + ", ".join(calc.muscle_label(m) for m in day_prim))
            for ex in pick["exercises"]:
                e = cat.get(ex["ex"], {"name": ex["ex"]})
                with st.expander(f"{e['name']} · {target(ex)}"):
                    exercise_guide(e, ex["id"])
            if st.button("Start workout", type="primary", width="stretch"):
                safe(A.post, "/api/sessions", {"dayId": pick["id"], "dayName": pick["name"], "level": level, "date": today_s})
                st.rerun()
        library_view()
    else:
        day = next((d for d in plan["days"] + [plan.get("core") or {}] if d.get("id") == active["dayId"]), None)
        st.subheader(active["dayName"])
        saved = {(s["planExId"], s["setNo"]): s for s in active.get("sets", [])}
        exercises = (day or {"exercises": []})["exercises"]

        def ex_done(ex) -> bool:
            return all(saved.get((ex["id"], n), {}).get("done") for n in range(1, int(ex["sets"]) + 1))

        first_open = next((ex["id"] for ex in exercises if not ex_done(ex)), None)
        rest_timer()
        infos = A.get("/api/history/last", keys=",".join(ex["ex"] for ex in exercises), day=active["dayId"], exclude=active["id"]) if exercises else {}
        all_rows: list[dict] = []
        changed = False
        for ex in exercises:
            e = cat.get(ex["ex"], {"name": ex["ex"], "kind": "weight"})
            mark = "✅" if ex_done(ex) else "⬜"
            last = infos.get(ex["ex"], {})
            pr = is_pr([saved[(ex["id"], n)] for n in range(1, int(ex["sets"]) + 1) if (ex["id"], n) in saved], last)
            with st.expander(f"{mark}  {e['name']} · {target(ex)}" + ("  🏆 PR" if pr else ""), expanded=(ex["id"] == first_open)):
                if pr:
                    st.success("🏆 New personal record! Your estimated one-rep max beat your previous best.")
                if last.get("last"):
                    st.caption("Last time: " + ", ".join(f"{to_disp(s['weightKg'])} {wl} × {s.get('reps') or '-'}"
                                                         for s in last["last"].get("sets", []) if s.get("weightKg") is not None))
                    b = last.get("best") or {}
                    if b.get("e1rmKg"):
                        st.caption(f"Best estimated one-rep max: {to_disp(b['e1rmKg'])} {wl}")
                if tip := suggestion(ex, e.get("kind", "weight"), last):
                    st.markdown(f'<div class="mf-sub">💡 {tip}</div>', unsafe_allow_html=True)
                timed = bool(ex.get("secs")) and not ex.get("reps")
                rcol = "Seconds" if timed else "Reps"
                if note := ex.get("note"):
                    st.caption(note)
                df = pd.DataFrame([{
                    "Set": n,
                    f"Weight ({wl})": to_disp(saved[(ex["id"], n)]["weightKg"]) if saved.get((ex["id"], n), {}).get("weightKg") is not None else 0.0,
                    ("Seconds" if timed else "Reps"): int(saved.get((ex["id"], n), {}).get("secs" if timed else "reps") or 0),
                    "Done": bool(saved.get((ex["id"], n), {}).get("done")),
                } for n in range(1, int(ex["sets"]) + 1)])
                ed = st.data_editor(df, hide_index=True, key=f"ed-{active['id']}-{ex['id']}", width="stretch", disabled=["Set"], num_rows="fixed")
                st.markdown("---")
                exercise_guide(e, ex["id"])
            for _, r in ed.iterrows():
                row = {"planExId": ex["id"], "exKey": ex["ex"], "setNo": int(r["Set"]), "weightKg": to_kg(float(r[f"Weight ({wl})"])) or None,
                       "reps": None if timed else (int(r[rcol]) or None), **({"secs": int(r[rcol]) or None} if timed else {}),
                       "done": bool(r["Done"])}
                all_rows.append(row)
                old = saved.get((ex["id"], row["setNo"]), {})
                amount = row.get("secs") if timed else row["reps"]
                if (old.get("weightKg") or None, (old.get("secs") if timed else old.get("reps")) or None, bool(old.get("done"))) != (
                        round(row["weightKg"], 6) if row["weightKg"] else None, amount, row["done"]):
                    if not (not old and not row["weightKg"] and not amount and not row["done"]):
                        changed = True
        if changed:  # save as you go, then redraw so finished exercises fold up
            safe(A.put, f"/api/sessions/{active['id']}/sets", {"sets": all_rows})
            st.rerun()
        st.markdown("&nbsp;")
        k = st.slider("Knee pain today", 0, 10, int(active["data"].get("kneePain", 0)))
        rpe = st.slider("How hard was it? (1 easy – 10 max)", 1, 10, int(active["data"].get("rpe", 6)))
        c1, c2 = st.columns(2)
        if c1.button("Finish workout", type="primary", width="stretch"):
            safe(A.patch, f"/api/sessions/{active['id']}", {"data": {"kneePain": k, "rpe": rpe},
                                                           "finishedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
            st.rerun()
        if c2.button("Discard", width="stretch"):
            A.delete(f"/api/sessions/{active['id']}")
            st.rerun()

# ═════════════ FOOD ═════════════
with t_food:
    day = st.date_input("Day", today, key="foodday").isoformat()
    log = A.get(f"/api/log/{day}")
    meals = boot["settings"]["meals"]
    tot = {k: sum((e["nutrients"] or {}).get(k, 0) for e in log["entries"]) for k in ("kcal", "protein", "carbs", "fat")}
    with st.container(key="row_macros"):
        cols = st.columns(4)
        for col, (k, lab) in zip(cols, [("kcal", "Calories"), ("protein", "Protein g"), ("carbs", "Carbs g"), ("fat", "Fat g")]):
            col.metric(lab, round(tot[k]))
    # ── your meal plan ──
    dd = date.fromisoformat(day)
    mp = mealplan.day_plan(dd.weekday())
    logged_plan = {(e["meal"].lower(), e["name"]) for e in log["entries"] if e.get("source") == "plan"}
    mnames = {m.lower(): m for m in meals}

    def plan_meal_name(m: dict) -> str:
        """The log stores the meal as its lower-case key ("breakfast", "snacks"), like the web app."""
        return m.get("slot") or (m["name"].lower() if m["name"].lower() in mnames else meals[0].lower())

    def log_plan_meal(m: dict) -> None:
        A.post("/api/log", {"date": day, "meal": plan_meal_name(m), "name": m["what"], "amount": 1, "unit": "serving",
                            "unitLabel": "plan serving", "source": "plan",
                            "nutrients": {"kcal": m["kcal"], "protein": m["protein"], "carbs": m["carbs"], "fat": m["fat"]}})

    t = mp["totals"]
    st.progress(min(1.0, tot["kcal"] / mealplan.KCAL_HIGH),
                text=f"{round(tot['kcal'])} of {mealplan.KCAL_LOW:,}–{mealplan.KCAL_HIGH:,} kcal today")
    with st.expander(f"🍽 Meal plan · {mp['day']} · about {t['kcal']:,} kcal", expanded=True):
        st.caption(f"Protein about {t['protein']} g · carbs about {t['carbs']} g · fat about {t['fat']} g. "
                   "Calories come from your plan; protein, carbs and fat are estimates from the portions.")
        pending = [m for m in mp["meals"] if (plan_meal_name(m).lower(), m["what"]) not in logged_plan]
        if pending and st.button(f"Log the whole day ({sum(m['kcal'] for m in pending):,} kcal)", type="primary", width="stretch", key="plan_all"):
            for m in pending:
                log_plan_meal(m)
            st.rerun()
        for m in mp["meals"]:
            done = (plan_meal_name(m).lower(), m["what"]) in logged_plan
            st.markdown(f"**{'✅ ' if done else ''}{m['name']}: {m['what']}**  \n"
                        f"<span class='mf-sub'>{m['kcal']} kcal · P {m['protein']} · C {m['carbs']} · F {m['fat']}</span>",
                        unsafe_allow_html=True)
            st.markdown("Portion: " + " · ".join(f"{it['food']} **{it['portion']}**" for it in m["items"]))
            st.caption(m["how"])
            if not done and st.button(f"Log {m['name'].lower()}", key=f"plan_{m['name']}", width="stretch"):
                log_plan_meal(m)
                st.rerun()
    with st.expander("Week at a glance"):
        st.dataframe(pd.DataFrame([{"Day": d["day"], "Dinner": d["meals"][3]["what"], "kcal": d["totals"]["kcal"],
                                    "Protein g": d["totals"]["protein"], "Carbs g": d["totals"]["carbs"]} for d in mealplan.week_plan()]),
                     hide_index=True, width="stretch")
        st.caption("Breakfast, lunch and snack are the same every day. Rice and pasta show up twice a week (Wednesday and Saturday).")
    with st.expander("Weighing tips"):
        for tip in mealplan.TIPS:
            st.markdown(f"- {tip}")
    with st.expander("Monthly shopping list"):
        st.dataframe(pd.DataFrame([{"Buy": a, "30-day amount": b} for a, b in ((x["item"], x["amount"]) for x in mealplan.SHOPPING)]), hide_index=True, width="stretch")
        st.caption("Frozen vegetables monthly; buy fresh salad ingredients weekly.")
    for meal in meals:
        es = [e for e in log["entries"] if e["meal"].lower() == meal.lower()]
        if es:
            st.markdown(f"**{meal}** · {round(sum((e['nutrients'] or {}).get('kcal', 0) for e in es))} kcal")
        for e in es:
            c1, c2 = st.columns([8, 1])
            c1.write(f"{e['name']} — {e['amount']:g} {e.get('unitLabel') or e['unit']} · {round((e['nutrients'] or {}).get('kcal', 0))} kcal")
            if c2.button("✕", key=f"del{e['id']}"):
                A.delete(f"/api/log/{e['id']}")
                st.rerun()
    st.markdown("### Add food")
    hr = datetime.now().hour
    default_meal = meals[0] if hr < 11 else meals[min(1, len(meals) - 1)] if hr < 16 else meals[min(2, len(meals) - 1)]
    with st.expander("📷 Scan a meal (AI)"):
        if not ai_on:
            st.info("Not connected yet. Add an AI model under More → AI connection to turn this on. "
                    "Until then, use Search below.")
        else:
            st.caption("Take or pick a photo. The AI names the foods and guesses portions; you fix the grams, "
                       "and the macros are worked out from those grams. For best accuracy, weigh the plate.")
            shot = st.file_uploader("Photo of your meal", type=["jpg", "jpeg", "png", "webp"], key="scanfile")
            if shot is not None and st.button("Analyze photo", type="primary"):
                with st.spinner("Looking at your meal…"):
                    res = safe(A.post, "/api/ai/meal", {"image": jpeg_b64(shot.getvalue())})
                if res:
                    st.session_state.scan = res
            scan = st.session_state.get("scan")
            if scan:
                if scan["notes"]:
                    st.caption(scan["notes"])
                if not scan["items"]:
                    st.info("I couldn't see any food in that photo.")
                picked = []
                for i, it in enumerate(scan["items"]):
                    c1, c2 = st.columns([3, 2])
                    on = c1.checkbox(f"{it['name']} ({it['confidence']} confidence)", value=True, key=f"sc_on{i}")
                    g = num(c2.text_input("grams", value=f"{it['grams']:g}", key=f"sc_g{i}", label_visibility="collapsed"))
                    per = (it.get("match") or {}).get("per100") or {}
                    if per.get("kcal") is not None:
                        nut = {k: round(v * (g or 0) / 100, 1) for k, v in per.items() if isinstance(v, (int, float))}
                        src = "usda"
                    else:
                        f = (g or 0) / it["grams"]
                        nut = {k: round(v * f, 1) for k, v in it["ai"].items()}
                        src = "ai"
                    st.caption(f"{round(nut.get('kcal', 0))} kcal · P {round(nut.get('protein', 0))} · C {round(nut.get('carbs', 0))} · F {round(nut.get('fat', 0))}"
                               + (" · from food database" if src == "usda" else " · AI estimate"))
                    if on and g:
                        picked.append((it, g, nut, src))
                sm = st.selectbox("Meal", meals, index=meals.index(default_meal), key="scan_meal") if scan["items"] else None
                if scan["items"] and st.button("Add selected to log", type="primary", disabled=not picked):
                    for it, g, nut, src in picked:
                        A.post("/api/log", {"date": day, "meal": sm.lower(), "name": it["name"], "amount": g, "unit": "g", "grams": g,
                                            "nutrients": nut, "source": "ai-photo" if src == "ai" else "usda"})
                    st.session_state.pop("scan", None)
                    st.rerun()
    sn = st.session_state.get(_counter, {}).get("search", 0)
    with st.form(f"search{sn}"):
        q = st.text_input("Search by name, or type a barcode", placeholder="e.g. greek yogurt")
        go = st.form_submit_button("Search", type="primary")
    if go and len(q.strip()) >= 2:
        res = safe(A.get, "/api/food/search", q=q.strip().replace(" ", "+"))
        if res:
            st.session_state.fres = res["local"] + res["branded"] + res["generic"]
            st.session_state.ferr = res.get("errors") or {}
    if st.session_state.get("fres") is not None:
        hits = st.session_state.fres
        for k, v in st.session_state.get("ferr", {}).items():
            st.caption(f"{k}: {v}")
        if not hits:
            st.info("No matches.")
        else:
            with st.form(f"addfood{sn}"):
                h = st.selectbox("Result", hits, index=None, placeholder="Choose a result",
                                 format_func=lambda h: f"{h['name']}" + (f" ({h['brand']})" if h.get("brand") else "") +
                                 (f" · {round(h['per100'].get('kcal', 0))} kcal/100 g" if h.get("per100") else ""))
                c1, c2 = st.columns(2)
                grams = num(c1.text_input("Amount (grams)", placeholder="e.g. 150"))
                meal = c2.selectbox("Meal", meals, index=meals.index(default_meal))
                if st.form_submit_button("Add to log", type="primary"):
                    if not h or not grams or grams <= 0:
                        st.error("Choose a result and type the grams.")
                    else:
                        per = h.get("per100") or {}
                        nut = {k: round(v * grams / 100, 1) for k, v in per.items() if isinstance(v, (int, float))}
                        safe(A.post, "/api/log", {"date": day, "meal": meal.lower(), "name": h["name"], "brand": h.get("brand"), "amount": grams,
                                                  "unit": "g", "grams": grams, "nutrients": nut, "source": h["source"],
                                                  **({"foodId": h["localId"]} if h.get("localId") else {})})
                        st.session_state.pop("fres", None)
                        bump("search")
                        st.rerun()
    with fresh_expander("Quick add calories", "quick"):
        with st.form("quick", clear_on_submit=True):
            n = st.text_input("What was it?", placeholder="e.g. Restaurant dinner")
            c = st.columns(4)
            kc, pr, ca, fa = (num(c[0].text_input("kcal")), num(c[1].text_input("Protein g")),
                              num(c[2].text_input("Carbs g")), num(c[3].text_input("Fat g")))
            ml = st.selectbox("Meal", meals, index=meals.index(default_meal), key="qm")
            if st.form_submit_button("Add") and kc:
                A.post("/api/log", {"date": day, "meal": ml.lower(), "name": n or "Quick add", "amount": 1, "unit": "serving", "source": "quick",
                                    "nutrients": {"kcal": kc, "protein": pr or 0, "carbs": ca or 0, "fat": fa or 0}})
                bump("quick")
                st.rerun()
    with fresh_expander("Water", "water"):
        with st.form("water"):
            ml = num(st.text_input("Total water today (ml)", placeholder="e.g. 1500"))
            if st.form_submit_button("Save") and ml is not None:
                A.put(f"/api/water/{day}", {"ml": ml})
                bump("water")
                st.rerun()

# ═════════════ BODY & BMI ═════════════
with t_body:
    st.markdown("### Log your weight")
    with st.form("weighin"):
        d = st.date_input("Date", today, key="bd").isoformat()
        w = num(st.text_input(f"Weight ({wl})", placeholder=f"e.g. {'210.5' if imperial else '95.5'}"))
        st.caption("Your BMI only needs weight and height. Tape measurements below are optional, for tracking inches.")
        with st.expander("Optional: tape measurements (cm)"):
            c1, c2, c3 = st.columns(3)
            waist, neck, hip = (num(c1.text_input("Waist")), num(c2.text_input("Neck")), num(c3.text_input("Hip")))
        if st.form_submit_button("Save", type="primary"):
            payload = {k: v for k, v in {"weightKg": to_kg(w) if w else None, "waistCm": waist, "neckCm": neck, "hipCm": hip}.items() if v}
            if payload:
                existing = next((e for e in body if e["date"] == d), {})
                if safe(A.put, f"/api/body/{d}", {**{k: v for k, v in existing.items() if k != "date"}, **payload}) is not None:
                    st.rerun()
            else:
                st.error("Type your weight.")

    st.markdown("### Your BMI")
    what_if = num(st.text_input(f"What if I weighed… ({wl})", placeholder="leave blank to use your latest weight"))
    use_kg = to_kg(what_if) if what_if else cur_kg
    if not hcm:
        st.info("Add your height under More to see your BMI.")
    elif use_kg:
        b = calc.bmi(use_kg, hcm)
        label, color = calc.category(b)
        lo, hi = calc.healthy_range_kg(hcm)
        left, right = st.columns([1, 1])
        with left:
            st.markdown(f'<div class="mf-sub">{"If you weighed " + str(to_disp(use_kg)) + " " + wl if what_if else "Right now"}</div>'
                        f'<div class="mf-big">{b:.1f}</div><span class="mf-chip" style="background:{color}">{label}</span>',
                        unsafe_allow_html=True)
            st.markdown(f'<p class="mf-sub" style="margin-top:.8rem">Healthy range for your height:<br><b style="color:{INK}">'
                        f'{to_disp(lo)}–{to_disp(hi)} {wl}</b></p>', unsafe_allow_html=True)
            if use_kg > hi:
                st.markdown(f'<p class="mf-sub">To reach the top of that range: <b style="color:{INK}">{to_disp(use_kg - hi)} {wl}</b> to go.</p>', unsafe_allow_html=True)
            elif use_kg < lo:
                st.markdown(f'<p class="mf-sub">About <b style="color:{INK}">{to_disp(lo - use_kg)} {wl}</b> under the healthy range.</p>', unsafe_allow_html=True)
        with right:
            st.markdown(f'<div style="text-align:center">{calc.figure_svg(b)}</div>', unsafe_allow_html=True)
        st.markdown(calc.scale_html(b), unsafe_allow_html=True)
        st.caption("BMI is a rough screen. It doesn't separate muscle from fat, so use it with your waist measurement and how you feel. "
                   "The figure is a simple illustration of build, not a photo of you.")
    h_m = hcm / 100
    meas = pd.DataFrame(body)
    if not meas.empty and "waistCm" in meas and meas["waistCm"].notna().any() and hcm:
        last_w = meas.dropna(subset=["waistCm"]).iloc[-1]
        st.metric("Waist-to-height ratio", f"{last_w['waistCm'] / hcm:.2f}", help="Under 0.5 is generally considered healthy.")
    st.markdown("### Fasting")
    fasts = A.get("/api/fasts")
    open_f = next((f for f in fasts if not f["endAt"]), None)
    if open_f:
        started = datetime.fromisoformat(open_f["startAt"].replace("Z", "+00:00"))
        hrs = (datetime.now(timezone.utc) - started).total_seconds() / 3600
        st.progress(min(1.0, hrs / open_f["targetHours"]), text=f"{hrs:.1f} of {open_f['targetHours']:g} hours")
        if st.button("End fast"):
            A.post(f"/api/fasts/{open_f['id']}/end", {})
            st.rerun()
    elif st.button("Start a fast"):
        A.post("/api/fasts/start", {"targetHours": boot["settings"]["fastingHours"]})
        st.rerun()

# ═════════════ GOAL PROJECTION ═════════════
with t_goal:
    st.markdown("### Where will I be?")
    st.caption("Pick how you want to lose weight and how long, and see the weight you'd reach.")
    if not (cur_kg and hcm):
        st.info("Log a weight and add your height first.")
    else:
        mode = st.segmented_control("Plan", ["Lose lb per week" if imperial else "Lose kg per week", "Cut calories a day", "Eat calories a day"],
                                    default="Cut calories a day", key="gmode")
        c1, c2 = st.columns(2)
        horizon = c2.selectbox("Time", ["3 months", "6 months", "1 year", "2 years", "3 years", "5 years"], index=2)
        days = {"3 months": 91, "6 months": 183, "1 year": 365, "2 years": 730, "3 years": 1095, "5 years": 1826}[horizon]
        floor_kg = calc.weight_at_bmi(18.5, hcm)
        pts, ok, note = [], True, ""
        if mode and mode.startswith("Lose"):
            amt = num(c1.text_input(f"{wl} per week", placeholder="e.g. 1.5" if imperial else "e.g. 0.7"))
            if amt:
                kg_wk = amt * LB if imperial else amt
                pts = calc.project_deficit(cur_kg, kg_wk * calc.KCAL_PER_KG / 7, today, days, floor_kg)
                if kg_wk / LB > 2.5:
                    note = "That's faster than is usually sustainable. Talk to your doctor before aiming this high."
        elif mode == "Cut calories a day":
            amt = num(c1.text_input("Calories cut per day", placeholder="e.g. 500"))
            if amt:
                pts = calc.project_deficit(cur_kg, amt, today, days, floor_kg)
                note = "Steady estimate: about 3,500 calories per pound. Real loss usually slows over time."
        else:
            amt = num(c1.text_input("Calories eaten per day", placeholder="e.g. 2200"))
            act_label = st.selectbox("How active are you?", list(calc.ACTIVITY), index=0)
            if amt:
                if not profile.get("sex") or not profile.get("age"):
                    ok = False
                    with st.form("sexage"):
                        st.caption("To estimate how many calories you burn, I need two more things. They stay in your data.")
                        f1, f2 = st.columns(2)
                        sx2 = f1.selectbox("Sex", ["male", "female"], index=None, placeholder="Choose")
                        ag2 = num(f2.text_input("Age", placeholder="years"))
                        if st.form_submit_button("Save", type="primary"):
                            if sx2 and ag2:
                                A.put("/api/profile", {"sex": sx2, "age": int(ag2)})
                                st.rerun()
                            else:
                                st.error("Choose sex and type your age.")
                else:
                    pts = calc.project_intake(cur_kg, amt, profile["sex"], hcm, profile["age"], calc.ACTIVITY[act_label], today, days, floor_kg)
                    note = "This one accounts for burning fewer calories as you get lighter, so loss slows and levels off."
        if pts and ok:
            end = pts[-1].kg
            tot = cur_kg - end
            b_end = calc.bmi(end, hcm)
            lab_end, col_end = calc.category(b_end)
            m = st.columns(3)
            m[0].metric(f"Weight in {horizon}", f"{to_disp(end)} {wl}")
            m[1].metric("Change", f"{'−' if tot >= 0 else '+'}{abs(to_disp(tot))} {wl}")
            m[2].metric("BMI then", f"{b_end:.1f}")
            st.markdown(f'<span class="mf-chip" style="background:{col_end}">{lab_end}</span>', unsafe_allow_html=True)
            if goal_kg and cur_kg > goal_kg:
                hit = calc.reach_date(pts, goal_kg)
                if hit:
                    st.success(f"You'd reach your goal of {to_disp(goal_kg)} {wl} around {hit.strftime('%B %Y')}.")
                else:
                    st.info(f"This plan doesn't reach your goal of {to_disp(goal_kg)} {wl} within {horizon}. Try a longer time or a bigger change.")
            if pts[-1].kg <= floor_kg + 0.01:
                st.warning("This plan would take you to the bottom of the healthy range, so I stopped the line there.")
            df = pd.DataFrame({"date": [p.day for p in pts], "weight": [to_disp(p.kg) for p in pts]})
            lo, hi = calc.healthy_range_kg(hcm)
            band = pd.DataFrame({"lo": [to_disp(lo)], "hi": [to_disp(hi)]})
            ch = alt.Chart(band).mark_rect(color="#22c55e", opacity=.14).encode(y="lo:Q", y2="hi:Q")
            line = alt.Chart(df).mark_line(color=TEAL, strokeWidth=3).encode(
                x=alt.X("date:T", title=None), y=alt.Y("weight:Q", title=wl, scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip("date:T", title="Date"), alt.Tooltip("weight:Q", title=wl)])
            layers = [ch, line]
            if goal_kg:
                layers.append(alt.Chart(pd.DataFrame({"g": [to_disp(goal_kg)]})).mark_rule(color="#f97316", strokeDash=[5, 4]).encode(y="g:Q"))
            st.altair_chart(alt.layer(*layers).properties(height=280), width="stretch")
            st.caption("Green band = healthy BMI range for your height" + (" · orange line = your goal weight." if goal_kg else "."))
            marks = []
            for lab, dd in [("1 month", 30), ("3 months", 91), ("6 months", 183), ("1 year", 365), ("2 years", 730), ("3 years", 1095), ("5 years", 1826)]:
                if dd <= days:
                    p = min(pts, key=lambda p: abs((p.day - today).days - dd))
                    marks.append({"When": lab, f"Weight ({wl})": to_disp(p.kg), "BMI": round(calc.bmi(p.kg, hcm), 1)})
            st.dataframe(pd.DataFrame(marks), hide_index=True, width="stretch")
            if note:
                st.caption(note)
            st.caption("Estimates only, not medical advice. Check big changes with your doctor.")
        elif mode:
            st.caption("Type an amount above to see your graph.")

# ═════════════ SHOT TRACKER ═════════════
with t_shot:
    shots = A.get("/api/shots")
    st.markdown("### Mounjaro (tirzepatide) shots")
    last_shot = shots[-1] if shots else None
    if last_shot:
        due = date.fromisoformat(last_shot["date"]) + timedelta(days=7)
        left_days = (due - today).days
        when = "today" if left_days == 0 else f"in {left_days} day{'s' if left_days != 1 else ''}" if left_days > 0 else f"{-left_days} day{'s' if left_days != -1 else ''} overdue"
        m = st.columns(4)
        m[0].metric("Shots logged", len(shots))
        m[1].metric("Current dose", f"{last_shot['doseMg']:g} mg")
        m[2].metric("Next shot", due.strftime("%a %b %-d"), when, delta_color="off" if left_days >= 0 else "inverse")
        first_w = weight_on(shots[0]["date"])
        if first_w and cur_kg:
            m[3].metric("Lost since 1st shot", f"{to_disp(first_w - cur_kg)} {wl}")
    sites = ["Abdomen – left", "Abdomen – right", "Thigh – left", "Thigh – right", "Upper arm – left", "Upper arm – right"]
    nxt_site = sites[(sites.index(last_shot["site"]) + 1) % len(sites)] if last_shot and last_shot.get("site") in sites else sites[0]
    with fresh_expander("➕ Log a shot", "shot", expanded=not shots):
        with st.form("shotform"):
            c1, c2 = st.columns(2)
            sd = c1.date_input("Date", today, key="shotdate")
            dose = c2.selectbox("Dose (mg)", DOSES, index=DOSES.index(last_shot["doseMg"]) if last_shot and last_shot["doseMg"] in DOSES else 0,
                                format_func=lambda v: f"{v:g} mg")
            site = st.selectbox("Where", sites, index=sites.index(nxt_site))
            note = st.text_input("Note (optional)", placeholder="side effects, appetite, anything")
            if st.form_submit_button("Save shot", type="primary"):
                if safe(A.post, "/api/shots", {"date": sd.isoformat(), "doseMg": dose, "site": site, "note": note}) is not None:
                    bump("shot")
                    st.rerun()
    if not shots:
        st.info("Log your first shot to start the graphs. Dose changes are decided with your prescriber; this just keeps the record.")
    else:
        rows = []
        prev_w = None
        for i, s in enumerate(shots, 1):
            w = weight_on(s["date"])
            rows.append({"n": i, "date": pd.Timestamp(s["date"]), "dose": f"{s['doseMg']:g} mg", "site": s.get("site", ""),
                         "weight": to_disp(w) if w else None, "change": round(to_disp(w) - prev_w, 1) if (w and prev_w is not None) else None, "note": s.get("note", "")})
            if w:
                prev_w = to_disp(w)
        sdf = pd.DataFrame(rows)
        if weights:
            wdf = pd.DataFrame({"date": [pd.Timestamp(d) for d, _ in weights], "weight": [to_disp(w) for _, w in weights]})
            line = alt.Chart(wdf).mark_line(color=TEAL, strokeWidth=3, point=alt.OverlayMarkDef(color=TEAL, size=35)).encode(
                x=alt.X("date:T", title=None), y=alt.Y("weight:Q", title=wl, scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip("date:T", title="Weigh-in"), alt.Tooltip("weight:Q", title=wl)])
            sp = sdf.dropna(subset=["weight"])
            rules = alt.Chart(sdf).mark_rule(strokeDash=[3, 4], opacity=.35, color="#6b7a75").encode(x="date:T")
            pts = alt.Chart(sp).mark_point(shape="triangle-down", filled=True, size=170, opacity=1).encode(
                x="date:T", y="weight:Q", color=alt.Color("dose:N", title="Dose", scale=alt.Scale(scheme="oranges")),
                tooltip=[alt.Tooltip("date:T", title="Shot"), "dose:N", alt.Tooltip("weight:Q", title=wl)])
            st.markdown("**Weight and shots**")
            st.altair_chart((rules + line + pts).properties(height=280), width="stretch")
            st.caption("Triangles are shots, coloured by dose.")
        ch = sdf.dropna(subset=["change"])
        if not ch.empty:
            st.markdown(f"**Weight change between shots ({wl})**")
            bars = alt.Chart(ch).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
                x=alt.X("n:O", title="Shot #"), y=alt.Y("change:Q", title=wl),
                color=alt.condition(alt.datum.change <= 0, alt.value(TEAL), alt.value("#f97316")),
                tooltip=[alt.Tooltip("date:T", title="Shot"), "dose:N", alt.Tooltip("change:Q", title=wl)])
            st.altair_chart(bars.properties(height=200), width="stretch")
        show = sdf.assign(date=sdf["date"].dt.strftime("%b %d, %Y")).rename(
            columns={"n": "#", "date": "Date", "dose": "Dose", "site": "Site", "weight": f"Weight ({wl})", "change": "Change", "note": "Note"})
        st.dataframe(show.iloc[::-1], hide_index=True, width="stretch")
        with fresh_expander("Remove a shot", "rmshot"):
            with st.form("rmshot"):
                pick = st.selectbox("Which one", shots[::-1], index=None, placeholder="Choose a shot",
                                    format_func=lambda s: f"{s['date']} · {s['doseMg']:g} mg · {s.get('site', '')}")
                if st.form_submit_button("Remove") and pick:
                    A.delete(f"/api/shots/{pick['id']}")
                    bump("rmshot")
                    st.rerun()
    st.caption("Not medical advice. Dose changes and side effects are for you and your prescriber.")

# ═════════════ PROGRESS ═════════════
with t_prog:
    rng = st.segmented_control("Range", ["4 weeks", "12 weeks", "All"], default="12 weeks", key="prange")
    frm = {"4 weeks": 28, "12 weeks": 84}.get(rng)
    P = A.get("/api/progress", **({"from": (today - timedelta(days=frm)).isoformat()} if frm else {}))
    if P["body"]:
        df = pd.DataFrame(P["body"]).set_index("date")
        st.markdown("**Weight**")
        st.line_chart(df["weightKg"].dropna().map(to_disp).rename(wl), color=TEAL)
        ms = [c for c in ("waistCm", "hipCm", "neckCm", "chestCm", "armCm", "thighCm") if c in df and df[c].notna().any()]
        if ms:
            st.markdown("**Measurements (cm)**")
            st.line_chart(df[ms].dropna(how="all"))
    if P["nutrition"]:
        nd = pd.DataFrame(P["nutrition"]).set_index("date")
        st.markdown("**Calories**")
        st.bar_chart(nd["kcal"], color=TEAL)
        st.markdown("**Macros (g)**")
        st.line_chart(nd[["protein", "carbs", "fat"]])
    fin = [s for s in P["sessions"] if s["finishedAt"]]
    if fin:
        sd = pd.DataFrame(fin)
        sd["volume"] = sd["volumeKg"].map(lambda v: round(v / LB if imperial else v))
        st.markdown(f"**Workout volume per session ({wl})**")
        st.bar_chart(sd.set_index("date")["volume"], color=TEAL)
        st.caption(f"{len(fin)} workouts finished in this range")
    if not (P["body"] or P["nutrition"] or fin):
        st.info("Log a workout, meal or weigh-in and your graphs appear here.")

# ═════════════ COACH ═════════════
with t_coach:
    wk0 = (today - timedelta(days=today.weekday())).isoformat()
    done_wk = A.app.db.query("SELECT COUNT(*) FROM sessions WHERE finished_at IS NOT NULL AND date >= ?", wk0)[0][0]
    goal_n = plan["weeklyGoal"]
    knee = [json.loads(r[0] or "{}").get("kneePain") for r in
            A.app.db.query("SELECT data FROM sessions WHERE finished_at IS NOT NULL ORDER BY date DESC, id DESC LIMIT 3")]
    knee = [k for k in knee if k is not None]
    prot = A.app.db.query("""SELECT AVG(p) FROM (SELECT SUM(json_extract(nutrients,'$.protein')) p FROM food_log
                             WHERE date >= ? AND date < ? GROUP BY date)""", (today - timedelta(days=7)).isoformat(), today_s)[0][0]
    recent_w = [w for d, w in weights if d >= (today - timedelta(days=14)).isoformat()]
    st.markdown("### Your week")
    with st.container(key="row_coach"):
        k1, k2, k3 = st.columns(3)
        k1.metric("Workouts", f"{done_wk} / {goal_n}")
        k2.metric("Knee pain", f"{sum(knee) / len(knee):.1f}/10" if knee else "–", help="Average of your last 3 finished workouts.")
        k3.metric("Protein/day", f"{round(prot)} g" if prot else "–", help="Average over the whole days you logged food in the last 7 days (today is left out).")
    tips = []
    if done_wk >= goal_n:
        tips.append("You've hit this week's workout goal. Rest, walk and eat well.")
    else:
        tips.append(f"{goal_n - done_wk} more workout{'s' if goal_n - done_wk != 1 else ''} to hit your goal this week. Short and easy still counts.")
    if knee and sum(knee) / len(knee) >= 5:
        tips.append("Knee pain has been 5 or higher. Swap to seated or machine moves, shorten the range, and check with your doctor if it lingers.")
    elif knee and sum(knee) / len(knee) <= 2:
        tips.append("Knees are feeling good, so it's a fine time to add a little weight where you hit the top of the rep range.")
    if prot is not None and prot < 90:
        tips.append("Protein is on the low side. Aim for a palm-sized portion of lean protein at every meal to hold on to muscle while you lose weight.")
    if len(recent_w) >= 2:
        d = recent_w[-1] - recent_w[0]
        tips.append(f"Over the last two weeks your weight moved {'down' if d < 0 else 'up'} {abs(to_disp(d))} {wl}.")
    for t in tips:
        st.markdown(f"- {t}")
    st.markdown("### Ask your trainer")
    if not ai_on:
        st.info("The chat turns on when you add an AI model under More → AI connection. The summary above works without it.")
    hist_c = st.session_state.setdefault("chat", [])
    for m_ in hist_c:
        with st.chat_message(m_["role"]):
            st.write(m_["content"])
    q_ = st.chat_input("Ask about your workout, weights or food", disabled=not ai_on, key="coachq")
    if q_:
        hist_c.append({"role": "user", "content": q_})
        with st.spinner("Thinking…"):
            res = safe(A.post, "/api/ai/chat", {"messages": hist_c})
        if res:
            hist_c.append({"role": "assistant", "content": res["reply"]})
        else:
            hist_c.pop()
        st.rerun()
    if hist_c and st.button("Clear chat"):
        st.session_state.chat = []
        st.rerun()

# ═════════════ MORE ═════════════
with t_more:
    st.markdown("### Profile")
    with st.form("prof"):
        c1, c2 = st.columns(2)
        un = c1.selectbox("Units", ["Imperial (lb, ft, in)", "Metric (kg, cm)"], index=0 if imperial else 1)
        lv = c2.selectbox("Workout level", ["beginner", "intermediate", "expert"], index=["beginner", "intermediate", "expert"].index(level))
        c3, c4 = st.columns(2)
        sx = c3.selectbox("Sex", ["male", "female"], index=None if not profile.get("sex") else ["male", "female"].index(profile["sex"]), placeholder="Choose")
        ag = num(c4.text_input("Age", value=str(profile.get("age") or ""), placeholder="years"))
        if un.startswith("Imperial"):
            h1, h2 = st.columns(2)
            hf = num(h1.text_input("Height: feet", value=str(int(hcm // 2.54 // 12)) if hcm else "", placeholder="ft"))
            hi_ = num(h2.text_input("inches", value=str(round(hcm / 2.54 % 12, 1)) if hcm else "", placeholder="in"))
            new_h = ((hf or 0) * 12 + (hi_ or 0)) * 2.54 if hf else None
        else:
            new_h = num(st.text_input("Height (cm)", value=str(round(hcm, 1)) if hcm else "", placeholder="cm"))
        gw_txt = st.text_input(f"Goal weight ({'lb' if un.startswith('Imperial') else 'kg'})",
                               value=str(round(goal_kg / LB if un.startswith("Imperial") else goal_kg, 1)) if goal_kg else "", placeholder="goal")
        if st.form_submit_button("Save profile", type="primary"):
            gw = num(gw_txt)
            upd = {"units": "imperial" if un.startswith("Imperial") else "metric"}
            if sx: upd["sex"] = sx
            if ag: upd["age"] = int(ag)
            if new_h: upd["heightCm"] = new_h
            if gw: upd["goalWeightKg"] = gw * LB if un.startswith("Imperial") else gw
            if lv != level:
                upd["level"] = lv
                upd["levelHistory"] = (profile.get("levelHistory") or []) + [{"date": today_s, "level": lv}]
            A.put("/api/profile", upd)
            st.rerun()
    st.markdown("### AI connection (optional)")
    st.caption("Powers the meal-photo scan and the trainer chat. Leave it off and everything else still works. "
               "Use an OpenAI-compatible service or a model running on your own computer (Ollama).")
    with st.form("aiconn"):
        prov = st.selectbox("Provider", ["Off", "openai", "ollama"], index=["", "openai", "ollama"].index(ai_cfg.get("provider") or "") if (ai_cfg.get("provider") or "") in ("", "openai", "ollama") else 0,
                            format_func=lambda v: {"Off": "Off", "openai": "OpenAI-compatible", "ollama": "Ollama"}[v])
        burl = st.text_input("Server address (blank for the default)", value=ai_cfg.get("baseUrl") or "", placeholder="https://api.openai.com/v1")
        mdl = st.text_input("Model name", value=ai_cfg.get("model") or "", placeholder="a vision-capable model")
        akey = st.text_input("API key", type="password", placeholder="saved" if ai_cfg.get("apiKeySet") else "paste key")
        c1, c2 = st.columns(2)
        save_ai = c1.form_submit_button("Save", type="primary")
        test_ai = c2.form_submit_button("Test")
    if save_ai or test_ai:
        patch = {"provider": "" if prov == "Off" else prov, "baseUrl": burl.strip(), "model": mdl.strip()}
        if akey:
            patch["apiKey"] = akey
        if save_ai:
            A.put("/api/settings", {"ai": patch})
            st.rerun()
        else:
            r = safe(A.post, "/api/ai/test", {"provider": patch["provider"], "baseUrl": patch["baseUrl"], "model": patch["model"], "apiKey": akey})
            if r and r.get("ok"):
                st.success("Connected." + ("" if r.get("modelFound") else " That model name wasn't in the server's list."))
            elif r:
                st.error(r.get("error") or "Could not connect.")
    st.markdown("### Food database key")
    with st.form("usda"):
        key = st.text_input("USDA key (optional, free at fdc.nal.usda.gov)", type="password",
                            placeholder="saved" if boot["settings"].get("usdaKeySet") else "paste key")
        if st.form_submit_button("Save key") and key:
            A.put("/api/settings", {"usdaKey": key})
            st.success("Saved")
    st.markdown("### Your data")
    st.download_button("Download everything (JSON)", json.dumps(A.get("/api/export"), indent=1), f"meskofit-export-{today_s}.json", "application/json")
    st.download_button("Download database backup (.db)", backup(A.app, None).body, f"meskofit-{today_s}.db", "application/vnd.sqlite3")
    st.caption("On Streamlit Cloud the data resets when the app restarts. Download a backup now and then.")
