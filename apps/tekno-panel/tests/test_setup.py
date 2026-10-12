import json
from pathlib import Path

from tekno_panel import setup


def test_inventory_is_read_from_disk(tmp_path):
    root, apps = tmp_path / "tekno_machine", tmp_path / "apps"
    for d in ("plugins/vst3/Dragonfly Room Reverb.vst3", "plugins/clap/lsp-plugins.clap", "plugins/lv2/x42-meter.lv2",
              "surge/usr/lib/vst3/Surge XT.vst3", "models/hf/hub/models--laion--clap-htsat-unfused", "models/venv/bin"):
        (apps / d).mkdir(parents=True)
    (apps / "models/venv/bin/python").write_text("")
    (apps / "plugins/vst3/readme.txt").write_text("not a plugin")
    for d in ("tools", "night", "handoff/tm-001", "handoff/ryan-giada-kit"):
        (root / d).mkdir(parents=True)
    for f in ("beats.sh", "_models_env.sh", "house_profile.json"):
        (root / "tools" / f).write_text("")
    (root / "night/tag_last.json").write_text(json.dumps({"total": 1500, "clap": True, "at": 1}))
    (root / "CHANGELOG.md").write_text("# Changes\n- 2026-10-11 22:00 old\n- 2026-10-12 00:10 new\n")
    u = setup.inventory(root, apps, env={"TEKNO_GATEWAY_LLM": "http://g/v1", "TEKNO_GATEWAY_MODEL": "auto"})
    assert u["plugins"] == {"vst3": ["Dragonfly Room Reverb", "Surge XT"], "clap": ["lsp-plugins"], "lv2": ["x42-meter"]}
    assert u["models"] == ["laion/clap-htsat-unfused"] and u["models_venv"]
    assert u["tools"] == ["beats.sh"]
    assert u["gateway"] == {"on": True, "model": "auto", "order": "gateway,local"}
    assert u["night"]["tag"]["total"] == 1500
    assert {d["name"] for d in u["deliveries"]} == {"tm-001", "ryan-giada-kit"}
    assert u["changelog"] == ["2026-10-12 00:10 new", "2026-10-11 22:00 old"]


def test_missing_machine_is_empty_not_an_error(tmp_path):
    u = setup.inventory(tmp_path / "none", tmp_path / "none", env={})
    assert u["plugins"] == {} and u["models"] == [] and u["deliveries"] == [] and u["changelog"] == []
    assert u["gateway"]["on"] is False
