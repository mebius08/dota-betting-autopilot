import pandas as pd
from sklearn.pipeline import Pipeline

from app.ml.model import create_model_pipeline, select_model_features


def test_create_model_pipeline_returns_pipeline() -> None:
    pipeline = create_model_pipeline()

    assert isinstance(pipeline, Pipeline)


def test_model_pipeline_can_fit_small_dataset() -> None:
    dataframe = pd.DataFrame(
        [
            _row("total_kills", "over", "after_draft", 1.92, 48.5, 1),
            _row("map_duration", "over", "pre_match", 1.80, 42.0, 0),
            _row("map_winner", "team_a", "live", 2.10, None, 1),
            _row("total_maps", "under", "after_draft", 1.70, 2.5, 0),
        ]
    )
    pipeline = create_model_pipeline()

    pipeline.fit(select_model_features(dataframe), dataframe["target"])
    probabilities = pipeline.predict_proba(select_model_features(dataframe))

    assert probabilities.shape[0] == len(dataframe)


def _row(
    market: str,
    selection: str,
    phase: str,
    odds: float,
    line: float | None,
    target: int,
) -> dict[str, object]:
    return {
        "market": market,
        "selection": selection,
        "phase": phase,
        "odds": odds,
        "line": line,
        "market_score": 25.0,
        "phase_score": 20.0,
        "line_score": 10.0,
        "streamer_score": 7.0,
        "risk_score": 5.0,
        "rule_final_score": 67.0,
        "hype_flag": False,
        "has_skip_warning": False,
        "streamer_strength_sum": 7.0,
        "streamer_confidence_max": 0.8,
        "target": target,
    }
