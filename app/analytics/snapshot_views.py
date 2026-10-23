"""Historical information-scope projections; no source is assumed available LIVE.

HistoricalSnapshot does not retain the requested replay tick. Callers may pass
it explicitly; otherwise it remains None, never inferred from observation ticks.
Unknown channels/provenance are errors. Team views discard unlisted payload
fields; GLOBAL rejects them so its accepted payloads remain lossless.
"""

from collections.abc import Mapping
from decimal import Decimal

from app.analytics.historical_snapshot import (
    HistoricalObservation, HistoricalSnapshot, MatchIdentity,
    _elapsed_seconds, _enemy_positions, _seconds,
)


CHANNEL_ORIGINS = {
    "economy": "HISTORICAL_ORACLE", "heroes": "HISTORICAL_ORACLE",
    "spatial_global": "HISTORICAL_ORACLE", "pov": "HISTORICAL_TEAM_POV",
    "pov_enemy_positions": "HISTORICAL_TEAM_POV",
}
VIEWS = {"HISTORICAL_GLOBAL": None, "HISTORICAL_RADIANT_POV": "RADIANT", "HISTORICAL_DIRE_POV": "DIRE"}
POV_FIELDS = (
    "visible_enemy_count", "visible_enemy_with_position_count", "hidden_enemy_count",
    "own_team_spread", "nearest_visible_enemy_distance", "own_team_spread_status",
    "nearest_visible_enemy_status",
)
ENEMY_FIELDS = (
    "source", "source_match_id", "match_id", "replay_sha256", "game_time_seconds",
    "replay_tick", "pov_team", "enemy_player_slot", "enemy_hero_id", "enemy_team",
    "visibility_state", "current_position_status", "current_x", "current_y",
    "last_seen_status", "last_seen_game_time_seconds", "last_seen_age_seconds",
    "last_seen_x", "last_seen_y", "last_seen_age_at_requested_time_seconds",
)
ECONOMY_FIELDS = (
    "radiant_net_worth", "dire_net_worth", "radiant_current_gold", "dire_current_gold",
    "radiant_total_xp", "dire_total_xp",
)
HERO_FIELDS = (
    "source", "source_match_id", "player_slot", "team", "hero_id", "hero_name",
    "level", "net_worth", "current_gold", "total_xp", "kills", "deaths", "assists",
    "last_hits", "denies",
)
SPATIAL_REQUIRED = ("source", "source_match_id", "player_slot", "team", "hero_id",
                    "x", "y", "position_status", "life_state")
SPATIAL_FIELDS = (*SPATIAL_REQUIRED, "hero_entity_class", "selected_hero_handle", "hero_entity_handle")
STRING_FIELDS = {"source", "source_match_id", "replay_sha256", "team", "pov_team",
                 "enemy_team", "hero_name", "hero_entity_class", "item_name", "visibility_state"}


def _fields(data: Mapping, allowed: tuple[str, ...], *, strict: bool,
            required: tuple[str, ...] | None = None) -> dict:
    if not isinstance(data, Mapping):
        raise ValueError("Observation payloads must be mappings.")
    required = allowed if required is None else required
    if any(field not in data for field in required) or (strict and set(data) - set(allowed)):
        raise ValueError("Payload does not match the explicit observation field allowlist.")
    result = {}
    for field in allowed:
        if field not in data:
            continue
        value = data[field]
        if value is not None:
            if field in STRING_FIELDS or field.endswith("_status"):
                if not isinstance(value, str):
                    raise ValueError("Observation text/status must be a string or None.")
            elif field == "match_id":
                if isinstance(value, bool) or not isinstance(value, (int, str)):
                    raise ValueError("Invalid match identity.")
            else:
                _seconds(value)  # Validate immutable numeric leaves without converting them.
        result[field] = value
    return result


def _oracle_payload(channel: str, values: Mapping) -> dict:
    if channel == "economy":
        return _fields(values, ECONOMY_FIELDS, strict=True)
    if not isinstance(values, Mapping) or not isinstance(values.get("players"), list):
        raise ValueError("Oracle hero/position payloads require a player list.")
    metadata = {key: value for key, value in values.items() if key != "players"}
    result = _fields(metadata, ("replay_sha256",) if channel == "spatial_global" else (),
                     strict=True, required=())
    players = []
    for player in values["players"]:
        if not isinstance(player, Mapping):
            raise ValueError("Player payload must be a mapping.")
        if channel == "spatial_global":
            copied = _fields(player, SPATIAL_FIELDS, strict=True, required=SPATIAL_REQUIRED)
        else:
            copied = _fields({key: value for key, value in player.items() if key != "items"},
                             HERO_FIELDS, strict=True)
            if "items" not in player or (player["items"] is not None and not isinstance(player["items"], list)):
                raise ValueError("Hero inventory must be a list or None.")
            copied["items"] = None if player["items"] is None else [
                _fields(item, ("inventory_slot", "item_name"), strict=True) for item in player["items"]
            ]
        players.append(copied)
    return result | {"players": players}


def _team_payload(channel: str, values: Mapping, teams: tuple[str, ...],
                  observation: HistoricalObservation, snapshot: HistoricalSnapshot, *, strict: bool) -> dict:
    if not isinstance(values, Mapping) or (strict and set(values) != {"RADIANT", "DIRE"}):
        raise ValueError("Team observation payload must contain the requested team scope.")
    result = {}
    for team in teams:
        if team not in values:
            raise ValueError("Requested team's observation is missing; no opposite-team fallback.")
        if channel == "pov":
            result[team] = _fields(values[team], POV_FIELDS, strict=strict)
            continue
        rows = values[team]
        if not isinstance(rows, list) or len(rows) != 5:
            raise ValueError("Enemy observations require five enemies per observer.")
        enemies = []
        for row in rows:
            enemy = _fields(row, ENEMY_FIELDS, strict=strict)
            if (enemy["pov_team"] != team or enemy["enemy_team"] != ("DIRE" if team == "RADIANT" else "RADIANT")
                    or enemy["source"] != snapshot.match.source
                    or enemy["source_match_id"] != snapshot.match.source_match_id
                    or str(enemy["match_id"]) != snapshot.match.source_match_id
                    or enemy["game_time_seconds"] != observation.actual_game_time_seconds
                    or enemy["replay_tick"] != observation.replay_tick):
                raise ValueError("Enemy observation has inconsistent team, match or frame identity.")
            # Reuse source validation, but retain the original types/values.
            _enemy_positions(enemy, observation.actual_game_time_seconds)
            seen = enemy["last_seen_game_time_seconds"]
            age = _elapsed_seconds(snapshot.requested_game_time_seconds, _seconds(seen)) if seen is not None else None
            if enemy["last_seen_age_at_requested_time_seconds"] != age:
                raise ValueError("Last-seen age at the requested time is inconsistent.")
            enemies.append(enemy)
        if (len({enemy["enemy_player_slot"] for enemy in enemies}) != 5
                or len({enemy["enemy_hero_id"] for enemy in enemies}) != 5):
            raise ValueError("Duplicate enemy identity.")
        result[team] = enemies
    return result


def project_snapshot(snapshot: HistoricalSnapshot, view: str, *,
                     requested_replay_tick: int | None = None) -> dict:
    """Return a detached historical view with explicit metadata and payload fields.

    GLOBAL preserves all present confirmed channels; optional channels absent in
    the input stay absent. Explicit NO_PRECEDING_SNAPSHOT envelopes are retained.
    Team views never consult oracle payloads or the other team's payload.
    """
    if not isinstance(view, str) or view not in VIEWS:
        raise ValueError("Unknown historical snapshot view.")
    if (not isinstance(snapshot, HistoricalSnapshot) or not isinstance(snapshot.match, MatchIdentity)
            or snapshot.snapshot_kind != "HISTORICAL_OBSERVATIONS"):
        raise ValueError("Expected a HistoricalSnapshot of observations, not predictions.")
    MatchIdentity(snapshot.match.source, snapshot.match.source_match_id)
    if requested_replay_tick is not None and (
        isinstance(requested_replay_tick, bool) or not isinstance(requested_replay_tick, int)
        or requested_replay_tick < 0
    ):
        raise ValueError("Requested replay tick must be a nonnegative integer or None.")
    time = snapshot.requested_game_time_seconds
    if not isinstance(time, Decimal) or not time.is_finite():
        raise ValueError("Requested game time must be a finite Decimal.")
    if not isinstance(snapshot.observations, Mapping) or set(snapshot.observations) - set(CHANNEL_ORIGINS):
        raise ValueError("Unknown observation channel; extend the allowlist explicitly.")
    team = VIEWS[view]
    observations = {}
    for channel, observation in snapshot.observations.items():
        if not isinstance(observation, HistoricalObservation) or observation.origin != CHANNEL_ORIGINS[channel]:
            raise ValueError("Observation provenance contradicts its channel.")
        if team is not None and channel not in ("pov", "pov_enemy_positions"):
            continue
        if observation.status == "NO_PRECEDING_SNAPSHOT":
            if any(value is not None for value in (observation.actual_game_time_seconds,
                                                  observation.replay_tick, observation.age_seconds, observation.values)):
                raise ValueError("Missing observations must preserve an empty envelope.")
            values = None
        elif observation.status == "AVAILABLE":
            observed, tick, age = observation.actual_game_time_seconds, observation.replay_tick, observation.age_seconds
            if (not isinstance(observed, Decimal) or not observed.is_finite() or observed > time
                    or isinstance(tick, bool) or not isinstance(tick, int) or tick < 0
                    or (requested_replay_tick is not None and tick > requested_replay_tick)
                    or not isinstance(age, Decimal) or not age.is_finite()
                    or age != _elapsed_seconds(time, observed)):
                raise ValueError("Observation time, tick or age contradicts the requested moment.")
            values = (_team_payload(channel, observation.values, (team,) if team else ("RADIANT", "DIRE"),
                                    observation, snapshot, strict=team is None)
                      if channel in ("pov", "pov_enemy_positions") else _oracle_payload(channel, observation.values))
        else:
            raise ValueError("Unknown observation availability status.")
        observations[channel] = {
            "origin": observation.origin, "status": observation.status,
            "actual_game_time_seconds": observation.actual_game_time_seconds,
            "replay_tick": observation.replay_tick, "age_seconds": observation.age_seconds, "values": values,
        }
    return {
        "view": view, "snapshot_kind": snapshot.snapshot_kind,
        "match": {"source": snapshot.match.source, "source_match_id": snapshot.match.source_match_id},
        "requested_game_time_seconds": time, "requested_replay_tick": requested_replay_tick,
        "observations": observations,
    }
