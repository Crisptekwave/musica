import json
import time

from tekno_panel import insights
from tekno_panel.history import History
from tekno_panel.state import Collector


def test_history_samples_and_series(tmp_path):
    h = History(tmp_path / "d" / "history.db")
    t0 = 1_791_700_000
    for i in range(20):
        h.sample({"gpu_util": i, "disk_free": 40 - i, "working": 1.0}, t0 + i * 30)
    s = h.series("1h", now=t0 + 600)
    assert s["t"] and len(s["t"]) == len(s["gpu_util"]) and s["bucket"] == 30
    assert s["disk_free"][-1] == 21 and max(s["gpu_util"]) == 19
    assert h.series("bogus", now=t0)["range"] == "bogus"  # unknown range falls back to 1h buckets


def test_history_events_dedupe_and_filter(tmp_path):
    h = History(tmp_path / "history.db")
    ev = [{"id": "a", "ts": 1.0, "kind": "run", "card": "e2e-001", "text": "run5 → stems"},
          {"id": "b", "ts": 2.0, "kind": "mail", "card": None, "text": "hello"}]
    assert h.add_events(ev) == 2
    assert h.add_events(ev) == 0  # same ids: nothing new
    assert [e["id"] for e in h.events()] == ["b", "a"]
    assert [e["id"] for e in h.events(card="e2e-001")] == ["a"]
    assert [e["id"] for e in h.events(q="stems")] == ["a"]


def test_history_unwritable_dir_is_harmless(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    h = History(f / "history.db")  # parent is a file
    assert not h.ok and h.events() == [] and h.series("1h")["t"] == []
    h.sample({"gpu_util": 1}, time.time())


def test_alerts_rules():
    snap = {"health": {"disk": {"ok": True, "warn": True, "free_gb": 12.0, "warn_gb": 20, "path": "/mnt/agent-data"},
                       "services": [{"name": "litellm", "status": "down", "via": "http down"}],
                       "gpu": {"ok": True, "temp": 88}, "ace": {"last_error": {"at": 1000.0, "text": "CUDA OOM"}},
                       "tracks_today": 6, "tracks_cap": 6},
            "sources": {"mail": {"ok": False, "error": "unreachable"}},
            "tracks": [{"slug": "e2e-001", "state": "failed", "lane": "Score", "current_task": {"last_error": "NO-GO"},
                        "board_behind": True, "board_activity": -3000}]}
    al = insights.alerts(snap, now=1500.0)
    keys = [a["key"] for a in al]
    assert keys[:2] == ["disk", "svc:litellm"]  # critical first
    assert {"src:mail", "gpu-hot", "ace-err", "failed:e2e-001", "behind:e2e-001", "cap"} <= set(keys)


def test_collector_records_activity_and_alerts(settings, cards):
    c = Collector(settings)
    snap = c.refresh()
    assert "alerts" in snap and isinstance(snap["activity"], list)
    assert any(a["key"] == "failed:dry-001" for a in snap["alerts"])
    kinds = {e["kind"] for e in c.history.events(200)}
    assert {"kanban", "render"} <= kinds
    assert snap["sources"]["history"]["ok"]
    json.dumps(snap)
