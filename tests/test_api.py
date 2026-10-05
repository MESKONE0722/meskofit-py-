"""End-to-end API test (port of the Go TestAPIEndToEnd)."""
from __future__ import annotations

import json

import httpx
import pytest
from starlette.testclient import TestClient

from meskofit.app import App, Options
from meskofit.server import build


def fake_foods(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    if p.startswith(("/api/v2/product/0012345678905", "/api/v2/product/012345678905")):
        return httpx.Response(200, json={"status": 1, "product": {
            "code": "0012345678905", "product_name": "Test Bar", "brands": "Acme", "serving_quantity": 40,
            "serving_size": "1 bar (40 g)", "nutriments": {"energy-kcal_100g": 400, "proteins_100g": 25,
                                                           "carbohydrates_100g": 40, "fat_100g": 15, "sodium_100g": 0.5},
            "ingredients_text": "whey protein, gelatin, sugar"}})
    if p.startswith("/api/v2/product/"):
        return httpx.Response(200, json={"status": 0})
    if p == "/search":
        return httpx.Response(200, json={"hits": [{"code": "0012345678905", "product_name": "Test Bar", "brands": ["Acme"],
                                                   "nutriments": {"energy-kcal_100g": 400}}]})
    if p.endswith("/foods/search"):
        return httpx.Response(200, json={"foods": [{"fdcId": 168878, "description": "Rice, white, cooked", "dataType": "SR Legacy",
                                                    "foodNutrients": [{"nutrientId": 1008, "unitName": "KCAL", "value": 130},
                                                                      {"nutrientId": 1003, "unitName": "G", "value": 2.7}],
                                                    "foodMeasures": [{"disseminationText": "1 cup", "gramWeight": 158}]}]})
    if "/food/" in p:
        return httpx.Response(200, json={"fdcId": 168878, "description": "Rice, white, cooked", "dataType": "SR Legacy",
                                         "foodNutrients": [{"nutrient": {"id": 1008, "unitName": "kcal"}, "amount": 130}],
                                         "foodPortions": [{"amount": 1, "gramWeight": 158, "modifier": "cup",
                                                           "measureUnit": {"name": "undetermined"}}]})
    return httpx.Response(404)


@pytest.fixture()
def env(tmp_path):
    app = App(Options(data_dir=tmp_path, version="test", http_port=8080))
    http = httpx.Client(transport=httpx.MockTransport(fake_foods))
    app.http = app.off.http = app.usda.http = http
    with TestClient(build(app), base_url="http://testserver") as c:
        yield app, c
    app.close()


def test_end_to_end(env):
    a, c = env
    boot = c.get("/api/bootstrap").json()
    assert boot["profile"] is None and boot["plans"]
    assert a.first_run()
    prof = c.put("/api/profile", json={"sex": "male", "heightCm": 182.9, "level": "beginner"}).json()
    assert prof["level"] == "beginner" and not a.first_run()

    # settings: secrets are write-only
    s = c.put("/api/settings", json={"usdaKey": "SECRET", "ai": {"provider": "ollama", "model": "m", "apiKey": "k"}}).json()
    assert s["usdaKey"] == "" and s["usdaKeySet"] is True and s["ai"]["apiKeySet"] is True
    assert a.usda_key() == "SECRET" and a.ai_config().api_key == "k" and a.ai_config().provider == "ollama"
    c.put("/api/settings", json={"ai": {"model": "m2"}})
    assert a.ai_config().api_key == "k" and a.ai_config().model == "m2"

    # plans
    plans = c.get("/api/plans").json()
    beg = plans["levels"]["beginner"]
    assert len(beg["days"]) == 5
    beg["days"][0]["exercises"].append({"id": "x1", "ex": "lib:Cable_Crossover", "sets": 2, "reps": "12"})
    r = c.put("/api/plans/beginner", json=beg)
    assert r.status_code == 200 and len(r.json()["customized"]) == 1
    assert "lib:Cable_Crossover" in c.get("/api/catalog").json()["exercises"]
    assert c.post("/api/plans/beginner/reset").json()["customized"] is None
    assert c.get("/api/library", params={"q": "leg press"}).json()["results"]
    assert c.get("/api/library/Leg_Press").json()["key"] == "lib:Leg_Press"

    # sessions
    r = c.post("/api/sessions", json={"dayId": "chest-triceps", "dayName": "Chest", "level": "beginner", "date": "2026-10-01"})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert c.post("/api/sessions", json={"dayId": "back-biceps", "date": "2026-10-01"}).status_code == 409
    sets = [{"planExId": "ct1", "exKey": "machine-chest-press", "setNo": n, "weightKg": 40.0, "reps": reps, "done": True}
            for n, reps in ((1, 12), (2, 10))]
    s = c.put(f"/api/sessions/{sid}/sets", json={"sets": sets}).json()
    assert s["setsDone"] == 2 and s["volumeKg"] == 880
    s = c.patch(f"/api/sessions/{sid}", json={"data": {"rpe": 6, "kneePain": 2}, "finishedAt": "2026-10-01T18:00:00Z"}).json()
    assert s["finishedAt"] and "kneePain" in json.dumps(s["data"])
    h = c.get("/api/history/last", params={"keys": "machine-chest-press", "day": "chest-triceps"}).json()["machine-chest-press"]
    assert h["last"] is not None and h["best"]["e1rmKg"] >= 55
    assert '"date":"2026-10-01"' in c.get("/api/history/exercise/machine-chest-press").text

    # food
    f = c.get("/api/food/barcode/012345678905").json()
    assert f["name"] == "Test Bar" and f["id"]
    assert c.get("/api/food/barcode/99999999").status_code == 404
    r = c.post("/api/foods", json={"name": "Mom's pastel", "barcode": "0012345678905",
                                   "perServing": {"kcal": 350, "protein": 12, "fat": 14}, "servingLabel": "1 pastel"})
    assert r.status_code == 201 and r.json()["custom"] is True
    assert c.get("/api/food/barcode/012345678905").json()["name"] == "Mom's pastel"
    res = c.get("/api/food/search", params={"q": "rice"}).json()
    assert len(res["generic"]) == 1 and len(res["branded"]) == 1
    item = c.get("/api/food/item/usda/168878").json()
    assert len(item["portions"]) == 1
    r = c.post("/api/log", json={"date": "2026-10-01", "meal": "lunch", "foodId": item["id"], "name": "Rice", "amount": 1.5,
                                 "unit": "portion", "unitLabel": "1 cup", "grams": 237,
                                 "nutrients": {"kcal": 308.1, "protein": 6.4, "fat": 0.7}})
    assert r.status_code == 201
    entry = r.json()
    c.post("/api/log", json={"entries": [{"date": "2026-10-01", "meal": "dinner", "name": "Quick add", "amount": 1,
                                          "unit": "serving", "nutrients": {"kcal": 500, "fat": 20}, "source": "quick"}]})
    day = c.get("/api/log/2026-10-01").json()
    assert len(day["entries"]) == 2
    e = c.patch(f"/api/log/{entry['id']}", json={"amount": 2, "nutrients": {"kcal": 410.8}}).json()
    assert e["amount"] == 2
    assert c.post("/api/log/copy", json={"from": "2026-10-01", "to": "2026-10-02", "meal": "lunch"}).json()["copied"] == 1
    assert "Quick add" in c.get("/api/foods/recent").text
    assert c.put("/api/water/2026-10-01", json={"ml": 1500}).status_code == 200

    # body, fasting, progress
    assert c.put("/api/body/2026-10-01", json={"weightKg": 210.9, "waistCm": 160}).json()["weightKg"] == 210.9
    c.put("/api/body/2026-10-08", json={"weightKg": 208.5})
    assert c.put("/api/body/2026-10-09", json={"weightKg": -1}).status_code == 400
    assert c.put("/api/body/nope", json={"weightKg": 1}).status_code == 400
    r = c.post("/api/fasts/start", json={"targetHours": 16, "startAt": "2026-10-01T00:00:00Z"})
    assert r.status_code == 201
    c.post(f"/api/fasts/{r.json()['id']}/end", json={"endAt": "2026-10-01T16:30:00Z"})
    prog = c.get("/api/progress").json()
    assert (len(prog["body"]), len(prog["sessions"]), len(prog["nutrition"]), len(prog["fasts"])) == (2, 1, 2, 1)
    n0 = prog["nutrition"][0]
    assert n0["kcal"] == pytest.approx(910.8) and n0["maxMealFat"] == 20
    assert prog["fasts"][0]["endAt"] == "2026-10-01T16:30:00Z"

    # photos
    r = c.post("/api/photos", data={"date": "2026-10-01"}, files={"image": ("a.jpg", b"\xff\xd8\xff\xe0\x01\x02\x03", "image/jpeg")})
    assert r.status_code == 201
    ph = r.json()
    assert c.get(ph["url"]).status_code == 200
    assert c.get("/api/photos").json()[0]["id"] == ph["id"]
    assert c.post("/api/photos", files={"image": ("a.jpg", b"not a jpeg", "image/jpeg")}).status_code == 400
    assert c.delete(f"/api/photos/{ph['id']}").status_code == 200
    assert c.get(ph["url"]).status_code == 404

    # export / backup
    r = c.get("/api/export")
    assert r.status_code == 200 and "SECRET" not in r.text and "Mom's pastel" in r.text
    r = c.get("/api/backup")
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3")

    # password
    r = c.post("/api/auth/password", json={"new": "gains"})
    assert r.status_code == 200 and c.cookies.get("mf_auth")
    saved = dict(c.cookies)
    c.cookies.clear()
    assert c.get("/api/bootstrap").status_code == 401
    assert c.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert c.post("/api/auth/login", json={"password": "gains"}).status_code == 200
    assert c.get("/api/bootstrap").status_code == 200
    c.cookies.clear()
    for k, v in saved.items():
        c.cookies.set(k, v)
    assert c.post("/api/auth/password", json={"current": "gains", "new": ""}).status_code == 200
    c.cookies.clear()
    assert c.get("/api/bootstrap").status_code == 200

    # exercise images, web app, unknown routes
    r = c.get("/img/ex/Leg_Press/0.jpg")
    assert r.status_code == 200 and r.content[:3] == b"\xff\xd8\xff"
    assert c.get("/img/ex/..%2F..%2Fetc/0.jpg").headers["content-type"] != "image/jpeg"
    assert c.get("/img/ex/nope/0.jpg").status_code == 404
    assert c.get("/api/nope").json() == {"error": "unknown API endpoint"}
    r = c.get("/progress")
    assert r.status_code == 200 and "<html" in r.text and r.headers["cache-control"] == "no-cache"
    assert r.headers["x-frame-options"] == "DENY"


def test_server_info_and_qr(env):
    _, c = env
    info = c.get("/api/server").json()
    assert info["version"] == "test" and "urls" in info
    assert c.get("/api/qr.svg", params={"data": "http://x"}).text.startswith("<svg")
    assert c.get("/api/qr.svg").status_code == 400
    assert c.get("/ca.crt").status_code == 404  # no certs in this fixture


def test_shots(env):
    _, c = env
    assert c.get("/api/shots").json() == []
    r = c.post("/api/shots", json={"date": "2026-10-02", "doseMg": 2.5, "site": "Abdomen"})
    assert r.status_code == 201 and r.json()["id"] == 1 and r.json()["drug"] == "Mounjaro"
    c.post("/api/shots", json={"date": "2026-09-25", "doseMg": 2.5})
    assert [s["date"] for s in c.get("/api/shots").json()] == ["2026-09-25", "2026-10-02"]
    assert c.post("/api/shots", json={"date": "nope", "doseMg": 2.5}).status_code == 400
    assert "Abdomen" in c.get("/api/export").text
    assert c.delete("/api/shots/1").status_code == 200
    assert len(c.get("/api/shots").json()) == 1


def test_ai_chat_disabled_and_context(tmp_path):
    from meskofit.local import open_local
    from meskofit.web import HTTPError
    l = open_local(str(tmp_path))
    l.put("/api/profile", {"setupDone": True, "level": "beginner"})
    with pytest.raises(HTTPError) as e:
        l.post("/api/ai/chat", {"messages": [{"role": "user", "content": "hi"}]})
    assert e.value.status == 412
    with pytest.raises(HTTPError) as e:
        l.post("/api/ai/chat", {"messages": []})
    assert e.value.status == 400
    l.app.ai.chat = lambda cfg, system, msgs, timeout=None: "Try 5 more lb. " + str("Level: beginner" in system)
    l.put("/api/settings", {"ai": {"provider": "ollama", "model": "m"}})
    assert l.post("/api/ai/chat", {"messages": [{"role": "user", "content": "hi"}]})["reply"].endswith("True")
