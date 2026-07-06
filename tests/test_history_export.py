import csv
from pathlib import Path

import pytest

from app import cli
from app.data_io import HISTORY_COLUMNS, export_history_to_csv
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match


def test_cli_help_includes_export_history(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "export-history" in output


def test_export_empty_history_writes_header(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    result = export_history_to_csv(repository, tmp_path / "nested" / "history.csv")

    assert result.row_count == 0
    assert _read_csv_rows(result.output_path) == [HISTORY_COLUMNS]


def test_export_history_rows_are_stable_and_secret_free(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_historical_match(make_historical_match("history-1"))

    first = tmp_path / "first.csv"
    second = tmp_path / "nested" / "second.csv"
    export_history_to_csv(repository, first)
    export_history_to_csv(repository, second)

    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
    rows = _read_dicts(first)
    assert rows[0]["id"] == "history-1"
    assert rows[0]["winner_side"] == "team_a"
    assert rows[0]["usable_for_match_winner_training"] == "1"
    assert "secret-token" not in first.read_text(encoding="utf-8")


def test_export_history_cli_creates_parent_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    output_path = tmp_path / "exports" / "history.csv"
    repository = SQLiteRepository(db_path)
    repository.save_historical_match(make_historical_match("history-1"))

    exit_code = cli.main(
        ["export-history", "--db", str(db_path), "--out", str(output_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Exported 1 historical matches to" in output
    assert output_path.exists()
    assert "history-1" in output_path.read_text(encoding="utf-8")


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8", newline="") as file:
        return list(csv.reader(file))


def _read_dicts(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))
