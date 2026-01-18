from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


@dataclass(frozen=True)
class AppConfig:
    comet_exe: str
    debug_port: int = 9223
    perplexity_url: str = "https://www.perplexity.ai/"
    auto_launch: bool = True
    restart_if_no_debug_port: bool = True


def load_config() -> AppConfig | None:
    if not CONFIG_PATH.exists():
        return None
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return AppConfig(**data)


def save_config(cfg: AppConfig) -> None:
    CONFIG_PATH.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")

