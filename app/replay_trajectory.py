from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, assert_never, cast


SUPPORTED_SCHEMA_VERSION = 1
TEAM_NAMES = ("RADIANT", "DIRE")
DOTA_TEAM_NUMBERS = (2, 3)
SNAPSHOT_INTERVAL_SECONDS = 60
CLOCK_DEVIATION_TOLERANCE_SECONDS = 0.01
PAUSE_ZERO_TOLERANCE_SECONDS = 0.01
REPLAY_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")

ROOT_FIELDS = (
    "schema_version",
    "match_id",
    "replay_sha256",
    "clarity_version",
    "game_mode",
    "league_id",
    "winner",
    "radiant_team_id",
    "radiant_team_tag",
    "dire_team_id",
    "dire_team_tag",
    "picks_bans",
    "clock_normalization_status",
    "clock_normalization_method",
    "zero_replay_tick",
    "game_end_time_seconds",
    "pause_ticks",
    "pause_seconds",
    "pause_witnessed",
    "snapshots",
)
SNAPSHOT_FIELDS = (
    "game_time_seconds",
    "source_game_time_seconds",
    "replay_tick",
    "game_state",
    "teams",
    "players",
)
TEAM_FIELDS = (
    "team",
    "kills",
    "deaths",
    "assists",
    "last_hits",
    "denies",
    "net_worth",
    "current_gold",
    "total_xp",
)
PLAYER_FIELDS = (
    "player_slot",
    "team",
    "hero_id",
    "hero_name",
    "level",
    "kills",
    "deaths",
    "assists",
    "last_hits",
    "denies",
    "net_worth",
    "current_gold",
    "total_xp",
    "items",
)
ITEM_FIELDS = ("inventory_slot", "item_name")
DRAFT_FIELDS = ("is_pick", "team", "hero_id")
AggregateField = Literal[
    "kills",
    "deaths",
    "assists",
    "last_hits",
    "denies",
    "net_worth",
    "current_gold",
    "total_xp",
]
AGGREGATE_FIELDS: tuple[AggregateField, ...] = (
    "kills",
    "deaths",
    "assists",
    "last_hits",
    "denies",
    "net_worth",
    "current_gold",
    "total_xp",
)


class ReplayTrajectoryError(ValueError):
    """Raised when a replay trajectory cannot be safely imported."""


class ReplayItem(TypedDict):
    inventory_slot: int
    item_name: str


class ReplayPlayer(TypedDict):
    player_slot: int
    team: str
    hero_id: int
    hero_name: str
    level: int
    kills: int
    deaths: int
    assists: int
    last_hits: int
    denies: int
    net_worth: int
    current_gold: int
    total_xp: int
    items: list[ReplayItem]


class ReplayTeam(TypedDict):
    team: str
    kills: int
    deaths: int
    assists: int
    last_hits: int
    denies: int
    net_worth: int
    current_gold: int
    total_xp: int


class ReplaySnapshot(TypedDict):
    game_time_seconds: int
    source_game_time_seconds: float
    replay_tick: int
    game_state: int
    teams: list[ReplayTeam]
    players: list[ReplayPlayer]


class DraftAction(TypedDict):
    is_pick: bool
    team: int
    hero_id: int


class ReplayTrajectory(TypedDict):
    schema_version: int
    match_id: int
    replay_sha256: str
    clarity_version: str
    game_mode: int
    league_id: int
    winner: int
    radiant_team_id: int | None
    radiant_team_tag: str | None
    dire_team_id: int | None
    dire_team_tag: str | None
    picks_bans: list[DraftAction]
    clock_normalization_status: str
    clock_normalization_method: str
    zero_replay_tick: int
    game_end_time_seconds: float
    pause_ticks: int
    pause_seconds: float
    pause_witnessed: bool
    snapshots: list[ReplaySnapshot]


@dataclass(frozen=True)
class ReplayTrajectoryImportResult:
    status: Literal["IMPORTED", "UNCHANGED"]
    destination: Path
    match_id: int
    replay_sha256: str
    snapshot_count: int
    artifact_size_bytes: int


def import_replay_trajectory(
    input_path: str | Path,
    local_data_dir: str | Path,
) -> ReplayTrajectoryImportResult:
    trajectory = load_replay_trajectory(input_path)
    canonical = canonical_replay_trajectory_bytes(trajectory)
    destination = Path(local_data_dir) / f"{trajectory['match_id']}.json"

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReplayTrajectoryError(
            "Cannot create the replay trajectory destination directory."
        ) from exc

    if _existing_artifact_is_identical(destination, canonical, trajectory):
        status: Literal["IMPORTED", "UNCHANGED"] = "UNCHANGED"
    else:
        wrote_artifact = _write_atomic(destination, canonical, trajectory)
        status = "IMPORTED" if wrote_artifact else "UNCHANGED"

    return ReplayTrajectoryImportResult(
        status=status,
        destination=destination,
        match_id=trajectory["match_id"],
        replay_sha256=trajectory["replay_sha256"],
        snapshot_count=len(trajectory["snapshots"]),
        artifact_size_bytes=len(canonical),
    )


def load_replay_trajectory(path: str | Path) -> ReplayTrajectory:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReplayTrajectoryError(
            "Input replay trajectory is unreadable UTF-8 JSON."
        ) from exc

    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_non_finite_constant,
        )
    except json.JSONDecodeError as exc:
        raise ReplayTrajectoryError(
            f"Input replay trajectory is invalid JSON: {exc.msg}."
        ) from exc

    return validate_replay_trajectory(value)


def validate_replay_trajectory(value: object) -> ReplayTrajectory:
    root = _object(value, "$", ROOT_FIELDS)

    schema_version = _integer(root["schema_version"], "schema_version")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ReplayTrajectoryError(
            "schema_version must be " f"{SUPPORTED_SCHEMA_VERSION}."
        )

    match_id = _positive_integer(root["match_id"], "match_id")
    replay_sha256 = _string(root["replay_sha256"], "replay_sha256")
    if REPLAY_SHA256_PATTERN.fullmatch(replay_sha256) is None:
        raise ReplayTrajectoryError(
            "replay_sha256 must contain exactly 64 hexadecimal characters."
        )

    clarity_version = _non_empty_string(
        root["clarity_version"], "clarity_version"
    )
    game_mode = _non_negative_integer(root["game_mode"], "game_mode")
    league_id = _non_negative_integer(root["league_id"], "league_id")
    winner = _integer(root["winner"], "winner")
    if winner not in DOTA_TEAM_NUMBERS:
        raise ReplayTrajectoryError("winner must be Dota team number 2 or 3.")

    radiant_team_id = _nullable_non_negative_integer(
        root["radiant_team_id"], "radiant_team_id"
    )
    radiant_team_tag = _nullable_string(
        root["radiant_team_tag"], "radiant_team_tag"
    )
    dire_team_id = _nullable_non_negative_integer(
        root["dire_team_id"], "dire_team_id"
    )
    dire_team_tag = _nullable_string(root["dire_team_tag"], "dire_team_tag")
    picks_bans = _parse_draft(root["picks_bans"])

    clock_status = _string(
        root["clock_normalization_status"], "clock_normalization_status"
    )
    if clock_status != "PROVEN":
        raise ReplayTrajectoryError(
            "clock_normalization_status must be PROVEN."
        )
    clock_method = _non_empty_string(
        root["clock_normalization_method"], "clock_normalization_method"
    )
    zero_replay_tick = _non_negative_integer(
        root["zero_replay_tick"], "zero_replay_tick"
    )
    game_end_time_seconds = _non_negative_number(
        root["game_end_time_seconds"], "game_end_time_seconds"
    )
    pause_ticks = _non_negative_integer(root["pause_ticks"], "pause_ticks")
    pause_seconds = _non_negative_number(
        root["pause_seconds"], "pause_seconds"
    )
    pause_witnessed = _boolean(root["pause_witnessed"], "pause_witnessed")
    _validate_pause(pause_ticks, pause_seconds, pause_witnessed)

    snapshots = _parse_snapshots(root["snapshots"])
    last_scheduled_time = snapshots[-1]["game_time_seconds"]
    if game_end_time_seconds < last_scheduled_time:
        raise ReplayTrajectoryError(
            "game_end_time_seconds cannot be earlier than the last snapshot."
        )
    if game_end_time_seconds >= last_scheduled_time + SNAPSHOT_INTERVAL_SECONDS:
        raise ReplayTrajectoryError(
            "The next scheduled full minute must be later than "
            "game_end_time_seconds."
        )

    return ReplayTrajectory(
        schema_version=schema_version,
        match_id=match_id,
        replay_sha256=replay_sha256.lower(),
        clarity_version=clarity_version,
        game_mode=game_mode,
        league_id=league_id,
        winner=winner,
        radiant_team_id=radiant_team_id,
        radiant_team_tag=radiant_team_tag,
        dire_team_id=dire_team_id,
        dire_team_tag=dire_team_tag,
        picks_bans=picks_bans,
        clock_normalization_status=clock_status,
        clock_normalization_method=clock_method,
        zero_replay_tick=zero_replay_tick,
        game_end_time_seconds=game_end_time_seconds,
        pause_ticks=pause_ticks,
        pause_seconds=pause_seconds,
        pause_witnessed=pause_witnessed,
        snapshots=snapshots,
    )


def canonical_replay_trajectory_bytes(trajectory: ReplayTrajectory) -> bytes:
    text = json.dumps(
        trajectory,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    return f"{text}\n".encode("utf-8")


def _parse_draft(value: object) -> list[DraftAction]:
    rows = _array(value, "picks_bans")
    result: list[DraftAction] = []
    for index, value_row in enumerate(rows):
        path = f"picks_bans[{index}]"
        row = _object(value_row, path, DRAFT_FIELDS)
        is_pick = _boolean(row["is_pick"], f"{path}.is_pick")
        team = _integer(row["team"], f"{path}.team")
        if team not in DOTA_TEAM_NUMBERS:
            raise ReplayTrajectoryError(f"{path}.team must be 2 or 3.")
        hero_id = _positive_integer(row["hero_id"], f"{path}.hero_id")
        result.append(
            DraftAction(is_pick=is_pick, team=team, hero_id=hero_id)
        )
    return result


def _parse_snapshots(value: object) -> list[ReplaySnapshot]:
    rows = _array(value, "snapshots")
    if not rows:
        raise ReplayTrajectoryError("snapshots must be non-empty.")

    snapshots: list[ReplaySnapshot] = []
    previous_source_time: float | None = None
    previous_replay_tick: int | None = None
    for index, value_row in enumerate(rows):
        snapshot = _parse_snapshot(value_row, index)
        scheduled_time = snapshot["game_time_seconds"]
        expected_time = index * SNAPSHOT_INTERVAL_SECONDS
        if scheduled_time != expected_time:
            raise ReplayTrajectoryError(
                f"snapshots[{index}].game_time_seconds must be "
                f"{expected_time}."
            )

        source_time = snapshot["source_game_time_seconds"]
        if previous_source_time is not None and source_time < previous_source_time:
            raise ReplayTrajectoryError(
                "source_game_time_seconds must be monotonic."
            )
        if (
            abs(source_time - scheduled_time)
            > CLOCK_DEVIATION_TOLERANCE_SECONDS
        ):
            raise ReplayTrajectoryError(
                f"snapshots[{index}] scheduled/source clock deviation exceeds "
                f"{CLOCK_DEVIATION_TOLERANCE_SECONDS:.2f} seconds."
            )

        replay_tick = snapshot["replay_tick"]
        if previous_replay_tick is not None and replay_tick <= previous_replay_tick:
            raise ReplayTrajectoryError("replay ticks must be strictly increasing.")

        previous_source_time = source_time
        previous_replay_tick = replay_tick
        snapshots.append(snapshot)

    if snapshots[0]["game_time_seconds"] != 0:
        raise ReplayTrajectoryError(
            "The first snapshot game_time_seconds must be exactly 0."
        )
    return snapshots


def _parse_snapshot(value: object, index: int) -> ReplaySnapshot:
    path = f"snapshots[{index}]"
    row = _object(value, path, SNAPSHOT_FIELDS)
    game_time = _non_negative_integer(
        row["game_time_seconds"], f"{path}.game_time_seconds"
    )
    source_time = _number(
        row["source_game_time_seconds"], f"{path}.source_game_time_seconds"
    )
    replay_tick = _non_negative_integer(row["replay_tick"], f"{path}.replay_tick")
    game_state = _non_negative_integer(row["game_state"], f"{path}.game_state")
    players = _parse_players(row["players"], path)
    teams = _parse_teams(row["teams"], players, path)
    return ReplaySnapshot(
        game_time_seconds=game_time,
        source_game_time_seconds=source_time,
        replay_tick=replay_tick,
        game_state=game_state,
        teams=teams,
        players=players,
    )


def _parse_players(value: object, snapshot_path: str) -> list[ReplayPlayer]:
    path = f"{snapshot_path}.players"
    rows = _array(value, path)
    if len(rows) != 10:
        raise ReplayTrajectoryError(f"{path} must contain exactly ten rows.")

    players = [_parse_player(row, path, index) for index, row in enumerate(rows)]
    slots = [player["player_slot"] for player in players]
    if len(set(slots)) != len(slots):
        raise ReplayTrajectoryError(f"{path} contains a duplicate player_slot.")
    if set(slots) != set(range(10)):
        raise ReplayTrajectoryError(f"{path} must contain exact slots 0 through 9.")

    radiant_count = sum(player["team"] == "RADIANT" for player in players)
    dire_count = sum(player["team"] == "DIRE" for player in players)
    if radiant_count != 5 or dire_count != 5:
        raise ReplayTrajectoryError(
            f"{path} must contain exactly five RADIANT and five DIRE players."
        )
    return sorted(players, key=lambda player: player["player_slot"])


def _parse_player(value: object, players_path: str, index: int) -> ReplayPlayer:
    path = f"{players_path}[{index}]"
    row = _object(value, path, PLAYER_FIELDS)
    player_slot = _non_negative_integer(row["player_slot"], f"{path}.player_slot")
    if player_slot > 9:
        raise ReplayTrajectoryError(f"{path}.player_slot must be between 0 and 9.")
    team = _team_name(row["team"], f"{path}.team")
    hero_id = _positive_integer(row["hero_id"], f"{path}.hero_id")
    hero_name = _string(row["hero_name"], f"{path}.hero_name")

    numeric_fields = {
        field: _non_negative_integer(row[field], f"{path}.{field}")
        for field in PLAYER_FIELDS[4:-1]
    }
    items = _parse_items(row["items"], path)
    return ReplayPlayer(
        player_slot=player_slot,
        team=team,
        hero_id=hero_id,
        hero_name=hero_name,
        level=numeric_fields["level"],
        kills=numeric_fields["kills"],
        deaths=numeric_fields["deaths"],
        assists=numeric_fields["assists"],
        last_hits=numeric_fields["last_hits"],
        denies=numeric_fields["denies"],
        net_worth=numeric_fields["net_worth"],
        current_gold=numeric_fields["current_gold"],
        total_xp=numeric_fields["total_xp"],
        items=items,
    )


def _parse_items(value: object, player_path: str) -> list[ReplayItem]:
    path = f"{player_path}.items"
    rows = _array(value, path)
    items: list[ReplayItem] = []
    slots: set[int] = set()
    for index, value_row in enumerate(rows):
        item_path = f"{path}[{index}]"
        row = _object(value_row, item_path, ITEM_FIELDS)
        inventory_slot = _non_negative_integer(
            row["inventory_slot"], f"{item_path}.inventory_slot"
        )
        if inventory_slot in slots:
            raise ReplayTrajectoryError(
                f"{path} contains duplicate inventory_slot {inventory_slot}."
            )
        slots.add(inventory_slot)
        item_name = _non_empty_string(row["item_name"], f"{item_path}.item_name")
        items.append(
            ReplayItem(inventory_slot=inventory_slot, item_name=item_name)
        )
    return sorted(items, key=lambda item: item["inventory_slot"])


def _parse_teams(
    value: object,
    players: list[ReplayPlayer],
    snapshot_path: str,
) -> list[ReplayTeam]:
    path = f"{snapshot_path}.teams"
    rows = _array(value, path)
    if len(rows) != 2:
        raise ReplayTrajectoryError(f"{path} must contain exactly two rows.")

    teams = [_parse_team(row, path, index) for index, row in enumerate(rows)]
    by_name = {team["team"]: team for team in teams}
    if set(by_name) != set(TEAM_NAMES) or len(by_name) != 2:
        raise ReplayTrajectoryError(
            f"{path} must contain exactly one RADIANT and one DIRE row."
        )

    for team_name in TEAM_NAMES:
        team = by_name[team_name]
        team_players = [player for player in players if player["team"] == team_name]
        for field in AGGREGATE_FIELDS:
            expected = sum(_aggregate_value(player, field) for player in team_players)
            actual = _aggregate_value(team, field)
            if actual != expected:
                raise ReplayTrajectoryError(
                    f"{path} {team_name}.{field} must equal the player sum "
                    f"{expected}; found {actual}."
                )

    return [by_name["RADIANT"], by_name["DIRE"]]


def _aggregate_value(
    row: ReplayPlayer | ReplayTeam,
    field: AggregateField,
) -> int:
    if field == "kills":
        return row["kills"]
    if field == "deaths":
        return row["deaths"]
    if field == "assists":
        return row["assists"]
    if field == "last_hits":
        return row["last_hits"]
    if field == "denies":
        return row["denies"]
    if field == "net_worth":
        return row["net_worth"]
    if field == "current_gold":
        return row["current_gold"]
    if field == "total_xp":
        return row["total_xp"]
    assert_never(field)


def _parse_team(value: object, teams_path: str, index: int) -> ReplayTeam:
    path = f"{teams_path}[{index}]"
    row = _object(value, path, TEAM_FIELDS)
    team = _team_name(row["team"], f"{path}.team")
    numeric_fields = {
        field: _non_negative_integer(row[field], f"{path}.{field}")
        for field in AGGREGATE_FIELDS
    }
    return ReplayTeam(
        team=team,
        kills=numeric_fields["kills"],
        deaths=numeric_fields["deaths"],
        assists=numeric_fields["assists"],
        last_hits=numeric_fields["last_hits"],
        denies=numeric_fields["denies"],
        net_worth=numeric_fields["net_worth"],
        current_gold=numeric_fields["current_gold"],
        total_xp=numeric_fields["total_xp"],
    )


def _validate_pause(
    pause_ticks: int,
    pause_seconds: float,
    pause_witnessed: bool,
) -> None:
    if pause_ticks == 0:
        if not math.isclose(
            pause_seconds,
            0.0,
            rel_tol=0.0,
            abs_tol=PAUSE_ZERO_TOLERANCE_SECONDS,
        ):
            raise ReplayTrajectoryError(
                "zero pause_ticks requires approximately zero pause_seconds."
            )
        return

    if pause_seconds <= 0.0:
        raise ReplayTrajectoryError(
            "positive pause_ticks requires positive pause_seconds."
        )
    if not pause_witnessed:
        raise ReplayTrajectoryError(
            "positive pause_ticks requires pause_witnessed=true."
        )


def _existing_artifact_is_identical(
    destination: Path,
    canonical: bytes,
    trajectory: ReplayTrajectory,
) -> bool:
    if not destination.exists():
        return False
    try:
        existing = destination.read_bytes()
    except OSError as exc:
        raise ReplayTrajectoryError(
            "Destination artifact exists but cannot be read; refusing to overwrite it."
        ) from exc

    if existing == canonical:
        return True

    existing_sha256 = _read_existing_sha256(existing)
    match_id = trajectory["match_id"]
    if existing_sha256 is None:
        raise ReplayTrajectoryError(
            f"Destination for match_id {match_id} already exists without a "
            "readable replay_sha256; refusing to overwrite it."
        )
    if existing_sha256 != trajectory["replay_sha256"]:
        raise ReplayTrajectoryError(
            f"Destination for match_id {match_id} has a different replay_sha256; "
            "refusing to overwrite it."
        )
    raise ReplayTrajectoryError(
        f"Destination for match_id {match_id} has the same replay_sha256 but "
        "different canonical content; refusing to overwrite it."
    )


def _write_atomic(
    destination: Path,
    canonical: bytes,
    trajectory: ReplayTrajectory,
) -> bool:
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as file:
            file.write(canonical)
            file.flush()
            os.fsync(file.fileno())

        if _existing_artifact_is_identical(destination, canonical, trajectory):
            return False
        os.replace(temporary, destination)
        return True
    except ReplayTrajectoryError:
        raise
    except OSError as exc:
        raise ReplayTrajectoryError(
            "Could not atomically write the replay trajectory artifact."
        ) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _read_existing_sha256(content: bytes) -> str | None:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if type(value) is not dict:
        return None
    replay_sha256 = value.get("replay_sha256")
    if type(replay_sha256) is not str:
        return None
    if REPLAY_SHA256_PATTERN.fullmatch(replay_sha256) is None:
        return None
    return replay_sha256.lower()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayTrajectoryError(f"Duplicate JSON field: {key}.")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> object:
    raise ReplayTrajectoryError(f"Non-finite JSON number is not allowed: {value}.")


def _object(
    value: object,
    path: str,
    expected_fields: tuple[str, ...],
) -> dict[str, object]:
    if type(value) is not dict:
        raise ReplayTrajectoryError(f"{path} must be a JSON object.")
    result = cast(dict[str, object], value)
    expected = set(expected_fields)
    actual = set(result)
    missing = sorted(expected - actual)
    if missing:
        raise ReplayTrajectoryError(
            f"{path} is missing required field(s): {', '.join(missing)}."
        )
    unexpected = sorted(actual - expected)
    if unexpected:
        raise ReplayTrajectoryError(
            f"{path} contains unexpected field(s): {', '.join(unexpected)}."
        )
    return result


def _array(value: object, path: str) -> list[object]:
    if type(value) is not list:
        raise ReplayTrajectoryError(f"{path} must be a JSON array.")
    return cast(list[object], value)


def _string(value: object, path: str) -> str:
    if type(value) is not str:
        raise ReplayTrajectoryError(f"{path} must be a string.")
    return value


def _non_empty_string(value: object, path: str) -> str:
    result = _string(value, path)
    if not result.strip():
        raise ReplayTrajectoryError(f"{path} must be a non-empty string.")
    return result


def _nullable_string(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, path)


def _boolean(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise ReplayTrajectoryError(f"{path} must be a boolean.")
    return value


def _integer(value: object, path: str) -> int:
    if type(value) is not int:
        raise ReplayTrajectoryError(f"{path} must be an integer.")
    return value


def _non_negative_integer(value: object, path: str) -> int:
    result = _integer(value, path)
    if result < 0:
        raise ReplayTrajectoryError(f"{path} must be non-negative.")
    return result


def _positive_integer(value: object, path: str) -> int:
    result = _integer(value, path)
    if result <= 0:
        raise ReplayTrajectoryError(f"{path} must be positive.")
    return result


def _nullable_non_negative_integer(value: object, path: str) -> int | None:
    if value is None:
        return None
    return _non_negative_integer(value, path)


def _number(value: object, path: str) -> float:
    if type(value) not in (int, float):
        raise ReplayTrajectoryError(f"{path} must be numeric.")
    try:
        result = float(cast(int | float, value))
    except OverflowError as exc:
        raise ReplayTrajectoryError(f"{path} must be finite.") from exc
    if not math.isfinite(result):
        raise ReplayTrajectoryError(f"{path} must be finite.")
    return result


def _non_negative_number(value: object, path: str) -> float:
    result = _number(value, path)
    if result < 0.0:
        raise ReplayTrajectoryError(f"{path} must be non-negative.")
    return result


def _team_name(value: object, path: str) -> str:
    result = _string(value, path)
    if result not in TEAM_NAMES:
        raise ReplayTrajectoryError(f"{path} must be RADIANT or DIRE.")
    return result
