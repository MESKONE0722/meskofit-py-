"""Tests for meskofit.ai using httpx.MockTransport (no network)."""
import json

import httpx
import pytest

from meskofit import ai


def make_client(handler) -> ai.Client:
    return ai.Client(httpx.Client(transport=httpx.MockTransport(handler)))


def test_extract_json():
    cases = {
        '{"a":1}': '{"a":1}',
        '```json\n{"a":1}\n```': '{"a":1}',
        'Sure! Here: {"a":{"b":2}} ': '{"a":{"b":2}}',
    }
    for inp, want in cases.items():
        assert ai.extract_json(inp) == want
    assert ai.extract_json("no json here") is None


def test_ollama():
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5vl:7b"}]})
        assert r.url.path == "/api/chat"
        body = json.loads(r.content)
        assert len(body["messages"][1]["images"]) == 1 and body["format"] and body["keep_alive"] == "15m"
        content = json.dumps({"items": [{"name": "rice", "grams": 150}]})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})

    cfg = ai.Config("ollama", "http://x/v1", "qwen2.5vl:7b")
    cl = make_client(handler)
    assert cl.models(cfg)[0] == "qwen2.5vl:7b"
    assert "rice" in cl.vision_json(cfg, "sys", "prompt", b"\xff\xd8", {"type": "object"})


def test_openai_retry_without_format():
    calls = []

    def handler(r: httpx.Request) -> httpx.Response:
        assert r.headers["authorization"] == "Bearer k"
        calls.append(1)
        if "response_format" in json.loads(r.content):
            return httpx.Response(400, json={"error": {"message": "response_format not supported"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"calories":120}\n```'}}]})

    cfg = ai.Config("openai", "http://x", "m", "k")
    assert make_client(handler).vision_json(cfg, "s", "p", b"\x01") == '{"calories":120}'
    assert len(calls) == 2


def test_http_error_message():
    e = ai.HTTPError(500, '{"error":{"message":"boom"}}')
    assert str(e) == "AI server answered 500: boom"


def test_disabled():
    with pytest.raises(ai.ErrDisabled):
        make_client(lambda r: httpx.Response(500)).vision_json(ai.Config(), "", "", b"")
    with pytest.raises(ai.ErrDisabled):
        make_client(lambda r: httpx.Response(500)).models(ai.Config())
