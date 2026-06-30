from datetime import datetime, timezone

from app.collectors import RawStreamerUtterance
from app.scoring import map_raw_utterances_to_entities


def test_mapper_detects_total_kills() -> None:
    utterance = map_one("тут овер киллов выглядит норм")

    assert utterance.detected_market == "total_kills"
    assert utterance.detected_selection == "over"
    assert utterance.signal_type == "over_kills"
    assert utterance.strength > 0


def test_mapper_detects_duration() -> None:
    utterance = map_one("карта будет долгая")

    assert utterance.detected_market == "map_duration"
    assert utterance.detected_selection == "over"


def test_mapper_detects_hype() -> None:
    utterance = map_one("all in хата")

    assert utterance.hype_flag is True


def test_mapper_sets_session_and_match() -> None:
    utterance = map_one(
        "тут овер киллов выглядит норм",
        session_id="session-42",
        match_id="match-99",
    )

    assert utterance.session_id == "session-42"
    assert utterance.match_id == "match-99"


def map_one(
    text: str,
    session_id: str = "session-1",
    match_id: str | None = "match-1",
):
    raw_utterances = [
        RawStreamerUtterance(
            text=text,
            created_at=datetime.now(timezone.utc),
        )
    ]
    return map_raw_utterances_to_entities(
        raw_utterances,
        session_id=session_id,
        match_id=match_id,
    )[0]
