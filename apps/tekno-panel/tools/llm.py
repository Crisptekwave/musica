"""Chat completions with failover between the local vLLM (Qwen) and the FreeLLMAPI gateway (Ryan's free-tier router).

Both speak the OpenAI API. `complete(body, order)` tries each endpoint in `order` ("local", "gateway") and returns the
first answer; an endpoint that is down, rate-limited or erroring is skipped. The gateway key is read from the one
FREELLMAPI_API_KEY line in Hermes' env file (or TEKNO_GATEWAY_KEY), so it is never copied anywhere. Only text goes to
the gateway: never secrets, and free providers may log what they get.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

LOCAL = (os.environ.get("TEKNO_FALLBACK_LLM", "http://127.0.0.1:8001/v1"), os.environ.get("TEKNO_FALLBACK_MODEL", "qwen3-coder"))
GATEWAY = (os.environ.get("TEKNO_GATEWAY_LLM", ""), os.environ.get("TEKNO_GATEWAY_MODEL", "auto"))
KEY_FILE = Path(os.environ.get("TEKNO_GATEWAY_KEY_FILE", os.path.expanduser("~/.hermes/.env")))


def gateway_key() -> str:
    if os.environ.get("TEKNO_GATEWAY_KEY"):
        return os.environ["TEKNO_GATEWAY_KEY"]
    try:
        m = re.search(r"^FREELLMAPI_API_KEY=[\"']?([^\"'\s]+)", KEY_FILE.read_text(), re.M)
    except OSError:
        return ""
    return m.group(1) if m else ""


def endpoints(order: list[str]) -> list[tuple[str, str, str, str]]:
    """(name, base url, model, key) for each configured endpoint, in order."""
    out = []
    for name in order:
        if name == "local":
            out.append(("local", *LOCAL, ""))
        elif name == "gateway" and GATEWAY[0]:
            key = gateway_key()
            if key:
                out.append(("gateway", *GATEWAY, key))
    return out


def up(order: list[str]) -> bool:
    for _, url, _, key in endpoints(order):
        req = urllib.request.Request(url + "/models", headers={"Authorization": f"Bearer {key}"} if key else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    return True
        except (OSError, ValueError):
            pass
    return False


def complete(body: dict, order: list[str], timeout: int = 300) -> tuple[dict, str]:
    """First answer from the endpoints in order: (message, "<endpoint>:<model>"). Raises the last error if all fail."""
    err: Exception = OSError("no LLM endpoint configured")
    for name, url, model, key in endpoints(order):
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})}
        req = urllib.request.Request(url + "/chat/completions", json.dumps({**body, "model": model}).encode(), headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())["choices"][0]["message"], f"{name}:{model}"
        except (OSError, ValueError, KeyError, IndexError) as e:  # down, 429/5xx (HTTPError is an OSError), bad reply
            err = e
    raise err
