from datetime import datetime
from pathlib import Path

import pytest

from app import cli
import app.history
from app.storage import SQLiteRepository
from tests.roster_test_helpers import (
    make_organization,
    make_player,
    make_roster_snapshot,
)


def test_cli_help_includes_lineage_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "lineage-status" in output


def test_lineage_status_help_documents_as_of(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["lineage-status", "--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "--as-of" in output
    assert "ISO-8601" in output


def test_lineage_status_missing_db_is_friendly_and_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(
        [
            "lineage-status",
            "--db",
            str(db_path),
            "--as-of",
            "2026-07-07T12:00:00Z",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Roster lineage status" in output
    assert "As of: 2026-07-07T12:00:00+00:00" in output
    assert "Point-in-time available roster snapshots: 0" in output
    assert "No roster snapshots found." in output
    assert not db_path.exists()


def test_lineage_status_prints_populated_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    players = [make_player(f"p{index}") for index in range(1, 6)]
    previous = make_roster_snapshot(
        "previous",
        organization=make_organization("org-a", "Org A"),
        players=players,
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    current = make_roster_snapshot(
        "current",
        organization=make_organization("org-b", "Org B"),
        players=list(reversed(players)),
        valid_from=_dt("2025-02-01T00:00:00Z"),
    )
    repository.upsert_roster_snapshot(previous)
    repository.upsert_roster_snapshot(current)

    exit_code = cli.main(
        [
            "lineage-status",
            "--db",
            str(db_path),
            "--as-of",
            "2026-07-07T12:00:00Z",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Point-in-time available roster snapshots: 2" in output
    assert "  explicit_valid_from: 2" in output
    assert "Exact continuity links: 1" in output
    assert "Strong continuity links: 0" in output
    assert "Ambiguous predecessor resolutions: 0" in output
    assert "Unlinked/root snapshots: 1" in output
    assert "Derived lineage components: 1" in output
    assert "Cross-organization accepted links: 1" in output
    assert "Same-organization accepted links: 0" in output
    assert "Largest predecessor chain size: 2" in output


def test_lineage_status_does_not_touch_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "test.db"
    SQLiteRepository(db_path)

    def exploding_provider(*args: object, **kwargs: object) -> object:
        raise AssertionError("provider should not be constructed")

    monkeypatch.setattr(app.history, "PandaScoreRosterCollector", exploding_provider)

    exit_code = cli.main(
        [
            "lineage-status",
            "--db",
            str(db_path),
            "--as-of",
            "2026-07-07T12:00:00Z",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Point-in-time available roster snapshots: 0" in output


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
