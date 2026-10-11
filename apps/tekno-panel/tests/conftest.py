import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, status TEXT NOT NULL,
  priority INTEGER DEFAULT 0, created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
  workspace_kind TEXT NOT NULL DEFAULT 'scratch', consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_failure_error TEXT, current_run_id INTEGER);
CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL, PRIMARY KEY (parent_id, child_id));
CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, author TEXT NOT NULL,
  body TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER,
  kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, profile TEXT, step_key TEXT,
  status TEXT NOT NULL, started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT, error TEXT);
"""

# Mirrors the real board on the VM (dry-001 blocked at Score, e2e-001 waiting at Study) plus a track in review.
TASKS = [
    ("t_b7322c26", "[dry-001] STUDY", "done"), ("t_cebebd6c", "[dry-001] GENERATE", "done"),
    ("t_e5cbb61d", "[dry-001] EDIT", "done"), ("t_e861a585", "[dry-001] FILTER", "done"),
    ("t_baaacaab", "[dry-001] MIX", "done"), ("t_c8c2b215", "[dry-001] POLISH", "done"),
    ("t_c6ac7c7f", "[dry-001] SCORE", "blocked"),
    ("t_24e3186b", "[e2e-001] STUDY", "running"), ("t_30970f13", "[e2e-001] SONG-PLAN", "todo"),
    ("t_3c117fa1", "[e2e-001] GENERATE", "todo"), ("t_23436a72", "[e2e-001] MASTER-SCORE", "todo"),
    ("t_aaaa0001", "[rev-002] MIX", "done"), ("t_aaaa0002", "[rev-002] SCORE", "done"),
    ("t_aaaa0003", "[rev-002] MIX: loop back", "done"), ("t_aaaa0004", "[rev-002] REVIEW", "review"),
]


@pytest.fixture
def board_db(tmp_path):
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    now = int(time.time())
    for i, (tid, title, status) in enumerate(TASKS):
        con.execute("insert into tasks (id,title,status,created_at,completed_at) values (?,?,?,?,?)",
                    (tid, title, status, now - 1000 + i, now - 900 + i if status == "done" else None))
    for a, b in zip(TASKS, TASKS[1:]):
        if a[1].split("]")[0] == b[1].split("]")[0]:
            con.execute("insert into task_links values (?,?)", (a[0], b[0]))
    con.execute("insert into task_comments (task_id,author,body,created_at) values (?,?,?,?)",
                ("t_c6ac7c7f", "default", "BLOCKED: DRY RUN SCORE FAIL -> would route: Filter (mid/high stereo "
                 "decorrelated corr 250-12k -0.05, mono loss -2.25 dB)", now - 100))
    con.execute("insert into task_runs (task_id,status,started_at,ended_at,outcome,summary) values (?,?,?,?,?,?)",
                ("t_aaaa0002", "done", now - 500, now - 490, "completed", "SCORE FAIL: route to Mix: knock -4 dB"))
    con.execute("insert into task_events (task_id,kind,created_at) values (?,?,?)", ("t_e861a585", "completed", now - 5))
    con.commit()
    con.close()
    return db


SCORE = {
    "LUFS_I": -9.8, "true_peak_dBTP": -1.1, "bpm_acf_4_16_64": [165.045, 165.045, 167.635],
    "key_top3": [["F minor", 0.38], ["F major", 0.31]], "key_target": "F minor",
    "corr": {"250-12k": 0.897, "full": 0.995}, "sub_S_over_M_dB": -54.6,
    "earloop": "not measured (EarLoop hook stubbed)", "auto_draft_overall": 6.4,
    "hard_fails": ["bpm [165.045, 165.045, 167.635]", "LUFS -9.8 not -9 +-0.5"], "PASS": False, "verdict": "NO-GO",
    "duration_s": 185.2, "EAR": ["pumping"],
}


@pytest.fixture
def cards(tmp_path):
    root = tmp_path / "cards"
    c = root / "e2e-001"
    for d in ("study", "plan", "gen", "stems", "swap", "var", "mix", "polish", "score"):
        (c / d).mkdir(parents=True)
    (c / "study" / "brief.json").write_text("{}")
    (c / "gen" / "0_intro.flac").write_bytes(b"fLaC" + b"\0" * 64)
    (c / "stems" / "bass.wav").write_bytes(b"RIFF" + b"\0" * 64)
    (c / "polish" / "master.wav").write_bytes(b"RIFF" + b"\0" * 64)
    (c / "score" / "score.json").write_text(json.dumps(SCORE))
    (c / "run.log").write_text("=== plan\n=== gen\n")
    d = root / "dry-001"
    (d / "gen").mkdir(parents=True)
    (d / "gen" / "take_1.flac").write_bytes(b"fLaC")
    (d / "gen" / "take_1.json").write_text("{}")
    (root / "secret.txt").write_text("nope")
    return root


@pytest.fixture
def settings(tmp_path, board_db, cards):
    from tekno_panel.config import Settings
    machine = tmp_path / "machine"
    (machine / "tools").mkdir(parents=True)
    (machine / "tools" / "SIFRES_remake_v02_in.mp3").write_bytes(b"ID3" + b"\0" * 32)
    return Settings(
        state_dir=tmp_path / "state", hermes_home=tmp_path / "hermes", kanban_db=board_db, cards_dir=cards,
        machine_root=machine, disk_path=tmp_path, ace_url="http://127.0.0.1:9", ace_log=tmp_path / "none.log",
        agent_mail_url="http://127.0.0.1:9/mcp/", services=[], hermes_bin=tmp_path / "hermes-bin",
        data_dir=tmp_path / "data",
    )
