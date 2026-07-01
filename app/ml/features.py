from dataclasses import asdict, dataclass

from app.domain import BetCandidate, StreamerUtterance


@dataclass
class MLFeatureRow:
    candidate_id: str
    market: str
    selection: str
    phase: str
    odds: float
    line: float | None
    market_score: float
    phase_score: float
    line_score: float
    streamer_score: float
    risk_score: float
    rule_final_score: float
    hype_flag: bool
    has_skip_warning: bool
    streamer_strength_sum: float
    streamer_confidence_max: float


def build_feature_row(
    candidate: BetCandidate,
    utterances: list[StreamerUtterance],
) -> MLFeatureRow:
    return MLFeatureRow(
        candidate_id=candidate.id,
        market=candidate.market,
        selection=candidate.selection,
        phase=candidate.phase,
        odds=candidate.odds,
        line=candidate.line,
        market_score=candidate.market_score,
        phase_score=candidate.phase_score,
        line_score=candidate.line_score,
        streamer_score=candidate.streamer_score,
        risk_score=candidate.risk_score,
        rule_final_score=candidate.final_score,
        hype_flag=any(utterance.hype_flag for utterance in utterances),
        has_skip_warning=any(
            utterance.signal_type == "skip_warning" for utterance in utterances
        ),
        streamer_strength_sum=sum(utterance.strength for utterance in utterances),
        streamer_confidence_max=max(
            (utterance.confidence for utterance in utterances),
            default=0.0,
        ),
    )


def feature_row_to_dict(row: MLFeatureRow) -> dict[str, object]:
    return asdict(row)
