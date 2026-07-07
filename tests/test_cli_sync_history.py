from datetime import datetime
from pathlib import Path

import pytest

from app import cli
import app.history
from app.history import HistoricalCollectionResult
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match


def test_cli_help_includes_sync_history(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "sync-history" in output


def test_sync_history_command_prints_summary_and_persists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        app.history,
        "PandaScoreHistoricalMatchCollector",
        _FakeHistoryCollector,
    )
    db_path = tmp_path / "test.db"

    exit_code = cli.main(
        [
            "sync-history",
            "--provider",
            "pandascore",
            "--db",
            str(db_path),
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-31",
            "--page-size",
            "2",
            "--max-pages",
            "3",
            "--timeout",
            "2.5",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Historical Dota sync" in output
    assert "Provider: pandascore" in output
    assert "Since: 2026-01-01" in output
    assert "Until: 2026-01-31" in output
    assert "Max pages: 3" in output
    assert "Fetched provider rows: 2" in output
    assert "Mapped historical matches: 1" in output
    assert "Usable winner records: 1" in output
    assert "Inserted: 1" in output
    assert SQLiteRepository(db_path).count_historical_matches() == 1


def test_sync_history_without_max_pages_passes_provider_completion_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        app.history,
        "PandaScoreHistoricalMatchCollector",
        _NoMaxPagesHistoryCollector,
    )
    db_path = tmp_path / "test.db"

    exit_code = cli.main(
        [
            "sync-history",
            "--provider",
            "pandascore",
            "--db",
            str(db_path),
            "--since",
            "2025-07-08",
            "--until",
            "2026-07-07",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Max pages: provider completion" in output
    assert SQLiteRepository(db_path).count_historical_matches() == 1


def test_sync_history_rejects_invalid_date(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(
        [
            "sync-history",
            "--provider",
            "pandascore",
            "--since",
            "not-a-date",
            "--until",
            "2026-01-31",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "YYYY-MM-DD" in captured.err


def test_sync_history_rejects_since_after_until(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(
        [
            "sync-history",
            "--provider",
            "pandascore",
            "--since",
            "2026-02-01",
            "--until",
            "2026-01-31",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "--since must be before or equal to --until." in output


def test_sync_history_missing_token_is_friendly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("PANDASCORE_TOKEN", raising=False)

    exit_code = cli.main(
        [
            "sync-history",
            "--provider",
            "pandascore",
            "--db",
            str(tmp_path / "test.db"),
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-31",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "PandaScore token is not configured." in captured.out
    assert "Set PANDASCORE_TOKEN" in captured.out
    assert "Traceback" not in captured.err


def test_sync_history_does_not_print_token_from_controlled_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        app.history,
        "PandaScoreHistoricalMatchCollector",
        _TokenErrorCollector,
    )

    exit_code = cli.main(
        [
            "sync-history",
            "--provider",
            "pandascore",
            "--db",
            str(tmp_path / "test.db"),
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-31",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "PandaScore token is not configured." in output
    assert "secret-token" not in output


class _FakeHistoryCollector:
    def __init__(self, *, timeout: float) -> None:
        assert timeout == 2.5

    def collect(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        max_pages: int | None,
    ) -> HistoricalCollectionResult:
        assert since is not None
        assert since.isoformat() == "2026-01-01T00:00:00+00:00"
        assert until is not None
        assert until.date().isoformat() == "2026-01-31"
        assert page_size == 2
        assert max_pages == 3
        return HistoricalCollectionResult(
            matches=[make_historical_match("history-1")],
            fetched_rows=2,
            skipped_rows=1,
            warnings=[],
        )


class _TokenErrorCollector:
    def __init__(self, *, timeout: float) -> None:
        pass

    def collect(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        max_pages: int | None,
    ) -> HistoricalCollectionResult:
        raise app.history.PandaScoreConfigurationError(
            "PandaScore token is not configured.\n"
            "Set PANDASCORE_TOKEN before using the pandascore provider."
        )


class _NoMaxPagesHistoryCollector:
    def __init__(self, *, timeout: float) -> None:
        assert timeout == 10.0

    def collect(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
        page_size: int,
        max_pages: int | None,
    ) -> HistoricalCollectionResult:
        assert since is not None
        assert until is not None
        assert page_size == 50
        assert max_pages is None
        return HistoricalCollectionResult(
            matches=[make_historical_match("history-unbounded")],
            fetched_rows=1,
            skipped_rows=0,
            warnings=[],
        )
