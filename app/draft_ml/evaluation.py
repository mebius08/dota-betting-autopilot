from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.draft_ml.dataset import build_draft_map_dataset
from app.draft_ml.model import DEFAULT_DRAFT_MODEL_PATH, load_draft_model
from app.historical_ml.evaluation import HistoricalModelMetrics, evaluate_probabilities

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass(frozen=True)
class DraftEvaluationResult:
    evaluated: bool
    rows: int
    recorded_metrics: object
    current_metrics: HistoricalModelMetrics | None
    message: str


def evaluate_draft_model_from_repository(
    repository: "SQLiteRepository",
    *,
    model_path: str | Path = DEFAULT_DRAFT_MODEL_PATH,
) -> DraftEvaluationResult:
    model = load_draft_model(model_path)
    dataset = build_draft_map_dataset(repository)
    if len(dataset) == 0:
        return DraftEvaluationResult(
            evaluated=False,
            rows=0,
            recorded_metrics=model.evaluation_metrics,
            current_metrics=None,
            message="No current post-draft rows to evaluate.",
        )
    probabilities = model.predict_team_a_probabilities(dataset)
    return DraftEvaluationResult(
        evaluated=True,
        rows=len(dataset),
        recorded_metrics=model.evaluation_metrics,
        current_metrics=evaluate_probabilities(dataset.y.tolist(), probabilities),
        message="Draft model current-dataset evaluation complete.",
    )
