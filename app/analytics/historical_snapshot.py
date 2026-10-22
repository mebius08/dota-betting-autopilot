"""Pure historical observation snapshots, independent of storage, web and models.

Inputs use the existing repository row shapes and must be loaded for the selected
match. Repository results omit identity; when source/source_match_id metadata is
present, it must match. Bare rows cannot independently prove their match scope.

Each source is selected independently by actual time, without interpolation or
tick-to-time conversion. Incomplete POV frames are not candidates. Malformed or
ambiguous records fail explicitly. There is no implicit freshness limit: age is
reported for the consumer to assess. Values remain observations at their source
times, not assertions about the complete state at the requested time or LIVE.

Optional hero records use compact snapshot mappings, with actual callback time
in source_game_time_seconds and a nested players roster. Well-formed partial
rosters are not candidates; malformed records fail explicitly, including those
outside the selected time. Bare compact frames must be scoped by the caller.
Hero inventory entries describe observed possession, never purchase events or
ability readiness. Economy and heroes remain historical oracle observations,
separate from team POV and never evidence of LIVE availability.

Spatial frames are a separate historical oracle channel, never team knowledge.
They require complete selected-hero rosters; corrupt frames fail explicitly.
An optional requested replay tick additionally bounds spatial selection, without
converting ticks to elapsed seconds. Optional replay_sha256 is a reference for
checking record hashes; provided hashes must also agree across spatial records.
As with bare identity, absent hash metadata cannot prove replay lineage.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal


Time = int | float | Decimal
ECONOMY_FIELDS = (
    "radiant_net_worth", "dire_net_worth", "radiant_current_gold",
    "dire_current_gold", "radiant_total_xp", "dire_total_xp",
)
POV_FIELDS = (
    "visible_enemy_count", "visible_enemy_with_position_count", "hidden_enemy_count",
    "own_team_spread", "nearest_visible_enemy_distance",
    "own_team_spread_status", "nearest_visible_enemy_status",
)
HERO_METRICS = (
    "level", "net_worth", "current_gold", "total_xp", "kills", "deaths",
    "assists", "last_hits", "denies",
)
TEAMS = ("RADIANT", "DIRE")


@dataclass(frozen=True)
class MatchIdentity:
    source: str
    source_match_id: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip()
               for value in (self.source, self.source_match_id)):
            raise ValueError("Match identity requires nonempty source and source_match_id.")


@dataclass(frozen=True)
class HistoricalObservation:
    origin: str
    status: Literal["AVAILABLE", "NO_PRECEDING_SNAPSHOT"]
    actual_game_time_seconds: Decimal | None
    replay_tick: int | None
    age_seconds: Decimal | None
    values: dict | None


@dataclass(frozen=True)
class HistoricalSnapshot:
    match: MatchIdentity
    requested_game_time_seconds: Decimal
    observations: dict[str, HistoricalObservation]
    snapshot_kind: Literal["HISTORICAL_OBSERVATIONS"] = "HISTORICAL_OBSERVATIONS"


def _seconds(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("Game time and numeric observations must be finite numbers.")
    # str(float) preserves the provided float's round-trip decimal representation;
    # existing Decimal values are kept without applying context precision.
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Game time and numeric observations must be finite numbers.")
    return number


def _row_time_tick(row: Mapping, match: MatchIdentity) -> tuple[Decimal, int]:
    if not isinstance(row, Mapping):
        raise ValueError("Observation records must be mappings.")
    if "source" in row or "source_match_id" in row:
        if (row.get("source"), row.get("source_match_id")) != (match.source, match.source_match_id):
            raise ValueError("Observation match identity does not match the selected match.")
    if "actual_game_time_seconds" not in row or "replay_tick" not in row:
        raise ValueError("Actual game time and replay tick are required; nominal time is not accepted.")
    tick = row["replay_tick"]
    if isinstance(tick, bool) or not isinstance(tick, int) or tick < 0:
        raise ValueError("Replay tick must be a nonnegative integer.")
    return _seconds(row["actual_game_time_seconds"]), tick


def _values(row: Mapping, fields: tuple[str, ...]) -> dict:
    if any(name not in row for name in fields):
        raise ValueError("Missing observation fields; use explicit None for unknown values.")
    result = {}
    for name in fields:
        value = row[name]
        if value is not None:
            if name.endswith("_status"):
                if not isinstance(value, str):
                    raise ValueError("Feature status must be a string or None.")
            else:
                _seconds(value)  # Validate without changing the stored metric/type.
        result[name] = value
    return result


def _as_of(candidates: Sequence[tuple[Decimal, int, dict]], time: Decimal,
           origin: str, max_tick: int | None = None) -> HistoricalObservation:
    selected = max((item for item in candidates
                    if item[0] <= time and (max_tick is None or item[1] <= max_tick)),
                   key=lambda item: (item[0], item[1]), default=None)
    if selected is None:
        return HistoricalObservation(origin, "NO_PRECEDING_SNAPSHOT", None, None, None, None)
    observed_time, tick, values = selected
    # Exact age, including Decimal timestamps finer than the default context.
    with localcontext() as context:
        context.prec = max(time.adjusted(), observed_time.adjusted()) - min(
            time.as_tuple().exponent, observed_time.as_tuple().exponent
        ) + 2
        age = time - observed_time
    return HistoricalObservation(origin, "AVAILABLE", observed_time, tick, age, values)


def _hero_candidates(records: Sequence[Mapping], match: MatchIdentity) -> list:
    candidates, ticks = [], set()
    for frame in records:
        if not isinstance(frame, Mapping) or "source_game_time_seconds" not in frame:
            raise ValueError("Hero frames require actual source_game_time_seconds, not nominal time.")
        time, tick = _row_time_tick(
            dict(frame, actual_game_time_seconds=frame["source_game_time_seconds"]), match,
        )
        if tick in ticks:
            raise ValueError("Duplicate hero replay tick.")
        ticks.add(tick)
        players = frame.get("players")
        if not isinstance(players, (list, tuple)) or len(players) > 10:
            raise ValueError("Hero frames require a roster of at most ten players.")
        for record in (frame, *players):
            if not isinstance(record, Mapping):
                raise ValueError("Hero records must be mappings.")
            if "match_id" in record and str(record["match_id"]) != match.source_match_id:
                raise ValueError("Hero match identity does not match the selected match.")
            for field in ("actual_game_time_seconds", "source_game_time_seconds"):
                if field in record and _seconds(record[field]) != time:
                    raise ValueError("Hero record has inconsistent actual frame times.")
        heroes, slots = [], set()
        for player in players:
            # Optional per-player lineage must agree with its enclosing frame.
            player_time, player_tick = _row_time_tick(
                {"actual_game_time_seconds": time, "replay_tick": tick} | dict(player), match,
            )
            if (player_time, player_tick) != (time, tick):
                raise ValueError("Hero record belongs to a different frame.")
            slot, team = player.get("player_slot"), player.get("team")
            if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0 or slot in slots:
                raise ValueError("Hero player slots must be unique nonnegative integers.")
            slots.add(slot)
            if team not in TEAMS:
                raise ValueError("Hero team must be RADIANT or DIRE.")
            if "hero_id" not in player or "hero_name" not in player or "items" not in player:
                raise ValueError("Missing hero identity or items; use explicit None for unknown values.")
            hero_id, hero_name = player["hero_id"], player["hero_name"]
            if hero_id is not None and (
                isinstance(hero_id, bool) or not isinstance(hero_id, int) or hero_id <= 0
            ):
                raise ValueError("Hero ID must be a positive integer or None.")
            if hero_name is not None and not isinstance(hero_name, str):
                raise ValueError("Hero name must be a string or None.")
            items = player["items"]
            if items is not None:
                if not isinstance(items, (list, tuple)):
                    raise ValueError("Hero items must be a sequence or None.")
                copied_items, inventory_slots = [], set()
                for item in items:
                    if not isinstance(item, Mapping) or "inventory_slot" not in item or "item_name" not in item:
                        raise ValueError("Items require inventory_slot and item_name.")
                    inventory_slot, name = item["inventory_slot"], item["item_name"]
                    if (isinstance(inventory_slot, bool) or not isinstance(inventory_slot, int)
                            or inventory_slot < 0 or inventory_slot in inventory_slots):
                        raise ValueError("Inventory slots must be unique nonnegative integers.")
                    if name is not None and not isinstance(name, str):
                        raise ValueError("Item name must be a string or None.")
                    inventory_slots.add(inventory_slot)
                    copied_items.append({"inventory_slot": inventory_slot, "item_name": name})
                items = copied_items
            heroes.append({
                "source": match.source, "source_match_id": match.source_match_id,
                "player_slot": slot, "team": team, "hero_id": hero_id, "hero_name": hero_name,
                **_values(player, HERO_METRICS), "items": items,
            })
        if any(sum(hero["team"] == team for hero in heroes) > 5 for team in TEAMS):
            raise ValueError("Complete hero frames must contain five RADIANT and five DIRE players.")
        if len(heroes) < 10:
            continue
        candidates.append((time, tick, {"players": heroes}))
    return candidates


def _sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in value
    ):
        raise ValueError("Replay SHA-256 must contain 64 hexadecimal characters.")
    return value.lower()


def _spatial_candidates(records: Sequence[Mapping], match: MatchIdentity,
                        replay_sha256: str | None) -> list:
    expected_sha = _sha256(replay_sha256) if replay_sha256 is not None else None
    candidates, ticks = [], set()
    for frame in records:
        if not isinstance(frame, Mapping) or "source_game_time_seconds" not in frame:
            raise ValueError("Spatial frames require actual source_game_time_seconds, not grid time.")
        time, tick = _row_time_tick(
            dict(frame, actual_game_time_seconds=frame["source_game_time_seconds"]), match,
        )
        if tick in ticks:
            raise ValueError("Duplicate spatial replay tick.")
        ticks.add(tick)
        players = frame.get("players")
        if not isinstance(players, (list, tuple)) or len(players) != 10:
            raise ValueError("Spatial frames require exactly ten main heroes.")
        for record in (frame, *players):
            if not isinstance(record, Mapping):
                raise ValueError("Spatial records must be mappings.")
            record_time, record_tick = _row_time_tick(
                {"actual_game_time_seconds": time, "replay_tick": tick} | dict(record), match,
            )
            if (record_time, record_tick) != (time, tick):
                raise ValueError("Spatial record belongs to a different frame.")
            if "source_game_time_seconds" in record and _seconds(record["source_game_time_seconds"]) != time:
                raise ValueError("Spatial record has inconsistent actual frame times.")
            if "source_replay_tick" in record:
                _, source_tick = _row_time_tick(dict(record, actual_game_time_seconds=time,
                                                     replay_tick=record["source_replay_tick"]), match)
                if source_tick != tick:
                    raise ValueError("Spatial record has inconsistent replay ticks.")
            if "match_id" in record and str(record["match_id"]) != match.source_match_id:
                raise ValueError("Spatial match identity does not match the selected match.")
            if "replay_sha256" in record:
                sha = _sha256(record["replay_sha256"])
                if expected_sha is not None and sha != expected_sha:
                    raise ValueError("Spatial replay SHA-256 does not match.")
                expected_sha = sha
        heroes, slots, hero_ids = [], set(), set()
        for player in players:
            required = ("player_slot", "team", "hero_id", "x", "y", "position_status", "life_state")
            if any(field not in player for field in required):
                raise ValueError("Missing spatial fields; use explicit None for unknown positions.")
            slot, team, hero_id = player["player_slot"], player["team"], player["hero_id"]
            if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0 or slot in slots:
                raise ValueError("Spatial player slots must be unique nonnegative integers.")
            if (isinstance(hero_id, bool) or not isinstance(hero_id, int) or hero_id <= 0
                    or hero_id in hero_ids or team not in TEAMS):
                raise ValueError("Spatial frames require unique hero IDs and RADIANT/DIRE teams.")
            slots.add(slot)
            hero_ids.add(hero_id)
            x, y, status, life = (player[field] for field in ("x", "y", "position_status", "life_state"))
            if not isinstance(status, str) or not status:
                raise ValueError("Spatial position_status must preserve a nonempty source status.")
            if life is not None and (isinstance(life, bool) or not isinstance(life, int) or life < 0):
                raise ValueError("Spatial life_state must be a nonnegative integer or None.")
            if (x is None) != (y is None):
                raise ValueError("Spatial coordinates must be a pair or both None.")
            if x is not None:
                _seconds(x)
                _seconds(y)
            if (status == "OBSERVED") != (x is not None) or (x is not None and life != 0):
                raise ValueError("Spatial position status, life state and coordinates contradict.")
            if status == "HERO_NOT_ALIVE" and life in (None, 0):
                raise ValueError("HERO_NOT_ALIVE requires a nonzero life_state.")
            identity = {}
            if "hero_entity_class" in player:
                name = player["hero_entity_class"]
                if name is not None and not isinstance(name, str):
                    raise ValueError("Hero entity class must be a string or None.")
                identity["hero_entity_class"] = name
            if "selected_hero_handle" in player or "hero_entity_handle" in player:
                for field in ("selected_hero_handle", "hero_entity_handle"):
                    handle = player.get(field)
                    if isinstance(handle, bool) or not isinstance(handle, int) or handle < 0:
                        raise ValueError("Spatial selected-hero handles must be nonnegative integers.")
                    identity[field] = handle
                if identity["selected_hero_handle"] != identity["hero_entity_handle"]:
                    raise ValueError("Spatial entity is not the selected main hero.")
            heroes.append({
                "source": match.source, "source_match_id": match.source_match_id,
                "player_slot": slot, "team": team, "hero_id": hero_id, **identity,
                "x": x, "y": y, "position_status": status, "life_state": life,
            })
        if any(sum(hero["team"] == team for hero in heroes) != 5 for team in TEAMS):
            raise ValueError("Spatial frames must contain five RADIANT and five DIRE heroes.")
        values = {"players": heroes}
        if "replay_sha256" in frame:
            values["replay_sha256"] = frame["replay_sha256"]
        candidates.append((time, tick, values))
    ordered = sorted(candidates, key=lambda item: item[1])
    if any(previous[0] > current[0] for previous, current in zip(ordered, ordered[1:])):
        raise ValueError("Spatial game time must be monotonic with replay ticks; equal times are allowed.")
    return candidates


def build_historical_snapshot(
    match: MatchIdentity,
    t: Time,
    economy_records: Sequence[Mapping],
    pov_records: Sequence[Mapping],
    *,
    hero_records: Sequence[Mapping] | None = None,
    spatial_records: Sequence[Mapping] | None = None,
    requested_replay_tick: int | None = None,
    replay_sha256: str | None = None,
) -> HistoricalSnapshot:
    """Return only whitelisted observations at or before t; never future targets.

    Unsorted inputs are supported. Equal times are resolved by the higher source
    tick. Economic and POV ticks remain independent clocks/lineage coordinates;
    no cross-source equality or conversion is assumed. Source records are copied,
    and the returned payloads have no mutable aliases into the inputs.

    Supply already-loaded compact snapshots through hero_records to add heroes;
    omitting it preserves the original two-channel result. Partial rosters are
    skipped without merging frames. Invalid/contradictory records raise ValueError
    rather than silently producing a partial state or hiding corrupt input.

    spatial_records accepts already-loaded sidecar snapshots. Spatial selection
    uses source_game_time_seconds and, if supplied, requested_replay_tick as a
    second upper bound. Other channels retain their existing independent as-of
    selection. Frame/player identity and SHA metadata, when present, are checked;
    replay_sha256 can supply an expected hash without changing MatchIdentity.
    """
    if not isinstance(match, MatchIdentity):
        raise ValueError("Pass a MatchIdentity, not a complete match/result record.")
    match = MatchIdentity(match.source, match.source_match_id)
    requested_time = _seconds(t)
    if requested_replay_tick is not None and (
        isinstance(requested_replay_tick, bool) or not isinstance(requested_replay_tick, int)
        or requested_replay_tick < 0
    ):
        raise ValueError("Requested replay tick must be a nonnegative integer or None.")
    economy, economic_ticks = [], set()
    for row in economy_records:
        time, tick = _row_time_tick(row, match)
        if tick in economic_ticks:
            raise ValueError("Duplicate economic replay tick.")
        economic_ticks.add(tick)
        economy.append((time, tick, _values(row, ECONOMY_FIELDS)))
    frames = {}
    for row in pov_records:
        time, tick = _row_time_tick(row, match)
        team = row.get("pov_team")
        if team not in TEAMS:
            raise ValueError("POV team must be RADIANT or DIRE.")
        frame_time, teams = frames.setdefault(tick, (time, {}))
        if frame_time != time or team in teams:
            raise ValueError("POV tick has inconsistent actual times or a duplicate team.")
        teams[team] = _values(row, POV_FIELDS)
    pov = [(time, tick, {team: teams[team] for team in TEAMS})
           for tick, (time, teams) in frames.items() if set(teams) == set(TEAMS)]
    observations = {
        "economy": _as_of(economy, requested_time, "HISTORICAL_ORACLE"),
        "pov": _as_of(pov, requested_time, "HISTORICAL_TEAM_POV"),
    }
    if hero_records is not None:
        observations["heroes"] = _as_of(
            _hero_candidates(hero_records, match), requested_time, "HISTORICAL_ORACLE",
        )
    if spatial_records is not None:
        observations["spatial_global"] = _as_of(
            _spatial_candidates(spatial_records, match, replay_sha256), requested_time,
            "HISTORICAL_ORACLE", requested_replay_tick,
        )
    return HistoricalSnapshot(match, requested_time, observations)
