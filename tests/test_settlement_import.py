from pathlib import Path

import pytest

from app.data_io import import_settlements_from_csv
from app.storage import SQLiteRepository
from tests.test_settlement import save_open_bet


def test_import_valid_settlement_uses_existing_profit_logic(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    bet = save_open_bet(repository, "session-1", "match-1", "candidate-1", "bet-1")
    csv_path = _write_settlements(
        tmp_path,
        "bet_id,outcome,profit_units\nbet-1,win,99.0\n",
    )

    result = import_settlements_from_csv(repository, csv_path)
    settled = repository.get_bet(bet.id)

    assert result.processed_rows == 1
    assert result.updated_bets == 1
    assert result.skipped_rows == 0
    assert result.warnings == []
    assert settled is not None
    assert settled.status == "settled"
    assert settled.result == "win"
    assert settled.profit_units == pytest.approx(bet.stake_pct * (bet.odds - 1))


def test_import_multiple_valid_rows_and_mixed_case_outcomes(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    save_open_bet(repository, "session-1", "match-1", "candidate-1", "bet-1")
    save_open_bet(repository, "session-1", "match-2", "candidate-2", "bet-2")
    csv_path = _write_settlements(
        tmp_path,
        "bet_id,outcome,profit_units\nbet-1,WIN,0.32\nbet-2,Loss,-0.35\n",
    )

    result = import_settlements_from_csv(repository, csv_path)

    assert result.updated_bets == 2
    assert repository.get_bet("bet-1").result == "win"  # type: ignore[union-attr]
    assert repository.get_bet("bet-2").result == "loss"  # type: ignore[union-attr]


def test_import_skips_unknown_bet_and_keeps_valid_rows(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    save_open_bet(repository, "session-1", "match-1", "candidate-1", "bet-1")
    csv_path = _write_settlements(
        tmp_path,
        (
            "bet_id,outcome,profit_units\n"
            "missing,win,0.32\n"
            "bet-1,void,0.0\n"
        ),
    )

    result = import_settlements_from_csv(repository, csv_path)

    assert result.processed_rows == 2
    assert result.updated_bets == 1
    assert result.skipped_rows == 1
    assert len(result.warnings) == 1
    assert "unknown bet_id" in result.warnings[0]
    assert repository.get_bet("bet-1").result == "void"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("line", "warning"),
    [
        ("bet-1,casino,0.0\n", "invalid outcome"),
        ("bet-1,win,not-a-number\n", "invalid profit_units"),
        (",win,0.0\n", "missing bet_id"),
        ("bet-1,win,0.0,extra\n", "malformed row"),
    ],
)
def test_import_skips_invalid_rows(
    tmp_path: Path,
    line: str,
    warning: str,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    save_open_bet(repository, "session-1", "match-1", "candidate-1", "bet-1")
    csv_path = _write_settlements(
        tmp_path,
        f"bet_id,outcome,profit_units\n{line}",
    )

    result = import_settlements_from_csv(repository, csv_path)

    assert result.processed_rows == 1
    assert result.updated_bets == 0
    assert result.skipped_rows == 1
    assert warning in result.warnings[0]
    assert repository.get_bet("bet-1").result == "unknown"  # type: ignore[union-attr]


def test_import_empty_csv_with_header(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    csv_path = _write_settlements(tmp_path, "bet_id,outcome,profit_units\n")

    result = import_settlements_from_csv(repository, csv_path)

    assert result.processed_rows == 0
    assert result.updated_bets == 0
    assert result.skipped_rows == 0
    assert result.warnings == []


def test_import_invalid_header_raises_and_does_not_update(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    save_open_bet(repository, "session-1", "match-1", "candidate-1", "bet-1")
    csv_path = _write_settlements(tmp_path, "bet_id,result,profit_units\nbet-1,win,0.0\n")

    with pytest.raises(ValueError, match="missing required columns"):
        import_settlements_from_csv(repository, csv_path)

    assert repository.get_bet("bet-1").result == "unknown"  # type: ignore[union-attr]


def _write_settlements(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "settlements.csv"
    path.write_text(text, encoding="utf-8")
    return path
