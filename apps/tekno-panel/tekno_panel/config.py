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
    "MASTER": "Master", "MASTER-SCORE": "Master",
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
    agent_mail_url: str = "http://100.84.117.126:8765/mcp/"
    agent_mail_project: str = "hermes-music"
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
    def reference_path(self) -> Path:
        return self.reference_audio or (self.machine_root / "tools" / "SIFRES_remake_v02_in.mp3")


DEFAULT_SERVICES = [
    # name shown in the UI, how to check it. Checks are tried in order; first conclusive wins.
    {"name": "acestep-api", "unit": "acestep-api", "http": "http://127.0.0.1:8011/health",
     "process": "acestep --server-name"},
    {"name": "acestep-lora", "unit": "acestep-lora", "lora_probe": "http://127.0.0.1:8011/monitoring/summary"},
    {"name": "hermes-gateway", "unit": "hermes-gateway", "process": "hermes gateway"},
    {"name": "litellm", "unit": "aihub-litellm", "http": "http://127.0.0.1:4000/health/liveliness",
     "process": "litellm"},
]


def load_settings() -> Settings:
    s = Settings(
        host=_env("TEKNO_HOST", "127.0.0.1"),
        port=int(_env("TEKNO_PORT", "3020")),
        demo=_env("TEKNO_DEMO", "0").lower() in ("1", "true", "yes", "on"),
        state_dir=Path(_env("TEKNO_STATE_DIR", str(HOME / ".config" / "tekno-panel"))),
        hermes_home=Path(_env("HERMES_HOME", str(HOME / ".hermes"))),
        board=_env("TEKNO_BOARD", "tekno-machine"),
        machine_root=Path(_env("TEKNO_MACHINE_ROOT", "/mnt/agent-data/tekno_machine")),
        disk_path=Path(_env("TEKNO_DISK_PATH", "/mnt/agent-data")),
        disk_warn_gb=float(_env("TEKNO_DISK_WARN_GB", "20")),
        ace_url=_env("TEKNO_ACE_URL", "http://127.0.0.1:8011"),
        ace_log=Path(_env("TEKNO_ACE_LOG", "/mnt/agent-data/ace-step/logs/acestep.log")),
        agent_mail_url=_env("TEKNO_AGENT_MAIL_URL", "http://100.84.117.126:8765/mcp/"),
        agent_mail_project=_env("TEKNO_AGENT_MAIL_PROJECT", "hermes-music"),
        tracks_per_day_cap=int(_env("TEKNO_TRACKS_PER_DAY_CAP", "6")),
        push_interval=float(_env("TEKNO_PUSH_INTERVAL", "1.5")),
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
