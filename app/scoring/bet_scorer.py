from dataclasses import dataclass

from app.domain import OddsSnapshot, StreamerUtterance
from app.scoring.market_classifier import classify_market, market_quality_score
from app.scoring.streamer_analyzer import streamer_score_for_candidate


@dataclass
class ScoreBreakdown:
    market_score: float
    phase_score: float
    line_score: float
    streamer_score: float
    risk_score: float
    penalties: float
    final_score: float
    explanation: str


def phase_score(phase: str, is_live: bool, is_suspended: bool) -> float:
    if phase == "finished":
        return -100.0
    if is_suspended:
        return -20.0
    if phase == "after_draft":
        return 20.0
    if phase == "pre_match":
        return 10.0
    if phase == "live":
        return 15.0
    return 0.0


def odds_quality_score(
    odds: float,
    min_odds: float = 1.30,
    max_odds: float = 3.50,
) -> float:
    if odds < min_odds:
        return -20.0
    if 1.40 <= odds <= 2.40:
        return 10.0
    if 2.40 < odds <= max_odds:
        return 6.0
    if odds > max_odds:
        return -5.0
    return 0.0


def score_odds_snapshot(
    snapshot: OddsSnapshot,
    streamer_utterances: list[StreamerUtterance],
    riskophile_bonus: float = 5.0,
) -> ScoreBreakdown:
    market_score = market_quality_score(snapshot.market)
    snapshot_phase_score = phase_score(
        snapshot.phase,
        snapshot.is_live,
        snapshot.is_suspended,
    )
    line_score = odds_quality_score(snapshot.odds)
    streamer_score = streamer_score_for_candidate(snapshot, streamer_utterances)
    risk_score = riskophile_bonus
    penalties = 0.0
    final_score = (
        market_score
        + snapshot_phase_score
        + line_score
        + streamer_score
        + risk_score
        + penalties
    )

    return ScoreBreakdown(
        market_score=market_score,
        phase_score=snapshot_phase_score,
        line_score=line_score,
        streamer_score=streamer_score,
        risk_score=risk_score,
        penalties=penalties,
        final_score=final_score,
        explanation=_build_explanation(
            snapshot=snapshot,
            market_score=market_score,
            phase_score_value=snapshot_phase_score,
            line_score=line_score,
            streamer_score=streamer_score,
            risk_score=risk_score,
        ),
    )


def _build_explanation(
    *,
    snapshot: OddsSnapshot,
    market_score: float,
    phase_score_value: float,
    line_score: float,
    streamer_score: float,
    risk_score: float,
) -> str:
    tier = classify_market(snapshot.market)
    parts = [f"{tier}-tier market"]

    if snapshot.is_suspended:
        parts.append("market is suspended")
    elif snapshot.phase == "after_draft":
        parts.append("after draft phase")
    elif snapshot.phase == "pre_match":
        parts.append("pre-match phase")
    elif snapshot.phase == "live":
        parts.append("live phase")
    elif snapshot.phase == "finished":
        parts.append("finished phase")
    else:
        parts.append("unknown phase")

    if line_score >= 10:
        parts.append("odds in good range")
    elif line_score > 0:
        parts.append("odds are acceptable")
    elif line_score < 0:
        parts.append("odds outside preferred range")
    else:
        parts.append("odds are neutral")

    if streamer_score > 0:
        parts.append("streamer speech supports selection")
    elif streamer_score < 0:
        parts.append("streamer speech penalty applied")
    else:
        parts.append("no streamer edge")

    if market_score < 0:
        parts.append("low quality market")
    if phase_score_value < 0:
        parts.append("phase penalty applied")
    if risk_score:
        parts.append("riskophile bonus applied")

    return "; ".join(parts)
