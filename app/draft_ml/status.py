from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.draft_ml.dataset import (
    DRAFT_CATEGORICAL_FEATURE_NAMES,
    DRAFT_FEATURE_SCHEMA_VERSION,
    DRAFT_PREDICTION_MODE,
    build_draft_map_dataset,
)
from app.draft_ml.model import (
    DEFAULT_DRAFT_MODEL_PATH,
    DraftModelCompatibilityError,
    load_draft_model,
)
from app.draft_ml.split import (
    DraftMinimumRowsPolicy,
    DraftTemporalSplit,
    DraftTemporalSplitPolicy,
    DraftTrainingDataError,
    split_draft_dataset,
    validate_minimum_draft_training_rows,
)

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass(frozen=True)
class DraftMLStatus:
    prediction_mode: str
    provider: str
    raw_historical_games: int
    complete_draft_target_games: int
    usable_post_draft_feature_rows: int
    categorical_feature_count: int
    numeric_feature_count: int
    feature_schema_version: int
    split: DraftTemporalSplit
    split_ready: bool
    readiness_reason: str
    artifact_path: Path
    artifact_exists: bool
    artifact_compatible: bool
    artifact_reason: str | None
    patch_semantics: str
    source_link_coverage: float


def build_draft_ml_status(
    repository: "SQLiteRepository",
    *,
    model_path: str | Path = DEFAULT_DRAFT_MODEL_PATH,
    split_policy: DraftTemporalSplitPolicy | None = None,
    minimum_rows_policy: DraftMinimumRowsPolicy | None = None,
) -> DraftMLStatus:
    from app.draft_history import build_draft_history_status

    temporal_policy = split_policy or DraftTemporalSplitPolicy()
    minimum_policy = minimum_rows_policy or DraftMinimumRowsPolicy()
    history_status = build_draft_history_status(repository, provider="opendota")
    dataset = build_draft_map_dataset(repository)
    split = split_draft_dataset(dataset, policy=temporal_policy)
    try:
        validate_minimum_draft_training_rows(dataset, split, policy=minimum_policy)
    except DraftTrainingDataError as exc:
        split_ready = False
        reason = str(exc)
    else:
        split_ready = True
        reason = "Ready to train POST_DRAFT map model."

    artifact_path = Path(model_path)
    artifact_exists = artifact_path.exists()
    artifact_compatible = False
    artifact_reason: str | None = None
    if artifact_exists:
        try:
            load_draft_model(artifact_path)
        except (FileNotFoundError, DraftModelCompatibilityError) as exc:
            artifact_reason = str(exc)
        else:
            artifact_compatible = True

    return DraftMLStatus(
        prediction_mode=DRAFT_PREDICTION_MODE,
        provider="opendota",
        raw_historical_games=history_status.historical_games,
        complete_draft_target_games=history_status.games_with_complete_5v5_picks,
        usable_post_draft_feature_rows=len(dataset),
        categorical_feature_count=len(DRAFT_CATEGORICAL_FEATURE_NAMES),
        numeric_feature_count=len(dataset.numeric_feature_names),
        feature_schema_version=DRAFT_FEATURE_SCHEMA_VERSION,
        split=split,
        split_ready=split_ready,
        readiness_reason=reason,
        artifact_path=artifact_path,
        artifact_exists=artifact_exists,
        artifact_compatible=artifact_compatible,
        artifact_reason=artifact_reason,
        patch_semantics="trusted provider patch when present; no date inference",
        source_link_coverage=history_status.source_link_coverage,
    )
