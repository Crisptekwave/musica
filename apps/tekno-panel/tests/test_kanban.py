import sqlite3

import pytest

from tekno_panel import kanban


@pytest.mark.parametrize("title,slug,lane", [
    ("[dry-001] STUDY", "dry-001", "Plan"),
    ("[e2e-001] SONG-PLAN", "e2e-001", "Plan"),
    ("[e2e-001] SOUND-SWAP", "e2e-001", "Sound Swap"),
    ("[e2e-001] MASTER-SCORE", "e2e-001", "Score"),  # blocks on the Score gate
    ("[acid-7] GENERATE: take 2 at 168 BPM", "acid-7", "Generate"),
    ("[x] Variation", "x", "Variation"),
    ("no brackets here", None, None),
])
def test_parse_title(title, slug, lane):
    s, l, _ = kanban.parse_title(title)
    assert (s, l) == (slug, lane)


def test_parse_sendback_real_comment():
    txt = "BLOCKED: DRY RUN SCORE FAIL -> would route: Filter (mid/high stereo decorrelated corr 250-12k -0.05)"
    sb = kanban.parse_sendback(txt, "Score")
    assert sb["from"] == "Score" and sb["to"] == "Filter"
    assert "decorrelated" in sb["reason"]


def test_parse_sendback_multiword_lane():
    sb = kanban.parse_sendback("FAIL: send back to Sound Swap - kick one-shot clicks", "Score")
    assert sb["to"] == "Sound Swap" and "clicks" in sb["reason"]


def test_parse_sendback_ignores_plain_notes():
    assert kanban.parse_sendback("study/brief.json written: 165 BPM, F minor", "Plan") is None
    assert kanban.parse_sendback("", "Plan") is None


def test_read_board_is_read_only(board_db):
    board = kanban.read_board(board_db)
    assert board["ok"] and len(board["tasks"]) == 15
    # the connection helper must refuse writes
    con = kanban._connect(board_db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("insert into task_comments (task_id,author,body,created_at) values ('x','y','z',1)")
    con.close()


def test_read_board_missing(tmp_path):
    b = kanban.read_board(tmp_path / "nope.db")
    assert not b["ok"] and "not found" in b["error"]


def test_build_tracks(board_db):
    tracks = {t["slug"]: t for t in kanban.build_tracks(kanban.read_board(board_db))}
    dry = tracks["dry-001"]
    assert dry["lane"] == "Score" and dry["state"] == "failed"
    # "would route" is a dry-run suggestion: shown, but not counted as a loop
    assert dry["last_sendback"]["to"] == "Filter" and dry["last_sendback"]["hypothetical"] and dry["loops"] == 0
    assert "would route" in dry["current_task"]["last_error"]  # blocked by hand: comment is the reason
    e2e = tracks["e2e-001"]
    assert e2e["lane"] == "Plan" and e2e["state"] == "working" and 0 < e2e["progress"] < 10
    rev = tracks["rev-002"]
    assert rev["lane"] == "Review" and rev["review_task_id"] == "t_aaaa0004"
    assert rev["loops"] >= 1  # MIX ran again after SCORE
    assert rev["last_sendback"]["to"] == "Mix"


def test_lane_activity(board_db):
    lanes = kanban.lane_activity(kanban.read_board(board_db))
    assert lanes["Plan"]["state"] == "working"
    assert lanes["Score"]["state"] == "failed"
    assert lanes["Review"]["state"] == "waiting"
    assert lanes["Filter"]["handoff_to"] == "Mix"  # completed 5 s ago -> particles fly to the child lane
    assert lanes["Generate"]["state"] == "idle"
    assert any("would route" in x["text"] for x in lanes["Score"]["log"])
