from pathlib import Path
from typing import Any

import joblib  # type: ignore[import-untyped]
import pandas as pd

from app.domain import BetCandidate, StreamerUtterance
from app.ml.features import build_feature_row, feature_row_to_dict
from app.ml.model import select_model_features


class MLBetPredictor:
    def __init__(self, model_path: str | Path) -> None:
        self.model_path = Path(model_path)
        self._model: Any | None = None
        if self.model_path.exists():
            self._model = joblib.load(self.model_path)

    def is_available(self) -> bool:
        return self._model is not None

    def predict_good_bet_probability(
        self,
        candidate: BetCandidate,
        utterances: list[StreamerUtterance],
    ) -> float | None:
        if self._model is None:
            return None

        row = feature_row_to_dict(build_feature_row(candidate, utterances))
        dataframe = pd.DataFrame([row])
        probabilities = self._model.predict_proba(select_model_features(dataframe))[0]
        classes = list(self._model.classes_)
        if 1 not in classes:
            return None

        return float(probabilities[classes.index(1)])

    def predict_ml_score(
        self,
        candidate: BetCandidate,
        utterances: list[StreamerUtterance],
    ) -> float | None:
        probability = self.predict_good_bet_probability(candidate, utterances)
        if probability is None:
            return None
        return probability * 100
