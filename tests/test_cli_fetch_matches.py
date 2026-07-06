from datetime import datetime, timezone

import pytest

from app import cli
import app.collectors
from app.domain import Match


def test_cli_help_includes_fetch_matches(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "fetch-matches" in output


def test_fetch_matches_command_prints_match_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app.collectors, "PandaScoreMatchCollector", _FakeCollector)

    exit_code = cli.main(
        [
            "fetch-matches",
            "--provider",
            "pandascore",
            "--status",
            "upcoming",
            "--limit",
            "5",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Provider: pandascore" in output
    assert "Matches: 1" in output
    assert "Team Spirit vs PARIVISION" in output
    assert "Tournament: DreamLeague" in output
    assert "Status: upcoming" in output


def test_fetch_matches_command_prints_empty_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app.collectors, "PandaScoreMatchCollector", _EmptyCollector)

    exit_code = cli.main(["fetch-matches", "--provider", "pandascore"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Matches: 0" in output
    assert "No matches found." in output


def test_fetch_matches_command_missing_token_is_friendly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("PANDASCORE_TOKEN", raising=False)

    exit_code = cli.main(["fetch-matches", "--provider", "pandascore"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "PandaScore token is not configured." in output
    assert "Set PANDASCORE_TOKEN" in output


class _FakeCollector:
    def __init__(
        self,
        *,
        timeout: float,
        limit: int,
        status_filter: str,
    ) -> None:
        assert timeout == 10.0
        assert limit == 5
        assert status_filter == "upcoming"

    def collect(self) -> list[Match]:
        return [
            Match(
                id="pandascore-1",
                session_id="pandascore",
                tournament_name="DreamLeague",
                team_a="Team Spirit",
                team_b="PARIVISION",
                format="bo3",
                status="upcoming",
                start_time=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
                external_id="1",
            )
        ]


class _EmptyCollector:
    def __init__(
        self,
        *,
        timeout: float,
        limit: int,
        status_filter: str,
    ) -> None:
        pass

    def collect(self) -> list[Match]:
        return []
