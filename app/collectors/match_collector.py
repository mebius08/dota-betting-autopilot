from datetime import datetime, timedelta, timezone

from app.domain import Match, Session


class MatchCollector:
    def fetch_matches(self, session: Session) -> list[Match]:
        raise NotImplementedError


class FakeMatchCollector(MatchCollector):
    def fetch_matches(self, session: Session) -> list[Match]:
        now = datetime.now(timezone.utc)
        return [
            Match(
                id=f"{session.id}-dreamleague-main",
                session_id=session.id,
                tournament_name=f"{session.tournament_keyword} Season 25",
                team_a="Team Spirit",
                team_b="Gaimin Gladiators",
                format="bo3",
                status="upcoming",
                start_time=now + timedelta(hours=1),
                external_id="fake-match-1",
            ),
            Match(
                id=f"{session.id}-other-tournament",
                session_id=session.id,
                tournament_name="Elite League",
                team_a="BetBoom Team",
                team_b="Team Liquid",
                format="bo3",
                status="upcoming",
                start_time=now + timedelta(hours=2),
                external_id="fake-match-2",
            ),
            Match(
                id=f"{session.id}-qualifier",
                session_id=session.id,
                tournament_name=f"{session.tournament_keyword} Qualifier",
                team_a="Nouns",
                team_b="Shopify Rebellion",
                format="bo3",
                status="live",
                start_time=now - timedelta(minutes=15),
                external_id="fake-match-3",
            ),
        ]


def match_in_scope(
    match: Match,
    tournament_keyword: str,
    blocked_keywords: list[str],
) -> bool:
    tournament_name = match.tournament_name.casefold()
    keyword = tournament_keyword.strip().casefold()
    if not keyword or keyword not in tournament_name:
        return False

    for blocked_keyword in blocked_keywords:
        normalized_blocked_keyword = blocked_keyword.strip().casefold()
        if normalized_blocked_keyword and normalized_blocked_keyword in tournament_name:
            return False

    return match.status in ("upcoming", "live")
