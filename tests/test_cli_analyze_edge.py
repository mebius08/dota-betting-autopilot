from pathlib import Path

import joblib
import pytest

from app import cli
from app.domain import BetCandidate, Match, OddsSnapshot, Session
from app.storage import SQLiteRepository, init_db
from tests.ml_test_helpers import NOW, make_utterance


class FixedCliProbabilityModel:
    classes_ = [0, 1]

    def __init__(self, probability: float) -> None:
        self.probability = probability

    def predict_proba(self, dataframe: object) -> list[list[float]]:
        return [[1.0 - self.probability, self.probability]]


def test_cli_help_includes_analyze_edge(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "analyze-edge" in output


def test_analyze_edge_empty_database_is_friendly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    init_db(db_path)

    exit_code = cli.main(["analyze-edge", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Edge analysis" in output
    assert "No candidates available for edge analysis." in output


def test_analyze_edge_missing_database_is_friendly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["analyze-edge", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Database not found" in output
    assert "Run app.main or app.cli run-once first." in output


def test_analyze_edge_prints_market_probability_with_missing_model(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    save_edge_bundle(repository)

    exit_code = cli.main(
        [
            "analyze-edge",
            "--db",
            str(db_path),
            "--model",
            str(tmp_path / "missing.joblib"),
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Candidate: Team Spirit" in output
    assert "Market: map_winner" in output
    assert "Bookmaker: fakebook" in output
    assert "Raw implied probability: 47.62%" in output
    assert "Fair market probability: 46.15%" in output
    assert "Model probability: unavailable" in output
    assert "Estimated edge: unavailable" in output
    assert "Expected value: unavailable" in output
    assert "Status: model_probability_unavailable" in output
    assert "Unavailable: 1" in output


def test_analyze_edge_prints_edge_and_expected_value_from_model_probability(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    model_path = tmp_path / "model.joblib"
    repository = SQLiteRepository(db_path)
    save_edge_bundle(repository)
    joblib.dump(FixedCliProbabilityModel(0.563), model_path)

    exit_code = cli.main(
        [
            "analyze-edge",
            "--db",
            str(db_path),
            "--model-path",
            str(model_path),
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Model probability: 56.30%" in output
    assert "Probability source: ml_predict_proba" in output
    assert "Estimated edge: +10.15 pp" in output
    assert "Expected value: +0.182 units" in output
    assert "Status: available" in output
    assert "Analyzed candidates: 1" in output
    assert "Edge available: 1" in output
    assert "BET recommendation" not in output
    assert "suggested stake" not in output.lower()
    assert "auto execution" not in output.lower()


def test_analyze_edge_filters_by_match_bookmaker_and_min_edge(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    model_path = tmp_path / "model.joblib"
    repository = SQLiteRepository(db_path)
    save_edge_bundle(repository)
    joblib.dump(FixedCliProbabilityModel(0.563), model_path)

    exit_code = cli.main(
        [
            "analyze-edge",
            "--db",
            str(db_path),
            "--model-path",
            str(model_path),
            "--match-id",
            "match-1",
            "--bookmaker",
            "fakebook",
            "--min-edge",
            "0.05",
            "--limit",
            "5",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Candidate: Team Spirit" in output
    assert "Analyzed candidates: 1" in output


def save_edge_bundle(repository: SQLiteRepository) -> None:
    session = make_session()
    match = make_match()
    repository.save_session(session)
    repository.save_match(match)
    repository.save_bet_candidate(make_candidate())
    repository.save_odds_snapshot(make_snapshot("snapshot-a", "Team Spirit", 2.10))
    repository.save_odds_snapshot(make_snapshot("snapshot-b", "PARIVISION", 1.80))
    repository.save_streamer_utterance(make_utterance(match_id="match-1"))


def make_session() -> Session:
    return Session(
        id="session-1",
        name="DreamLeague",
        tournament_keyword="DreamLeague",
        streamer_channel="manual_transcript",
        execution_mode="paper",
        target_bets_per_match=1.2,
        max_bets_per_match=3,
        score_threshold=62,
        active=True,
        created_at=NOW,
    )


def make_match() -> Match:
    return Match(
        id="match-1",
        session_id="session-1",
        tournament_name="DreamLeague Season 25",
        team_a="Team Spirit",
        team_b="PARIVISION",
        format="bo3",
        status="upcoming",
        start_time=NOW,
        external_id="fixture-1",
    )


def make_candidate() -> BetCandidate:
    return BetCandidate(
        id="candidate-1",
        session_id="session-1",
        match_id="match-1",
        market="map_winner",
        selection="Team Spirit",
        line=None,
        odds=2.10,
        phase="pre_match",
        market_score=25,
        phase_score=10,
        line_score=10,
        streamer_score=0,
        risk_score=5,
        final_score=50,
        decision="watch",
        explanation="candidate",
        created_at=NOW,
    )


def make_snapshot(snapshot_id: str, selection: str, odds: float) -> OddsSnapshot:
    return OddsSnapshot(
        id=snapshot_id,
        session_id="session-1",
        match_id="match-1",
        external_market_id="market-1",
        market="map_winner",
        selection=selection,
        line=None,
        odds=odds,
        phase="pre_match",
        is_live=False,
        is_suspended=False,
        bookmaker="fakebook",
        created_at=NOW,
    )
