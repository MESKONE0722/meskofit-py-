"""Send photos to a vision-capable language model and ask for JSON back.

Two dialects are spoken:

* "ollama": Ollama's native /api/chat (JSON-schema constrained output)
* "openai": any OpenAI-compatible /chat/completions endpoint
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class Config:
    provider: str = ""  # "", "ollama", "openai"
    base_url: str = ""
    model: str = ""
    api_key: str = ""

    def enabled(self) -> bool:
        return self.provider in ("ollama", "openai") and self.model.strip() != ""

    def base(self) -> str:
        b = self.base_url.strip().rstrip("/")
        if b == "":
            return "http://localhost:11434" if self.provider == "ollama" else "http://localhost:1234/v1"
        if self.provider == "ollama":
            b = b.removesuffix("/v1").removesuffix("/api")
        return b


class AIError(Exception):
    """Any failure talking to the AI server."""


class ErrDisabled(AIError):
    """Raised when no provider is configured."""

    def __init__(self, msg: str = "no AI vision model is set up — add one in Settings → AI photo analysis"):
        super().__init__(msg)


class HTTPError(AIError):
    """The AI server answered with a non-2xx status."""

    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(self._message())

    def _message(self) -> str:
        msg = self.body
        try:
            err = json.loads(self.body).get("error")
        except (ValueError, AttributeError):
            err = None
        if isinstance(err, str):
            msg = err
        elif isinstance(err, dict) and isinstance(err.get("message"), str):
            msg = err["message"]
        return f"AI server answered {self.status}: {msg[:300]}"


def extract_json(s: str) -> str | None:
    """Pull the first JSON object out of a reply that may include code fences or chatter."""
    s = s.strip()
    if s.startswith("{") and _valid(s):
        return s
    start = s.find("{")
    end = s.rfind("}")
    while start >= 0 and end > start:
        cand = s[start : end + 1]
        if _valid(cand):
            return cand
        end = s.rfind("}", 0, end)
    return None


def _valid(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except ValueError:
        return False


class Client:
    """Talks to the configured vision model. Local models can be slow, so the default timeout is 4 minutes."""

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=240)

    def models(self, cfg: Config, timeout: float | None = None) -> list[str]:
        """List the models the endpoint offers."""
        if cfg.provider == "ollama":
            resp = self._do(cfg, "GET", cfg.base() + "/api/tags", None, timeout)
            return [m.get("name", "") for m in (resp.get("models") or [])]
        if cfg.provider == "openai":
            resp = self._do(cfg, "GET", cfg.base() + "/models", None, timeout)
            return [m.get("id", "") for m in (resp.get("data") or [])]
        raise ErrDisabled()

    def vision_json(
        self, cfg: Config, system: str, prompt: str, jpeg: bytes, schema: dict | None = None, timeout: float | None = None
    ) -> str:
        """Send one JPEG plus instructions and return the model's JSON object as text."""
        if not cfg.enabled():
            raise ErrDisabled()
        img = base64.b64encode(jpeg).decode()
        content = ""
        if cfg.provider == "ollama":
            req = {
                "model": cfg.model,
                "stream": False,
                "keep_alive": "15m",
                "options": {"temperature": 0.1},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt, "images": [img]},
                ],
                "format": schema if schema is not None else "json",
            }
            resp = self._do(cfg, "POST", cfg.base() + "/api/chat", req, timeout)
            content = ((resp.get("message") or {}).get("content")) or ""
        else:

            def build(with_format: bool) -> dict:
                r: dict[str, Any] = {
                    "model": cfg.model,
                    "temperature": 0.1,
                    "messages": [
                        {"role": "system", "content": system},
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img}},
                            ],
                        },
                    ],
                }
                if with_format:
                    r["response_format"] = {"type": "json_object"}
                return r

            url = cfg.base() + "/chat/completions"
            try:
                resp = self._do(cfg, "POST", url, build(True), timeout)
            except HTTPError as e:
                if e.status != 400:
                    raise
                # Some servers reject json_object; ask again relying on the prompt alone.
                resp = self._do(cfg, "POST", url, build(False), timeout)
            choices = resp.get("choices") or []
            if not choices:
                raise AIError("the model returned no answer")
            content = ((choices[0].get("message") or {}).get("content")) or ""
        raw = extract_json(content)
        if raw is None:
            raise AIError(f"the model did not return JSON: {content[:200]}")
        return raw

    def chat(self, cfg: Config, system: str, messages: list[dict], timeout: float | None = None) -> str:
        """Plain text conversation: `messages` are {"role": "user"|"assistant", "content": str}."""
        if not cfg.enabled():
            raise ErrDisabled()
        msgs = [{"role": "system", "content": system}, *messages]
        if cfg.provider == "ollama":
            resp = self._do(cfg, "POST", cfg.base() + "/api/chat",
                            {"model": cfg.model, "stream": False, "keep_alive": "15m", "messages": msgs}, timeout)
            content = ((resp.get("message") or {}).get("content")) or ""
        else:
            resp = self._do(cfg, "POST", cfg.base() + "/chat/completions",
                            {"model": cfg.model, "temperature": 0.4, "messages": msgs}, timeout)
            choices = resp.get("choices") or []
            if not choices:
                raise AIError("the model returned no answer")
            content = ((choices[0].get("message") or {}).get("content")) or ""
        if not content.strip():
            raise AIError("the model returned an empty answer")
        return content.strip()

    def _do(self, cfg: Config, method: str, url: str, body: Any, timeout: float | None) -> dict:
        headers = {"Content-Type": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = "Bearer " + cfg.api_key
        kw: dict[str, Any] = {}
        if timeout is not None:
            kw["timeout"] = timeout
        try:
            resp = self.http.request(
                method, url, headers=headers, content=None if body is None else json.dumps(body).encode(), **kw
            )
        except httpx.HTTPError as e:
            raise AIError(f"could not reach the AI server at {cfg.base()} — is it running? ({e})") from e
        data = resp.content[: 8 << 20]
        if resp.status_code >= 300:
            raise HTTPError(resp.status_code, data.decode("utf-8", "replace"))
        try:
            out = json.loads(data)
        except ValueError as e:
            raise AIError(f"bad response from the AI server: {e}") from e
        return out if isinstance(out, dict) else {}
