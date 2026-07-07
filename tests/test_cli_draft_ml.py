from pathlib import Path

import pytest

from app import cli


def test_cli_help_includes_draft_and_diagnostic_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "diagnose-historical-ml" in output
    assert "sync-drafts" in output
    assert "draft-history-status" in output
    assert "draft-ml-status" in output
    assert "train-draft-ml" in output
    assert "evaluate-draft-ml" in output


def test_draft_history_status_missing_db_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["draft-history-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Historical games: 0" in output
    assert "Fallback draft provider: OpenDota structured API" in output
    assert "No historical draft games found." in output
    assert not db_path.exists()


def test_draft_ml_status_missing_db_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["draft-ml-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Prediction mode: POST_DRAFT_MAP" in output
    assert "Usable post-draft feature rows: 0" in output
    assert "Run sync-drafts first" in output
    assert not db_path.exists()
