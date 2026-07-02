from pathlib import Path

import pytest

from app import cli
from app.storage import SQLiteRepository, init_db
from tests.ml_test_helpers import save_training_bundle


def test_evaluate_ml_on_empty_database_does_not_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)

    exit_code = cli.main(["evaluate-ml", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Status: not_enough_data" in output
    assert "Not enough data" in output


def test_evaluate_ml_missing_database_is_friendly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["evaluate-ml", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Database not found" in output
    assert "Not enough data" in output


def test_evaluate_ml_with_synthetic_settled_records_prints_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    for index in range(12):
        save_training_bundle(
            repository,
            index,
            "win" if index % 2 == 0 else "loss",
        )

    exit_code = cli.main(
        [
            "evaluate-ml",
            "--db",
            str(db_path),
            "--min-records",
            "10",
            "--test-size",
            "0.25",
            "--seed",
            "7",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Evaluation / backtest" in output
    assert "Status: evaluated" in output
    assert "rule-based:" in output
    assert "ml:" in output
    assert "Conclusion:" in output


def test_cli_help_includes_evaluate_ml(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "evaluate-ml" in output
