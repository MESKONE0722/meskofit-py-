"""Password, cookie and middleware behaviour (port of the Go auth.go semantics)."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from meskofit import auth
from meskofit.db import Database
from meskofit.web import HTTPError, Query, Req


class FakeApp:
    def __init__(self, path):
        self.db = Database(path)
        self.login_lock = threading.Lock()
        self.login_fail: dict[str, int] = {}


def req(body=None, cookies=None, https=False):
    return Req("POST", "/", {}, Query(""), {}, cookies or {}, json.dumps(body).encode() if body is not None else b"",
               "1.2.3.4", https, "h")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)


def cookie_of(resp) -> str:
    return resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]


def test_flow(tmp_path):
    a = FakeApp(tmp_path)
    assert auth.handle_status(a, req()) == {"required": False, "ok": True}
    with pytest.raises(HTTPError) as e:
        auth.handle_set_password(a, req({"new": "abc"}))
    assert (e.value.status, e.value.msg) == (400, "use at least 4 characters")
    r = auth.handle_set_password(a, req({"new": "abcd"}, https=True))
    sc = r.headers["set-cookie"]
    assert sc.startswith("mf_auth=") and "Path=/" in sc and "Max-Age=34560000" in sc
    assert "HttpOnly" in sc and "Secure" in sc and "SameSite=Lax" in sc
    c = cookie_of(r)
    assert auth.handle_status(a, req(cookies={"mf_auth": c})) == {"required": True, "ok": True}
    assert auth.handle_status(a, req()) == {"required": True, "ok": False}
    assert auth.handle_status(a, req(cookies={"mf_auth": c[:-2] + "xx"}))["ok"] is False
    with pytest.raises(HTTPError) as e:
        auth.handle_login(a, req({"password": "nope"}))
    assert (e.value.status, e.value.msg) == (401, "wrong password") and a.login_fail == {"1.2.3.4": 1}
    r = auth.handle_login(a, req({"password": "abcd"}))
    assert "Secure" not in r.headers["set-cookie"] and a.login_fail == {}
    assert auth.handle_logout(a, req()).headers["set-cookie"] == "mf_auth=; Path=/; Max-Age=0; HttpOnly"
    with pytest.raises(HTTPError) as e:
        auth.handle_set_password(a, req({"current": "bad", "new": ""}))
    assert e.value.status == 403 and e.value.msg == "current password is wrong"
    assert auth.handle_set_password(a, req({"current": "abcd", "new": ""})) == {"ok": True, "required": False}
    assert auth.auth_config(a) is None


def test_reset_password(tmp_path):
    a = FakeApp(tmp_path)
    auth.handle_set_password(a, req({"new": "abcd"}))
    auth.reset_password(a)
    assert auth.auth_config(a) is None


def test_middleware(tmp_path):
    a = FakeApp(tmp_path)

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = auth.AuthMiddleware(inner, a)

    async def status(path, cookie=None):
        out = []

        async def send(m):
            out.append(m)

        async def rcv():
            return {"type": "http.request"}

        h = [(b"cookie", f"mf_auth={cookie}".encode())] if cookie else []
        await mw({"type": "http", "path": path, "headers": h, "method": "GET", "query_string": b""}, rcv, send)
        return out[0]["status"]

    async def go():
        paths = ["/api/sessions", "/api/auth/logout", "/photos/a.jpg"]
        free = ["/api/auth", "/api/auth/login", "/api/health", "/ca.crt", "/meskofit.mobileconfig", "/", "/assets/a.js", "/img/ex/x/0"]
        assert [await status(p) for p in paths + free] == [200] * 11  # password off: everything open
        c = cookie_of(auth.handle_set_password(a, req({"new": "abcd"})))
        assert [await status(p) for p in paths] == [401] * 3
        assert [await status(p) for p in free] == [200] * 8
        assert [await status(p, c) for p in paths] == [200] * 3

    asyncio.run(go())
