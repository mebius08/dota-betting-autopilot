from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml


TournamentStage = Literal["main", "ti_closed_qualifier"]
_ALLOWED_STAGES = frozenset({"main", "ti_closed_qualifier"})
TOURNAMENT_ALLOWLIST_PATH = (
    Path(__file__).resolve().parent / "config" / "tournament_allowlist.yaml"
)


class TournamentAllowlistError(ValueError):
    pass


@dataclass(frozen=True)
class TournamentAllowlistEntry:
    league_id: int
    canonical_series: str
    edition: str
    stage: TournamentStage


@dataclass(frozen=True)
class TournamentAllowlist:
    version: int
    entries: tuple[TournamentAllowlistEntry, ...]

    @property
    def league_ids(self) -> tuple[int, ...]:
        return tuple(entry.league_id for entry in self.entries)

    def entry_for(self, league_id: int) -> TournamentAllowlistEntry | None:
        return next(
            (entry for entry in self.entries if entry.league_id == league_id),
            None,
        )

    def require(self, league_id: int) -> TournamentAllowlistEntry:
        entry = self.entry_for(league_id)
        if entry is None:
            raise TournamentAllowlistError(
                f"league_id_not_allowlisted league_id={league_id}"
            )
        return entry


def load_tournament_allowlist(
    path: str | Path = TOURNAMENT_ALLOWLIST_PATH,
) -> TournamentAllowlist:
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            payload = yaml.safe_load(config_file)
    except OSError as exc:
        raise TournamentAllowlistError("tournament_allowlist_unavailable") from exc
    except yaml.YAMLError as exc:
        raise TournamentAllowlistError("tournament_allowlist_invalid_yaml") from exc

    if not isinstance(payload, dict):
        raise TournamentAllowlistError("tournament_allowlist_root_invalid")
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise TournamentAllowlistError("tournament_allowlist_version_invalid")
    raw_entries = payload.get("leagues")
    if not isinstance(raw_entries, list):
        raise TournamentAllowlistError("tournament_allowlist_leagues_invalid")

    entries: list[TournamentAllowlistEntry] = []
    seen_league_ids: set[int] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise TournamentAllowlistError("tournament_allowlist_entry_invalid")
        league_id = raw_entry.get("league_id")
        if (
            not isinstance(league_id, int)
            or isinstance(league_id, bool)
            or league_id <= 0
        ):
            raise TournamentAllowlistError(
                "tournament_allowlist_league_id_invalid"
            )
        if league_id in seen_league_ids:
            raise TournamentAllowlistError(
                f"tournament_allowlist_duplicate_league_id league_id={league_id}"
            )
        stage = raw_entry.get("stage")
        if not isinstance(stage, str) or stage not in _ALLOWED_STAGES:
            raise TournamentAllowlistError(
                f"tournament_allowlist_stage_invalid league_id={league_id}"
            )
        entries.append(
            TournamentAllowlistEntry(
                league_id=league_id,
                canonical_series=_required_text(
                    raw_entry,
                    "canonical_series",
                    league_id=league_id,
                ),
                edition=_required_text(raw_entry, "edition", league_id=league_id),
                stage=cast(TournamentStage, stage),
            )
        )
        seen_league_ids.add(league_id)

    return TournamentAllowlist(version=version, entries=tuple(entries))


def _required_text(
    entry: dict[object, object],
    field: str,
    *,
    league_id: int,
) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise TournamentAllowlistError(
            f"tournament_allowlist_{field}_invalid league_id={league_id}"
        )
    return value.strip()
