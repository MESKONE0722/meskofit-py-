"""Optional app password. Off by default (it's your own network), but worth turning on if other people
share your Wi-Fi or you expose it via a VPN. Port of internal/app/auth.go.

The password is PBKDF2-HMAC-SHA256 hashed in the kv table (key "auth"); a login sets an HMAC-signed
"mf_auth" cookie. ``AuthMiddleware`` returns 401 ``{"error": "login required"}`` for protected paths.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Receive, Scope, Send

from .web import HTTPError, Req, Router, error_resp, json_resp

COOKIE_NAME = "mf_auth"
COOKIE_DAYS = 400
ITERATIONS = 120000

router = Router()
_secret_lock = threading.Lock()


# ───────────────────────── storage / crypto ─────────────────────────

def auth_config(app) -> dict | None:
    """The stored password record ({salt, hash, iter}), or None when no password is set."""
    try:
        d = app.db.kv_get("auth")
    except Exception:  # noqa: BLE001 - Go treats a read error as "off"
        return None
    if not isinstance(d, dict) or not d.get("hash"):
        return None
    return d


def secret(app) -> bytes:
    """The cookie-signing key, generated and stored on first use."""
    with _secret_lock:
        s = app.db.kv_get("secret")
        if isinstance(s, str) and s:
            try:
                b = base64.b64decode(s)
                if len(b) >= 32:
                    return b
            except ValueError:
                pass
        b = os.urandom(32)
        app.db.kv_set("secret", base64.b64encode(b).decode())
        return b


def hash_password(pw: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations, 32)


def _password_ok(d: dict, pw: str) -> bool:
    try:
        salt = base64.b64decode(d.get("salt", ""))
        want = base64.b64decode(d.get("hash", ""))
        return hmac.compare_digest(hash_password(pw, salt, int(d.get("iter", 0))), want)
    except (ValueError, TypeError):
        return False


def token_for(app, d: dict, exp: int) -> str:
    m = hmac.new(secret(app), f"{exp}|{d['hash']}".encode(), hashlib.sha256)
    return f"{exp}." + base64.urlsafe_b64encode(m.digest()).decode().rstrip("=")


def valid_cookie(app, cookies: dict[str, str], d: dict) -> bool:
    """True if the mf_auth cookie is present, unexpired and signed for the current password."""
    v = cookies.get(COOKIE_NAME)
    if not v:
        return False
    exp_s, sep, _ = v.partition(".")
    if not sep:
        return False
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if time.time() > exp:
        return False
    return hmac.compare_digest(token_for(app, d, exp).encode(), v.encode())


def needs_auth(p: str) -> bool:
    """Which paths require the password: all of /api/ except status, login and health, plus /photos/."""
    if p.startswith("/api/"):
        return p not in ("/api/auth", "/api/auth/login", "/api/health")
    return p.startswith("/photos/")


# ───────────────────────── cookies ─────────────────────────

def _set_cookie_header(app, d: dict, secure: bool) -> str:
    exp = int(time.time()) + COOKIE_DAYS * 24 * 3600
    s = f"{COOKIE_NAME}={token_for(app, d, exp)}; Path=/; Max-Age={COOKIE_DAYS * 24 * 3600}; HttpOnly"
    return s + ("; Secure" if secure else "") + "; SameSite=Lax"


_LOGOUT_COOKIE = f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly"


def _with_cookie(obj: Any, cookie: str, status: int = 200):
    r = json_resp(obj, status)
    r.headers.append("set-cookie", cookie)
    return r


# ───────────────────────── routes ─────────────────────────

@router.get("/api/auth")
def handle_status(app, req: Req):
    d = auth_config(app)
    return {"required": d is not None, "ok": d is None or valid_cookie(app, req.cookies, d)}


def _body_str(body: dict, key: str) -> str:
    v = body.get(key, "")
    if v is None:
        return ""
    if not isinstance(v, str):
        raise HTTPError(400, f"invalid JSON body: json: cannot unmarshal {type(v).__name__} into Go struct field .{key} of type string")
    return v


@router.post("/api/auth/login")
def handle_login(app, req: Req):
    body = req.json_obj()
    pw = _body_str(body, "password")
    d = auth_config(app)
    if d is None:
        return {"ok": True}
    ip = req.client_ip
    with app.login_lock:
        fails = app.login_fail.get(ip, 0)
    if fails > 5:
        time.sleep(min(fails, 30) * 0.3)
    if not _password_ok(d, pw):
        with app.login_lock:
            app.login_fail[ip] = app.login_fail.get(ip, 0) + 1
        time.sleep(0.4)
        raise HTTPError(401, "wrong password")
    with app.login_lock:
        app.login_fail.pop(ip, None)
    return _with_cookie({"ok": True}, _set_cookie_header(app, d, req.https))


@router.post("/api/auth/logout")
def handle_logout(app, req: Req):
    return _with_cookie({"ok": True}, _LOGOUT_COOKIE)


@router.post("/api/auth/password")
def handle_set_password(app, req: Req):
    """Set, change or (with an empty new password) remove the password."""
    body = req.json_obj()
    current, new = _body_str(body, "current"), _body_str(body, "new")
    d = auth_config(app)
    if d is not None and not _password_ok(d, current):
        time.sleep(0.4)
        raise HTTPError(403, "current password is wrong")
    if new == "":
        app.db.exec("DELETE FROM kv WHERE key = 'auth'")
        return {"ok": True, "required": False}
    if len(new.encode()) < 4:  # Go counts bytes
        raise HTTPError(400, "use at least 4 characters")
    salt = os.urandom(16)
    nd = {"salt": base64.b64encode(salt).decode(), "hash": "", "iter": ITERATIONS}
    nd["hash"] = base64.b64encode(hash_password(new, salt, ITERATIONS)).decode()
    app.db.kv_set("auth", nd)
    return _with_cookie({"ok": True, "required": True}, _set_cookie_header(app, nd, req.https))


def reset_password(app) -> None:
    """Remove the app password (used by the -reset-password flag)."""
    app.db.exec("DELETE FROM kv WHERE key = 'auth'")


# ───────────────────────── middleware ─────────────────────────

class AuthMiddleware:
    """Pure-ASGI gate: when a password is set, protected paths need a valid mf_auth cookie."""

    def __init__(self, app: ASGIApp, meskofit_app) -> None:
        self.app = app
        self.mf = meskofit_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and needs_auth(scope["path"]):
            if not await run_in_threadpool(self._allowed, scope):
                resp = error_resp(401, "login required")
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _allowed(self, scope: Scope) -> bool:
        d = auth_config(self.mf)
        if d is None:
            return True
        raw = b"; ".join(v for k, v in scope["headers"] if k == b"cookie").decode("latin-1")
        return valid_cookie(self.mf, cookie_parser(raw) if raw else {}, d)
