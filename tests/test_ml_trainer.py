from pathlib import Path

from app.ml import train_model_from_repository
from app.storage import SQLiteRepository
from tests.ml_test_helpers import save_training_bundle


def test_train_model_not_enough_rows(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    save_training_bundle(repository, 1, "win")

    result = train_model_from_repository(
        repository,
        model_path=tmp_path / "model.joblib",
        min_rows=30,
    )

    assert result.trained is False
    assert result.rows == 1
    assert "Not enough" in result.message
    assert not (tmp_path / "model.joblib").exists()


def test_train_model_needs_both_classes(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    for index in range(4):
        save_training_bundle(repository, index, "win")

    result = train_model_from_repository(
        repository,
        model_path=tmp_path / "model.joblib",
        min_rows=4,
    )

    assert result.trained is False
    assert result.positive_rows == 4
    assert result.negative_rows == 0
    assert "both win and loss" in result.message
    assert not (tmp_path / "model.joblib").exists()


def test_train_model_creates_model_file(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    for index in range(6):
        save_training_bundle(repository, index, "win" if index % 2 == 0 else "loss")

    model_path = tmp_path / "models" / "model.joblib"
    result = train_model_from_repository(
        repository,
        model_path=model_path,
        min_rows=6,
    )

    assert result.trained is True
    assert result.rows == 6
    assert result.positive_rows == 3
    assert result.negative_rows == 3
    assert result.model_path == model_path
    assert model_path.exists()


def test_training_result_counts_are_correct(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    for index in range(3):
        save_training_bundle(repository, index, "win")
    for index in range(3, 5):
        save_training_bundle(repository, index, "loss")

    result = train_model_from_repository(
        repository,
        model_path=tmp_path / "model.joblib",
        min_rows=5,
    )

    assert result.positive_rows == 3
    assert result.negative_rows == 2
