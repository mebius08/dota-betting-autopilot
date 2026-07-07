from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re
import unicodedata

from app.history.domain import HistoricalMatch


class HistoricalCompetitionFamily(str, Enum):
    THE_INTERNATIONAL = "the_international"
    ESPORTS_WORLD_CUP = "esports_world_cup"
    DREAMLEAGUE = "dreamleague"
    BLAST = "blast"
    ESL = "esl"
    PGL = "pgl"
    FISSURE_PLAYGROUND = "fissure_playground"
    BETBOOM_DACHA = "betboom_dacha"
    UNKNOWN = "unknown"


_SCOPE_START = datetime(2025, 7, 8, tzinfo=timezone.utc)


@dataclass(frozen=True)
class HistoricalCompetitionScopePolicy:
    scope_id: str
    target_start_at: datetime
    allowed_families: frozenset[HistoricalCompetitionFamily]
    exclude_qualifiers: bool = True

    def __post_init__(self) -> None:
        if not self.scope_id.strip():
            raise ValueError("scope_id must not be empty")
        if self.target_start_at.tzinfo is None:
            raise ValueError("target_start_at must be timezone-aware")
        if not self.allowed_families:
            raise ValueError("allowed_families must not be empty")

    def as_dict(self) -> dict[str, object]:
        return {
            "scope_id": self.scope_id,
            "target_start_at": _format_utc_timestamp(self.target_start_at),
            "allowed_families": [
                family.value for family in sorted(self.allowed_families, key=str)
            ],
            "exclude_qualifiers": self.exclude_qualifiers,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> HistoricalCompetitionScopePolicy:
        return cls(
            scope_id=str(value["scope_id"]),
            target_start_at=_datetime_from_mapping_value(
                value["target_start_at"]
            ),
            allowed_families=frozenset(
                HistoricalCompetitionFamily(str(family))
                for family in _iterable_value(value["allowed_families"])
            ),
            exclude_qualifiers=bool(value["exclude_qualifiers"]),
        )


EWC_2026_BASELINE_SCOPE = HistoricalCompetitionScopePolicy(
    scope_id="ewc_2026_baseline",
    target_start_at=_SCOPE_START,
    allowed_families=frozenset(
        {
            HistoricalCompetitionFamily.THE_INTERNATIONAL,
            HistoricalCompetitionFamily.ESPORTS_WORLD_CUP,
            HistoricalCompetitionFamily.DREAMLEAGUE,
            HistoricalCompetitionFamily.BLAST,
            HistoricalCompetitionFamily.ESL,
            HistoricalCompetitionFamily.PGL,
            HistoricalCompetitionFamily.FISSURE_PLAYGROUND,
            HistoricalCompetitionFamily.BETBOOM_DACHA,
        }
    ),
    exclude_qualifiers=True,
)

DEFAULT_HISTORICAL_COMPETITION_SCOPE = EWC_2026_BASELINE_SCOPE

HISTORICAL_COMPETITION_CLASSIFICATION_PRECEDENCE: tuple[
    HistoricalCompetitionFamily,
    ...,
] = (
    HistoricalCompetitionFamily.THE_INTERNATIONAL,
    HistoricalCompetitionFamily.ESPORTS_WORLD_CUP,
    HistoricalCompetitionFamily.DREAMLEAGUE,
    HistoricalCompetitionFamily.FISSURE_PLAYGROUND,
    HistoricalCompetitionFamily.BETBOOM_DACHA,
    HistoricalCompetitionFamily.BLAST,
    HistoricalCompetitionFamily.PGL,
    HistoricalCompetitionFamily.ESL,
)


def normalize_competition_metadata_text(value: object | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    normalized = "".join(
        character if character.isalnum() else " " for character in normalized
    )
    return re.sub(r"\s+", " ", normalized).strip()


def competition_identity_fields(match: HistoricalMatch) -> tuple[str, ...]:
    fields: list[str] = []
    for value in (
        match.tournament_name,
        match.league_name,
        match.series_name,
    ):
        normalized = normalize_competition_metadata_text(value)
        if normalized and normalized not in fields:
            fields.append(normalized)
    return tuple(fields)


def competition_identity_text(match: HistoricalMatch) -> str:
    return " ".join(competition_identity_fields(match))


def classify_historical_competition_family(
    match: HistoricalMatch,
) -> HistoricalCompetitionFamily:
    fields = competition_identity_fields(match)
    for family in HISTORICAL_COMPETITION_CLASSIFICATION_PRECEDENCE:
        if _matches_family(fields, family):
            return family
    return HistoricalCompetitionFamily.UNKNOWN


def is_historical_competition_qualifier(match: HistoricalMatch) -> bool:
    return any(
        _has_phrase(field, ("open", "qualifier"))
        or _has_phrase(field, ("closed", "qualifier"))
        or _has_phrase(field, ("regional", "qualifier"))
        or _has_token(field, "qualifier")
        or _has_token(field, "qualification")
        for field in competition_identity_fields(match)
    )


def is_historical_match_scope_eligible_target(
    match: HistoricalMatch,
    policy: HistoricalCompetitionScopePolicy = DEFAULT_HISTORICAL_COMPETITION_SCOPE,
) -> bool:
    if match.started_at < policy.target_start_at:
        return False
    family = classify_historical_competition_family(match)
    if family not in policy.allowed_families:
        return False
    if policy.exclude_qualifiers and is_historical_competition_qualifier(match):
        return False
    return match.usable_for_match_winner_training


def validate_historical_scope_compatible(
    value: Mapping[str, object] | None,
    *,
    expected: HistoricalCompetitionScopePolicy = DEFAULT_HISTORICAL_COMPETITION_SCOPE,
) -> None:
    if value is None:
        raise ValueError("Historical model competition scope metadata missing.")
    artifact_policy = HistoricalCompetitionScopePolicy.from_mapping(value)
    if artifact_policy.scope_id != expected.scope_id:
        raise ValueError(
            "Historical model competition scope mismatch: "
            f"artifact={artifact_policy.scope_id}, expected={expected.scope_id}."
        )
    if artifact_policy.target_start_at != expected.target_start_at:
        raise ValueError(
            "Historical model competition scope start mismatch: "
            f"artifact={_format_utc_timestamp(artifact_policy.target_start_at)}, "
            f"expected={_format_utc_timestamp(expected.target_start_at)}."
        )
    if artifact_policy.allowed_families != expected.allowed_families:
        raise ValueError("Historical model competition scope family set mismatch.")
    if artifact_policy.exclude_qualifiers != expected.exclude_qualifiers:
        raise ValueError(
            "Historical model competition scope qualifier policy mismatch."
        )


def _matches_family(
    fields: tuple[str, ...],
    family: HistoricalCompetitionFamily,
) -> bool:
    tokens = _field_tokens(fields)
    if family is HistoricalCompetitionFamily.THE_INTERNATIONAL:
        return any(_has_phrase(field, ("the", "international")) for field in fields)
    if family is HistoricalCompetitionFamily.ESPORTS_WORLD_CUP:
        return any(
            _has_phrase(field, ("esports", "world", "cup"))
            for field in fields
        ) or "ewc" in tokens
    if family is HistoricalCompetitionFamily.DREAMLEAGUE:
        return any(
            _has_token(field, "dreamleague")
            or _has_phrase(field, ("dream", "league"))
            for field in fields
        )
    if family is HistoricalCompetitionFamily.FISSURE_PLAYGROUND:
        return "fissure" in tokens and "playground" in tokens
    if family is HistoricalCompetitionFamily.BETBOOM_DACHA:
        return "betboom" in tokens and "dacha" in tokens
    if family is HistoricalCompetitionFamily.BLAST:
        return "blast" in tokens
    if family is HistoricalCompetitionFamily.PGL:
        return "pgl" in tokens
    if family is HistoricalCompetitionFamily.ESL:
        return "esl" in tokens
    return False


def _has_token(field: str, token: str) -> bool:
    return token in field.split()


def _field_tokens(fields: tuple[str, ...]) -> set[str]:
    return {token for field in fields for token in field.split()}


def _has_phrase(field: str, phrase: tuple[str, ...]) -> bool:
    tokens = field.split()
    width = len(phrase)
    return any(tuple(tokens[index : index + width]) == phrase for index in range(len(tokens)))


def _datetime_from_mapping_value(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _iterable_value(value: object) -> Iterable[object]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ValueError("expected iterable scope metadata value")
    return value
