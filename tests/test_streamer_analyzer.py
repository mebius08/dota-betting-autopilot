from datetime import datetime, timezone

from app.collectors import RawStreamerUtterance
from app.domain import OddsSnapshot
from app.scoring import (
    analyze_streamer_utterance_text,
    map_raw_utterances_to_entities,
    streamer_score_for_candidate,
)


def test_over_kills_utterance_detected() -> None:
    analysis = analyze_streamer_utterance_text("тут овер киллов выглядит норм")

    assert analysis["signal_type"] == "over_kills"
    assert analysis["detected_market"] == "total_kills"
    assert analysis["detected_selection"] == "over"


def test_duration_over_utterance_detected() -> None:
    analysis = analyze_streamer_utterance_text("карта будет долгая")

    assert analysis["signal_type"] == "duration_over"
    assert analysis["detected_market"] == "map_duration"
    assert analysis["detected_selection"] == "over"


def test_duration_under_or_stomp_detected() -> None:
    analysis = analyze_streamer_utterance_text("быстрый стомп")

    assert analysis["signal_type"] == "duration_under"
    assert analysis["detected_market"] == "map_duration"
    assert analysis["detected_selection"] == "under"


def test_skip_warning_detected() -> None:
    analysis = analyze_streamer_utterance_text("лучше не лезть, мутно")

    assert analysis["signal_type"] == "skip_warning"
    assert float(str(analysis["strength"])) < 0
    assert float(str(analysis["confidence"])) >= 0.7


def test_hype_detected() -> None:
    analysis = analyze_streamer_utterance_text("all in хата")

    assert analysis["hype_flag"] is True
    assert float(str(analysis["strength"])) <= 0


def test_strength_is_clamped_to_expected_range() -> None:
    analysis = analyze_streamer_utterance_text(
        "all in хата фри бабки овер киллов овер киллов bloodbath"
    )
    strength = float(str(analysis["strength"]))

    assert -8 <= strength <= 8


def test_streamer_score_for_total_kills_over_positive() -> None:
    snapshot = make_snapshot(market="total_kills", selection="over")
    utterances = map_raw_utterances_to_entities(
        [
            RawStreamerUtterance(
                text="тут овер киллов выглядит норм",
                created_at=datetime.now(timezone.utc),
            )
        ],
        session_id="session-1",
        match_id="match-1",
    )

    assert streamer_score_for_candidate(snapshot, utterances) > 0


def test_streamer_score_penalizes_opposite_signal() -> None:
    snapshot = make_snapshot(market="total_kills", selection="over")
    utterances = map_raw_utterances_to_entities(
        [
            RawStreamerUtterance(
                text="андер киллов, мало киллов",
                created_at=datetime.now(timezone.utc),
            )
        ],
        session_id="session-1",
        match_id="match-1",
    )

    assert streamer_score_for_candidate(snapshot, utterances) < 0


def test_skip_warning_penalizes_any_candidate() -> None:
    snapshot = make_snapshot(market="map_winner", selection="Team Spirit")
    utterances = map_raw_utterances_to_entities(
        [
            RawStreamerUtterance(
                text="лучше не лезть, мутно",
                created_at=datetime.now(timezone.utc),
            )
        ],
        session_id="session-1",
        match_id="match-1",
    )

    assert streamer_score_for_candidate(snapshot, utterances) <= -5


def make_snapshot(market: str, selection: str) -> OddsSnapshot:
    return OddsSnapshot(
        id="snapshot-1",
        session_id="session-1",
        match_id="match-1",
        external_market_id="market-1",
        market=market,
        selection=selection,
        line=48.5,
        odds=1.92,
        phase="after_draft",
        is_live=False,
        is_suspended=False,
        bookmaker="fakebook",
        created_at=datetime.now(timezone.utc),
    )
