import re

from app.domain import Match
from app.tournaments.domain import (
    CompetitiveStage,
    TournamentIdentity,
    TournamentRound,
    TournamentStage,
    build_tournament_stage,
    unknown_tournament_stage,
)
from app.tournaments.parser import (
    normalize_stage_label,
)


EWC_2026_DOTA = TournamentIdentity(
    id="ewc_2026_dota2",
    canonical_name="EWC 2026 Dota 2",
    game="Dota 2",
    year=2026,
    organizer="Esports World Cup",
    series="EWC",
)


_TOURNAMENT_PUNCTUATION_RE = re.compile(r"[^0-9a-z]+")
_YEAR_RE = re.compile(r"20\d{2}")


def normalize_tournament_name(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(_TOURNAMENT_PUNCTUATION_RE.sub(" ", value.casefold()).split())


def identify_tournament(value: str | None) -> TournamentIdentity | None:
    if is_ewc_2026_dota_tournament(value):
        return EWC_2026_DOTA
    return None


def is_ewc_2026_dota_tournament(value: str | None) -> bool:
    normalized = normalize_tournament_name(value)
    if not normalized:
        return False
    if _has_conflicting_year(normalized):
        return False

    return (
        _contains_phrase(normalized, "esports world cup")
        or _contains_phrase(normalized, "ewc 2026")
        or _contains_phrase(normalized, "ewc 26")
        or _contains_phrase(normalized, "dota 2 at ewc 26")
        or _contains_phrase(normalized, "dota2 at ewc 26")
        or _contains_phrase(normalized, "dota 2 ewc 2026")
        or _contains_phrase(normalized, "dota2 ewc 2026")
    )


def parse_ewc_2026_stage(value: str | None) -> TournamentStage:
    raw_label = "" if value is None else value
    normalized = normalize_stage_label(value)
    if not normalized:
        return unknown_tournament_stage(raw_label)

    if _is_ewc_group_label(normalized):
        return build_tournament_stage(
            CompetitiveStage.GROUP,
            round=TournamentRound.GROUP,
            raw_label=raw_label,
        )

    if _is_ewc_survival_label(normalized):
        round_value = (
            TournamentRound.SURVIVAL_FINAL
            if _is_grand_final_label(normalized)
            else TournamentRound.SURVIVAL
        )
        return build_tournament_stage(
            CompetitiveStage.CROSSOVER,
            round=round_value,
            raw_label=raw_label,
        )

    if _is_placement_label(normalized):
        return build_tournament_stage(
            CompetitiveStage.PLACEMENT,
            round=TournamentRound.THIRD_PLACE,
            raw_label=raw_label,
        )

    if _has_quarterfinal(normalized):
        return build_tournament_stage(
            CompetitiveStage.SINGLE_ELIMINATION,
            round=TournamentRound.QUARTERFINAL,
            raw_label=raw_label,
            is_elimination_match=True,
            loss_means_elimination=True,
            has_lower_bracket_fallback=False,
        )

    if _has_semifinal(normalized):
        return build_tournament_stage(
            CompetitiveStage.SINGLE_ELIMINATION,
            round=TournamentRound.SEMIFINAL,
            raw_label=raw_label,
            is_elimination_match=True,
            loss_means_elimination=True,
            has_lower_bracket_fallback=False,
        )

    if _is_grand_final_label(normalized):
        return build_tournament_stage(
            CompetitiveStage.GRAND_FINAL,
            round=TournamentRound.GRAND_FINAL,
            raw_label=raw_label,
        )

    return unknown_tournament_stage(raw_label)


def belongs_to_ewc_2026(match: Match) -> bool:
    return is_ewc_2026_dota_tournament(match.tournament_name)


def is_active_ewc_2026_match(match: Match) -> bool:
    return belongs_to_ewc_2026(match) and match.status in ("upcoming", "live")


def stage_for_ewc_2026_match(match: Match) -> TournamentStage:
    for label in (match.tournament_name, match.format):
        stage = parse_ewc_2026_stage(label)
        if stage.competitive_stage is not CompetitiveStage.UNKNOWN:
            return stage
    return unknown_tournament_stage(match.tournament_name)


def _contains_phrase(normalized: str, phrase: str) -> bool:
    return f" {phrase} " in f" {normalized} "


def _has_conflicting_year(normalized: str) -> bool:
    years = _YEAR_RE.findall(normalized)
    return bool(years) and "2026" not in years


def _is_ewc_group_label(normalized: str) -> bool:
    tokens = normalized.split()
    return "group" in tokens or bool(tokens) and tokens[0] in ("s1", "s2", "s3")


def _is_ewc_survival_label(normalized: str) -> bool:
    return (
        "survival" in normalized.split()
        or "crossover" in normalized.split()
        or "play in" in normalized
        or "playin" in normalized
    )


def _is_placement_label(normalized: str) -> bool:
    return "third place" in normalized or "3rd place" in normalized


def _is_grand_final_label(normalized: str) -> bool:
    return "grand final" in normalized or "grand finals" in normalized


def _has_quarterfinal(normalized: str) -> bool:
    return "quarterfinal" in normalized or "quarter final" in normalized


def _has_semifinal(normalized: str) -> bool:
    return "semifinal" in normalized or "semi final" in normalized
