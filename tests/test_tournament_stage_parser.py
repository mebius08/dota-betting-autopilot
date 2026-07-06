import pytest

from app.tournaments import CompetitiveStage, TournamentRound, parse_tournament_stage


@pytest.mark.parametrize("label", ["Group Stage", "Group A", "Group B"])
def test_parse_group_stage_labels(label: str) -> None:
    assert parse_tournament_stage(label).competitive_stage is CompetitiveStage.GROUP


@pytest.mark.parametrize("label", ["Play-In", "Play In", "Crossover", "Survival"])
def test_parse_crossover_labels(label: str) -> None:
    assert parse_tournament_stage(label).competitive_stage is CompetitiveStage.CROSSOVER


@pytest.mark.parametrize(
    "label",
    [
        "Upper Bracket",
        "Upper Bracket Round 1",
        "Upper Bracket Quarterfinal",
        "Upper Bracket Semifinal",
        "Upper Bracket Final",
        "UB Round 1",
        "UB Semifinal",
        "UB Final",
    ],
)
def test_parse_upper_bracket_labels(label: str) -> None:
    assert (
        parse_tournament_stage(label).competitive_stage
        is CompetitiveStage.UPPER_BRACKET
    )


@pytest.mark.parametrize(
    "label",
    [
        "Lower Bracket",
        "Lower Bracket Round 1",
        "Lower Bracket Semifinal",
        "Lower Bracket Final",
        "LB Round 1",
        "LB Semifinal",
        "LB Final",
    ],
)
def test_parse_lower_bracket_labels(label: str) -> None:
    assert (
        parse_tournament_stage(label).competitive_stage
        is CompetitiveStage.LOWER_BRACKET
    )


@pytest.mark.parametrize(
    ("label", "expected_round"),
    [
        ("Quarterfinal", TournamentRound.QUARTERFINAL),
        ("Quarter-final", TournamentRound.QUARTERFINAL),
        ("Quarter Final", TournamentRound.QUARTERFINAL),
        ("Semifinal", TournamentRound.SEMIFINAL),
        ("Semi-final", TournamentRound.SEMIFINAL),
        ("Semi Final", TournamentRound.SEMIFINAL),
    ],
)
def test_parse_single_elimination_rounds(
    label: str,
    expected_round: TournamentRound,
) -> None:
    stage = parse_tournament_stage(label)

    assert stage.competitive_stage is CompetitiveStage.SINGLE_ELIMINATION
    assert stage.round is expected_round


@pytest.mark.parametrize("label", ["Grand Final", "Grand Finals"])
def test_parse_grand_final(label: str) -> None:
    assert parse_tournament_stage(label).competitive_stage is CompetitiveStage.GRAND_FINAL


@pytest.mark.parametrize("label", ["Third Place", "3rd Place"])
def test_parse_placement(label: str) -> None:
    assert parse_tournament_stage(label).competitive_stage is CompetitiveStage.PLACEMENT


def test_unknown_stage_is_safe() -> None:
    assert parse_tournament_stage("").competitive_stage is CompetitiveStage.UNKNOWN
    assert (
        parse_tournament_stage("mysterious label").competitive_stage
        is CompetitiveStage.UNKNOWN
    )


@pytest.mark.parametrize(
    ("label", "expected_stage"),
    [
        ("Upper Bracket Final", CompetitiveStage.UPPER_BRACKET),
        ("Lower Bracket Final", CompetitiveStage.LOWER_BRACKET),
        ("Survival Grand Final", CompetitiveStage.CROSSOVER),
    ],
)
def test_parser_priority_keeps_bracket_and_survival_context(
    label: str,
    expected_stage: CompetitiveStage,
) -> None:
    assert parse_tournament_stage(label).competitive_stage is expected_stage


def test_historical_double_elimination_labels_are_representable() -> None:
    assert (
        parse_tournament_stage("Upper Bracket Semifinal").competitive_stage
        is CompetitiveStage.UPPER_BRACKET
    )
    assert (
        parse_tournament_stage("Lower Bracket Round 2").competitive_stage
        is CompetitiveStage.LOWER_BRACKET
    )
