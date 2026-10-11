"""Runtime configuration for TEKNO CONTROL.

Everything comes from environment variables (systemd `EnvironmentFile=`) with
defaults that match the Tekno Machine VM layout. Nothing here is secret: the
passcode and session key live in files under ``TEKNO_STATE_DIR``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.path.expanduser("~"))

# UI lanes, in pipeline order.
LANES = [
    "Plan", "Generate", "Stems", "Sound Swap", "Edit", "Filter", "Variation",
    "Mix", "Polish", "Master", "Score", "Review", "Done",
]

# Kanban title lane tokens ("[slug] LANE: ...") -> UI lane.
LANE_ALIASES = {
    "STUDY": "Plan", "PLAN": "Plan", "SONG-PLAN": "Plan", "SONGPLAN": "Plan", "BACKLOG": "Plan",
    "GENERATE": "Generate", "GEN": "Generate", "ACE": "Generate",
    "STEMS": "Stems", "STEM": "Stems",
    "SOUND-SWAP": "Sound Swap", "SOUNDSWAP": "Sound Swap", "SWAP": "Sound Swap",
    "EDIT": "Edit", "ASSEMBLE": "Edit",
    "FILTER": "Filter",
    "VARIATION": "Variation", "VAR": "Variation",
    "MIX": "Mix",
    "POLISH": "Polish",
    # MASTER-SCORE runs polish + master + score_card; it blocks on the Score gate, so it sits in Score.
    "MASTER": "Master", "MASTER-SCORE": "Score",
    "SCORE": "Score", "QC": "Score",
    "REVIEW": "Review",
    "DONE": "Done",
}

# Card sub-folder -> lane, used to see how far a track got on disk.
FOLDER_LANES = [
    ("study", "Plan"), ("plan", "Plan"), ("gen", "Generate"), ("stems", "Stems"),
    ("swap", "Sound Swap"), ("edit", "Edit"), ("filter", "Filter"), ("var", "Variation"),
    ("mix", "Mix"), ("polish", "Master"), ("score", "Score"), ("review", "Review"),
]


# Lane scripts in <machine>/scripts -> UI lane(s). lane_dsp.py picks its lane from the mode argument.
SCRIPT_LANES = {
    "song_plan": ["Plan"], "ace_sections": ["Generate"], "ace_job": ["Generate"], "regen_exact": ["Generate"],
    "assemble": ["Edit"], "stems": ["Stems"], "sound_swap": ["Sound Swap"], "variation": ["Variation"],
    "mix_stems": ["Mix"], "score_card": ["Score"], "fair_card": ["Score"], "ref_compare": ["Score"],
    "handoff": ["Done"],
}
LANE_DSP_MODES = {"edit": ["Edit"], "filter": ["Filter"], "mix": ["Mix"], "polish": ["Polish", "Master"]}
# Hermes kanban worker profiles (hermes -p tekno-<lane> ... "work kanban task t_x") -> lane.
PROFILE_LANES = {
    "tekno-study": "Plan", "tekno-plan": "Plan", "tekno-generate": "Generate", "tekno-stems": "Stems",
    "tekno-swap": "Sound Swap", "tekno-edit": "Edit", "tekno-filter": "Filter", "tekno-variation": "Variation",
    "tekno-mix": "Mix", "tekno-polish": "Polish", "tekno-master": "Master", "tekno-score": "Score",
}
# run_e2e.sh step headers ("=== stems 11:39:41") -> lane.
RUN_STEP_LANES = {
    "study": "Plan", "plan": "Plan", "gen": "Generate", "edit": "Edit", "stems": "Stems", "swap": "Sound Swap",
    "var": "Variation", "filter": "Filter", "mix": "Mix", "polish": "Polish", "master": "Master", "score": "Score",
}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _hermes_board_db(hermes_home: Path, board: str) -> Path:
    """Resolve the kanban DB for a board the way Hermes lays it out."""
    candidates = [
        hermes_home / "kanban" / "boards" / board / "kanban.db",
        hermes_home / "kanban.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 3020
    demo: bool = False
    state_dir: Path = HOME / ".config" / "tekno-panel"
    hermes_home: Path = HOME / ".hermes"
    hermes_bin: Path = HOME / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"
    board: str = "tekno-machine"
    kanban_db: Path | None = None
    machine_root: Path = Path("/mnt/agent-data/tekno_machine")
    cards_dir: Path | None = None
    reference_audio: Path | None = None
    disk_path: Path = Path("/mnt/agent-data")
    disk_warn_gb: float = 20.0
    ace_url: str = "http://127.0.0.1:8011"
    ace_log: Path = Path("/mnt/agent-data/ace-step/logs/acestep.log")
    lora_log: Path = Path("/mnt/agent-data/ace-step/logs/lora_default.log")
    # ACE runs as user units of another account: their lifecycle is read from the system journal
    # (journalctl USER_UNIT=...), their program output from the log files above.
    ace_units: tuple = ("acestep-api.service", "acestep-lora.service")
    proc_root: Path = Path("/proc")
    # the panel's own data (metrics history, activity, audit log): on the data disk, never the root disk
    data_dir: Path = Path("/mnt/agent-data/apps/tekno-panel-data")
    controls: bool = True
    ace_user: str = "Gebruiker"   # owner of the lane scripts, card folders and the ACE/agent-mail user units
    ace_uid: int = 1001
    ace_workdir: Path = Path("/mnt/agent-data/ace-step")  # run_e2e.sh is run from here
    cgroup_root: Path = Path("/sys/fs/cgroup")
    agent_mail_url: str = "http://100.84.117.126:8765/mcp/"
    agent_mail_project: str = "hermes-music"
    # chat shows track threads (card-<slug>) from any project plus all mail of these projects
    agent_mail_projects: tuple = ("hermes-music", "tekno-machine")
    # agent-mail handle -> who it is (the team-mail identities: Grok Bot, Hermes, Claude)
    agent_aliases: str = "LavenderElk=Grok Bot,CopperHeron=Hermes,SilverOtter=Claude,Tekno-Panel=Panel"
    tracks_per_day_cap: int = 6
    push_interval: float = 1.5
    services: list[dict] = field(default_factory=list)

    @property
    def kanban_path(self) -> Path:
        return self.kanban_db or _hermes_board_db(self.hermes_home, self.board)

    @property
    def cards_path(self) -> Path:
        return self.cards_dir or (self.machine_root / "cards")

    @property
    def handoff_path(self) -> Path:
        return self.machine_root / "handoff"

    @property
    def scripts_path(self) -> Path:
        return self.machine_root / "scripts"

    @property
    def reference_path(self) -> Path:
        return self.reference_audio or (self.machine_root / "tools" / "SIFRES_remake_v02_in.mp3")


DEFAULT_SERVICES = [
    # name shown in the UI, how to check it. Checks are tried in order; first conclusive wins.
    {"name": "acestep-api", "unit": "acestep-api", "cgroup": "acestep-api.service",
     "http": "http://127.0.0.1:8011/health", "process": "acestep --server-name"},
    {"name": "acestep-lora", "journal": "acestep-lora.service", "lora_log": True,
     "lora_probe": "http://127.0.0.1:8011/monitoring/summary"},
    {"name": "hermes-gateway", "unit": "hermes-gateway", "process": "hermes gateway"},
    {"name": "litellm", "unit": "aihub-litellm", "http": "http://127.0.0.1:4000/health/liveliness",
     "process": "litellm"},
]


def load_settings() -> Settings:
    s = Settings(
        host=_env("TEKNO_HOST", "127.0.0.1"),
        port=int(_env("TEKNO_PORT", "3020")),
        demo=_env("TEKNO_DEMO", _env("DEMO", "0")).lower() in ("1", "true", "yes", "on"),
        state_dir=Path(_env("TEKNO_STATE_DIR", str(HOME / ".config" / "tekno-panel"))),
        hermes_home=Path(_env("HERMES_HOME", str(HOME / ".hermes"))),
        board=_env("TEKNO_BOARD", "tekno-machine"),
        machine_root=Path(_env("TEKNO_MACHINE_ROOT", "/mnt/agent-data/tekno_machine")),
        disk_path=Path(_env("TEKNO_DISK_PATH", "/mnt/agent-data")),
        disk_warn_gb=float(_env("TEKNO_DISK_WARN_GB", "20")),
        ace_url=_env("TEKNO_ACE_URL", "http://127.0.0.1:8011"),
        ace_log=Path(_env("TEKNO_ACE_LOG", "/mnt/agent-data/ace-step/logs/acestep.log")),
        lora_log=Path(_env("TEKNO_LORA_LOG", "/mnt/agent-data/ace-step/logs/lora_default.log")),
        ace_units=tuple(u.strip() for u in _env("TEKNO_ACE_UNITS", "acestep-api.service,acestep-lora.service").split(",") if u.strip()),
        agent_mail_url=_env("TEKNO_AGENT_MAIL_URL", "http://100.84.117.126:8765/mcp/"),
        agent_mail_project=_env("TEKNO_AGENT_MAIL_PROJECT", "hermes-music"),
        agent_mail_projects=tuple(x.strip() for x in _env("TEKNO_AGENT_MAIL_PROJECTS", "hermes-music,tekno-machine").split(",") if x.strip()),
        agent_aliases=_env("TEKNO_AGENT_ALIASES", "LavenderElk=Grok Bot,CopperHeron=Hermes,SilverOtter=Claude,Tekno-Panel=Panel"),
        tracks_per_day_cap=int(_env("TEKNO_TRACKS_PER_DAY_CAP", "6")),
        push_interval=float(_env("TEKNO_PUSH_INTERVAL", "1.5")),
        data_dir=Path(_env("TEKNO_DATA_DIR", "/mnt/agent-data/apps/tekno-panel-data")),
        controls=_env("TEKNO_CONTROLS", "1").lower() in ("1", "true", "yes", "on"),
        ace_user=_env("TEKNO_ACE_USER", "Gebruiker"),
        ace_uid=int(_env("TEKNO_ACE_UID", "1001")),
        ace_workdir=Path(_env("TEKNO_ACE_WORKDIR", "/mnt/agent-data/ace-step")),
    )
    s.hermes_bin = Path(_env("TEKNO_HERMES_BIN", str(s.hermes_home / "hermes-agent" / "venv" / "bin" / "hermes")))
    if os.environ.get("TEKNO_KANBAN_DB"):
        s.kanban_db = Path(os.environ["TEKNO_KANBAN_DB"])
    if os.environ.get("TEKNO_CARDS_DIR"):
        s.cards_dir = Path(os.environ["TEKNO_CARDS_DIR"])
    if os.environ.get("TEKNO_REFERENCE_AUDIO"):
        s.reference_audio = Path(os.environ["TEKNO_REFERENCE_AUDIO"])
    services_json = os.environ.get("TEKNO_SERVICES_JSON")
    s.services = json.loads(services_json) if services_json else [dict(x) for x in DEFAULT_SERVICES]
    return s
