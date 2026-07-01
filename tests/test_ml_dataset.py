from app.ml.dataset import TRAINING_COLUMNS, build_training_dataframe
from tests.ml_test_helpers import make_bet, make_candidate, make_utterance


def test_build_training_dataframe_includes_win_loss() -> None:
    candidate_win = make_candidate(candidate_id="candidate-win", match_id="match-1")
    candidate_loss = make_candidate(candidate_id="candidate-loss", match_id="match-2")
    bets = [
        make_bet("session-1", "match-1", "candidate-win", "bet-1", result="win"),
        make_bet(
            "session-1",
            "match-2",
            "candidate-loss",
            "bet-2",
            result="loss",
            profit_units=-0.35,
        ),
    ]

    dataframe = build_training_dataframe(
        bets,
        [candidate_win, candidate_loss],
        {"match-1": [make_utterance(match_id="match-1")]},
    )

    assert list(dataframe["target"]) == [1, 0]
    assert list(dataframe["profit_units"]) == [0.32, -0.35]


def test_build_training_dataframe_skips_unknown_push_void() -> None:
    candidates = [make_candidate(candidate_id="candidate-1")]
    bets = [
        make_bet("session-1", "match-1", "candidate-1", "bet-1", result="unknown"),
        make_bet("session-1", "match-1", "candidate-1", "bet-2", result="push"),
        make_bet("session-1", "match-1", "candidate-1", "bet-3", result="void"),
    ]

    dataframe = build_training_dataframe(bets, candidates, {})

    assert dataframe.empty
    assert list(dataframe.columns) == TRAINING_COLUMNS


def test_build_training_dataframe_matches_bet_to_candidate() -> None:
    candidate = make_candidate(candidate_id="candidate-actual")
    bet = make_bet(
        "session-1",
        "match-1",
        "candidate-actual",
        "bet-1",
        result="win",
    )

    dataframe = build_training_dataframe([bet], [candidate], {})

    assert list(dataframe["candidate_id"]) == ["candidate-actual"]


def test_build_training_dataframe_empty_input() -> None:
    dataframe = build_training_dataframe([], [], {})

    assert dataframe.empty
    assert list(dataframe.columns) == TRAINING_COLUMNS


def test_build_training_dataframe_uses_utterances_by_match() -> None:
    candidate = make_candidate(match_id="match-1")
    bet = make_bet("session-1", "match-1", candidate.id, "bet-1", result="win")

    dataframe = build_training_dataframe(
        [bet],
        [candidate],
        {"match-1": [make_utterance(match_id="match-1", strength=6.0)]},
    )

    assert list(dataframe["streamer_strength_sum"]) == [6.0]
