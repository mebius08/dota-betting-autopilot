from datetime import datetime, timezone

import pytest

from app.collectors.match_collector import normalize_team_name
from app.domain import Match
from app.tournaments import (
    EWC_2026_DOTA,
    CompetitiveStage,
    TournamentRound,
    belongs_to_ewc_2026,
    identify_tournament,
    is_active_ewc_2026_match,
    is_ewc_2026_dota_tournament,
    parse_ewc_2026_stage,
    stage_for_ewc_2026_match,
)


@pytest.mark.parametrize(
    "label",
    [
        "Esports World Cup 2026",
        "EWC 2026",
        "Dota 2 at EWC 26",
        "  esports   world cup 2026  ",
        "Esports World Cup / Group Stage",
    ],
)
def test_identify_ewc_2026_dota_aliases(label: str) -> None:
    assert identify_tournament(label) == EWC_2026_DOTA
    assert is_ewc_2026_dota_tournament(label) is True


@pytest.mark.parametrize(
    "label",
    ["DreamLeague", "World Cup qualifier", "", "Esports World Cup 2025"],
)
def test_unrelated_tournament_names_do_not_resolve_to_ewc(label: str) -> None:
    assert identify_tournament(label) is None
    assert is_ewc_2026_dota_tournament(label) is False


@pytest.mark.parametrize(
    "label",
    ["Group Stage", "group", "S1 [1.1]", "S2 [1.2]", "S3 [1.3]"],
)
def test_parse_ewc_group_labels(label: str) -> None:
    assert parse_ewc_2026_stage(label).competitive_stage is CompetitiveStage.GROUP


@pytest.mark.parametrize(
    "label",
    ["Survival", "Survival - Grand Final #1", "Survival - Grand Final #4"],
)
def test_parse_ewc_survival_as_crossover(label: str) -> None:
    stage = parse_ewc_2026_stage(label)

    assert stage.competitive_stage is CompetitiveStage.CROSSOVER
    assert stage.competitive_stage is not CompetitiveStage.GRAND_FINAL


@pytest.mark.parametrize(
    ("label", "expected_round"),
    [
        ("Playoffs - Quarterfinal #1", TournamentRound.QUARTERFINAL),
        ("Playoffs - Quarterfinal #4", TournamentRound.QUARTERFINAL),
        ("Playoffs - Semifinal #1", TournamentRound.SEMIFINAL),
        ("Playoffs - Semifinal #2", TournamentRound.SEMIFINAL),
    ],
)
def test_parse_ewc_playoff_rounds_as_single_elimination(
    label: str,
    expected_round: TournamentRound,
) -> None:
    stage = parse_ewc_2026_stage(label)

    assert stage.competitive_stage is CompetitiveStage.SINGLE_ELIMINATION
    assert stage.round is expected_round
    assert stage.competitive_stage is not CompetitiveStage.UPPER_BRACKET
    assert stage.is_elimination_match is True
    assert stage.loss_means_elimination is True
    assert stage.has_lower_bracket_fallback is False


def test_parse_ewc_grand_final_and_placement() -> None:
    assert (
        parse_ewc_2026_stage("Playoffs - Grand Final #1").competitive_stage
        is CompetitiveStage.GRAND_FINAL
    )
    assert (
        parse_ewc_2026_stage("Playoffs - 3rd place #1").competitive_stage
        is CompetitiveStage.PLACEMENT
    )


def test_parse_unknown_ewc_stage_is_safe() -> None:
    assert (
        parse_ewc_2026_stage("unknown label").competitive_stage
        is CompetitiveStage.UNKNOWN
    )


def test_ewc_membership_and_active_scope_are_distinct() -> None:
    upcoming = _match(status="upcoming")
    live = _match(match_id="live", status="live")
    completed = _match(match_id="completed", status="finished")
    unknown_stage = _match(match_id="unknown", tournament_name="EWC 2026")

    assert belongs_to_ewc_2026(upcoming) is True
    assert is_active_ewc_2026_match(upcoming) is True
    assert is_active_ewc_2026_match(live) is True
    assert belongs_to_ewc_2026(completed) is True
    assert is_active_ewc_2026_match(completed) is False
    assert belongs_to_ewc_2026(unknown_stage) is True
    assert (
        stage_for_ewc_2026_match(unknown_stage).competitive_stage
        is CompetitiveStage.UNKNOWN
    )


def test_non_ewc_match_is_out_of_scope() -> None:
    assert belongs_to_ewc_2026(_match(tournament_name="DreamLeague")) is False


def test_team_organization_tags_are_not_roster_aliases() -> None:
    assert normalize_team_name("Tundra Esports") != normalize_team_name("1W")
    assert normalize_team_name("HEROIC") != normalize_team_name("LGD Gaming")


def _match(
    *,
    match_id: str = "match-1",
    tournament_name: str = "Esports World Cup 2026 / Playoffs - Quarterfinal #1",
    status: str = "upcoming",
) -> Match:
    return Match(
        id=match_id,
        session_id="session-1",
        tournament_name=tournament_name,
        team_a="Team Spirit",
        team_b="PARIVISION",
        format="bo3",
        status=status,  # type: ignore[arg-type]
        start_time=datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc),
        external_id=match_id,
    )
