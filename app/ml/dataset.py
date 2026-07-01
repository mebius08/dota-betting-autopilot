from typing import Any

import pandas as pd

from app.domain import Bet, BetCandidate, StreamerUtterance
from app.ml.features import build_feature_row, feature_row_to_dict


FEATURE_COLUMNS = [
    "candidate_id",
    "market",
    "selection",
    "phase",
    "odds",
    "line",
    "market_score",
    "phase_score",
    "line_score",
    "streamer_score",
    "risk_score",
    "rule_final_score",
    "hype_flag",
    "has_skip_warning",
    "streamer_strength_sum",
    "streamer_confidence_max",
]
TRAINING_COLUMNS = [*FEATURE_COLUMNS, "profit_units", "target"]


def build_training_dataframe(
    bets: list[Bet],
    candidates: list[BetCandidate],
    utterances_by_match: dict[str, list[StreamerUtterance]],
) -> pd.DataFrame:
    candidates_by_id = {candidate.id: candidate for candidate in candidates}
    rows: list[dict[str, Any]] = []

    for bet in bets:
        target = _target_from_bet(bet)
        if target is None:
            continue

        candidate = candidates_by_id.get(bet.candidate_id)
        if candidate is None:
            continue

        feature_row = build_feature_row(
            candidate,
            utterances_by_match.get(candidate.match_id, []),
        )
        row = feature_row_to_dict(feature_row)
        row["profit_units"] = bet.profit_units
        row["target"] = target
        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=TRAINING_COLUMNS)

    return pd.DataFrame(rows, columns=TRAINING_COLUMNS)


def _target_from_bet(bet: Bet) -> int | None:
    if bet.result == "win":
        return 1
    if bet.result == "loss":
        return 0
    return None
