import pytest

from app.services import SessionManager


def make_config(**overrides):
    config = {
        "mode": {"execution": "paper"},
        "session": {"tournament_keyword": "DreamLeague"},
        "streamer": {"channel": "streamer_name"},
        "betting": {
            "target_bets_per_match": 1.2,
            "max_bets_per_match": 3,
            "score_threshold": 62,
        },
    }

    for section, values in overrides.items():
        config[section].update(values)

    return config


def test_start_session_creates_active_session() -> None:
    manager = SessionManager()

    session = manager.start_session(make_config())

    assert manager.is_active() is True
    assert manager.get_active_session() == session
    assert session.active is True
    assert session.execution_mode == "paper"
    assert session.tournament_keyword == "DreamLeague"
    assert session.streamer_channel == "streamer_name"
    assert session.target_bets_per_match == 1.2
    assert session.max_bets_per_match == 3
    assert session.score_threshold == 62


def test_start_session_rejects_second_active_session() -> None:
    manager = SessionManager()
    manager.start_session(make_config())

    with pytest.raises(RuntimeError, match="Active session already exists"):
        manager.start_session(make_config())


def test_start_session_validates_config_values() -> None:
    manager = SessionManager()

    with pytest.raises(ValueError, match="execution_mode"):
        manager.start_session(make_config(mode={"execution": "manual"}))

    with pytest.raises(ValueError, match="tournament_keyword"):
        manager.start_session(make_config(session={"tournament_keyword": ""}))

    with pytest.raises(ValueError, match="streamer_channel"):
        manager.start_session(make_config(streamer={"channel": ""}))

    with pytest.raises(ValueError, match="target_bets_per_match"):
        manager.start_session(make_config(betting={"target_bets_per_match": 0}))

    with pytest.raises(ValueError, match="max_bets_per_match"):
        manager.start_session(make_config(betting={"max_bets_per_match": 0}))

    with pytest.raises(ValueError, match="score_threshold"):
        manager.start_session(make_config(betting={"score_threshold": 101}))


def test_stop_session_deactivates_active_session() -> None:
    manager = SessionManager()
    session = manager.start_session(make_config())

    stopped_session = manager.stop_session(session.id)

    assert stopped_session == session
    assert stopped_session.active is False
    assert stopped_session.ended_at is not None
    assert manager.get_active_session() is None
    assert manager.is_active() is False


def test_stop_session_validates_state_and_session_id() -> None:
    manager = SessionManager()

    with pytest.raises(RuntimeError, match="Active session does not exist"):
        manager.stop_session("missing")

    session = manager.start_session(make_config())

    with pytest.raises(ValueError, match="session_id"):
        manager.stop_session(f"{session.id}-wrong")
