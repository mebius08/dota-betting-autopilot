import pytest

from app.tournaments import (
    CompetitiveStage,
    TournamentRound,
    build_tournament_stage,
)


def test_competitive_stage_taxonomy_contains_expected_model_categories() -> None:
    assert {stage.value for stage in CompetitiveStage} == {
        "group",
        "crossover",
        "upper_bracket",
        "lower_bracket",
        "single_elimination",
        "grand_final",
        "placement",
        "unknown",
    }


@pytest.mark.parametrize(
    (
        "competitive_stage",
        "expected_elimination",
        "expected_loss_eliminates",
        "expected_lower_fallback",
        "expected_placement",
    ),
    [
        (CompetitiveStage.GROUP, False, False, False, False),
        (CompetitiveStage.CROSSOVER, False, False, False, False),
        (CompetitiveStage.UPPER_BRACKET, False, False, True, False),
        (CompetitiveStage.LOWER_BRACKET, True, True, False, False),
        (CompetitiveStage.SINGLE_ELIMINATION, True, True, False, False),
        (CompetitiveStage.GRAND_FINAL, True, True, False, False),
        (CompetitiveStage.PLACEMENT, False, False, False, True),
    ],
)
def test_stage_semantic_defaults(
    competitive_stage: CompetitiveStage,
    expected_elimination: bool,
    expected_loss_eliminates: bool,
    expected_lower_fallback: bool,
    expected_placement: bool,
) -> None:
    stage = build_tournament_stage(
        competitive_stage,
        round=TournamentRound.UNKNOWN,
    )

    assert stage.is_elimination_match is expected_elimination
    assert stage.loss_means_elimination is expected_loss_eliminates
    assert stage.has_lower_bracket_fallback is expected_lower_fallback
    assert stage.is_placement_match is expected_placement


def test_stage_convenience_flags_track_competitive_stage() -> None:
    group = build_tournament_stage(
        CompetitiveStage.GROUP,
        round=TournamentRound.GROUP,
    )
    single_elimination = build_tournament_stage(
        CompetitiveStage.SINGLE_ELIMINATION,
        round=TournamentRound.QUARTERFINAL,
    )
    grand_final = build_tournament_stage(
        CompetitiveStage.GRAND_FINAL,
        round=TournamentRound.GRAND_FINAL,
    )

    assert group.is_group_stage is True
    assert single_elimination.is_single_elimination is True
    assert grand_final.is_grand_final is True
