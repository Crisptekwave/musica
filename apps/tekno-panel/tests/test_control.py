import pytest
from fastapi.testclient import TestClient

from tekno_panel.server import create_app


class ActRunner:
    def __init__(self, code=0):
        self.calls, self.code = [], code

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return self.code, "ok", "nope" if self.code else ""


@pytest.fixture
def act():
    return ActRunner()


@pytest.fixture
def cl(settings, act):
    app = create_app(settings, start_collector=False, action_runner=act)
    with TestClient(app) as c:
        c.passcode = app.state.auth.passcode
        c.post("/api/login", json={"passcode": c.passcode})
        c.app_ = app
        yield c


def do(c, action, **params):
    return c.post("/api/action", json={"action": action, "params": params}, headers={"x-tekno": "1"})


def test_pause_resume_writes_flag_and_audit(cl, settings, act):
    assert do(cl, "machine.pause", reason="disk check").status_code == 200
    flag = settings.machine_root / "PAUSED"
    assert flag.exists() and "disk check" in flag.read_text()
    assert cl.get("/api/state").json()["machine"]["paused"] is True
    assert do(cl, "machine.resume").json()["result"] == "PAUSED removed" and not flag.exists()
    audit = cl.get("/api/audit").json()["audit"]
    assert [a["action"] for a in audit[:2]] == ["machine.resume", "machine.pause"] and not act.calls


def test_card_actions_use_fixed_hermes_argv(cl, settings, act):
    assert do(cl, "card.note", task_id="t_23436a72", note="check   the\nseams").status_code == 200
    assert act.calls[-1][-5:] == ["comment", "--author", "tekno-control", "t_23436a72", "check the seams"]
    assert do(cl, "card.unblock", task_id="t_23436a72").status_code == 200
    assert act.calls[-1][-2:] == ["unblock", "t_23436a72"]
    assert do(cl, "card.block", task_id="t_23436a72").status_code == 400  # reason required
    assert do(cl, "card.note", task_id="t_x; rm -rf /", note="x").status_code == 400
    assert do(cl, "card.note", task_id="t_23436a72", note="").status_code == 400


def test_danger_needs_passcode(cl, act):
    r = do(cl, "service.restart", service="acestep-api")
    assert r.status_code == 403 and not act.calls
    r = do(cl, "service.restart", service="acestep-api", passcode="000000" if cl.passcode != "000000" else "111111")
    assert r.status_code == 403 and not act.calls
    r = do(cl, "service.restart", service="acestep-api", passcode=cl.passcode)
    assert r.status_code == 200
    assert act.calls[-1][:4] == ["sudo", "-n", "-u", "Gebruiker"] and act.calls[-1][-2:] == ["restart", "acestep-api.service"]
    assert do(cl, "service.restart", service="sshd", passcode=cl.passcode).status_code == 400


def test_lane_run_argv_and_guards(cl, settings, act):
    r = do(cl, "lane.run", card="e2e-001", step="edit", passcode=cl.passcode)
    assert r.status_code == 200, r.text
    argv = act.calls[-1]
    uid = settings.ace_uid
    assert argv[:9] == ["sudo", "-n", "-u", "Gebruiker", "env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus", "bash", "-c"]
    assert "$" in argv[9] and "e2e-001" not in argv[9]  # card/step only as positional args
    assert argv[-3:-1] == ["e2e-001", "edit"] and argv[-1].endswith("/e2e-001/run2.log")
    assert do(cl, "lane.run", card="e2e-001", step="rm", passcode=cl.passcode).status_code == 400
    assert do(cl, "lane.run", card="../etc", step="edit", passcode=cl.passcode).status_code == 400
    (settings.machine_root / "PAUSED").write_text("x")
    assert do(cl, "lane.run", card="e2e-001", step="edit", passcode=cl.passcode).status_code == 409


def test_lane_stop_needs_running_work(cl):
    assert do(cl, "lane.stop", card="e2e-001", passcode=cl.passcode).status_code == 409


def test_failed_command_is_reported_and_audited(settings):
    app = create_app(settings, start_collector=False, action_runner=ActRunner(code=1))
    with TestClient(app) as c:
        c.post("/api/login", json={"passcode": app.state.auth.passcode})
        r = do(c, "card.note", task_id="t_23436a72", note="x")
        assert r.status_code == 502 and "nope" in r.json()["detail"]
        assert c.get("/api/audit").json()["audit"][0]["ok"] is False


def test_rate_limit_and_guards(cl, settings):
    assert cl.post("/api/action", json={"action": "machine.pause"}).status_code == 403  # no x-tekno header
    assert do(cl, "shell.exec", cmd="id").status_code == 404
    codes = [do(cl, "machine.resume").status_code for _ in range(10)]
    assert 429 in codes


def test_controls_need_login_and_can_be_off(settings):
    settings.controls = False
    app = create_app(settings, start_collector=False, action_runner=ActRunner())
    with TestClient(app) as c:
        assert do(c, "machine.pause").status_code == 401
        c.post("/api/login", json={"passcode": app.state.auth.passcode})
        assert do(c, "machine.pause").status_code == 403


def test_demo_actions_do_nothing(cl, settings, act):
    r = cl.post("/api/action", json={"action": "machine.pause", "demo": True}, headers={"x-tekno": "1"})
    assert r.json()["demo"] and not (settings.machine_root / "PAUSED").exists() and not act.calls


def test_lane_run_body_returns_at_once(tmp_path, settings):
    """The launcher must not keep the caller's pipe open while the lane runs (that looked like a failed start)."""
    import subprocess
    import time as _t
    from tekno_panel.control import Controller
    ctrl = Controller(settings, tmp_path / "data")
    argv, _, _ = ctrl._plan("lane.run", {"card": "e2e-001", "step": "edit"}, [])
    body = argv[argv.index("-c") + 1]
    script = tmp_path / "slow.sh"
    script.write_text("sleep 3; echo done \"$1\" \"$2\"\n")
    log = tmp_path / "run.log"
    t0 = _t.time()
    p = subprocess.run(["bash", "-c", body, "tekno-run", str(tmp_path), str(script), "e2e-001", "edit", str(log)],
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 0 and p.stdout.strip() == "started"
    assert _t.time() - t0 < 2
    for _ in range(50):
        if log.exists() and "done" in log.read_text():
            break
        _t.sleep(0.1)
    assert log.read_text().strip() == "done e2e-001 edit"
    bad = subprocess.run(["bash", "-c", body, "tekno-run", str(tmp_path / "missing"), str(script), "a", "b", str(log)],
                         capture_output=True, text=True, timeout=5)
    assert bad.returncode != 0 and "started" not in bad.stdout
