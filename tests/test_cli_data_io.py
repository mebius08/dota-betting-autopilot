from pathlib import Path

import pytest

from app import cli
from app.storage import SQLiteRepository
from tests.ml_test_helpers import make_bet, make_candidate, make_match, make_session
from tests.test_settlement import save_open_bet


def test_cli_help_includes_data_io_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "export-bets" in output
    assert "export-candidates" in output
    assert "export-utterances" in output
    assert "import-settlements" in output
    assert "inspect-dataset" in output


def test_export_bets_command_uses_tmp_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    output_path = tmp_path / "exports" / "bets.csv"
    repository = SQLiteRepository(db_path)
    repository.save_session(make_session("session-1"))
    repository.save_match(make_match("session-1", "match-1"))
    repository.save_bet_candidate(make_candidate("session-1", "match-1"))
    repository.save_bet(make_bet("session-1", "match-1", "candidate-1", "bet-1"))

    exit_code = cli.main(
        ["export-bets", "--db", str(db_path), "--out", str(output_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Exported 1 bets to" in output
    assert output_path.exists()
    assert "bet-1" in output_path.read_text(encoding="utf-8")


def test_import_settlements_command_prints_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    csv_path = tmp_path / "settlements.csv"
    repository = SQLiteRepository(db_path)
    save_open_bet(repository, "session-1", "match-1", "candidate-1", "bet-1")
    csv_path.write_text(
        "bet_id,outcome,profit_units\nmissing,win,0.32\nbet-1,push,0.0\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        ["import-settlements", "--db", str(db_path), "--csv", str(csv_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Processed rows: 2" in output
    assert "Updated bets: 1" in output
    assert "Skipped rows: 1" in output
    assert "Warnings: 1" in output
    assert "unknown bet_id" in output
    assert repository.get_bet("bet-1").result == "push"  # type: ignore[union-attr]


def test_inspect_dataset_command_prints_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    SQLiteRepository(db_path)

    exit_code = cli.main(["inspect-dataset", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"Database: {db_path.as_posix()}" in output
    assert "Sessions: 0" in output
    assert "Usable ML records: 0" in output
    assert "Readiness: not_enough_data" in output
