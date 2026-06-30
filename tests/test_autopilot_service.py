from typing import Any

from app.collectors import FakeMatchCollector, FakeOddsCollector, FakeTwitchCollector
from app.collectors.odds_collector import OddsCollector
from app.domain import Match, OddsSnapshot
from app.execution import ExecutionEngine, PaperExecutor
from app.services import AutopilotService, SessionManager


def make_config() -> dict[str, Any]:
    return {
        "mode": {"execution": "paper"},
        "session": {
            "tournament_keyword": "DreamLeague",
            "blocked_keywords": ["Qualifier", "Academy", "Showmatch"],
        },
        "streamer": {"channel": "streamer_name"},
        "betting": {
            "target_bets_per_match": 1.2,
            "max_bets_per_match": 3,
            "score_threshold": 62,
            "default_stake_pct": 0.35,
        },
    }


class RecordingOddsCollector(OddsCollector):
    def __init__(self) -> None:
        self.inner = FakeOddsCollector()
        self.seen_matches: list[Match] = []

    def fetch_odds(self, match: Match) -> list[OddsSnapshot]:
        self.seen_matches.append(match)
        return self.inner.fetch_odds(match)


def make_service(odds_collector: OddsCollector | None = None) -> AutopilotService:
    paper_executor = PaperExecutor()
    return AutopilotService(
        match_collector=FakeMatchCollector(),
        odds_collector=odds_collector or FakeOddsCollector(),
        twitch_collector=FakeTwitchCollector(),
        execution_engine=ExecutionEngine(paper_executor),
    )


def test_run_once_creates_at_least_one_paper_bet_with_fake_collectors() -> None:
    config = make_config()
    session = SessionManager().start_session(config)
    service = make_service()

    bets = service.run_once(session, config)

    assert len(bets) >= 1
    assert all(bet.mode == "paper" for bet in bets)
    assert all(bet.status == "placed" for bet in bets)


def test_run_once_ignores_matches_outside_session_scope() -> None:
    config = make_config()
    session = SessionManager().start_session(config)
    odds_collector = RecordingOddsCollector()
    service = make_service(odds_collector)

    service.run_once(session, config)

    assert len(odds_collector.seen_matches) == 1
    assert "DreamLeague" in odds_collector.seen_matches[0].tournament_name


def test_run_once_ignores_qualifier_matches() -> None:
    config = make_config()
    session = SessionManager().start_session(config)
    odds_collector = RecordingOddsCollector()
    service = make_service(odds_collector)

    service.run_once(session, config)

    assert all(
        "Qualifier" not in match.tournament_name
        for match in odds_collector.seen_matches
    )
