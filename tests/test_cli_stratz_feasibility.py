from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import cli
import app.stratz as stratz


def test_cli_help_includes_stratz_feasibility_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "probe-stratz-history" in output


def test_stratz_probe_missing_token_is_friendly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(stratz.STRATZ_TOKEN_ENV, raising=False)

    exit_code = cli.main(["probe-stratz-history", "--sample-size", "1"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "STRATZ token is not configured." in captured.out
    assert stratz.STRATZ_TOKEN_ENV in captured.out
    assert "Traceback" not in captured.err


def test_stratz_probe_prints_fake_result_without_db(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(stratz, "StratzGraphQLClient", _FakeStratzClient)
    monkeypatch.setattr(stratz, "StratzFeasibilityProbe", _FakeStratzProbe)

    exit_code = cli.main(
        [
            "probe-stratz-history",
            "--sample-size",
            "2",
            "--match-id",
            "9001",
            "--timeout",
            "2.5",
            "--delay-seconds",
            "0",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "STRATZ free historical game data feasibility probe" in output
    assert "Sampled match IDs: 9001" in output
    assert "STRATZ_FREE_SOURCE_INSUFFICIENT" in output


class _FakeStratzClient:
    def __init__(self, *, timeout: float) -> None:
        assert timeout == 2.5


class _FakeStratzProbe:
    def __init__(self, client: _FakeStratzClient) -> None:
        self.client = client

    def run(
        self,
        *,
        sample_size: int,
        match_ids: tuple[str, ...],
        delay_seconds: float,
        real_source: bool,
    ) -> stratz.StratzProbeResult:
        assert sample_size == 2
        assert match_ids == ("9001",)
        assert delay_seconds == 0
        assert real_source is True
        return stratz.StratzProbeResult(
            real_source=True,
            probe_started_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
            request_count=1,
            sampled_match_ids=("9001",),
            sample_selection_method="fixture",
            query_field_names=("match",),
            query_plan=None,
            access_capability=None,
            analyses=(),
            coverage=(),
            verdict=stratz.SourceVerdict.STRATZ_FREE_SOURCE_INSUFFICIENT,
        )
