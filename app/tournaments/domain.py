from dataclasses import dataclass
from enum import Enum
from typing import Final


@dataclass(frozen=True)
class TournamentIdentity:
    id: str
    canonical_name: str
    game: str
    year: int
    organizer: str | None = None
    series: str | None = None


class CompetitiveStage(str, Enum):
    GROUP = "group"
    CROSSOVER = "crossover"
    UPPER_BRACKET = "upper_bracket"
    LOWER_BRACKET = "lower_bracket"
    SINGLE_ELIMINATION = "single_elimination"
    GRAND_FINAL = "grand_final"
    PLACEMENT = "placement"
    UNKNOWN = "unknown"


class TournamentRound(str, Enum):
    GROUP = "group"
    PLAY_IN = "play_in"
    CROSSOVER = "crossover"
    SURVIVAL = "survival"
    SURVIVAL_FINAL = "survival_final"
    UPPER_BRACKET = "upper_bracket"
    UPPER_BRACKET_ROUND = "upper_bracket_round"
    UPPER_BRACKET_QUARTERFINAL = "upper_bracket_quarterfinal"
    UPPER_BRACKET_SEMIFINAL = "upper_bracket_semifinal"
    UPPER_BRACKET_FINAL = "upper_bracket_final"
    LOWER_BRACKET = "lower_bracket"
    LOWER_BRACKET_ROUND = "lower_bracket_round"
    LOWER_BRACKET_QUARTERFINAL = "lower_bracket_quarterfinal"
    LOWER_BRACKET_SEMIFINAL = "lower_bracket_semifinal"
    LOWER_BRACKET_FINAL = "lower_bracket_final"
    QUARTERFINAL = "quarterfinal"
    SEMIFINAL = "semifinal"
    GRAND_FINAL = "grand_final"
    THIRD_PLACE = "third_place"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TournamentStage:
    competitive_stage: CompetitiveStage
    round: TournamentRound
    raw_label: str
    is_elimination_match: bool
    loss_means_elimination: bool
    has_lower_bracket_fallback: bool
    is_placement_match: bool = False

    @property
    def is_group_stage(self) -> bool:
        return self.competitive_stage is CompetitiveStage.GROUP

    @property
    def is_crossover_stage(self) -> bool:
        return self.competitive_stage is CompetitiveStage.CROSSOVER

    @property
    def is_upper_bracket(self) -> bool:
        return self.competitive_stage is CompetitiveStage.UPPER_BRACKET

    @property
    def is_lower_bracket(self) -> bool:
        return self.competitive_stage is CompetitiveStage.LOWER_BRACKET

    @property
    def is_single_elimination(self) -> bool:
        return self.competitive_stage is CompetitiveStage.SINGLE_ELIMINATION

    @property
    def is_grand_final(self) -> bool:
        return self.competitive_stage is CompetitiveStage.GRAND_FINAL


_STAGE_DEFAULTS: Final[dict[CompetitiveStage, tuple[bool, bool, bool, bool]]] = {
    CompetitiveStage.GROUP: (False, False, False, False),
    CompetitiveStage.CROSSOVER: (False, False, False, False),
    CompetitiveStage.UPPER_BRACKET: (False, False, True, False),
    CompetitiveStage.LOWER_BRACKET: (True, True, False, False),
    CompetitiveStage.SINGLE_ELIMINATION: (True, True, False, False),
    CompetitiveStage.GRAND_FINAL: (True, True, False, False),
    CompetitiveStage.PLACEMENT: (False, False, False, True),
    CompetitiveStage.UNKNOWN: (False, False, False, False),
}


def build_tournament_stage(
    competitive_stage: CompetitiveStage,
    *,
    round: TournamentRound,
    raw_label: str = "",
    is_elimination_match: bool | None = None,
    loss_means_elimination: bool | None = None,
    has_lower_bracket_fallback: bool | None = None,
    is_placement_match: bool | None = None,
) -> TournamentStage:
    (
        default_elimination,
        default_loss_eliminates,
        default_lower_fallback,
        default_placement,
    ) = _STAGE_DEFAULTS[competitive_stage]

    return TournamentStage(
        competitive_stage=competitive_stage,
        round=round,
        raw_label=raw_label,
        is_elimination_match=(
            default_elimination
            if is_elimination_match is None
            else is_elimination_match
        ),
        loss_means_elimination=(
            default_loss_eliminates
            if loss_means_elimination is None
            else loss_means_elimination
        ),
        has_lower_bracket_fallback=(
            default_lower_fallback
            if has_lower_bracket_fallback is None
            else has_lower_bracket_fallback
        ),
        is_placement_match=(
            default_placement
            if is_placement_match is None
            else is_placement_match
        ),
    )


def unknown_tournament_stage(raw_label: str = "") -> TournamentStage:
    return build_tournament_stage(
        CompetitiveStage.UNKNOWN,
        round=TournamentRound.UNKNOWN,
        raw_label=raw_label,
    )
