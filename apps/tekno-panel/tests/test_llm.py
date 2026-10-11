import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import llm  # noqa: E402


def server(status, seen):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(status)
            self.end_headers()
            if status == 200:
                self.wfile.write(json.dumps({"choices": [{"message": {"role": "assistant", "content": "hi"}}]}).encode())

        def log_message(self, *a):
            pass
    s = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_port}/v1"


def test_key_comes_from_the_one_hermes_line(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("OTHER=secret\nFREELLMAPI_API_KEY='freellmapi-abc123'\n")
    monkeypatch.delenv("TEKNO_GATEWAY_KEY", raising=False)
    monkeypatch.setattr(llm, "KEY_FILE", f)
    assert llm.gateway_key() == "freellmapi-abc123"
    monkeypatch.setattr(llm, "KEY_FILE", tmp_path / "missing")
    assert llm.gateway_key() == ""


def test_rate_limited_gateway_falls_over_to_local(monkeypatch):
    g_seen, l_seen = [], []
    g, gurl = server(429, g_seen)
    loc, lurl = server(200, l_seen)
    monkeypatch.setattr(llm, "GATEWAY", (gurl, "deepseek-v4-pro"))
    monkeypatch.setattr(llm, "LOCAL", (lurl, "qwen3-coder"))
    monkeypatch.setenv("TEKNO_GATEWAY_KEY", "freellmapi-k")
    msg, used = llm.complete({"messages": []}, ["gateway", "local"])
    assert msg["content"] == "hi" and used == "local:qwen3-coder"
    assert g_seen[0][0] == "Bearer freellmapi-k" and g_seen[0][1]["model"] == "deepseek-v4-pro"
    assert l_seen[0][0] is None  # the key never goes to the local server
    g.shutdown()
    loc.shutdown()


def test_gateway_is_skipped_without_url_or_key_and_all_down_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(llm, "GATEWAY", ("", "auto"))
    assert [e[0] for e in llm.endpoints(["gateway", "local"])] == ["local"]
    monkeypatch.setattr(llm, "GATEWAY", ("http://127.0.0.1:9/v1", "auto"))
    monkeypatch.delenv("TEKNO_GATEWAY_KEY", raising=False)
    monkeypatch.setattr(llm, "KEY_FILE", tmp_path / "none")
    assert [e[0] for e in llm.endpoints(["gateway", "local"])] == ["local"]
    monkeypatch.setattr(llm, "LOCAL", ("http://127.0.0.1:9/v1", "x"))
    with pytest.raises(OSError):
        llm.complete({"messages": []}, ["gateway", "local"], timeout=2)
