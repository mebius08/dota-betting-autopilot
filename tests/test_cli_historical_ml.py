from pathlib import Path

import pytest

from app import cli
from app.storage import init_db


def test_cli_help_includes_historical_ml_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "train-historical-ml" in output
    assert "historical-ml-status" in output
    assert "evaluate-historical-ml" in output


def test_historical_ml_status_handles_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(
        [
            "historical-ml-status",
            "--db",
            str(tmp_path / "missing.db"),
            "--model-path",
            str(tmp_path / "missing.joblib"),
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Historical matches: 0" in output
    assert "Model artifact exists: no" in output


def test_train_historical_ml_insufficient_data_fails_cleanly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    model_path = tmp_path / "historical.joblib"
    init_db(db_path)

    exit_code = cli.main(
        [
            "train-historical-ml",
            "--db",
            str(db_path),
            "--model-path",
            str(model_path),
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Not enough usable historical feature rows" in output
    assert not model_path.exists()


def test_evaluate_historical_ml_missing_model_is_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)

    exit_code = cli.main(
        [
            "evaluate-historical-ml",
            "--db",
            str(db_path),
            "--model-path",
            str(tmp_path / "missing.joblib"),
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Historical ML model artifact not found" in output
