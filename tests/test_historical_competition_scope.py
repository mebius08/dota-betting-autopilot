from datetime import datetime
from pathlib import Path

import pytest

from app.history import (
    DEFAULT_HISTORICAL_COMPETITION_SCOPE,
    HistoricalCompetitionFamily,
    HistoricalMatch,
    build_historical_feature_dataset,
    classify_historical_competition_family,
    is_historical_competition_qualifier,
    is_historical_match_scope_eligible_target,
    normalize_competition_metadata_text,
)
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("The International 2025", HistoricalCompetitionFamily.THE_INTERNATIONAL),
        ("The International 14", HistoricalCompetitionFamily.THE_INTERNATIONAL),
        ("Esports World Cup 2025", HistoricalCompetitionFamily.ESPORTS_WORLD_CUP),
        ("EWC 2026 Main Event", HistoricalCompetitionFamily.ESPORTS_WORLD_CUP),
        ("DreamLeague Season 27", HistoricalCompetitionFamily.DREAMLEAGUE),
        ("DreamLeague Season 28", HistoricalCompetitionFamily.DREAMLEAGUE),
        ("BLAST Slam I", HistoricalCompetitionFamily.BLAST),
        ("BLAST Slam 2026", HistoricalCompetitionFamily.BLAST),
        ("ESL One Raleigh 2025", HistoricalCompetitionFamily.ESL),
        ("ESL Pro Tour 2026", HistoricalCompetitionFamily.ESL),
        ("PGL Wallachia Season 4", HistoricalCompetitionFamily.PGL),
        ("PGL Dota 2 Masters 2026", HistoricalCompetitionFamily.PGL),
        ("FISSURE Playground", HistoricalCompetitionFamily.FISSURE_PLAYGROUND),
        (
            "FISSURE Playground 2 2025",
            HistoricalCompetitionFamily.FISSURE_PLAYGROUND,
        ),
        ("BetBoom Dacha Dubai 2025", HistoricalCompetitionFamily.BETBOOM_DACHA),
        ("BetBoom Dacha Belgrade 2026", HistoricalCompetitionFamily.BETBOOM_DACHA),
    ],
)
def test_competition_family_classifier_accepts_variable_display_names(
    name: str,
    family: HistoricalCompetitionFamily,
) -> None:
    assert _classify(tournament_name=name) is family


@pytest.mark.parametrize(
    "name",
    [
        "International Invitational",
        "Newcastle Cup",
        "Blastoff Invitational",
        "FISSURE Universe",
        "FISSURE Universe Episode 5",
        "FISSURE Invitational",
        "BetBoom Aegis Cup",
    ],
)
def test_competition_family_classifier_rejects_unsafe_partial_matches(
    name: str,
) -> None:
    assert _classify(tournament_name=name) is HistoricalCompetitionFamily.UNKNOWN


def test_classifier_uses_tournament_league_and_series_metadata() -> None:
    assert (
        _classify(tournament_name="Group Stage", league_name="DreamLeague Season 28")
        is HistoricalCompetitionFamily.DREAMLEAGUE
    )
    assert (
        _classify(tournament_name="Upper Bracket", series_name="PGL Wallachia")
        is HistoricalCompetitionFamily.PGL
    )
    assert (
        _classify(tournament_name="Playoffs", league_name="ESL Pro Tour")
        is HistoricalCompetitionFamily.ESL
    )
    assert (
        _classify(tournament_name="Playground 2", league_name="FISSURE")
        is HistoricalCompetitionFamily.FISSURE_PLAYGROUND
    )
    assert (
        _classify(tournament_name="Dacha Belgrade 2026", league_name="BetBoom")
        is HistoricalCompetitionFamily.BETBOOM_DACHA
    )


def test_classifier_precedence_keeps_specific_families() -> None:
    assert (
        _classify(tournament_name="ESL Pro Tour", league_name="DreamLeague Season 28")
        is HistoricalCompetitionFamily.DREAMLEAGUE
    )


def test_normalization_is_deterministic_and_punctuation_tolerant() -> None:
    assert normalize_competition_metadata_text("  FISSURE--Playground\t2026  ") == (
        "fissure playground 2026"
    )
    assert normalize_competition_metadata_text(None) == ""


@pytest.mark.parametrize(
    "name",
    [
        "DreamLeague Closed Qualifier",
        "ESL Qualifier",
        "The International Regional Qualifier",
        "PGL Closed Qualifier",
        "FISSURE Playground Closed Qualifier",
        "BetBoom Dacha Qualifier",
        "DreamLeague Qualification",
        "ESL Open Qualifier",
    ],
)
def test_qualifier_detection_overrides_allowed_family(name: str) -> None:
    match = _match(tournament_name=name)

    assert is_historical_competition_qualifier(match)
    assert not is_historical_match_scope_eligible_target(match)


@pytest.mark.parametrize(
    "name",
    [
        "DreamLeague Season 28",
        "ESL One Raleigh 2025",
        "The International 2025",
        "PGL Wallachia Season 4",
        "FISSURE Playground 2",
        "BetBoom Dacha Belgrade 2026",
    ],
)
def test_allowed_main_events_are_scope_eligible(name: str) -> None:
    assert is_historical_match_scope_eligible_target(_match(tournament_name=name))


def test_scope_start_boundary_is_inclusive() -> None:
    before = _match(
        tournament_name="DreamLeague Season 28",
        started_at=_dt("2025-07-07T23:59:59Z"),
        ended_at=_dt("2025-07-08T02:00:00Z"),
    )
    exact = _match(
        tournament_name="DreamLeague Season 28",
        started_at=_dt("2025-07-08T00:00:00Z"),
        ended_at=_dt("2025-07-08T02:00:00Z"),
    )
    later = _match(
        tournament_name="DreamLeague Season 28",
        started_at=_dt("2025-07-09T00:00:00Z"),
        ended_at=_dt("2025-07-09T02:00:00Z"),
    )

    assert not is_historical_match_scope_eligible_target(before)
    assert is_historical_match_scope_eligible_target(exact)
    assert is_historical_match_scope_eligible_target(later)


def test_raw_store_stays_broad_while_historical_ml_targets_are_scoped(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    allowed = _match(match_id="allowed", tournament_name="DreamLeague Season 28")
    disallowed = _match(
        match_id="disallowed",
        tournament_name="FISSURE Universe",
    )
    qualifier = _match(
        match_id="qualifier",
        tournament_name="DreamLeague Closed Qualifier",
    )
    for match in (allowed, disallowed, qualifier):
        repository.save_historical_match(match)

    rows = build_historical_feature_dataset(
        repository,
        target_scope_policy=DEFAULT_HISTORICAL_COMPETITION_SCOPE,
    )

    assert repository.count_historical_matches() == 3
    assert [row.feature_row.source_match_id for row in rows] == ["allowed"]


def test_scope_filtering_does_not_weaken_point_in_time_history(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    included = _match(
        match_id="included",
        started_at=_dt("2026-01-01T10:00:00Z"),
        ended_at=_dt("2026-01-01T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="opponent",
        tournament_name="DreamLeague Season 28",
    )
    ended_after_target = _match(
        match_id="ended-after-target",
        started_at=_dt("2026-01-10T09:00:00Z"),
        ended_at=_dt("2026-01-10T13:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="opponent",
        tournament_name="DreamLeague Season 28",
    )
    target = _match(
        match_id="target",
        started_at=_dt("2026-01-10T12:00:00Z"),
        ended_at=_dt("2026-01-10T14:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="team-b",
        tournament_name="DreamLeague Season 28",
    )
    for match in (included, ended_after_target, target):
        repository.save_historical_match(match)

    rows = build_historical_feature_dataset(
        repository,
        target_scope_policy=DEFAULT_HISTORICAL_COMPETITION_SCOPE,
    )
    target_row = next(
        row for row in rows if row.feature_row.source_match_id == "target"
    )

    assert target_row.feature_row.team_a_history_matches == 1


def _classify(
    *,
    tournament_name: str | None = None,
    league_name: str | None = None,
    series_name: str | None = None,
) -> HistoricalCompetitionFamily:
    return classify_historical_competition_family(
        _match(
            tournament_name=tournament_name,
            league_name=league_name,
            series_name=series_name,
        )
    )


def _match(
    *,
    match_id: str = "match-1",
    tournament_name: str | None,
    league_name: str | None = None,
    series_name: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    team_a_source_id: str | None = "team-a",
    team_b_source_id: str | None = "team-b",
) -> HistoricalMatch:
    return make_historical_match(
        match_id,
        started_at=started_at or _dt("2026-01-01T10:00:00Z"),
        ended_at=ended_at or _dt("2026-01-01T12:00:00Z"),
        team_a_source_id=team_a_source_id,
        team_b_source_id=team_b_source_id,
        tournament_name=tournament_name,
        league_name=league_name,
        series_name=series_name,
    )


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
