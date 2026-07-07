from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cli
from app.historical_ml import (
    CatBoostCandidateConfig,
    CatBoostCandidateDiagnostics,
    ModelDiagnostics,
)
from app.historical_ml.evaluation import HistoricalModelMetrics
from app.storage import init_db


def test_cli_help_includes_draft_and_diagnostic_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "diagnose-historical-ml" in output
    assert "sync-drafts" in output
    assert "draft-history-status" in output
    assert "draft-ml-status" in output
    assert "train-draft-ml" in output
    assert "evaluate-draft-ml" in output


def test_draft_history_status_missing_db_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["draft-history-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Historical games: 0" in output
    assert "Fallback draft provider: OpenDota structured API" in output
    assert "No historical draft games found." in output
    assert not db_path.exists()


def test_draft_ml_status_missing_db_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["draft-ml-status", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Prediction mode: POST_DRAFT_MAP" in output
    assert "Usable post-draft feature rows: 0" in output
    assert "Run sync-drafts first" in output
    assert not db_path.exists()


def test_diagnose_historical_ml_prints_ranked_top_catboost_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import app.historical_ml as historical_ml

    db_path = tmp_path / "test.db"
    init_db(db_path)
    worse = _candidate(decay_days=30.0, validation_brier=0.204, test_brier=0.01)
    selected = _candidate(decay_days=120.0, validation_brier=0.202, test_brier=0.99)
    middle = _candidate(decay_days=60.0, validation_brier=0.203, test_brier=0.50)

    monkeypatch.setattr(
        historical_ml,
        "diagnose_historical_ml_from_repository",
        lambda repository, logistic_model_path: SimpleNamespace(
            evaluated=True,
            message="ok",
            rows=3,
            feature_count=1,
            split=None,
            split_positive_label_rates={},
            baselines=(),
            logistic=None,
            catboost_available=True,
            catboost_candidates=(worse, selected, middle),
            selected_catboost=selected,
            validation_feature_drift=(),
            test_feature_drift=(),
        ),
    )

    exit_code = cli.main(["diagnose-historical-ml", "--db", str(db_path)])
    output = capsys.readouterr().out
    top_block = output.split("Top CatBoost candidates:", maxsplit=1)[1]
    candidate_lines = [
        line.strip()
        for line in top_block.splitlines()
        if line.strip().startswith("decay=")
    ]

    assert exit_code == 0
    assert candidate_lines[:3] == [
        "decay=120 depth=4 l2=3 val_brier=0.202000 "
        "val_log_loss=0.600000 val_accuracy=0.500",
        "decay=60 depth=4 l2=3 val_brier=0.203000 "
        "val_log_loss=0.600000 val_accuracy=0.500",
        "decay=30 depth=4 l2=3 val_brier=0.204000 "
        "val_log_loss=0.600000 val_accuracy=0.500",
    ]


def _candidate(
    *,
    decay_days: float,
    validation_brier: float,
    test_brier: float,
) -> CatBoostCandidateDiagnostics:
    return CatBoostCandidateDiagnostics(
        config=CatBoostCandidateConfig(
            decay_days=decay_days,
            depth=4,
            l2_leaf_reg=3.0,
        ),
        diagnostics=ModelDiagnostics(
            train_metrics=_metrics(0.30),
            validation_metrics=_metrics(validation_brier),
            test_metrics=_metrics(test_brier),
            test_probability_buckets=(),
            test_chronological_buckets=(),
            test_family_metrics=(),
            test_stage_metrics=(),
        ),
    )


def _metrics(brier: float) -> HistoricalModelMetrics:
    return HistoricalModelMetrics(
        row_count=10,
        positive_label_rate=0.5,
        average_predicted_probability=0.5,
        brier_score=brier,
        log_loss=0.60,
        accuracy=0.50,
    )
