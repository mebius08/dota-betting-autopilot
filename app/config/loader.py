from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(config_path)

    with config_path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file)

    if loaded is None:
        raise ValueError("Config YAML is empty")

    if not isinstance(loaded, dict):
        raise ValueError("Config root must be a mapping")

    return loaded
