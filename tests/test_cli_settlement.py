from pathlib import Path

import pytest

from app import cli
from app.storage import SQLiteRepository, init_db
from tests.test_settlement import save_open_bet


def test_open_bets_command_empty_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)

    exit_code = cli.main(["open-bets", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Open bets: 0" in output


def test_open_bets_command_lists_bet(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    save_open_bet(repository, "session-1", "match-1", "candidate-1", "bet-1")

    exit_code = cli.main(["open-bets", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Open bets: 1" in output
    assert "total_kills" in output
    assert "over" in output


def test_settle_bet_command_win(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    bet = save_open_bet(repository, "session-1", "match-1", "candidate-1", "bet-1")

    exit_code = cli.main(
        [
            "settle-bet",
            "--db",
            str(db_path),
            "--bet-id",
            bet.id,
            "--result",
            "win",
        ]
    )
    output = capsys.readouterr().out
    settled = repository.get_bet(bet.id)

    assert exit_code == 0
    assert "Settled bet" in output
    assert "result=win" in output
    assert "profit_units" in output
    assert settled is not None
    assert settled.status == "settled"
    assert settled.result == "win"


def test_settle_bet_command_invalid_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)

    exit_code = cli.main(
        [
            "settle-bet",
            "--db",
            str(db_path),
            "--bet-id",
            "missing",
            "--result",
            "win",
        ]
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert exit_code != 0
    assert "not found" in output or "No bet" in output


def test_settle_bet_invalid_result_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)

    exit_code = cli.main(
        [
            "settle-bet",
            "--db",
            str(db_path),
            "--bet-id",
            "bet-1",
            "--result",
            "casino",
        ]
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert exit_code != 0
    assert "invalid choice" in output
