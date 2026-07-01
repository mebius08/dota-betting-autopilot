from pathlib import Path

import pytest

from app import cli
from app.storage import init_db


def test_train_ml_on_empty_database_does_not_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)

    exit_code = cli.main(["train-ml", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "trained: False" in output
    assert "Not enough settled bets" in output


def test_train_ml_missing_database_handles_gracefully(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["train-ml", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code != 0
    assert "Database not found" in output


def test_run_once_use_ml_without_model_uses_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "autopilot.db"
    transcript_path = tmp_path / "streamer_transcript.txt"
    transcript_path.write_text("over kills looks playable\n", encoding="utf-8")

    exit_code = cli.main(
        [
            "run-once",
            "--tournament",
            "DreamLeague",
            "--transcript",
            str(transcript_path),
            "--db",
            str(db_path),
            "--use-ml",
            "--model-path",
            str(tmp_path / "missing.joblib"),
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "ML model not found, using rule-based scoring fallback" in output


def test_cli_help_includes_train_ml(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "train-ml" in output
