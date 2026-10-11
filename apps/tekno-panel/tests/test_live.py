"""Live-data parsers, checked against samples copied from the real tekno machine (2026-10-11)."""
import json
import os
import time

import pytest

from tekno_panel import acelog, files, machine, mail, procs
from tekno_panel.state import Collector, apply_processes, merge_tracks

CARDS = "/mnt/agent-data/tekno_machine/cards"
S = "/mnt/agent-data/tekno_machine/scripts"
PY = "/mnt/agent-data/ace-step/tools-venv/bin/python"

# score.json as score_card.py wrote it for e2e-001 (run5, 11:43:34), trimmed
REAL_SCORE = {
    "file": f"{CARDS}/e2e-001/polish/master.wav", "duration_s": 186.181, "sr": 44100, "subtype": "PCM_24",
    "LUFS_I": -9.2, "LRA": 2.2, "true_peak_dBTP": -1.1, "bpm_acf_4_16_64": [165.045, 164.965, 167.595],
    "key_top3": [["F minor", 0.434], ["F major", 0.401], ["C major", 0.274]], "bpm_target": 165.0,
    "key_target": "F minor", "key_target_score": 0.434, "key_parallel_score": 0.401,
    "corr": {"20-120": 1.0, "120-250": 0.998, "250-1k": 0.975, "1k-4k": 0.794, "4k-12k": 0.454, "250-12k": 0.876,
             "full": 0.987},
    "sub_S_over_M_dB": -54.1, "fair": "== master.wav  grid t0 35.9 ms\n", "fair_sub_median": 9.05,
    "fair_knock100_250_median": -8.850000000000001,
    "vs_sifres": {"band_diff_dB_vs_SIFRES": {"sub 30-80": 0.4, "low 80-250": 0.6, "lowmid 250-800": -2.1,
                                             "mid 800-2k": -3.6, "high 2-6k": -3.0, "air 6-20k": -6.4},
                  "band_rms_dist_dB": 3.36, "hf_tilt_dB": -4.7, "corr_1-4k": 0.794, "corr_1-4k_ref": 0.374,
                  "crest_dB": 7.7, "crest_ref_dB": 9.3},
    "earloop": "not measured (EarLoop hook stubbed)",
    "auto_draft_scores": {"sound_fit": 4.2, "groove": 4.6, "darkness": 8.0, "acid": 6.0, "cleanliness": 7.5,
                          "originality": 6.0},
    "auto_draft_overall": 6.0, "EAR": ["acid identity/evolution", "originality"],
    "hard_fails": ["bpm [np.float64(165.045), np.float64(164.965), np.float64(167.595)]"], "PASS": False,
    "verdict": "NO-GO",
}

RUN5 = """=== edit 11:39:39
[{"section": "intro", "start_s": 0.016, "bars": 16, "kick_found": true}]
=== stems 11:39:41
{"drums_rel_dB": -3.0, "bass_rel_dB": -4.3, "other_rel_dB": -14.3, "vocals_rel_dB": -18.5, "vocal_screen": "PASS"}
=== swap 11:41:25
=== var 11:41:32
=== mix 11:41:35
=== master 11:41:45
iter 7.7 -3.2 -9.2 -1.1 -2.75
{"mode": "polish", "LUFS_I": -9.2, "TP": -1.1}
{"verdict": "NO-GO", "fails": ["bpm [np.float64(165.045), np.float64(164.965), np.float64(167.595)]"], "overall_draft": 6.0, "cats": {}}
score exit 1
"""

ACE_LOG = (
    "2026-10-11 11:28:24.826 | INFO     | acestep.core.generation.handler.lora.lifecycle:add_lora:304 - LoRA adapter 'v3ep30' loaded from /mnt/agent-data/ace-step/lora_v3/ckpt_links/v3ep30 (adapters=['v3ep30'], targets=256)\n"
    "2026-10-11 11:28:24.971 | INFO     | acestep.core.generation.handler.lora.controls:set_lora_scale:169 - LoRA scale for 'v3ep30' set to 1.00 (modified=256)\n"
    "2026-10-11 11:31:40.830 | INFO     | acestep.inference:generate_music:662 - [generate_music] LLM usage decision: thinking=True\n"
    "2026-10-11 11:31:40.831 | INFO     | acestep.inference:generate_music:720 - LM chunk 1/1 (infer_type=llm_dit) (size: 1, seeds: [449701072])\n"
    "2026-10-11 11:31:40.831 | INFO     | acestep.llm_inference:generate_with_stop_condition:1482 - Phase 2: Generating audio codes...\n"
    "Generating:   0%|          | 0/1 [00:14<?, ?steps/s, Prefill=3455tok/s, Decode=16tok/s]\r"
    "Generating: 100%|##########| 1/1 [00:15<00:00, 15.55s/steps]\n"
    "2026-10-11 11:31:56.385 | INFO     | acestep.llm_inference:generate_with_stop_condition:1650 - Phase 2 completed in 15.55s. Generated 247 audio codes\n"
    "2026-10-11 11:31:56.400 | INFO     | acestep.core.generation.handler.generate_music:generate_music:329 - [generate_music] Starting generation...\n"
    "2026-10-11 11:32:01.838 | INFO     | acestep.core.generation.handler.service_generate_execute:_execute_service_generate_diffusion:276 - [service_generate] DiT diffusion via PyTorch (cuda)...\n"
    "\r  0%|          | 0/50 [00:00<?, ?steps/s]\r 50%|#####     | 25/50 [00:09<00:09,  2.57steps/s]"
)
ACE_DONE = (
    "\r100%|##########| 50/50 [00:19<00:00,  2.57steps/s]\n"
    "2026-10-11 11:32:21.287 | INFO     | acestep.core.generation.handler.generate_music_decode:_prepare:42 - [generate_music] Model generation completed. Decoding latents...\n"
    "2026-10-11 11:32:24.471 | INFO     | acestep.core.generation.handler.generate_music_payload:_build:50 - [generate_music] Done! Generated 1 audio tensors.\n"
    "2026-10-11 11:32:25.694 | DEBUG    | acestep.audio_utils:save_audio:319 - [AudioSaver] Saved audio to /mnt/agent-data/ace-step/ACE-Step-1.5/acestep/ui/gradio_outputs/api_1791718300/c67ca950-9a7f-ccf6-80c1-09e519c8017a.flac (flac, 48000Hz)\n"
)


# ---------------------------------------------------------------- processes
@pytest.mark.parametrize("argv,script,lanes,card", [
    ([PY, f"{S}/song_plan.py", f"{CARDS}/e2e-001"], "song_plan.py", ["Plan"], "e2e-001"),
    ([PY, f"{S}/ace_sections.py", f"{CARDS}/e2e-001", "--seed", "7"], "ace_sections.py", ["Generate"], "e2e-001"),
    (["python3", f"{S}/regen_exact.py", f"{CARDS}/e2e-001", "--only", "drop1"], "regen_exact.py", ["Generate"], "e2e-001"),
    ([PY, f"{S}/assemble.py", f"{CARDS}/e2e-001"], "assemble.py", ["Edit"], "e2e-001"),
    ([PY, f"{S}/stems.py", f"{CARDS}/acid-7"], "stems.py", ["Stems"], "acid-7"),
    (["/mnt/agent-data/tekno_machine/venv-demucs/bin/python", "-m", "demucs", "-n", "htdemucs", "-o",
      f"{CARDS}/e2e-001/stems/_demucs", f"{CARDS}/e2e-001/edit/edit.wav"], "demucs", ["Stems"], "e2e-001"),
    ([PY, f"{S}/sound_swap.py", f"{CARDS}/e2e-001"], "sound_swap.py", ["Sound Swap"], "e2e-001"),
    ([PY, f"{S}/variation.py", f"{CARDS}/e2e-001"], "variation.py", ["Variation"], "e2e-001"),
    ([PY, f"{S}/mix_stems.py", f"{CARDS}/e2e-001", "2", "-1.5"], "mix_stems.py", ["Mix"], "e2e-001"),
    ([PY, f"{S}/lane_dsp.py", "polish", f"{CARDS}/e2e-001/mix/premaster.wav", f"{CARDS}/e2e-001/polish/master.wav"],
     "lane_dsp.py polish", ["Polish", "Master"], "e2e-001"),
    ([PY, f"{S}/lane_dsp.py", "filter", f"{CARDS}/dry-001/edit/edit.wav", f"{CARDS}/dry-001/filter/filter.wav"],
     "lane_dsp.py filter", ["Filter"], "dry-001"),
    ([PY, f"{S}/score_card.py", f"{CARDS}/e2e-001", f"{CARDS}/e2e-001/polish/master.wav"], "score_card.py", ["Score"], "e2e-001"),
    ([PY, f"{S}/fair_card.py", f"{CARDS}/e2e-001/polish/master.wav", "165"], "fair_card.py", ["Score"], "e2e-001"),
])
def test_classify_lane_scripts(argv, script, lanes, card):
    c = procs.classify(argv)
    assert (c["script"], c["lanes"], c["card"]) == (script, lanes, card)


def test_classify_orchestrator_worker_and_noise():
    c = procs.classify(["bash", f"{S}/run_e2e.sh", "e2e-001", "edit"])
    assert c["orchestrator"] and c["card"] == "e2e-001" and c["lanes"] == []
    w = procs.classify(["/home/k/.hermes/venv/bin/python", "hermes", "-p", "tekno-mix", "--cli", "chat", "-q",
                        "work kanban task t_6e39fc6f"])
    assert w["lanes"] == ["Mix"] and w["task"] == "t_6e39fc6f" and w["worker"]
    assert procs.classify([PY, f"{S}/lane_dsp.py", "bogus", "a", "b"]) is None
    assert procs.classify(["uv", "run", "acestep", "--server-name", "127.0.0.1"]) is None
    assert procs.classify(["vim", "scripts/notes.txt"]) is None


def _fake_proc(root, pid, argv, start_ticks=1000):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    fields = ["S", "1"] + ["0"] * 17 + [str(start_ticks)] + ["0"] * 10
    (d / "stat").write_text(f"{pid} (python (x)) " + " ".join(fields))


def test_scan_fake_proc(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "stat").write_text("cpu 1 2 3\nbtime 1791700000\n")
    _fake_proc(root, 4100, [PY, f"{S}/stems.py", f"{CARDS}/e2e-001"], start_ticks=500)
    _fake_proc(root, 4200, ["/usr/bin/ffmpeg", "-i", "/mnt/agent-data/tekno_machine/tmp_l.wav"])
    _fake_proc(root, 4300, [PY, f"{S}/score_card.py", f"{CARDS}/dry-001", "x.wav"], start_ticks=900)
    _fake_proc(root, 4400, ["bash", "-c", "echo hi"])
    (root / "4500").mkdir()  # vanished / unreadable process
    out = procs.scan(root, skip_pids={4300})
    assert [p["pid"] for p in out] == [4100]
    assert out[0]["since"] == pytest.approx(1791700000 + 500 / os.sysconf("SC_CLK_TCK"))


def test_gpu_lock_parsing_tag(tmp_path):
    lock = tmp_path / ".gpu.lock"
    lock.write_text("8714 /mnt/agent-data/tekno_machine/cards/e2e-001 regen\n")
    root = tmp_path / "proc"
    (root / "8714").mkdir(parents=True)  # no /proc/locks in this fake root -> fall back to "pid alive"
    g = procs.gpu_lock(lock, root)
    assert g["card"] == "e2e-001" and g["tag"] == "regen" and g["pid"] == 8714


# ---------------------------------------------------------------- ACE log + journal
def test_ace_log_take_in_flight():
    lines = [x for x in __import__("re").split(r"[\r\n]+", ACE_LOG) if x.strip()]
    p = acelog.parse(lines)
    assert p["job"]["seed"] == "449701072" and p["job"]["phase"] == "dit"
    assert (p["job"]["step"], p["job"]["total"]) == (25, 50)
    assert acelog.take_progress(p["job"], time.time()) == 60
    assert p["lora"] == {"name": "v3ep30", "scale": 1.0, "at": p["lora"]["at"]}
    assert any("LM done in 15.6 s" in e["text"] for e in p["events"])


def test_ace_log_take_saved():
    lines = [x for x in __import__("re").split(r"[\r\n]+", ACE_LOG + ACE_DONE) if x.strip()]
    p = acelog.parse(lines)
    assert p["job"] is None
    assert p["last_done"]["seed"] == "449701072" and p["last_done"]["took_s"] == pytest.approx(44.9, abs=0.1)
    assert p["events"][-1]["text"].startswith("take saved")


def test_ace_state_from_fresh_log(tmp_path):
    log = tmp_path / "acestep.log"
    log.write_text(ACE_LOG)
    a = machine.ace("http://127.0.0.1:9", tmp_path / "nolock", log)
    assert a["status"] == "generating" and a["progress"] == 60 and a["job"]["seed"] == "449701072"
    old = time.time() - 600
    os.utime(log, (old, old))  # log went quiet: the take is not running any more
    a = machine.ace("http://127.0.0.1:9", tmp_path / "nolock", log)
    assert a["status"] == "down" and a["job"] is None


def test_ace_state_from_lane_process(tmp_path):
    proc = {"pid": 9, "script": "ace_sections.py", "lanes": ["Generate"], "card": "e2e-001", "since": 5.0}
    a = machine.ace("http://127.0.0.1:9", tmp_path / "nolock", tmp_path / "none", [proc])
    assert a["status"] == "generating" and a["job"]["card"] == "e2e-001" and a["job"]["script"] == "ace_sections.py"


class _Runner:
    def __init__(self, out, code=0):
        self.out, self.code, self.argv = out, code, None

    def __call__(self, argv, **kw):
        self.argv = argv
        return type("P", (), {"returncode": self.code, "stdout": self.out})()


def test_journal_lifecycle_and_lora_status(tmp_path):
    rows = [
        {"__REALTIME_TIMESTAMP": "1791718000000000", "USER_UNIT": "acestep-lora.service",
         "MESSAGE": "Starting acestep-lora.service - Load default ACE-Step LoRA (v3 ep30, scale 1.0, trigger hermestek)..."},
        {"__REALTIME_TIMESTAMP": "1791718228000000", "USER_UNIT": "acestep-lora.service",
         "MESSAGE": "Finished acestep-lora.service - Load default ACE-Step LoRA (v3 ep30, scale 1.0, trigger hermestek)."},
        {"__REALTIME_TIMESTAMP": "1791717999000000", "USER_UNIT": "acestep-api.service",
         "MESSAGE": "Started acestep-api.service - ACE-Step 1.5 API+Gradio (cuda, XL-SFT + 4B LM, 127.0.0.1:8011)."},
    ]
    r = _Runner("\n".join(json.dumps(x) for x in rows) + "\nnot json\n")
    j = acelog.journal(["acestep-api.service", "acestep-lora.service"], runner=r)
    assert "--user" not in r.argv and "USER_UNIT=acestep-api.service" in r.argv and "+" in r.argv
    assert [x["unit"] for x in j] == ["acestep-api.service", "acestep-lora.service", "acestep-lora.service"]
    assert acelog.unit_state(j, "acestep-lora.service")["verb"] == "Finished"
    log = tmp_path / "lora_default.log"
    log.write_text('load_lora event: complete | data: ["\\u2705 LoRA \'v3ep30\' loaded from /x/v3ep30"]\n'
                   'set_use_lora event: complete | data: ["\\u2705 LoRA enabled"]\n')
    lora = acelog.lora_log(log)
    assert lora["ok"] and "v3ep30" in lora["text"]
    s = machine.service({"name": "acestep-lora", "journal": "acestep-lora.service", "lora_log": True}, j, lora)
    assert s["status"] == "up" and "finished" in s["via"]
    log.write_text('load_lora event: complete | data: ["\\u274c LoRA loading is not supported on quantized models."]\n')
    s = machine.service({"name": "acestep-lora", "journal": "acestep-lora.service", "lora_log": True}, j,
                        acelog.lora_log(log))
    assert s["status"] == "failed"
    assert acelog.journal(["x.service"], runner=_Runner("", code=1)) == []


def test_cgroup_service_of_other_user(tmp_path):
    cg = tmp_path / "cg" / "user.slice" / "user-1001.slice" / "user@1001.service" / "app.slice" / "acestep-api.service"
    cg.mkdir(parents=True)
    (cg / "cgroup.procs").write_text("1828\n3243\n")
    s = machine.service({"name": "acestep-api", "cgroup": "acestep-api.service"}, [], None, tmp_path / "cg")
    assert s["status"] == "up" and s["via"] == "cgroup:acestep-api.service"
    (cg / "cgroup.procs").write_text("")
    assert acelog.cgroup_active(tmp_path / "cg", "acestep-api.service") is False


# ---------------------------------------------------------------- card files, handoff, scores
@pytest.fixture
def machine_tree(tmp_path):
    root = tmp_path / "tekno_machine"
    c = root / "cards" / "e2e-001"
    for d in ("plan", "gen", "edit", "stems", "swap", "var", "mix", "polish", "score", "attempt2/score",
              "attempt2/polish", "attempt3/score", "attempt1"):
        (c / d).mkdir(parents=True)
    (c / "plan" / "plan.json").write_text(json.dumps({
        "bpm": 165.0, "key": "F minor", "bar_s": 1.45, "total_bars": 128, "total_s": 186.18,
        "sections": [{"name": "intro", "bars": 16, "start_bar": 1, "energy": 4, "gen_seconds": 26.18, "prompt": "x"},
                     {"name": "drop1", "bars": 32, "start_bar": 33, "energy": 9, "gen_seconds": 49.45, "prompt": "y"}],
        "energy_curve": [4, 9], "variation_rule": "a change every 16-32 bars"}))
    (c / "var" / "report.json").write_text(json.dumps({"events": ["fill bar 16", "riser bars 29-32"],
                                                       "max_bars_without_change": 16}))
    (c / "polish" / "report.json").write_text(json.dumps({"mode": "polish", "in": "/x", "out": "/y",
                                                          "limiter_max_gr_dB": -2.75, "LUFS_I": -9.2, "TP": -1.1}))
    (c / "stems" / "report.json").write_text(json.dumps({"vocal_screen": "PASS", "vocals_rel_dB": -18.5}))
    for f in ("polish/master.wav", "mix/premaster.wav", "var/kick.wav", "gen/2_drop1_r1_s685754911.flac"):
        (c / f).write_bytes(b"RIFF" + b"\0" * 32)
    (c / "score" / "score.json").write_text(json.dumps(REAL_SCORE))
    old = dict(REAL_SCORE, LUFS_I=-9.8, auto_draft_overall=6.4, vs_sifres="not measured: TypeError float32")
    (c / "attempt1" / "score.json").write_text(json.dumps(old))
    a2 = dict(REAL_SCORE, auto_draft_overall=5.4, fair_knock100_250_median=-9.1)
    (c / "attempt2" / "score" / "score.json").write_text(json.dumps(a2))
    (c / "attempt3" / "score" / "score.json").write_text(json.dumps(dict(REAL_SCORE, auto_draft_overall=5.9)))
    for n, txt in (("run.log", "=== plan 09:30:35\nscore exit 1\n"), ("run5.log", RUN5)):
        (c / n).write_text(txt)
    h = root / "handoff" / "e2e-001_tekno_165_Fmin"
    (h / "stems").mkdir(parents=True)
    (h / "master.wav").write_bytes(b"RIFF" + b"\0" * 32)
    (h / "stems" / "kick.wav").write_bytes(b"RIFF" + b"\0" * 32)
    (h / "score.json").write_text(json.dumps(a2))  # handoff.py copies the card's score at hand-off time
    (h / "HANDOFF.md").write_text("# HANDOFF: e2e-001_tekno_165_Fmin\n- Master: master.wav, -9.2 LUFS-I\n")
    t0 = time.time()
    os.utime(h / "master.wav", (t0 - 7000, t0 - 7000))
    os.utime(h / "stems" / "kick.wav", (t0 - 7000, t0 - 7000))
    for f in ("mix/premaster.wav", "var/kick.wav", "gen/2_drop1_r1_s685754911.flac"):
        os.utime(c / f, (t0 - 200, t0 - 200))
    os.utime(c / "polish" / "master.wav", (t0 - 100, t0 - 100))
    os.utime(c / "run.log", (t0 - 9000, t0 - 9000))
    for i, f in enumerate(("attempt1/score.json", "attempt2/score/score.json", "attempt3/score/score.json")):
        os.utime(c / f, (t0 - 8000 + i * 100, t0 - 8000 + i * 100))
    os.utime(h / "score.json", (t0 - 7000, t0 - 7000))
    return root


def test_real_score_badges(machine_tree):
    sc = files.card_score(machine_tree / "cards" / "e2e-001")
    b = {x["label"]: x for x in sc["badges"]}
    assert sc["verdict"] == "NO-GO" and not sc["go"]
    assert b["BPM"]["status"] == "fail" and b["BPM"]["value"] == 164.97 and "167.595" in b["BPM"]["detail"]
    assert b["Key"]["status"] == "pass" and "0.434" in b["Key"]["detail"]
    assert b["LUFS-I"]["status"] == "pass" and b["True peak"]["status"] == "pass"
    assert b["Sub mono S/M"]["status"] == "pass"
    assert b["Stereo corr"]["status"] == "pass" and b["Stereo corr"]["value"] == 0.987
    assert b["Kick knock"]["status"] == "warn" and b["Kick knock"]["value"] == -8.9
    assert b["vs SIFRES"]["status"] == "info" and b["vs SIFRES"]["value"] == "3.4 dB"
    assert b["Monotony"]["status"] == "pass" and b["Monotony"]["value"] == "16 bars"
    assert b["Mix Judge"]["status"] == "na" and b["Mix Judge"]["draft"] == 6.0
    assert b["EarLoop"]["status"] == "na"
    assert sc["ref"]["bands"]["air 6-20k"] == -6.4 and sc["ref"]["crest_ref"] == 9.3
    assert sc["metrics"]["knock"] < -8 and sc["metrics"]["ref_dist"] == 3.36
    assert sc["hard_fails"] == ["bpm [165.045, 164.965, 167.595]"]


def test_ref_compare_error_string():
    b = {x["label"]: x for x in files.score_badges(__import__("pathlib").Path("/nonexistent"),
                                                    dict(REAL_SCORE, vs_sifres="not measured: boom"))}
    assert b["vs SIFRES"]["status"] == "na" and "boom" in b["vs SIFRES"]["detail"]


def test_scan_cards_plan_runs_handoff_history(machine_tree):
    d = files.scan(machine_tree / "cards", machine_tree / "handoff")
    info = d["cards"]["e2e-001"]
    assert info["plan"]["bpm"] == 165.0 and [s["name"] for s in info["plan"]["sections"]] == ["intro", "drop1"]
    assert info["reports"]["var"]["max_bars_without_change"] == 16 and info["reports"]["var"]["events"] == 2
    assert "in" not in info["reports"]["polish"] and info["reports"]["polish"]["limiter_max_gr_dB"] == -2.75
    runs = info["runs"]
    assert [r["name"] for r in runs] == ["run.log", "run5.log"]
    r5 = runs[-1]
    assert r5["from"] == "edit" and r5["last"]["lane"] == "Master" and r5["finished"] and r5["exit"] == 1
    assert r5["verdict"] == "NO-GO" and r5["draft"] == 6.0 and r5["fails"] == ["bpm [165.045, 164.965, 167.595]"]
    assert info["attempts"] == 3
    # handoff: mapped to its card, stale (older than the card's master), audio served from the handoff root
    hp = info["handoff"][0]
    assert hp["project"] == "e2e-001_tekno_165_Fmin" and hp["stale"] and hp["stems"] == 1
    ha = [a for a in d["audio"] if a["root"] == "handoff"]
    assert {a["path"] for a in ha} == {"e2e-001_tekno_165_Fmin/master.wav", "e2e-001_tekno_165_Fmin/stems/kick.wav"}
    assert d["audio"][0]["path"] == "polish/master.wav"  # newest render first, attempt folders not listed
    assert not any("attempt" in a["path"] for a in d["audio"])
    # history: attempts + current, the handoff copy of attempt2 merged into one entry
    hist = d["history"]
    assert [h["run"] for h in hist] == ["attempt1", "attempt2", "attempt3", "current"]
    assert hist[1]["runs"] == ["attempt2", "handoff"]
    assert hist[0]["metrics"]["lufs"] == -9.8


def test_handoff_card_name():
    assert files.handoff_card("e2e-001_tekno_165_Fmin", ["e2e-001", "e2e"]) == "e2e-001"
    assert files.handoff_card("acid-7_tekno_170_Amin", []) == "acid-7"


def test_files_lane_with_plan(machine_tree):
    assert files.files_lane(machine_tree / "cards" / "e2e-001") == "Score"


# ---------------------------------------------------------------- state merge
def test_process_lights_lane_and_moves_track(machine_tree):
    disk = files.scan(machine_tree / "cards", machine_tree / "handoff")
    board = [{"slug": "e2e-001", "lane": "Score", "state": "failed", "progress": 83, "loops": 0, "created_at": 1,
              "last_activity": 100, "board_activity": 100, "tasks": [{"id": "t_23436a72"}], "sendbacks": []}]
    running = [{"pid": 77, "script": "stems.py", "lanes": ["Stems"], "card": "e2e-001", "since": 5.0}]
    t = merge_tracks(board, disk, {"status": "idle"}, time.time(), running)[0]
    assert t["state"] == "working" and t["lane"] == "Stems" and t["live"]["pid"] == 77
    assert t["board_lane"] == "Score" and t["board_state"] == "failed"
    assert t["loops"] == 1 and t["loops_src"] == "disk"  # 2 run logs on disk = 1 loop back
    assert t["board_behind"] and t["plan"]["key"] == "F minor"
    lanes = {ln: {"lane": ln, "state": "idle", "task": None, "log": []} for ln in ("Stems", "Score", "Master")}
    lanes["Score"]["state"] = "failed"
    apply_processes(lanes, running, disk, time.time())
    assert lanes["Stems"]["state"] == "working" and "stems.py" in lanes["Stems"]["task"]["title"]
    assert lanes["Score"]["state"] == "failed"  # sleeping/failed lanes keep their card status


def test_unfinished_run_log_keeps_lane_working(machine_tree):
    c = machine_tree / "cards" / "e2e-001"
    (c / "run6.log").write_text("=== edit 11:50:00\n[]\n=== stems 11:50:02\n")
    disk = files.scan(machine_tree / "cards", machine_tree / "handoff")
    board = [{"slug": "e2e-001", "lane": "Score", "state": "failed", "progress": 83, "loops": 0, "created_at": 1,
              "last_activity": 1, "tasks": [], "sendbacks": []}]
    orch = [{"pid": 5, "script": "run_e2e.sh", "lanes": [], "card": "e2e-001", "since": 1.0, "orchestrator": True}]
    t = merge_tracks(board, disk, {}, time.time(), orch)[0]
    assert t["state"] == "working" and t["lane"] == "Stems" and t["live"]["via"] == "run log"
    lanes = {"Stems": {"lane": "Stems", "state": "idle", "task": None, "log": []}}
    apply_processes(lanes, orch, disk, time.time())
    assert lanes["Stems"]["state"] == "working"


# ---------------------------------------------------------------- agent-mail
UNIFIED = {"messages": [
    {"id": 2, "subject": "MIX done", "body_md": "premaster -13 LUFS", "created_ts": "2026-10-11 09:53:42.000001",
     "importance": "normal", "thread_id": "card-e2e-001", "sender": "MixBot", "project_slug": "tekno-machine",
     "recipients": "LavenderElk, ScoreBot"},
    {"id": 1, "subject": "hello", "body_md": "hi", "created_ts": "2026-10-11 09:01:28.195753", "importance": "normal",
     "thread_id": "general", "sender": "LavenderElk", "project_slug": "hermes-music", "recipients": ""},
]}


def test_mail_unified_and_threads(monkeypatch):
    m = mail.AgentMail("http://127.0.0.1:9/mcp/", "hermes-music", "bearer")
    seen = {}

    def fake_get(path, params=None):
        seen["path"], seen["params"] = path, params
        return UNIFIED
    monkeypatch.setattr(m, "get", fake_get)
    msgs = m.unified(5000)
    assert seen == {"path": "/mail/api/unified-inbox", "params": {"limit": 1000}}
    assert [x["id"] for x in msgs] == [1, 2] and msgs[1]["to"] == ["LavenderElk", "ScoreBot"]
    assert msgs[1]["ts"] == "2026-10-11T09:53:42.000001+00:00" and msgs[1]["project"] == "tekno-machine"
    th = mail.threads(msgs, ["e2e-001", "dry-001"])
    assert [t["id"] for t in th] == ["card-e2e-001", "card-dry-001", "general"]
    assert th[0]["slug"] == "e2e-001" and th[0]["count"] == 1 and th[1]["count"] == 0


def test_mail_get_whitelist_and_redaction():
    m = mail.AgentMail("http://127.0.0.1:9/mcp/", "p", None)
    with pytest.raises(PermissionError):
        m.get("/mail/api/delete-messages")
    with pytest.raises(PermissionError):
        m.get("/mail/api/overseer/send")
    err = "Error reading resource 'resource://inbox/x?project=p&agent_token=SECRET123&limit=5': Invalid agent_token"
    assert "SECRET123" not in mail.redact(err) and "agent_token=***" in mail.redact(err)
    assert mail.identity([{"name": "Tekno-Panel", "token": "SECRET", "project": "hermes-music"}]) == \
        {"name": "Tekno-Panel", "project": "hermes-music", "read_only": True}
    assert mail.identity([]) is None


def test_collector_snapshot_live(settings, machine_tree, tmp_path, monkeypatch):
    settings.cards_dir = machine_tree / "cards"
    settings.machine_root = machine_tree
    (machine_tree / "tools").mkdir(exist_ok=True)
    settings.proc_root = tmp_path / "noproc"
    (settings.state_dir).mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "agent_mail_agents.json").write_text(
        json.dumps([{"name": "Tekno-Panel", "token": "TOPSECRETTOKEN", "project": "hermes-music"}]))
    c = Collector(settings)
    monkeypatch.setattr(c.mail, "directory", lambda: [{"name": "LavenderElk"}])
    monkeypatch.setattr(c.mail, "unified", lambda limit=400: mail.normalize_messages(UNIFIED["messages"])[::-1])
    snap = c.refresh()
    raw = json.dumps(snap, default=str)
    assert "TOPSECRETTOKEN" not in raw  # the agent token never leaves the server
    assert snap["chat"]["status"] == "ok" and snap["chat"]["identity"]["name"] == "Tekno-Panel"
    assert snap["chat"]["threads"][0]["id"] == "card-e2e-001"
    assert snap["handoff"][0]["project"] == "e2e-001_tekno_165_Fmin"
    assert any(a["url"].startswith("/handoff/e2e-001_tekno_165_Fmin/master.wav?v=") for a in snap["audio"])
    assert snap["freshness"]["files"] and len(snap["history"]) >= 4
    e2e = next(t for t in snap["tracks"] if t["slug"] == "e2e-001")
    assert e2e["plan"]["bpm"] == 165.0 and e2e["runs"][-1]["name"] == "run5.log"


# ---------------------------------------------------------------- regressions from the review
def test_refresh_survives_ace_take_without_lane_process(settings, tmp_path):
    """A take started from the ACE UI: fresh log, no GPU lock, no lane script. Used to crash every refresh."""
    log = tmp_path / "acestep.log"
    log.write_text(ACE_LOG)
    settings.ace_log = log
    settings.proc_root = tmp_path / "noproc"
    snap = Collector(settings).refresh()
    a = snap["health"]["ace"]
    assert a["status"] == "generating" and a["job"]["script"] is None and a["job"]["card"] is None
    json.dumps(snap)


def test_ace_prefers_lane_script_over_worker(tmp_path):
    worker = {"pid": 1, "script": "hermes -p tekno-generate", "lanes": ["Generate"], "card": "e2e-001", "since": 1.0,
              "worker": True}
    script = {"pid": 2, "script": "ace_sections.py", "lanes": ["Generate"], "card": "e2e-001", "since": 9.0}
    a = machine.ace("http://127.0.0.1:9", tmp_path / "nolock", tmp_path / "none", [worker, script])
    assert a["job"]["script"] == "ace_sections.py" and a["job"]["since"] == 9.0


def test_crashed_run_log_without_process_is_not_working(machine_tree):
    c = machine_tree / "cards" / "e2e-001"
    (c / "run6.log").write_text("=== edit 11:50:00\n=== stems 11:50:02\nTraceback (most recent call last):\n")
    disk = files.scan(machine_tree / "cards", machine_tree / "handoff")
    board = [{"slug": "e2e-001", "lane": "Score", "state": "failed", "progress": 83, "loops": 0, "created_at": 1,
              "last_activity": 1, "tasks": [], "sendbacks": []}]
    t = merge_tracks(board, disk, {}, time.time(), [])[0]
    assert t["state"] == "failed" and t["lane"] == "Score" and not t.get("live")


def test_odd_score_files_do_not_break_the_scan(machine_tree, tmp_path):
    c = machine_tree / "cards" / "e2e-001"
    (c / "attempt3" / "score" / "score.json").write_text(
        '{"corr": 0.97, "key_top3": [[]], "mix_judge": {"overall": "n/a"}, "hard_fails": "bpm", '
        '"bpm_acf_4_16_64": 165, "LUFS_I": NaN, "sub_S_over_M_dB": -Infinity, "EAR": null}')
    weird = machine_tree / "cards" / "zz-002" / "score"
    weird.mkdir(parents=True)
    (weird / "score.json").write_text("[1, 2, 3]")
    (machine_tree / "handoff" / "latest").symlink_to(machine_tree / "handoff" / "gone")
    d = files.scan(machine_tree / "cards", machine_tree / "handoff")
    assert "e2e-001" in d["cards"] and d["handoff"] and len(d["history"]) >= 3
    a3 = next(h for h in d["history"] if h["run"] == "attempt3")
    assert a3["metrics"]["lufs"] is None and a3["metrics"]["sub"] is None
    from tekno_panel.server import dumps
    json.loads(dumps(d))  # strict JSON, no NaN


def test_snapshot_json_is_strict_even_with_nan():
    from tekno_panel.server import dumps
    assert json.loads(dumps({"a": float("nan"), "b": [float("-inf"), 1.5]})) == {"a": None, "b": [None, 1.5]}


def test_arrow_chain_is_not_a_route():
    from tekno_panel import kanban
    assert kanban.parse_routes("NO-GO. edit -> stems -> mix -> master all ran; score FAIL bpm 164.97", "Score") == []
    assert kanban.parse_routes("FAIL: chain was Mix -> Polish -> Master", "Score") == []
    r = kanban.parse_routes("BLOCKED: HF tilt -8.1 -> Mix. Not sent to Review.", "Score")
    assert [x["to"] for x in r] == ["Mix"]


@pytest.mark.parametrize("argv,ok", [
    (["vim", f"{S}/mix_stems.py"], False),
    (["less", f"{S}/score_card.py"], False),
    (["grep", "-n", "x", f"{S}/stems.py"], False),
    (["/home/k/.hermes/venv/bin/python", "hermes", "-p", "tekno-mix", "--cli"], False),  # interactive session
    ([PY, "-u", f"{S}/stems.py", f"{CARDS}/e2e-001"], True),
    (["uv", "run", "python", f"{S}/variation.py", f"{CARDS}/e2e-001"], True),
    ([f"{S}/song_plan.py", f"{CARDS}/e2e-001"], True),
    (["/bin/bash", f"{S}/run_e2e.sh", "e2e-001", "edit"], True),
])
def test_only_running_scripts_count(argv, ok):
    assert (procs.classify(argv) is not None) == ok


def test_chat_keeps_track_threads_and_own_projects(settings, monkeypatch):
    c = Collector(settings)
    msgs = mail.normalize_messages(UNIFIED["messages"] + [
        {"id": 9, "thread_id": "secret-plans", "sender": "X", "project_slug": "other-project", "body_md": "no",
         "created_ts": "2026-10-11 10:00:00.000000"}])
    monkeypatch.setattr(c.mail, "directory", lambda: [])
    monkeypatch.setattr(c.mail, "unified", lambda limit=400: msgs)
    chat = c._read_mail(["e2e-001"])
    assert {m["thread"] for m in chat["messages"]} == {"card-e2e-001", "general"}
