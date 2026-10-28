"""Read-only composition of an existing historical snapshot and local context.

The caller must explicitly attest POST_DRAFT: neither cache nor snapshot proves
the draft-completion timestamp. This is not a LIVE/pre-draft adapter or a Team
POV projection. Context is a separate metadata section, never a new observation.
Known actor identities are cross-checked by exact slot; unknown links stay NULL.
The snapshot's existing selection, timestamps, ticks and provenance are retained.
"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.analytics.historical_match_context import (
    DEFAULT_CACHE_DIR, load_historical_match_context,
)
from app.analytics.historical_snapshot import (
    HistoricalObservation, HistoricalSnapshot, MatchIdentity,
)


ORIGINS = {
    "economy": "HISTORICAL_ORACLE", "heroes": "HISTORICAL_ORACLE",
    "spatial_global": "HISTORICAL_ORACLE", "pov": "HISTORICAL_TEAM_POV",
    "pov_enemy_positions": "HISTORICAL_TEAM_POV",
}


@dataclass(frozen=True)
class ContextualHistoricalSnapshot:
    snapshot: HistoricalSnapshot
    match_context: dict
    phase: Literal["POST_DRAFT"]


def _lineage(row: Mapping, context: Mapping):
    if not isinstance(row, Mapping):
        raise ValueError("Snapshot payload must be a mapping.")
    mid = str(context["match_id"])
    if (("match_id" in row and str(row["match_id"]) != mid)
            or ("source_match_id" in row and row["source_match_id"] != mid)
            or ("source" in row and row["source"] != "replay_clarity")):
        raise ValueError("Snapshot payload match identity mismatch.")
    if "replay_sha256" in row and (
        not isinstance(row["replay_sha256"], str)
        or row["replay_sha256"].lower() != context["replay_sha256"].lower()
    ):
        raise ValueError("Snapshot/context replay SHA mismatch.")


def _actor(row: Mapping, context: Mapping, players: dict, *, enemy=False):
    _lineage(row, context)
    fields = ("enemy_player_slot", "enemy_hero_id", "enemy_team") if enemy else (
        "player_slot", "hero_id", "team",
    )
    if any(field not in row for field in fields):
        raise ValueError("Snapshot actor requires slot, hero_id and side; use NULL for unknowns.")
    slot, hero, team = (row[field] for field in fields)
    if type(slot) is not int or slot not in players:
        raise ValueError("Snapshot actor slot is not present in context.")
    player = players[slot]
    for name, observed in (("hero_id", hero), ("team", team),
                           ("account_id", row.get("account_id"))):
        known = player[name]
        if observed is not None and known is not None and (
            type(observed) is not type(known) or observed != known
        ):
            raise ValueError(f"Snapshot/context {name} mismatch at slot {slot}.")


def compose_historical_snapshot(
    match_id: int | str, snapshot: HistoricalSnapshot, *,
    phase: Literal["POST_DRAFT"], cache_dir: str | Path = DEFAULT_CACHE_DIR,
) -> ContextualHistoricalSnapshot:
    """Read one validated context and pair it with a detached snapshot copy.

    phase is a required caller assertion, not an inferred game-time threshold.
    No actors, accounts or missing draft actions are reconstructed. When actor
    channels or replay hashes are absent, those links cannot be independently
    verified; match identity still must agree exactly. Requested replay tick is
    not stored by HistoricalSnapshot and is never invented here.
    """
    if phase != "POST_DRAFT":
        raise ValueError("Only explicitly POST_DRAFT historical snapshots are supported.")
    if (not isinstance(snapshot, HistoricalSnapshot)
            or snapshot.snapshot_kind != "HISTORICAL_OBSERVATIONS"):
        raise ValueError("An existing HistoricalSnapshot is required, never LIVE state.")
    context = load_historical_match_context(match_id, cache_dir=cache_dir)
    if snapshot.match != MatchIdentity("replay_clarity", str(context["match_id"])):
        raise ValueError("Snapshot/context match_id or source mismatch.")
    players = {row["player_slot"]: row for row in context["players"]}
    for channel, observation in snapshot.observations.items():
        if (channel not in ORIGINS or not isinstance(observation, HistoricalObservation)
                or observation.origin != ORIGINS[channel]):
            raise ValueError("Unsupported historical channel or provenance.")
        if observation.values is None:
            continue
        _lineage(observation.values, context)
        if channel in ("heroes", "spatial_global"):
            for row in observation.values["players"]:
                _actor(row, context, players)
        elif channel == "pov_enemy_positions":
            for observer, rows in observation.values.items():
                if observer not in ("RADIANT", "DIRE"):
                    raise ValueError("Unknown observer side.")
                for row in rows:
                    _actor(row, context, players, enemy=True)
                    if row.get("pov_team") != observer or row["enemy_team"] == observer:
                        raise ValueError("Enemy/observer side mismatch.")
    return ContextualHistoricalSnapshot(deepcopy(snapshot), context, phase)
