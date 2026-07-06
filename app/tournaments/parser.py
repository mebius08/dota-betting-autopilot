import re

from app.tournaments.domain import (
    CompetitiveStage,
    TournamentRound,
    TournamentStage,
    build_tournament_stage,
    unknown_tournament_stage,
)


_PUNCTUATION_RE = re.compile(r"[^0-9a-z]+")


def normalize_stage_label(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(_PUNCTUATION_RE.sub(" ", value.casefold()).split())


def parse_tournament_stage(value: str | None) -> TournamentStage:
    raw_label = "" if value is None else value
    normalized = normalize_stage_label(value)
    if not normalized:
        return unknown_tournament_stage(raw_label)

    if _is_upper_bracket_label(normalized):
        return build_tournament_stage(
            CompetitiveStage.UPPER_BRACKET,
            round=_upper_bracket_round(normalized),
            raw_label=raw_label,
        )

    if _is_lower_bracket_label(normalized):
        return build_tournament_stage(
            CompetitiveStage.LOWER_BRACKET,
            round=_lower_bracket_round(normalized),
            raw_label=raw_label,
        )

    if _is_group_label(normalized):
        return build_tournament_stage(
            CompetitiveStage.GROUP,
            round=TournamentRound.GROUP,
            raw_label=raw_label,
        )

    if _is_crossover_label(normalized):
        return build_tournament_stage(
            CompetitiveStage.CROSSOVER,
            round=_crossover_round(normalized),
            raw_label=raw_label,
        )

    if _is_placement_label(normalized):
        return build_tournament_stage(
            CompetitiveStage.PLACEMENT,
            round=TournamentRound.THIRD_PLACE,
            raw_label=raw_label,
        )

    if _is_grand_final_label(normalized):
        return build_tournament_stage(
            CompetitiveStage.GRAND_FINAL,
            round=TournamentRound.GRAND_FINAL,
            raw_label=raw_label,
        )

    if _has_quarterfinal(normalized):
        return build_tournament_stage(
            CompetitiveStage.SINGLE_ELIMINATION,
            round=TournamentRound.QUARTERFINAL,
            raw_label=raw_label,
        )

    if _has_semifinal(normalized):
        return build_tournament_stage(
            CompetitiveStage.SINGLE_ELIMINATION,
            round=TournamentRound.SEMIFINAL,
            raw_label=raw_label,
        )

    return unknown_tournament_stage(raw_label)


def _is_upper_bracket_label(normalized: str) -> bool:
    tokens = normalized.split()
    return "upper bracket" in normalized or "ub" in tokens


def _is_lower_bracket_label(normalized: str) -> bool:
    tokens = normalized.split()
    return "lower bracket" in normalized or "lb" in tokens


def _is_group_label(normalized: str) -> bool:
    return "group" in normalized.split()


def _is_crossover_label(normalized: str) -> bool:
    return (
        "play in" in normalized
        or "playin" in normalized
        or "crossover" in normalized.split()
        or "survival" in normalized.split()
    )


def _is_placement_label(normalized: str) -> bool:
    return "third place" in normalized or "3rd place" in normalized


def _is_grand_final_label(normalized: str) -> bool:
    return "grand final" in normalized or "grand finals" in normalized


def _has_quarterfinal(normalized: str) -> bool:
    return "quarterfinal" in normalized or "quarter final" in normalized


def _has_semifinal(normalized: str) -> bool:
    return "semifinal" in normalized or "semi final" in normalized


def _upper_bracket_round(normalized: str) -> TournamentRound:
    if _has_quarterfinal(normalized):
        return TournamentRound.UPPER_BRACKET_QUARTERFINAL
    if _has_semifinal(normalized):
        return TournamentRound.UPPER_BRACKET_SEMIFINAL
    if _has_final(normalized):
        return TournamentRound.UPPER_BRACKET_FINAL
    if "round" in normalized.split():
        return TournamentRound.UPPER_BRACKET_ROUND
    return TournamentRound.UPPER_BRACKET


def _lower_bracket_round(normalized: str) -> TournamentRound:
    if _has_quarterfinal(normalized):
        return TournamentRound.LOWER_BRACKET_QUARTERFINAL
    if _has_semifinal(normalized):
        return TournamentRound.LOWER_BRACKET_SEMIFINAL
    if _has_final(normalized):
        return TournamentRound.LOWER_BRACKET_FINAL
    if "round" in normalized.split():
        return TournamentRound.LOWER_BRACKET_ROUND
    return TournamentRound.LOWER_BRACKET


def _crossover_round(normalized: str) -> TournamentRound:
    if "survival" in normalized.split() and _is_grand_final_label(normalized):
        return TournamentRound.SURVIVAL_FINAL
    if "survival" in normalized.split():
        return TournamentRound.SURVIVAL
    if "play in" in normalized or "playin" in normalized:
        return TournamentRound.PLAY_IN
    return TournamentRound.CROSSOVER


def _has_final(normalized: str) -> bool:
    tokens = normalized.split()
    return "final" in tokens or "finals" in tokens
