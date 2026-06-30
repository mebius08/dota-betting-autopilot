from datetime import datetime, timezone

from app.domain import OddsPhase, OddsSnapshot, StreamerUtterance
from app.scoring import score_odds_snapshot


def make_snapshot(
    *,
    market: str = "total_kills",
    selection: str = "over",
    odds: float = 1.92,
    phase: OddsPhase = "after_draft",
    is_live: bool = False,
    is_suspended: bool = False,
) -> OddsSnapshot:
    return OddsSnapshot(
        id="snapshot-1",
        session_id="session-1",
        match_id="match-1",
        external_market_id="market-1",
        market=market,
        selection=selection,
        line=48.5,
        odds=odds,
        phase=phase,
        is_live=is_live,
        is_suspended=is_suspended,
        bookmaker="fakebook",
        created_at=datetime.now(timezone.utc),
    )


def test_a_tier_after_draft_good_odds_has_high_score() -> None:
    score = score_odds_snapshot(
        make_snapshot(),
        [make_utterance(signal_type="over_kills", strength=8.0)],
    )

    assert score.final_score >= 62
    assert "A-tier" in score.explanation


def test_suspended_market_gets_strong_negative_phase_score() -> None:
    score = score_odds_snapshot(
        make_snapshot(
            market="live_total_kills",
            phase="live",
            is_live=True,
            is_suspended=True,
        ),
        [],
    )

    assert score.phase_score == -20
    assert score.final_score < 20


def test_too_low_odds_gets_negative_line_score() -> None:
    score = score_odds_snapshot(make_snapshot(odds=1.12), [])

    assert score.line_score == -20


def test_streamer_over_kills_increases_score() -> None:
    without_signal = score_odds_snapshot(make_snapshot(), [])
    with_signal = score_odds_snapshot(
        make_snapshot(),
        [make_utterance(signal_type="over_kills", strength=8.0)],
    )

    assert with_signal.final_score > without_signal.final_score


def test_skip_warning_decreases_score() -> None:
    without_signal = score_odds_snapshot(make_snapshot(), [])
    with_skip_warning = score_odds_snapshot(
        make_snapshot(),
        [make_utterance(signal_type="skip_warning", strength=-8.0)],
    )

    assert with_skip_warning.final_score < without_signal.final_score


def test_hype_does_not_make_bad_market_good() -> None:
    score = score_odds_snapshot(
        make_snapshot(market="first_blood"),
        [make_utterance(signal_type="hype", strength=-3.0, hype_flag=True)],
    )

    assert score.final_score < 62


def make_utterance(
    signal_type: str,
    strength: float,
    hype_flag: bool = False,
) -> StreamerUtterance:
    return StreamerUtterance(
        id="utterance-1",
        session_id="session-1",
        match_id="match-1",
        source="fake",
        text="test utterance",
        detected_market="total_kills",
        detected_selection="over",
        detected_team=None,
        signal_type=signal_type,
        strength=strength,
        confidence=0.8,
        hype_flag=hype_flag,
        created_at=datetime.now(timezone.utc),
    )
