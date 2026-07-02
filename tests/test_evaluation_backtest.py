from pathlib import Path

from app.evaluation import (
    EvaluationRecord,
    build_evaluation_dataset,
    compare_evaluation_results,
    evaluate_ml_model,
    evaluate_rule_based,
    run_evaluation_backtest,
    split_train_test,
)
from app.storage import SQLiteRepository
from tests.ml_test_helpers import (
    make_bet,
    make_candidate,
    make_match,
    make_session,
    save_training_bundle,
)


def _record(index: int) -> EvaluationRecord:
    target = 1 if index % 2 == 0 else 0
    return EvaluationRecord(
        bet_id=f"bet-{index}",
        candidate_id=f"candidate-{index}",
        result="win" if target == 1 else "loss",
        target=target,
        profit_units=0.35 if target == 1 else -0.35,
        stake_pct=0.35,
        rule_score=70.0 if target == 1 else 30.0,
        features={
            "candidate_id": f"candidate-{index}",
            "market": "total_kills",
            "selection": "over",
            "phase": "after_draft",
            "odds": 1.9,
            "line": 48.5,
            "market_score": 25.0,
            "phase_score": 20.0,
            "line_score": 10.0,
            "streamer_score": float(index),
            "risk_score": 5.0,
            "rule_final_score": 70.0 if target == 1 else 30.0,
            "hype_flag": False,
            "has_skip_warning": False,
            "streamer_strength_sum": float(index),
            "streamer_confidence_max": 0.7,
        },
    )


def test_split_train_test_is_deterministic() -> None:
    records = [_record(index) for index in range(10)]

    first_train, first_test = split_train_test(records, test_size=0.3, seed=42)
    second_train, second_test = split_train_test(records, test_size=0.3, seed=42)

    assert [record.bet_id for record in first_train] == [
        record.bet_id for record in second_train
    ]
    assert [record.bet_id for record in first_test] == [
        record.bet_id for record in second_test
    ]
    assert len(first_train) == 7
    assert len(first_test) == 3


def test_split_train_test_handles_small_dataset() -> None:
    train, test = split_train_test([_record(1)], test_size=0.3, seed=42)

    assert len(train) == 1
    assert len(test) == 0


def test_backtest_returns_not_enough_data(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    save_training_bundle(repository, 1, "win")

    report = run_evaluation_backtest(repository, min_records=10)

    assert report.status == "not_enough_data"
    assert report.conclusion == "Not enough data"
    assert report.dataset.usable_records == 1


def test_build_evaluation_dataset_ignores_push_void_and_unknown(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    session = make_session("session-1")
    match = make_match("session-1", "match-1")
    candidate = make_candidate("session-1", "match-1", "candidate-1")
    repository.save_session(session)
    repository.save_match(match)
    repository.save_bet_candidate(candidate)
    repository.save_bet(
        make_bet("session-1", "match-1", "candidate-1", "bet-win", result="win")
    )
    repository.save_bet(
        make_bet("session-1", "match-1", "candidate-1", "bet-push", result="push")
    )
    repository.save_bet(
        make_bet("session-1", "match-1", "candidate-1", "bet-void", result="void")
    )
    repository.save_bet(
        make_bet(
            "session-1",
            "match-1",
            "candidate-1",
            "bet-open",
            result="unknown",
            status="placed",
            profit_units=0.0,
        )
    )

    dataset = build_evaluation_dataset(repository)

    assert dataset.usable_records == 1
    assert dataset.ignored_push_void == 2
    assert dataset.ignored_unknown_open == 1


def test_rule_and_ml_evaluation_return_comparable_results() -> None:
    records = [_record(index) for index in range(12)]
    train_records, test_records = split_train_test(records, test_size=0.25, seed=7)

    rule_result = evaluate_rule_based(test_records)
    ml_result = evaluate_ml_model(train_records, test_records)
    conclusion = compare_evaluation_results(rule_result, ml_result)

    assert rule_result.status == "evaluated"
    assert rule_result.metrics.accuracy == 1.0
    assert ml_result.metrics.total_records == len(test_records)
    assert conclusion in {
        "ML looks better",
        "Rule looks better",
        "Not enough data",
        "Inconclusive",
    }
