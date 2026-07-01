from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import joblib  # type: ignore[import-untyped]

from app.ml.dataset import build_training_dataframe
from app.ml.model import create_model_pipeline, select_model_features

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass
class TrainingResult:
    trained: bool
    rows: int
    positive_rows: int
    negative_rows: int
    model_path: Path | None
    message: str


def train_model_from_repository(
    repository: "SQLiteRepository",
    model_path: str | Path = "data/models/bet_model.joblib",
    min_rows: int = 30,
) -> TrainingResult:
    path = Path(model_path)
    bets = repository.list_bets()
    candidates = repository.list_bet_candidates()
    match_ids = {candidate.match_id for candidate in candidates}
    utterances_by_match = {
        match_id: repository.list_streamer_utterances_by_match(match_id)
        for match_id in match_ids
    }
    dataframe = build_training_dataframe(bets, candidates, utterances_by_match)
    rows = len(dataframe)
    positive_rows = int(dataframe["target"].sum()) if rows else 0
    negative_rows = rows - positive_rows

    if rows < min_rows:
        return TrainingResult(
            trained=False,
            rows=rows,
            positive_rows=positive_rows,
            negative_rows=negative_rows,
            model_path=None,
            message="Not enough settled bets to train model",
        )

    if positive_rows == 0 or negative_rows == 0:
        return TrainingResult(
            trained=False,
            rows=rows,
            positive_rows=positive_rows,
            negative_rows=negative_rows,
            model_path=None,
            message="Need both win and loss examples",
        )

    pipeline = create_model_pipeline()
    pipeline.fit(select_model_features(dataframe), dataframe["target"])
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, path)

    return TrainingResult(
        trained=True,
        rows=rows,
        positive_rows=positive_rows,
        negative_rows=negative_rows,
        model_path=path,
        message="Model trained successfully",
    )
