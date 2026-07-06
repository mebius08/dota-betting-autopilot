import csv
from pathlib import Path

from app.data_io.csv_export import (
    BET_COLUMNS,
    CANDIDATE_COLUMNS,
    UTTERANCE_COLUMNS,
    export_bets_to_csv,
    export_candidates_to_csv,
    export_utterances_to_csv,
)
from app.storage import SQLiteRepository
from tests.ml_test_helpers import (
    make_bet,
    make_candidate,
    make_match,
    make_session,
    make_utterance,
)


def test_export_empty_database_writes_headers(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")

    bets_result = export_bets_to_csv(repository, tmp_path / "nested" / "bets.csv")
    candidates_result = export_candidates_to_csv(
        repository,
        tmp_path / "nested" / "candidates.csv",
    )
    utterances_result = export_utterances_to_csv(
        repository,
        tmp_path / "nested" / "utterances.csv",
    )

    assert bets_result.row_count == 0
    assert candidates_result.row_count == 0
    assert utterances_result.row_count == 0
    assert _read_csv_rows(bets_result.output_path) == [BET_COLUMNS]
    assert _read_csv_rows(candidates_result.output_path) == [CANDIDATE_COLUMNS]
    assert _read_csv_rows(utterances_result.output_path) == [UTTERANCE_COLUMNS]


def test_export_synthetic_records(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_session(make_session("session-1"))
    repository.save_match(make_match("session-1", "match-1"))
    repository.save_bet_candidate(make_candidate("session-1", "match-1"))
    repository.save_bet(
        make_bet(
            "session-1",
            "match-1",
            "candidate-1",
            "bet-1",
            result="win",
            status="settled",
            profit_units=0.32,
        )
    )
    repository.save_streamer_utterance(
        make_utterance(
            session_id="session-1",
            match_id="match-1",
            text="over kills looks playable",
        )
    )

    bets_result = export_bets_to_csv(repository, tmp_path / "bets.csv")
    candidates_result = export_candidates_to_csv(
        repository,
        tmp_path / "candidates.csv",
    )
    utterances_result = export_utterances_to_csv(
        repository,
        tmp_path / "utterances.csv",
    )

    assert bets_result.row_count == 1
    assert candidates_result.row_count == 1
    assert utterances_result.row_count == 1
    assert _read_dicts(bets_result.output_path)[0]["id"] == "bet-1"
    assert _read_dicts(bets_result.output_path)[0]["created_at"].startswith(
        "2026-06-30T08:00:00"
    )
    assert _read_dicts(candidates_result.output_path)[0]["decision"] == "bet"
    utterance_row = _read_dicts(utterances_result.output_path)[0]
    assert utterance_row["text"] == "over kills looks playable"
    assert utterance_row["hype_flag"] == "0"


def test_repeated_export_is_stable(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_session(make_session("session-1"))
    repository.save_match(make_match("session-1", "match-1"))
    repository.save_bet_candidate(make_candidate("session-1", "match-1"))
    repository.save_bet(make_bet("session-1", "match-1", "candidate-1", "bet-1"))

    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    export_bets_to_csv(repository, first)
    export_bets_to_csv(repository, second)

    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8", newline="") as file:
        return list(csv.reader(file))


def _read_dicts(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))
