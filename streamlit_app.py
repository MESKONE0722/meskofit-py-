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

from meskofit import calc
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


def card_html(inner: str) -> None:
    st.markdown(f'<div class="mf-card">{inner}</div>', unsafe_allow_html=True)


gate()
A = api()
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

t_train, t_food, t_body, t_goal, t_shot, t_prog, t_more = st.tabs(["Train", "Food", "Body & BMI", "Goal", "Shots", "Progress", "More"])


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


# ═════════════ TRAIN ═════════════
with t_train:
    active = A.get("/api/sessions/active")
    if active is None:
        st.caption(f"{plan['label']} plan · {plan['weeklyGoal']} workouts a week · {plan['effort']}")
        days = plan["days"] + ([plan["core"]] if plan.get("core") else [])
        pick = st.selectbox("Choose a workout", days, format_func=lambda d: d["name"], index=None, placeholder="Choose a workout")
        if pick:
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
        base = Path(str(resources.files("meskofit") / "data" / "exercise-img"))
        all_rows: list[dict] = []
        changed = False
        for ex in exercises:
            e = cat.get(ex["ex"], {"name": ex["ex"], "kind": "weight"})
            mark = "✅" if ex_done(ex) else "⬜"
            with st.expander(f"{mark}  {e['name']} · {target(ex)}", expanded=(ex["id"] == first_open)):
                last = A.get("/api/history/last", keys=ex["ex"], day=active["dayId"]).get(ex["ex"], {})
                if last.get("last"):
                    st.caption("Last time: " + ", ".join(f"{to_disp(s['weightKg'])} {wl} × {s.get('reps') or '-'}"
                                                         for s in last["last"].get("sets", []) if s.get("weightKg") is not None))
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
                        safe(A.post, "/api/log", {"date": day, "meal": meal, "name": h["name"], "brand": h.get("brand"), "amount": grams,
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
                A.post("/api/log", {"date": day, "meal": ml, "name": n or "Quick add", "amount": 1, "unit": "serving", "source": "quick",
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
