from datetime import datetime, timezone
from pathlib import Path

from app.domain import Match, Session, StreamerUtterance
from app.storage import SQLiteRepository


def test_save_streamer_utterance(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    session = make_session("session-1")
    match = make_match(session.id, "match-1")
    utterance = make_utterance(session.id, match.id, "utterance-1")

    repository.save_session(session)
    repository.save_match(match)
    repository.save_streamer_utterance(utterance)

    utterances = repository.list_streamer_utterances_by_session(session.id)

    assert utterances == [utterance]


def test_save_streamer_utterances_batch(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    session = make_session("session-1")
    first_utterance = make_utterance(session.id, None, "utterance-1")
    second_utterance = make_utterance(session.id, None, "utterance-2")

    repository.save_session(session)
    repository.save_streamer_utterances([first_utterance, second_utterance])

    utterances = repository.list_streamer_utterances_by_session(session.id)

    assert len(utterances) == 2


def test_list_streamer_utterances_by_session(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    first_session = make_session("session-1")
    second_session = make_session("session-2")
    first_match = make_match(first_session.id, "match-1")
    second_match = make_match(second_session.id, "match-2")
    first_utterance = make_utterance(
        first_session.id,
        first_match.id,
        "utterance-1",
    )
    second_utterance = make_utterance(
        second_session.id,
        second_match.id,
        "utterance-2",
    )

    for session in (first_session, second_session):
        repository.save_session(session)
    for match in (first_match, second_match):
        repository.save_match(match)
    repository.save_streamer_utterances([first_utterance, second_utterance])

    utterances = repository.list_streamer_utterances_by_session(first_session.id)

    assert utterances == [first_utterance]


def test_list_streamer_utterances_by_match(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    session = make_session("session-1")
    first_match = make_match(session.id, "match-1")
    first_utterance = make_utterance(session.id, first_match.id, "utterance-1")
    second_utterance = make_utterance(session.id, None, "utterance-2")

    repository.save_session(session)
    repository.save_match(first_match)
    repository.save_streamer_utterances([first_utterance, second_utterance])

    utterances = repository.list_streamer_utterances_by_match(first_match.id)

    assert utterances == [first_utterance]


def test_datetime_roundtrip(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    session = make_session("session-1")
    utterance = make_utterance(session.id, None, "utterance-1")

    repository.save_session(session)
    repository.save_streamer_utterance(utterance)

    loaded = repository.list_streamer_utterances_by_session(session.id)

    assert loaded[0].created_at == utterance.created_at


def make_session(session_id: str) -> Session:
    return Session(
        id=session_id,
        name="DreamLeague",
        tournament_keyword="DreamLeague",
        streamer_channel="streamer_name",
        execution_mode="paper",
        target_bets_per_match=1.2,
        max_bets_per_match=3,
        score_threshold=62,
        active=True,
        created_at=datetime(2026, 6, 30, 8, 0, tzinfo=timezone.utc),
        ended_at=None,
    )


def make_match(session_id: str, match_id: str) -> Match:
    return Match(
        id=match_id,
        session_id=session_id,
        tournament_name="DreamLeague Season 25",
        team_a="Team Spirit",
        team_b="Gaimin Gladiators",
        format="bo3",
        status="upcoming",
        start_time=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc),
        external_id="external-match",
    )


def make_utterance(
    session_id: str,
    match_id: str,
    utterance_id: str,
) -> StreamerUtterance:
    return StreamerUtterance(
        id=utterance_id,
        session_id=session_id,
        match_id=match_id,
        source="fake",
        text="тут овер киллов выглядит норм",
        detected_market="total_kills",
        detected_selection="over",
        detected_team=None,
        signal_type="over_kills",
        strength=7.0,
        confidence=0.8,
        hype_flag=False,
        created_at=datetime(2026, 6, 30, 8, 5, tzinfo=timezone.utc),
    )
