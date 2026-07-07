from pathlib import Path

import pytest

from app import cli
import app.history
from app.history import RosterCollectionResult
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match
from tests.roster_test_helpers import make_roster_snapshot


def test_cli_help_includes_sync_rosters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "sync-rosters" in output


def test_sync_rosters_command_prints_summary_and_persists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        app.history,
        "PandaScoreRosterCollector",
        _FakeRosterCollector,
    )
    db_path = tmp_path / "test.db"
    SQLiteRepository(db_path).save_historical_match(
        make_historical_match("history-1", tournament_source_id="300")
    )

    exit_code = cli.main(
        [
            "sync-rosters",
            "--provider",
            "pandascore",
            "--db",
            str(db_path),
            "--max-tournaments",
            "3",
            "--timeout",
            "2.5",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Roster history sync" in output
    assert "Provider: pandascore" in output
    assert "Max tournaments: 3" in output
    assert "Tournaments requested: 1" in output
    assert "Rosters fetched: 1" in output
    assert "Unique players seen: 5" in output
    assert "Unique organizations seen: 1" in output
    assert "Snapshots inserted: 1" in output
    assert SQLiteRepository(db_path).count_roster_snapshots() == 1


def test_sync_rosters_provider_failure_is_friendly_and_hides_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        app.history,
        "PandaScoreRosterCollector",
        _TokenErrorRosterCollector,
    )
    db_path = tmp_path / "test.db"
    SQLiteRepository(db_path).save_historical_match(
        make_historical_match("history-1", tournament_source_id="300")
    )

    exit_code = cli.main(
        [
            "sync-rosters",
            "--provider",
            "pandascore",
            "--db",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "PandaScore token is not configured." in captured.out
    assert "secret-token" not in captured.out
    assert "Traceback" not in captured.err


class _FakeRosterCollector:
    def __init__(self, *, timeout: float) -> None:
        assert timeout == 2.5

    def collect(
        self,
        *,
        tournament_source_ids: list[str],
        max_tournaments: int,
    ) -> RosterCollectionResult:
        assert tournament_source_ids == ["300"]
        assert max_tournaments == 3
        return RosterCollectionResult(
            snapshots=[make_roster_snapshot("main", tournament_source_id="300")],
            tournaments_requested=1,
            fetched_rows=1,
            skipped_records=0,
            warnings=[],
        )


class _TokenErrorRosterCollector:
    def __init__(self, *, timeout: float) -> None:
        pass

    def collect(
        self,
        *,
        tournament_source_ids: list[str],
        max_tournaments: int,
    ) -> RosterCollectionResult:
        raise app.history.PandaScoreConfigurationError(
            "PandaScore token is not configured.\n"
            "Set PANDASCORE_TOKEN before using the pandascore provider."
        )
