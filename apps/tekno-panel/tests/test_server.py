import subprocess

import pytest
from fastapi.testclient import TestClient

from tekno_panel import review
from tekno_panel.server import create_app


class Runner:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.code, stdout="ok", stderr="boom" if self.code else "")


@pytest.fixture
def runner():
    return Runner()


@pytest.fixture
def client(settings, runner):
    app = create_app(settings, start_collector=False, runner=runner)
    with TestClient(app) as c:
        c.passcode = app.state.auth.passcode
        yield c


def login(c):
    r = c.post("/api/login", json={"passcode": c.passcode})
    assert r.status_code == 200
    return c


def test_requires_login(client):
    assert client.get("/api/state").status_code == 401
    assert client.get("/media/e2e-001/polish/master.wav").status_code == 401
    assert client.get("/api/session").json()["authed"] is False
    # the shell itself is public (no data in it)
    assert client.get("/").status_code == 200
    assert client.get("/manifest.webmanifest").status_code == 200


def test_passcode_file_created(settings, client):
    p = settings.state_dir / "passcode"
    assert p.exists() and len(p.read_text().strip()) == 6
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_wrong_passcode_and_lockout(client):
    for _ in range(5):
        assert client.post("/api/login", json={"passcode": "nope"}).status_code == 401
    # even the right code is refused while locked out
    assert client.post("/api/login", json={"passcode": client.passcode}).status_code == 429


def test_forged_cookie_rejected(client):
    client.cookies.set("tekno_session", "9999999999.AAAA")
    assert client.get("/api/state").status_code == 401


def test_state_live(client):
    s = login(client).get("/api/state").json()
    assert s["mode"] == "live" and s["sources"]["kanban"]["ok"]
    slugs = {t["slug"]: t for t in s["tracks"]}
    assert slugs["dry-001"]["state"] == "failed"
    assert slugs["e2e-001"]["files_lane"] == "Score"
    assert any(a["url"] == "/media/e2e-001/polish/master.wav" for a in s["audio"])
    assert s["reference"]["url"] == "/media-ref"
    assert s["scores"][0]["verdict"] == "NO-GO"
    assert s["health"]["disk"]["ok"]


def test_state_demo(client):
    s = login(client).get("/api/state?demo=1").json()
    assert s["mode"] == "demo" and len(s["tracks"]) == 7


def test_websocket_pushes(client):
    login(client)
    with client.websocket_connect("/ws?demo=1") as ws:
        a = ws.receive_json()
        assert a["mode"] == "demo"


def test_websocket_rejects_anonymous(settings, runner):
    app = create_app(settings, start_collector=False, runner=runner)
    with TestClient(app) as c:
        with pytest.raises(Exception):
            with c.websocket_connect("/ws") as ws:
                ws.receive_json()


def test_media_streaming_and_range(client):
    login(client)
    r = client.get("/media/e2e-001/polish/master.wav")
    assert r.status_code == 200 and r.content.startswith(b"RIFF")
    r = client.get("/media/e2e-001/polish/master.wav", headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.content == b"RIFF"
    assert client.get("/media-ref").status_code == 200


@pytest.mark.parametrize("path", [
    "/media/e2e-001/../secret.txt", "/media/e2e-001/score/score.json", "/media/..%2F/etc/passwd",
    "/media/e2e-001/%2e%2e/%2e%2e/etc/passwd", "/static/../tekno_panel/auth.py",
])
def test_media_traversal_blocked(client, path):
    login(client)
    assert client.get(path).status_code == 404


def test_security_headers(client):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"


def test_review_approve_writes_comment_via_cli(client, runner, settings):
    login(client)
    r = client.post("/api/review/t_aaaa0004", json={"decision": "approve", "note": "acid rolls"},
                    headers={"x-tekno": "1"})
    assert r.status_code == 200, r.text
    argv = runner.calls[-1]
    assert argv[:7] == [str(settings.hermes_bin), "kanban", "--board", "tekno-machine", "comment", "--author", "tekno-control"]
    assert argv[7] == "t_aaaa0004" and argv[8] == "APPROVED via TEKNO CONTROL: acid rolls"


def test_review_reject_needs_note(client, runner):
    login(client)
    r = client.post("/api/review/t_aaaa0004", json={"decision": "reject", "note": "  "}, headers={"x-tekno": "1"})
    assert r.status_code == 400 and not runner.calls
    r = client.post("/api/review/t_aaaa0004", json={"decision": "reject", "note": "kick too thin; rm -rf /"},
                    headers={"x-tekno": "1"})
    assert r.status_code == 200
    assert runner.calls[-1][-1] == "REJECTED via TEKNO CONTROL: kick too thin; rm -rf /"


def test_review_only_on_review_cards(client, runner):
    login(client)
    r = client.post("/api/review/t_c6ac7c7f", json={"decision": "approve"}, headers={"x-tekno": "1"})
    assert r.status_code == 409 and not runner.calls
    r = client.post("/api/review/t_deadbeef", json={"decision": "approve"}, headers={"x-tekno": "1"})
    assert r.status_code == 404
    r = client.post("/api/review/bad;id", json={"decision": "approve"}, headers={"x-tekno": "1"})
    assert r.status_code in (400, 404)


def test_review_needs_csrf_header_and_login(client, runner):
    assert client.post("/api/review/t_aaaa0004", json={"decision": "approve"}).status_code == 401
    login(client)
    assert client.post("/api/review/t_aaaa0004", json={"decision": "approve"}).status_code == 403
    assert not runner.calls


def test_review_cli_failure_surfaces(settings):
    app = create_app(settings, start_collector=False, runner=Runner(code=1))
    with TestClient(app) as c:
        c.post("/api/login", json={"passcode": app.state.auth.passcode})
        r = c.post("/api/review/t_aaaa0004", json={"decision": "approve"}, headers={"x-tekno": "1"})
        assert r.status_code == 502 and "boom" in r.json()["detail"]


def test_review_demo_writes_nothing(client, runner):
    login(client)
    r = client.post("/api/review/t_whatever", json={"decision": "approve", "demo": True}, headers={"x-tekno": "1"})
    assert r.status_code == 200 and r.json()["demo"] is True and not runner.calls


def test_build_comment():
    assert review.build_comment("approve", "") == "APPROVED via TEKNO CONTROL"
    assert review.build_comment("approve", "a\n\nb") == "APPROVED via TEKNO CONTROL: a b"
    with pytest.raises(review.ReviewError):
        review.build_comment("reject", "")
