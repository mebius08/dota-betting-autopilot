from pathlib import Path

import joblib

from app.ml import MLBetPredictor, train_model_from_repository
from app.storage import SQLiteRepository
from tests.ml_test_helpers import make_candidate, make_utterance, save_training_bundle


class FakeProbabilityModel:
    def __init__(self, classes: list[int], probabilities: list[float]) -> None:
        self.classes_ = classes
        self.probabilities = probabilities

    def predict_proba(self, dataframe: object) -> list[list[float]]:
        return [self.probabilities]


class FakeModelWithoutPredictProba:
    classes_ = [0, 1]


def test_predictor_returns_none_when_model_missing(tmp_path: Path) -> None:
    predictor = MLBetPredictor(tmp_path / "missing.joblib")

    assert predictor.is_available() is False
    assert predictor.predict_good_bet_probability(make_candidate(), []) is None
    assert predictor.predict_ml_score(make_candidate(), []) is None


def test_predictor_returns_probability_after_training(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    for index in range(6):
        save_training_bundle(repository, index, "win" if index % 2 == 0 else "loss")
    model_path = tmp_path / "model.joblib"
    train_model_from_repository(repository, model_path=model_path, min_rows=6)

    predictor = MLBetPredictor(model_path)
    probability = predictor.predict_good_bet_probability(
        make_candidate(),
        [make_utterance()],
    )

    assert predictor.is_available() is True
    assert probability is not None
    assert 0 <= probability <= 1


def test_predictor_returns_ml_score_between_0_and_100(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    for index in range(6):
        save_training_bundle(repository, index, "win" if index % 2 == 0 else "loss")
    model_path = tmp_path / "model.joblib"
    train_model_from_repository(repository, model_path=model_path, min_rows=6)

    predictor = MLBetPredictor(model_path)
    score = predictor.predict_ml_score(make_candidate(), [make_utterance()])

    assert score is not None
    assert 0 <= score <= 100


def test_predictor_uses_explicit_positive_class_mapping(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    joblib.dump(FakeProbabilityModel([1, 0], [0.65, 0.35]), model_path)

    predictor = MLBetPredictor(model_path)

    assert predictor.predict_good_bet_probability(make_candidate(), []) == 0.65
    assert predictor.predict_win_probability(make_candidate(), []) == 0.65


def test_predictor_returns_none_without_predict_proba(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    joblib.dump(FakeModelWithoutPredictProba(), model_path)

    predictor = MLBetPredictor(model_path)

    assert predictor.predict_good_bet_probability(make_candidate(), []) is None


def test_predictor_returns_none_without_positive_class(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    joblib.dump(FakeProbabilityModel([0], [1.0]), model_path)

    predictor = MLBetPredictor(model_path)

    assert predictor.predict_good_bet_probability(make_candidate(), []) is None


def test_predictor_returns_none_for_invalid_probability_output(
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "model.joblib"
    joblib.dump(FakeProbabilityModel([0, 1], [0.2, float("nan")]), model_path)

    predictor = MLBetPredictor(model_path)

    assert predictor.predict_good_bet_probability(make_candidate(), []) is None
