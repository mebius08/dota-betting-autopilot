from pathlib import Path

import pytest

from app.config import load_config


def test_load_config_reads_valid_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
mode:
  execution: paper
session:
  tournament_keyword: DreamLeague
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config["mode"]["execution"] == "paper"
    assert config["session"]["tournament_keyword"] == "DreamLeague"


def test_load_config_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.yaml")


def test_load_config_raises_for_empty_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "empty.yaml"
    config_path.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        load_config(config_path)
