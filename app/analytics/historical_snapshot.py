"""Pure historical observation snapshots, independent of storage, web and models.

Inputs use the existing repository row shapes and must be loaded for the selected
match. Repository results omit identity; when source/source_match_id metadata is
present, it must match. Bare rows cannot independently prove their match scope.

Each source is selected independently by actual time, without interpolation or
tick-to-time conversion. Incomplete POV frames are not candidates. Malformed or
ambiguous records fail explicitly. There is no implicit freshness limit: age is
reported for the consumer to assess. Values remain observations at their source
times, not assertions about the complete state at the requested time or LIVE.

The observation envelope can represent further confirmed sources later; this
module currently accepts only economy and team POV, never forecast outputs.
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
           origin: str) -> HistoricalObservation:
    selected = max((item for item in candidates if item[0] <= time),
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


def build_historical_snapshot(
    match: MatchIdentity,
    t: Time,
    economy_records: Sequence[Mapping],
    pov_records: Sequence[Mapping],
) -> HistoricalSnapshot:
    """Return only whitelisted observations at or before t; never future targets.

    Unsorted inputs are supported. Equal times are resolved by the higher source
    tick. Economic and POV ticks remain independent clocks/lineage coordinates;
    no cross-source equality or conversion is assumed. Source records are copied,
    and the returned payloads have no mutable aliases into the inputs.
    """
    if not isinstance(match, MatchIdentity):
        raise ValueError("Pass a MatchIdentity, not a complete match/result record.")
    match = MatchIdentity(match.source, match.source_match_id)
    requested_time = _seconds(t)
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
    return HistoricalSnapshot(match, requested_time, {
        "economy": _as_of(economy, requested_time, "HISTORICAL_ORACLE"),
        "pov": _as_of(pov, requested_time, "HISTORICAL_TEAM_POV"),
    })
