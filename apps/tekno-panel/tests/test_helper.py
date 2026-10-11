import os
import socket
import struct
import threading

from tekno_panel import helper
from tekno_panel.control import Controller


def test_helper_only_serves_sudo_actions(settings):
    ctrl = Controller(settings, settings.data_dir)
    r = helper.handle(ctrl, {"action": "machine.pause"})
    assert r["code"] == 126
    r = helper.handle(ctrl, {"action": "lane.run", "params": {"card": "../x", "step": "edit"}}, scan=lambda root: [])
    assert r["code"] == 125 and "unknown card" in r["err"]
    r = helper.handle(ctrl, {"action": "service.restart", "params": {"service": "sshd"}}, scan=lambda root: [])
    assert r["code"] == 125


def test_helper_builds_its_own_argv(settings, monkeypatch):
    ctrl = Controller(settings, settings.data_dir)
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return type("P", (), {"returncode": 0, "stdout": "started", "stderr": ""})()
    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    r = helper.handle(ctrl, {"action": "lane.run", "params": {"card": "e2e-001", "step": "mix", "argv": ["rm", "-rf", "/"]}},
                      scan=lambda root: [])
    assert r["code"] == 0 and seen["argv"][:4] == ["sudo", "-n", "-u", "Gebruiker"] and "rm" not in seen["argv"]
    assert seen["argv"][6].startswith("umask 022;")


def test_client_round_trip(tmp_path):
    path = tmp_path / "h.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def one():
        c, _ = srv.accept()
        with c:
            pid, uid, gid = struct.unpack("3i", c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            assert uid == os.getuid()
            assert b"service.restart" in c.recv(4096)
            c.sendall(b'{"code": 0, "out": "ok", "err": ""}\n')
    t = threading.Thread(target=one)
    t.start()
    assert helper.client("service.restart", {"service": "litellm"}, path) == (0, "ok", "")
    t.join()
    srv.close()
    code, _, err = helper.client("service.restart", {}, tmp_path / "missing.sock")
    assert code == 127 and "helper not running" in err


def test_panel_uses_helper_for_sudo_only(settings):
    calls = []
    ctrl = Controller(settings, settings.data_dir, runner=lambda argv: (calls.append(("run", argv)) or (0, "", "")),
                      helper=lambda name, params: (calls.append(("helper", name, params)) or (0, "started", "")))
    ok = lambda code: True
    ctrl.run("lane.run", {"card": "e2e-001", "step": "edit", "passcode": "x"}, [], "ip", ok)
    ctrl.run("card.note", {"task_id": "t_23436a72", "note": "hi"}, [], "ip", ok)
    assert calls[0] == ("helper", "lane.run", {"card": "e2e-001", "step": "edit"})  # no passcode forwarded
    assert calls[1][0] == "run" and calls[1][1][-1] == "hi"
