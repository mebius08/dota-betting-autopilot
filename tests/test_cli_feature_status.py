from pathlib import Path

import pytest

from app import cli
import app.history
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match


def test_cli_help_includes_feature_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "feature-status" in output


def test_feature_status_help_documents_as_of_and_decay(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["feature-status", "--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "--as-of" in output
    assert "--decay-days" in output
    assert "ISO-8601" in output


def test_feature_status_missing_db_is_friendly_and_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(
        [
            "feature-status",
            "--db",
            str(db_path),
            "--as-of",
            "2026-07-07T12:00:00Z",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Historical feature status" in output
    assert "As of: 2026-07-07T12:00:00+00:00" in output
    assert "Historical matches available: 0" in output
    assert "No point-in-time historical matches available." in output
    assert not db_path.exists()


def test_feature_status_prints_populated_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    repository.save_historical_match(
        make_historical_match(
            "history-1",
            team_a_source_id="team-a",
            team_b_source_id="team-b",
            winner_side="team_a",
        )
    )

    exit_code = cli.main(
        [
            "feature-status",
            "--db",
            str(db_path),
            "--as-of",
            "2026-01-02T00:00:00Z",
            "--decay-days",
            "60",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Decay days: 60" in output
    assert "Historical matches available: 1" in output
    assert "Usable match-result records: 1" in output
    assert "Stable teams in strength state: 2" in output
    assert "Average raw history matches per team: 1.00" in output
    assert "Cold-start policy:" in output


def test_feature_status_does_not_touch_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    SQLiteRepository(db_path)

    def exploding_provider(*args: object, **kwargs: object) -> object:
        raise AssertionError("provider should not be constructed")

    monkeypatch.setattr(
        app.history,
        "PandaScoreHistoricalMatchCollector",
        exploding_provider,
    )

    exit_code = cli.main(
        [
            "feature-status",
            "--db",
            str(db_path),
            "--as-of",
            "2026-07-07T12:00:00Z",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Historical matches available: 0" in output
