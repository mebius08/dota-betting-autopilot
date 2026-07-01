from typing import Any

from sklearn.compose import ColumnTransformer  # type: ignore[import-untyped]
from sklearn.impute import SimpleImputer  # type: ignore[import-untyped]
from sklearn.linear_model import LogisticRegression  # type: ignore[import-untyped]
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
from sklearn.preprocessing import (  # type: ignore[import-untyped]
    OneHotEncoder,
    StandardScaler,
)


CATEGORICAL_FEATURES = ["market", "selection", "phase"]
NUMERIC_FEATURES = [
    "odds",
    "line",
    "market_score",
    "phase_score",
    "line_score",
    "streamer_score",
    "risk_score",
    "rule_final_score",
    "streamer_strength_sum",
    "streamer_confidence_max",
    "hype_flag",
    "has_skip_warning",
]


def create_model_pipeline() -> Pipeline:
    numeric_pipeline = Pipeline(
        steps=[
            ("imputer", SimpleImputer(strategy="constant", fill_value=0.0)),
            ("scaler", StandardScaler()),
        ]
    )
    preprocessor = ColumnTransformer(
        transformers=[
            (
                "categorical",
                OneHotEncoder(handle_unknown="ignore"),
                CATEGORICAL_FEATURES,
            ),
            ("numeric", numeric_pipeline, NUMERIC_FEATURES),
        ]
    )
    model = LogisticRegression(max_iter=1000, class_weight="balanced")

    return Pipeline(
        steps=[
            ("preprocessor", preprocessor),
            ("model", model),
        ]
    )


def get_feature_columns() -> list[str]:
    return [*CATEGORICAL_FEATURES, *NUMERIC_FEATURES]


def select_model_features(dataframe: Any) -> Any:
    return dataframe[get_feature_columns()].copy()
