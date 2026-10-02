"""MeskoFit on Streamlit: the same data and logic as the web app, with a Streamlit interface.

Run:  streamlit run streamlit_app.py
Data lives in ./meskofit-data (or $MESKOFIT_DATA). Set a password with $MESKOFIT_PASSWORD or the
Streamlit secret `password`. On Streamlit Community Cloud the disk is wiped on restart: use the
Backup download on the Settings tab, or host it somewhere with a persistent disk.
"""
from __future__ import annotations

import hmac
import os
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from meskofit.local import Local, open_local
from meskofit.web import HTTPError

LB = 0.45359237
st.set_page_config(page_title="MeskoFit", page_icon="💪", layout="centered")


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
    entered = st.text_input("Password", type="password")
    if entered:
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


gate()
A = api()
boot = A.get("/api/bootstrap")
profile = boot["profile"] or {}
today = date.today().isoformat()

# ───── units ─────
imperial = st.sidebar.radio("Units", ["lb / in", "kg / cm"], index=0 if profile.get("units", "imperial") == "imperial" else 1) == "lb / in"
wl = "lb" if imperial else "kg"
to_disp = (lambda kg: round(kg / LB, 1)) if imperial else (lambda kg: round(kg, 1))
to_kg = (lambda v: v * LB) if imperial else (lambda v: v)
st.sidebar.caption(f"MeskoFit {boot['version']}")

st.title("💪 MeskoFit")
if not profile.get("setupDone"):
    st.subheader("Set up")
    with st.form("setup"):
        name = st.text_input("Name (optional)", profile.get("name", ""))
        level = st.selectbox("Level", ["beginner", "intermediate", "expert"])
        c1, c2 = st.columns(2)
        h = c1.number_input("Height (in)" if imperial else "Height (cm)", 40.0, 100.0 if imperial else 250.0, 72.0 if imperial else 183.0)
        w = c2.number_input(f"Current weight ({wl})", 50.0, 900.0, 200.0)
        gw = st.number_input(f"Goal weight ({wl})", 50.0, 900.0, 180.0)
        age = st.number_input("Age", 14, 100, 44)
        if st.form_submit_button("Save and start", type="primary"):
            hcm = h * 2.54 if imperial else h
            A.put("/api/profile", {"setupDone": True, "name": name, "level": level, "heightCm": hcm, "age": int(age),
                                   "units": "imperial" if imperial else "metric", "goalWeightKg": to_kg(gw),
                                   "startWeightKg": to_kg(w), "startDate": today,
                                   "levelHistory": [{"date": today, "level": level}]})
            A.put(f"/api/body/{today}", {"weightKg": to_kg(w)})
            st.rerun()
    st.stop()

level = profile.get("level") or (profile.get("levelHistory") or [{}])[-1].get("level", "beginner")
plans = boot["plans"]["levels"]
plan = plans.get(level, plans["beginner"])
cat = A.get("/api/catalog")["exercises"]
t_train, t_food, t_body, t_prog, t_set = st.tabs(["Train", "Food", "Body", "Progress", "Settings"])

# ═════════════ TRAIN ═════════════
with t_train:
    active = A.get("/api/sessions/active")
    if active is None:
        st.caption(f"{plan['label']} plan, {plan['weeklyGoal']} workouts a week. {plan['effort']}")
        days = plan["days"] + ([plan["core"]] if plan.get("core") else [])
        pick = st.selectbox("Workout", days, format_func=lambda d: d["name"])
        w = pick
        for ex in w["exercises"]:
            e = cat.get(ex["ex"], {"name": ex["ex"]})
            st.write(f"**{e['name']}** — {ex['sets']} × {ex['reps']}")
        if st.button("Start workout", type="primary"):
            safe(A.post, "/api/sessions", {"dayId": w["id"], "dayName": w["name"], "level": level, "date": today})
            st.rerun()
    else:
        day = next((d for d in plan["days"] + [plan.get("core") or {}] if d.get("id") == active["dayId"]), None)
        st.subheader(active["dayName"])
        saved = {(s["planExId"], s["setNo"]): s for s in active.get("sets", [])}
        rows_by_ex = {}
        for ex in (day or {"exercises": []})["exercises"]:
            e = cat.get(ex["ex"], {"name": ex["ex"], "kind": "weight"})
            with st.expander(f"{e['name']}  ({ex['sets']} × {ex['reps']})", expanded=True):
                if e.get("steps"):
                    st.caption(e["steps"][0])
                imgs = [f"/img/ex/{e['lib']}/{i}.jpg" for i in e.get("frames", [])] if e.get("lib") else []
                if imgs:
                    from importlib import resources
                    from pathlib import Path
                    base = Path(str(resources.files("meskofit") / "data" / "exercise-img"))
                    files = [base / e["lib"] / f"{i}.jpg" for i in e.get("frames", [])]
                    files = [str(f) for f in files if f.is_file()]
                    if files:
                        st.image(files, width=150)
                last = A.get("/api/history/last", keys=ex["ex"], day=active["dayId"]).get(ex["ex"], {})
                if last.get("last"):
                    st.caption("Last time: " + ", ".join(
                        f"{to_disp(s['weightKg'])} {wl} × {s['reps']}" for s in last["last"].get("sets", []) if s.get("weightKg") is not None))
                df = pd.DataFrame([{
                    "Set": n,
                    f"Weight ({wl})": to_disp(saved[(ex['id'], n)]["weightKg"]) if (ex["id"], n) in saved and saved[(ex["id"], n)].get("weightKg") is not None else 0.0,
                    "Reps": saved[(ex["id"], n)].get("reps") or 0 if (ex["id"], n) in saved else 0,
                    "Done": saved[(ex["id"], n)]["done"] if (ex["id"], n) in saved else False,
                } for n in range(1, int(ex["sets"]) + 1)])
                rows_by_ex[ex["id"]] = (ex, st.data_editor(df, hide_index=True, key=f"ed-{active['id']}-{ex['id']}", width="stretch",
                                                          disabled=["Set"], num_rows="fixed"))
        k = st.slider("Knee pain today (0–10)", 0, 10, int(active["data"].get("kneePain", 0)))
        rpe = st.slider("How hard was it? (RPE 1–10)", 1, 10, int(active["data"].get("rpe", 6)))
        c1, c2, c3 = st.columns(3)
        if c1.button("Save sets"):
            sets = [{"planExId": ex["id"], "exKey": ex["ex"], "setNo": int(r["Set"]), "weightKg": to_kg(float(r[f"Weight ({wl})"])) or None,
                     "reps": int(r["Reps"]) or None, "done": bool(r["Done"])} for ex, df in rows_by_ex.values() for _, r in df.iterrows()]
            safe(A.put, f"/api/sessions/{active['id']}/sets", {"sets": sets})
            st.success("Saved")
        if c2.button("Finish workout", type="primary"):
            sets = [{"planExId": ex["id"], "exKey": ex["ex"], "setNo": int(r["Set"]), "weightKg": to_kg(float(r[f"Weight ({wl})"])) or None,
                     "reps": int(r["Reps"]) or None, "done": bool(r["Done"])} for ex, df in rows_by_ex.values() for _, r in df.iterrows()]
            safe(A.put, f"/api/sessions/{active['id']}/sets", {"sets": sets})
            safe(A.patch, f"/api/sessions/{active['id']}", {"data": {"kneePain": k, "rpe": rpe},
                                                           "finishedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
            st.rerun()
        if c3.button("Discard"):
            A.delete(f"/api/sessions/{active['id']}")
            st.rerun()

# ═════════════ FOOD ═════════════
with t_food:
    day = st.date_input("Day", date.today(), key="foodday").isoformat()
    log = A.get(f"/api/log/{day}")
    meals = boot["settings"]["meals"]
    tot = {k: sum((e["nutrients"] or {}).get(k, 0) for e in log["entries"]) for k in ("kcal", "protein", "carbs", "fat")}
    m = st.columns(4)
    for col, (k, lab) in zip(m, [("kcal", "kcal"), ("protein", "Protein g"), ("carbs", "Carbs g"), ("fat", "Fat g")]):
        col.metric(lab, round(tot[k]))
    for meal in meals:
        es = [e for e in log["entries"] if e["meal"].lower() == meal.lower()]
        if es:
            st.markdown(f"**{meal}**")
        for e in es:
            c1, c2 = st.columns([6, 1])
            c1.write(f"{e['name']} — {e['amount']:g} {e.get('unitLabel') or e['unit']} · {round((e['nutrients'] or {}).get('kcal', 0))} kcal")
            if c2.button("✕", key=f"del{e['id']}"):
                A.delete(f"/api/log/{e['id']}")
                st.rerun()
    st.divider()
    st.markdown("**Add food**")
    q = st.text_input("Search by name or type a barcode", key="fq")
    if q and len(q) >= 2:
        res = safe(A.get, "/api/food/search", q=q.replace(" ", "+"))
        if res:
            hits = res["local"] + res["branded"] + res["generic"]
            for k, v in (res.get("errors") or {}).items():
                st.caption(f"{k}: {v}")
            if hits:
                h = st.selectbox("Result", hits, format_func=lambda h: f"{h['name']}" + (f" ({h['brand']})" if h.get("brand") else "") +
                                 (f" — {round(h['per100'].get('kcal', 0))} kcal/100g" if h.get("per100") else ""))
                c1, c2 = st.columns(2)
                grams = c1.number_input("Grams", 1.0, 5000.0, 100.0, 10.0)
                meal = c2.selectbox("Meal", meals)
                if st.button("Add to log", type="primary"):
                    per = h.get("per100") or {}
                    nut = {k: round(v * grams / 100, 1) for k, v in per.items() if isinstance(v, (int, float))}
                    safe(A.post, "/api/log", {"date": day, "meal": meal, "name": h["name"], "brand": h.get("brand"), "amount": grams,
                                              "unit": "g", "grams": grams, "nutrients": nut, "source": h["source"],
                                              **({"foodId": h["localId"]} if h.get("localId") else {})})
                    st.rerun()
            else:
                st.info("No matches")
    with st.expander("Quick add calories"):
        with st.form("quick", clear_on_submit=True):
            n = st.text_input("What", "Quick add")
            c = st.columns(4)
            kc, pr, ca, fa = c[0].number_input("kcal", 0, 5000, 0), c[1].number_input("P g", 0, 500, 0), c[2].number_input("C g", 0, 800, 0), c[3].number_input("F g", 0, 500, 0)
            ml = st.selectbox("Meal", meals, key="qm")
            if st.form_submit_button("Add") and kc:
                A.post("/api/log", {"date": day, "meal": ml, "name": n, "amount": 1, "unit": "serving", "source": "quick",
                                    "nutrients": {"kcal": kc, "protein": pr, "carbs": ca, "fat": fa}})
                st.rerun()
    water = st.number_input("Water today (ml)", 0, 10000, 0, 250)
    if st.button("Save water"):
        A.put(f"/api/water/{day}", {"ml": water})
        st.success("Saved")

# ═════════════ BODY ═════════════
with t_body:
    body = A.get("/api/body")
    with st.form("body"):
        d = st.date_input("Date", date.today(), key="bd").isoformat()
        last = body[-1] if body else {}
        w = st.number_input(f"Weight ({wl})", 0.0, 900.0, to_disp(last["weightKg"]) if last.get("weightKg") else 0.0, 0.1)
        c1, c2, c3 = st.columns(3)
        waist, neck, hip = c1.number_input("Waist cm", 0.0, 300.0, 0.0), c2.number_input("Neck cm", 0.0, 100.0, 0.0), c3.number_input("Hip cm", 0.0, 300.0, 0.0)
        if st.form_submit_button("Save", type="primary"):
            payload = {k: v for k, v in {"weightKg": to_kg(w) if w else None, "waistCm": waist or None, "neckCm": neck or None, "hipCm": hip or None}.items() if v}
            if payload:
                existing = next((e for e in body if e["date"] == d), {})
                safe(A.put, f"/api/body/{d}", {**{k: v for k, v in existing.items() if k not in ("date",)}, **payload})
                st.rerun()
    h_m = (profile.get("heightCm") or 0) / 100
    if body and h_m and body[-1].get("weightKg"):
        st.metric("BMI", round(body[-1]["weightKg"] / h_m**2, 1))
    st.markdown("**Fasting**")
    fasts = A.get("/api/fasts")
    open_f = next((f for f in fasts if not f["endAt"]), None)
    if open_f:
        st.write(f"Fasting since {open_f['startAt']} (goal {open_f['targetHours']:g} h)")
        if st.button("End fast"):
            A.post(f"/api/fasts/{open_f['id']}/end", {})
            st.rerun()
    elif st.button("Start fast"):
        A.post("/api/fasts/start", {"targetHours": boot["settings"]["fastingHours"]})
        st.rerun()

# ═════════════ PROGRESS ═════════════
with t_prog:
    rng = st.radio("Range", ["4 weeks", "12 weeks", "All"], horizontal=True, index=1)
    frm = {"4 weeks": 28, "12 weeks": 84}.get(rng)
    P = A.get("/api/progress", **({"from": (date.today() - timedelta(days=frm)).isoformat()} if frm else {}))
    if P["body"]:
        df = pd.DataFrame(P["body"]).set_index("date")
        st.markdown("**Weight**")
        st.line_chart(df["weightKg"].dropna().map(to_disp).rename(wl))
        meas = [c for c in ("waistCm", "hipCm", "neckCm", "chestCm", "armCm", "thighCm") if c in df and df[c].notna().any()]
        if meas:
            st.markdown("**Measurements (cm)**")
            st.line_chart(df[meas].dropna(how="all"))
    if P["nutrition"]:
        nd = pd.DataFrame(P["nutrition"]).set_index("date")
        st.markdown("**Calories**")
        st.bar_chart(nd["kcal"])
        st.markdown("**Macros (g)**")
        st.line_chart(nd[["protein", "carbs", "fat"]])
    if P["sessions"]:
        sd = pd.DataFrame([s for s in P["sessions"] if s["finishedAt"]])
        if not sd.empty:
            st.markdown("**Workouts: volume per session**")
            sd["volume"] = sd["volumeKg"].map(lambda v: round(v / LB if imperial else v))
            st.bar_chart(sd.set_index("date")["volume"])
            st.caption(f"{len(sd)} workouts finished in this range")
    if not (P["body"] or P["nutrition"] or P["sessions"]):
        st.info("Log a workout, meal or weigh-in and your graphs appear here.")

# ═════════════ SETTINGS ═════════════
with t_set:
    lv = st.selectbox("Level", ["beginner", "intermediate", "expert"], index=["beginner", "intermediate", "expert"].index(level))
    if lv != level and st.button("Switch level"):
        hist = (profile.get("levelHistory") or []) + [{"date": today, "level": lv}]
        A.put("/api/profile", {"level": lv, "levelHistory": hist})
        st.rerun()
    key = st.text_input("USDA food key (optional, free at fdc.nal.usda.gov)", type="password")
    if st.button("Save key") and key:
        A.put("/api/settings", {"usdaKey": key})
        st.success("Saved")
    import json
    st.download_button("Download JSON export", json.dumps(A.get("/api/export"), indent=1), f"meskofit-export-{today}.json", "application/json")
    from meskofit.routes_body import backup

    if st.button("Prepare database backup"):
        r = backup(A.app, None)
        st.download_button("Download backup (.db)", r.body, f"meskofit-{today}.db", "application/vnd.sqlite3")
