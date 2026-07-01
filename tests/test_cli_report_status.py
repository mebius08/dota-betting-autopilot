from pathlib import Path

import pytest

from app import cli
from app.storage import SQLiteRepository, init_db
from tests.ml_test_helpers import make_utterance, save_training_bundle
from tests.test_report_service import save_bet_bundle


def test_report_command_empty_db(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)

    exit_code = cli.main(["report", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Bets: 0" in output


def test_report_command_with_bet(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    save_bet_bundle(
        repository,
        "session-1",
        "match-1",
        "candidate-1",
        "bet-1",
        result="win",
        status="settled",
        profit_units=0.32,
    )

    exit_code = cli.main(["report", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Bets: 1" in output
    assert "Profit units" in output
    assert "Total staked units" in output


def test_report_command_show_utterances(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    save_bet_bundle(repository, "session-1", "match-1", "candidate-1", "bet-1")
    repository.save_streamer_utterance(
        make_utterance(
            session_id="session-1",
            match_id="match-1",
            text="over kills looks playable",
        )
    )

    exit_code = cli.main(["report", "--db", str(db_path), "--show-utterances"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "over kills looks playable" in output
    assert "confidence=0.8" in output


def test_ml_status_command_empty_db(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)

    exit_code = cli.main(["ml-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "ML training rows: 0" in output


def test_ml_status_command_ready_or_not_enough(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    save_training_bundle(repository, 1, "win")
    save_training_bundle(repository, 2, "loss")

    exit_code = cli.main(["ml-status", "--db", str(db_path), "--min-rows", "30"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Can train" in output
