from pathlib import Path

from app.ml import build_ml_training_status
from app.storage import SQLiteRepository
from tests.ml_test_helpers import save_training_bundle


def test_ml_status_empty_db(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")

    status = build_ml_training_status(repository)

    assert status.training_rows == 0
    assert status.can_train is False


def test_ml_status_not_enough_rows(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    save_training_bundle(repository, 1, "win")
    save_training_bundle(repository, 2, "loss")

    status = build_ml_training_status(repository, min_rows=30)

    assert status.can_train is False
    assert "Not enough" in status.reason


def test_ml_status_one_class_only(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    for index in range(4):
        save_training_bundle(repository, index, "win")

    status = build_ml_training_status(repository, min_rows=4)

    assert status.can_train is False
    assert "both win and loss" in status.reason


def test_ml_status_ready(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    for index in range(4):
        save_training_bundle(repository, index, "win" if index % 2 == 0 else "loss")

    status = build_ml_training_status(repository, min_rows=4)

    assert status.can_train is True
    assert "Ready" in status.reason
