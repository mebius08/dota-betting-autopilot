from datetime import datetime, timezone

from app.domain import Match, OddsSnapshot


class OddsCollector:
    def fetch_odds(self, match: Match) -> list[OddsSnapshot]:
        raise NotImplementedError


class FakeOddsCollector(OddsCollector):
    def fetch_odds(self, match: Match) -> list[OddsSnapshot]:
        now = datetime.now(timezone.utc)
        return [
            OddsSnapshot(
                id=f"{match.id}-total-kills",
                session_id=match.session_id,
                match_id=match.id,
                external_market_id="fake-market-total-kills",
                market="total_kills",
                selection="over",
                line=48.5,
                odds=1.92,
                phase="after_draft",
                is_live=False,
                is_suspended=False,
                bookmaker="fakebook",
                created_at=now,
            ),
            OddsSnapshot(
                id=f"{match.id}-duration",
                session_id=match.session_id,
                match_id=match.id,
                external_market_id="fake-market-duration",
                market="map_duration",
                selection="over",
                line=39.5,
                odds=2.05,
                phase="after_draft",
                is_live=False,
                is_suspended=False,
                bookmaker="fakebook",
                created_at=now,
            ),
            OddsSnapshot(
                id=f"{match.id}-map-winner-a",
                session_id=match.session_id,
                match_id=match.id,
                external_market_id="fake-market-winner-a",
                market="map_winner",
                selection=match.team_a,
                line=None,
                odds=1.85,
                phase="pre_match",
                is_live=False,
                is_suspended=False,
                bookmaker="fakebook",
                created_at=now,
            ),
            OddsSnapshot(
                id=f"{match.id}-first-blood",
                session_id=match.session_id,
                match_id=match.id,
                external_market_id="fake-market-first-blood",
                market="first_blood",
                selection=match.team_b,
                line=None,
                odds=1.12,
                phase="pre_match",
                is_live=False,
                is_suspended=False,
                bookmaker="fakebook",
                created_at=now,
            ),
            OddsSnapshot(
                id=f"{match.id}-next-kill",
                session_id=match.session_id,
                match_id=match.id,
                external_market_id="fake-market-next-kill",
                market="next_kill",
                selection=match.team_a,
                line=None,
                odds=4.2,
                phase="live",
                is_live=True,
                is_suspended=False,
                bookmaker="fakebook",
                created_at=now,
            ),
            OddsSnapshot(
                id=f"{match.id}-suspended-live-total",
                session_id=match.session_id,
                match_id=match.id,
                external_market_id="fake-market-suspended-live-total",
                market="live_total_kills",
                selection="over",
                line=54.5,
                odds=2.15,
                phase="live",
                is_live=True,
                is_suspended=True,
                bookmaker="fakebook",
                created_at=now,
            ),
        ]
