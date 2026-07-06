from datetime import datetime
from pathlib import Path

from app.history import get_team_history_before, list_training_matches_before
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match


def test_training_matches_before_uses_completed_at_strictly(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    match_a = make_historical_match(
        "match-a",
        started_at=_dt("2026-01-01T10:00:00Z"),
        ended_at=_dt("2026-01-01T12:00:00Z"),
    )
    match_b = make_historical_match(
        "match-b",
        started_at=_dt("2026-01-03T10:00:00Z"),
        ended_at=_dt("2026-01-03T15:00:00Z"),
    )
    match_c = make_historical_match(
        "match-c",
        started_at=_dt("2026-01-05T10:00:00Z"),
        ended_at=_dt("2026-01-05T12:00:00Z"),
    )

    for match in (match_a, match_b, match_c):
        repository.save_historical_match(match)

    matches = list_training_matches_before(
        repository,
        _dt("2026-01-03T12:00:00Z"),
    )

    assert [match.id for match in matches] == ["match-a"]


def test_match_completed_exactly_at_cutoff_is_excluded(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_historical_match(
        make_historical_match(
            "exact",
            started_at=_dt("2026-01-03T10:00:00Z"),
            ended_at=_dt("2026-01-03T12:00:00Z"),
        )
    )

    assert list_training_matches_before(
        repository,
        _dt("2026-01-03T12:00:00Z"),
    ) == []


def test_started_before_cutoff_finished_after_cutoff_is_excluded(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_historical_match(
        make_historical_match(
            "late-finish",
            started_at=_dt("2026-01-03T10:00:00Z"),
            ended_at=_dt("2026-01-03T15:00:00Z"),
        )
    )

    assert repository.list_historical_matches_before(
        _dt("2026-01-03T12:00:00Z")
    ) == []


def test_team_history_uses_provider_id_and_same_name_without_transfer_aliases(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    one_w = make_historical_match(
        "one-w",
        team_a_name="1W",
        team_a_source_id="1w-id",
    )
    tundra = make_historical_match(
        "tundra",
        team_a_name="Tundra Esports",
        team_a_source_id="tundra-id",
    )
    heroic = make_historical_match(
        "heroic",
        team_a_name="HEROIC",
        team_a_source_id="heroic-id",
    )

    for match in (one_w, tundra, heroic):
        repository.save_historical_match(match)

    cutoff = _dt("2026-01-02T00:00:00Z")

    assert [
        match.id for match in get_team_history_before(repository, "1w-id", cutoff)
    ] == ["one-w"]
    assert [
        match.id for match in get_team_history_before(repository, " 1w ", cutoff)
    ] == ["one-w"]
    assert get_team_history_before(repository, "LGD Gaming", cutoff) == []


def test_history_order_is_chronological(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    for match in (
        make_historical_match("third", ended_at=_dt("2026-01-03T12:00:00Z")),
        make_historical_match("first", ended_at=_dt("2026-01-01T12:00:00Z")),
        make_historical_match("second", ended_at=_dt("2026-01-02T12:00:00Z")),
    ):
        repository.save_historical_match(match)

    matches = repository.list_historical_matches_before(
        _dt("2026-01-04T00:00:00Z")
    )

    assert [match.id for match in matches] == ["first", "second", "third"]


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
