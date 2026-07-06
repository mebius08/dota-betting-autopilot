from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import cli
from app.domain import Match, Session
from app.storage import SQLiteRepository, init_db


def test_cli_help_includes_ewc_status(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "ewc-status" in output


def test_ewc_status_empty_database_is_friendly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)

    exit_code = cli.main(["ewc-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "EWC 2026 Dota scope" in output
    assert "Tournament id: ewc_2026_dota2" in output
    assert "Scoped matches: 0" in output
    assert "No persisted EWC 2026 Dota matches found." in output
    assert "Traceback" not in output


def test_ewc_status_missing_database_is_friendly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["ewc-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Scoped matches: 0" in output
    assert "Run app.main or app.cli run-once first." in output


def test_ewc_status_counts_scope_stage_distribution_and_statuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    session = _session()
    repository.save_session(session)
    for match in [
        _match("ewc-group", "Esports World Cup 2026 / Group Stage", "upcoming"),
        _match("ewc-survival", "EWC 2026 / Survival - Grand Final #1", "live"),
        _match(
            "ewc-quarterfinal",
            "Dota 2 at EWC 26 / Playoffs - Quarterfinal #1",
            "finished",
        ),
        _match("ewc-grand-final", "EWC 2026 / Playoffs - Grand Final #1", "finished"),
        _match("dreamleague", "DreamLeague / Group Stage", "upcoming"),
    ]:
        repository.save_match(match)

    exit_code = cli.main(["ewc-status", "--db", str(repository.db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Scoped matches: 4" in output
    assert "  group: 1" in output
    assert "  crossover: 1" in output
    assert "  upper_bracket: 0" in output
    assert "  lower_bracket: 0" in output
    assert "  single_elimination: 1" in output
    assert "  grand_final: 1" in output
    assert "  placement: 0" in output
    assert "  unknown: 0" in output
    assert "Upcoming: 1" in output
    assert "Live: 1" in output
    assert "Completed: 2" in output
    assert "DreamLeague" not in output


def test_ewc_status_does_not_print_betting_instructions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_session(_session())
    repository.save_match(_match("ewc-unknown", "EWC 2026", "upcoming"))

    exit_code = cli.main(["ewc-status", "--db", str(repository.db_path)])
    output = capsys.readouterr().out
    normalized_output = output.casefold()

    assert exit_code == 0
    assert "  unknown: 1" in output
    assert "bet recommendation" not in normalized_output
    assert "stake" not in normalized_output
    assert "auto execution" not in normalized_output


def _session() -> Session:
    return Session(
        id="session-1",
        name="EWC 2026",
        tournament_keyword="EWC 2026",
        streamer_channel="manual",
        execution_mode="paper",
        target_bets_per_match=1.0,
        max_bets_per_match=2,
        score_threshold=60.0,
        active=True,
        created_at=datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc),
    )


def _match(match_id: str, tournament_name: str, status: str) -> Match:
    return Match(
        id=match_id,
        session_id="session-1",
        tournament_name=tournament_name,
        team_a="Team Spirit",
        team_b="PARIVISION",
        format="bo3",
        status=status,  # type: ignore[arg-type]
        start_time=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
        external_id=match_id,
    )
