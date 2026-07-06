from datetime import datetime, timezone

import pytest

from app import cli
import app.collectors
from app.collectors.oddspapi_odds_collector import (
    OddsPapiFixture,
    OddsPapiFixtureOdds,
)
from app.domain import OddsSnapshot


def test_cli_help_includes_fetch_odds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "fetch-odds" in output


def test_fetch_odds_command_prints_grouped_odds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app.collectors, "OddsPapiOddsCollector", _FakeCollector)

    exit_code = cli.main(
        [
            "fetch-odds",
            "--provider",
            "oddspapi",
            "--limit",
            "5",
            "--bookmakers",
            "pinnacle, bet365",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Provider: oddspapi" in output
    assert "Dota 2 fixtures with odds: 1" in output
    assert "Team Spirit vs PARIVISION" in output
    assert "Fixture: fixture-1" in output
    assert "Bookmaker: pinnacle" in output
    assert "Market: map_winner" in output
    assert "Team Spirit: 1.82" in output
    assert "PARIVISION: 2.04" in output


def test_fetch_odds_command_prints_empty_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app.collectors, "OddsPapiOddsCollector", _EmptyCollector)

    exit_code = cli.main(["fetch-odds", "--provider", "oddspapi"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Dota 2 fixtures with odds: 0" in output
    assert "No Dota 2 odds found." in output


def test_fetch_odds_command_missing_key_is_friendly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ODDSPAPI_API_KEY", raising=False)

    exit_code = cli.main(["fetch-odds", "--provider", "oddspapi"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "OddsPapi API key is not configured." in output
    assert "Set ODDSPAPI_API_KEY" in output


class _FakeCollector:
    def __init__(
        self,
        *,
        timeout: float,
        limit: int,
        bookmakers: list[str],
    ) -> None:
        assert timeout == 10.0
        assert limit == 5
        assert bookmakers == ["pinnacle", "bet365"]

    def collect(self) -> list[OddsPapiFixtureOdds]:
        created_at = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
        fixture = OddsPapiFixture(
            id="fixture-1",
            team_a="Team Spirit",
            team_b="PARIVISION",
            start_time=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
            has_odds=True,
        )
        return [
            OddsPapiFixtureOdds(
                fixture=fixture,
                snapshots=[
                    _snapshot(
                        selection="Team Spirit",
                        odds=1.82,
                        bookmaker="pinnacle",
                        created_at=created_at,
                    ),
                    _snapshot(
                        selection="PARIVISION",
                        odds=2.04,
                        bookmaker="pinnacle",
                        created_at=created_at,
                    ),
                ],
            )
        ]


class _EmptyCollector:
    def __init__(
        self,
        *,
        timeout: float,
        limit: int,
        bookmakers: list[str],
    ) -> None:
        pass

    def collect(self) -> list[OddsPapiFixtureOdds]:
        return []


def _snapshot(
    *,
    selection: str,
    odds: float,
    bookmaker: str,
    created_at: datetime,
) -> OddsSnapshot:
    return OddsSnapshot(
        id=f"{selection}-{bookmaker}",
        session_id="oddspapi",
        match_id="oddspapi-fixture-1",
        external_market_id="market-1",
        market="map_winner",
        selection=selection,
        line=None,
        odds=odds,
        phase="pre_match",
        is_live=False,
        is_suspended=False,
        bookmaker=bookmaker,
        created_at=created_at,
    )
