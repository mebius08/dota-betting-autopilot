from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import cli
import app.public_pages as public_pages


def test_cli_help_includes_public_match_page_probe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "probe-public-match-pages" in output


def test_public_match_page_probe_prints_fake_result_without_db(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(public_pages, "PublicPageHttpClient", _FakePublicClient)
    monkeypatch.setattr(public_pages, "PublicMatchPageProbe", _FakePublicProbe)

    exit_code = cli.main(
        [
            "probe-public-match-pages",
            "--source",
            "stratz",
            "--match-id",
            "8886013461",
            "--timeout",
            "2.5",
            "--delay-seconds",
            "0",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Public professional Dota match page feasibility probe" in output
    assert "Source: stratz" in output
    assert "INSUFFICIENT SOURCE" in output


def test_public_match_page_probe_requires_target(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(public_pages, "PublicPageHttpClient", _FakePublicClient)
    monkeypatch.setattr(public_pages, "PublicMatchPageProbe", _FakePublicProbe)

    exit_code = cli.main(["probe-public-match-pages", "--delay-seconds", "0"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "At least one --match-id or --page-url is required." in output


class _FakePublicClient:
    def __init__(self, *, timeout: float) -> None:
        assert timeout == 2.5 or timeout == 10.0


class _FakePublicProbe:
    def __init__(self, client: _FakePublicClient) -> None:
        self.client = client

    def run(
        self,
        *,
        source: public_pages.PublicPageSource,
        match_ids: tuple[str, ...],
        page_urls: tuple[str, ...],
        delay_seconds: float,
        fetch_referenced_resources: bool,
    ) -> public_pages.PublicPageProbeResult:
        if not match_ids and not page_urls:
            raise ValueError("At least one --match-id or --page-url is required.")
        assert source is public_pages.PublicPageSource.STRATZ
        assert match_ids == ("8886013461",)
        assert page_urls == ()
        assert delay_seconds == 0
        assert fetch_referenced_resources is True
        return public_pages.PublicPageProbeResult(
            source=source,
            probe_started_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
            request_count=2,
            policy=public_pages.PublicPolicyCheck(
                source=source,
                robots_url="https://stratz.com/robots.txt",
                http_status=200,
                content_type="text/plain",
                byte_size=100,
                checked_path="/match/8886013461",
                path_disallowed=False,
                relevant_rules=(),
                content_signals=(),
            ),
            analyses=(
                public_pages.PublicPageAnalysis(
                    source=source,
                    match_id="8886013461",
                    url="https://stratz.com/match/8886013461",
                    http_status=403,
                    content_type="text/html",
                    byte_size=0,
                    access_status=public_pages.PublicPageAccessStatus.HTTP_FORBIDDEN,
                    static_html_findings=("HTTP 403 forbidden.",),
                    embedded_state_findings=("not inspected",),
                    referenced_resource_findings=("not inspected",),
                    observations={},
                ),
            ),
            coverage=(),
            recommendation=public_pages.PublicSourceRecommendation.INSUFFICIENT_SOURCE,
        )
