"""Port of internal/food/food_test.go; all HTTP is served by httpx.MockTransport."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from meskofit.food.off import OFF, NotFound
from meskofit.food.types import barcode_variants, same_barcode, upce_to_upca
from meskofit.food.usda import USDA, RateLimited

TESTDATA = Path(__file__).parent / "testdata"


def near(a: float, b: float) -> bool:
    return abs(a - b) < 0.01


def test_upce():
    assert upce_to_upca("04252614") == "042100005264"
    v = barcode_variants("737628064502")
    assert v[0] == "737628064502" and "0737628064502" in v
    assert same_barcode("00737628064502", "737628064502")


def test_off_product():
    fixture = (TESTDATA / "off_product.json").read_bytes()
    hits = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req)
        assert req.url.path.startswith("/api/v2/product/"), req.url.path
        assert req.headers.get("user-agent")
        if "0000" in req.url.path:
            return httpx.Response(200, json={"status": 0, "status_verbose": "product not found"})
        return httpx.Response(200, content=fixture)

    o = OFF(httpx.Client(transport=httpx.MockTransport(handler)), product_url="https://off.test")
    f = o.product("737628064502")
    assert f.name and f.brand == "Simply Asia" and f.barcode == "0737628064502"
    assert near(f.per100["kcal"], 385) and near(f.per100["protein"], 9.62) and near(f.per100["sodium"], 288)
    assert near(f.perServing["kcal"], 200) and f.servingGrams == 52
    assert "peanuts" in f.allergens and f.nutriscore == "d" and f.nova == 4
    assert "E330" in f.additives
    assert len(f.portions) == 2
    with pytest.raises(NotFound):
        o.product("0000000")


def test_off_search():
    fixture = (TESTDATA / "off_search.json").read_bytes()
    o = OFF(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=fixture))),
            search_url="https://search.test")
    hits = o.search("greek yogurt", 3)
    assert len(hits) == 3
    assert hits[0].brand == "Chobani" and near(hits[0].per100["protein"], 9.41)


def test_off_search_legacy_fallback():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "search.test":
            return httpx.Response(500, text="boom")
        assert req.url.path == "/cgi/search.pl"
        return httpx.Response(200, json={"products": [{"code": "1", "product_name": "Yogurt", "brands": "A, B",
                                                       "nutriments": {"energy-kj_100g": 418.4}}]})

    o = OFF(httpx.Client(transport=httpx.MockTransport(handler)), product_url="https://off.test", search_url="https://search.test")
    hits = o.search("yogurt", 5)
    assert [h.brand for h in hits] == ["A"] and near(hits[0].per100["kcal"], 100)


USDA_SEARCH = """{"totalHits":2,"foods":[
 {"fdcId":168878,"description":"Rice, white, long-grain, regular, enriched, cooked","dataType":"SR Legacy","foodCategory":"Cereal Grains and Pasta",
  "foodNutrients":[{"nutrientId":1003,"nutrientName":"Protein","nutrientNumber":"203","unitName":"G","value":2.69},
   {"nutrientId":1004,"nutrientNumber":"204","unitName":"G","value":0.28},{"nutrientId":1005,"nutrientNumber":"205","unitName":"G","value":28.2},
   {"nutrientId":1008,"nutrientNumber":"208","unitName":"KCAL","value":130},{"nutrientId":1093,"nutrientNumber":"307","unitName":"MG","value":1},
   {"nutrientId":1079,"nutrientNumber":"291","unitName":"G","value":0.4}],
  "foodMeasures":[{"disseminationText":"1 cup","gramWeight":158},{"disseminationText":"Quantity not specified","gramWeight":186}]},
 {"fdcId":2346393,"description":"Chicken breast, roasted","dataType":"Foundation",
  "foodNutrients":[{"nutrientId":1003,"nutrientNumber":"203","unitName":"G","value":31.0},{"nutrientId":2047,"nutrientNumber":"957","unitName":"KCAL","value":165},
  {"nutrientId":2048,"nutrientNumber":"958","unitName":"KCAL","value":158},{"nutrientId":1004,"nutrientNumber":"204","unitName":"G","value":3.6}]}]}"""

USDA_DETAILS = """{"fdcId":169756,"description":"Oats","dataType":"SR Legacy","foodCategory":{"description":"Breakfast Cereals"},
 "foodNutrients":[{"type":"FoodNutrient","nutrient":{"id":1008,"number":"208","name":"Energy","unitName":"kcal"},"amount":389},
  {"type":"FoodNutrient","nutrient":{"id":1003,"number":"203","name":"Protein","unitName":"g"},"amount":16.9},
  {"type":"FoodNutrient","nutrient":{"id":1114,"number":"328","name":"Vitamin D","unitName":"µg"},"amount":0.5},
  {"type":"FoodNutrient","nutrient":{"id":1093,"number":"307","name":"Sodium","unitName":"mg"},"amount":2}],
 "foodPortions":[{"amount":1,"gramWeight":156,"modifier":"cup","measureUnit":{"id":9999,"name":"undetermined","abbreviation":"undetermined"}},
  {"amount":0.5,"gramWeight":40,"modifier":"","measureUnit":{"id":1000,"name":"cup","abbreviation":"cup"}},
  {"gramWeight":240,"modifier":"10205","portionDescription":"1 bowl","measureUnit":{"name":"undetermined"}}]}"""

USDA_BRANDED_SEARCH = """{"foods":[{"fdcId":555,"description":"GREEK YOGURT, NONFAT","dataType":"Branded","brandOwner":"CHOBANI","gtinUpc":"894700010137",
 "servingSize":170,"servingSizeUnit":"g","householdServingFullText":"1 container","foodNutrients":[{"nutrientId":1008,"unitName":"KCAL","value":53},{"nutrientId":1003,"unitName":"G","value":9.4}]}]}"""

USDA_BRANDED_DETAILS = """{"fdcId":555,"description":"GREEK YOGURT, NONFAT","dataType":"Branded","brandOwner":"CHOBANI","gtinUpc":"894700010137",
 "ingredients":"CULTURED NONFAT MILK","servingSize":170,"servingSizeUnit":"g","householdServingFullText":"1 container",
 "foodNutrients":[{"nutrient":{"id":1008,"number":"208","unitName":"kcal"},"amount":53},{"nutrient":{"id":1003,"number":"203","unitName":"g"},"amount":9.41}],
 "labelNutrients":{"calories":{"value":90},"protein":{"value":16}}}"""


def usda_handler(req: httpx.Request) -> httpx.Response:
    assert req.url.params.get("api_key") == "TESTKEY"
    if req.url.path == "/foods/search":
        body = json.loads(req.content)
        if body["dataType"][0] == "Branded":
            return httpx.Response(200, text=USDA_BRANDED_SEARCH)
        if body["query"] == "limit":
            return httpx.Response(429, text='{"error":{"code":"OVER_RATE_LIMIT"}}')
        return httpx.Response(200, text=USDA_SEARCH)
    if req.url.path == "/food/555":
        return httpx.Response(200, text=USDA_BRANDED_DETAILS)
    if req.url.path.startswith("/food/"):
        return httpx.Response(200, text=USDA_DETAILS)
    return httpx.Response(404)


def test_usda():
    u = USDA(httpx.Client(transport=httpx.MockTransport(usda_handler)), lambda: "TESTKEY", base_url="https://usda.test")

    hits = u.generic("rice", 10)
    assert len(hits) == 2
    assert near(hits[0].per100["kcal"], 130) and near(hits[0].per100["carbs"], 28.2)
    assert near(hits[1].per100["kcal"], 158)  # Atwater specific preferred

    foods = u.search_foods("rice", 5)
    assert len(foods[0].portions) == 2 and foods[0].portions[1]["label"] == "Typical portion"

    d = u.details("169756")
    assert near(d.per100["kcal"], 389) and near(d.per100["vitD"], 0.5) and d.category == "Breakfast Cereals"
    assert "|".join(p["label"] for p in d.portions) == "1 cup|0.5 cup|1 bowl"

    b = u.barcode("0894700010137")
    assert b.name == "Greek Yogurt, Nonfat" and b.brand == "Chobani" and b.servingGrams == 170
    assert near(b.perServing["protein"], 16)

    with pytest.raises(RateLimited):
        u.generic("limit", 5)


def test_usda_demo_key_and_bad_id():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params.get("api_key"))
        return httpx.Response(200, text=USDA_SEARCH)

    u = USDA(httpx.Client(transport=httpx.MockTransport(handler)), lambda: "  ", base_url="https://usda.test")
    u.generic("rice", 3)
    assert seen == ["DEMO_KEY"]
    with pytest.raises(NotFound):
        u.details("abc")
