from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Literal, TypeAlias, cast

from app import live_state_signal as signal
from app import replay_state_history as history


IN_PROGRESS_STATE = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"
SAMPLING_INTERVAL_SECONDS = 60
IGNORE_REASON: Literal["game_not_in_progress"] = "game_not_in_progress"
OFF_BOUNDARY_REASON: Literal["not_sampling_boundary"] = "not_sampling_boundary"

PLAYER_METRICS = (
    "kills",
    "deaths",
    "assists",
    "last_hits",
    "denies",
    "gold",
    "net_worth",
)
TEAM_SPECS = (("team2", "radiant"), ("team3", "dire"))

Numeric: TypeAlias = history.Numeric
BuildStatus: TypeAlias = Literal["BUILT", "UNCHANGED"]
IgnoreReason: TypeAlias = Literal[
    "game_not_in_progress", "not_sampling_boundary"
]


class DotaGSILiveError(ValueError):
    """Raised when a sanitized Dota spectator GSI payload is invalid."""


@dataclass(frozen=True)
class NormalizedDotaGSIPayload:
    match_id: int
    snapshot: dict[str, Numeric]


@dataclass(frozen=True)
class IgnoredDotaGSIPayload:
    reason: IgnoreReason
    match_id: int | None
    game_time_seconds: Numeric


DotaGSINormalizationResult: TypeAlias = (
    NormalizedDotaGSIPayload | IgnoredDotaGSIPayload
)


@dataclass(frozen=True)
class IgnoredDotaGSILiveResult:
    status: BuildStatus
    response_status: Literal["ignored"]
    output_path: Path


DotaGSILiveResult: TypeAlias = signal.LiveStateSignalResult | IgnoredDotaGSILiveResult


def load_dota_gsi_payload(input_path: str | Path) -> dict[str, object]:
    path = Path(input_path)
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise DotaGSILiveError("Dota GSI payload JSON cannot be read.") from exc
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DotaGSILiveError(
            "Dota GSI payload is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise DotaGSILiveError("Dota GSI payload root must be an object.")
    if any(not isinstance(key, str) for key in payload):
        raise DotaGSILiveError("Dota GSI payload field names must be strings.")
    return cast(dict[str, object], payload)


def normalize_dota_gsi_payload(payload: object) -> DotaGSINormalizationResult:
    if not isinstance(payload, Mapping):
        raise DotaGSILiveError("Dota GSI payload root must be an object.")
    if any(not isinstance(key, str) for key in payload):
        raise DotaGSILiveError("Dota GSI payload field names must be strings.")
    if "auth" in payload:
        raise DotaGSILiveError(
            "Dota GSI payload must be sanitized and must not contain auth."
        )

    map_payload = _required_mapping(payload, "map", "Dota GSI payload")
    game_state = map_payload.get("game_state")
    if not isinstance(game_state, str):
        raise DotaGSILiveError("Dota GSI map.game_state must be a string.")
    game_time = _validated_numeric(
        map_payload.get("clock_time"),
        "Dota GSI map.clock_time",
    )
    unresolved_match_id = _optional_match_id(map_payload.get("matchid"))

    if game_state != IN_PROGRESS_STATE or game_time < 0:
        return IgnoredDotaGSIPayload(
            reason=IGNORE_REASON,
            match_id=unresolved_match_id,
            game_time_seconds=game_time,
        )

    match_id = _required_match_id(map_payload.get("matchid"))
    if game_time % SAMPLING_INTERVAL_SECONDS != 0:
        return IgnoredDotaGSIPayload(
            reason=OFF_BOUNDARY_REASON,
            match_id=match_id,
            game_time_seconds=game_time,
        )

    team_totals = _validate_and_total_teams(payload)
    radiant = team_totals["team2"]
    dire = team_totals["team3"]
    snapshot: dict[str, Numeric] = {
        "game_time_seconds": game_time,
        "kill_diff": radiant["kills"] - dire["kills"],
        "death_diff": radiant["deaths"] - dire["deaths"],
        "assist_diff": radiant["assists"] - dire["assists"],
        "last_hit_diff": radiant["last_hits"] - dire["last_hits"],
        "deny_diff": radiant["denies"] - dire["denies"],
        "net_worth_diff": radiant["net_worth"] - dire["net_worth"],
        "current_gold_diff": radiant["gold"] - dire["gold"],
        "total_xp_diff": radiant["xp"] - dire["xp"],
        "total_kills": radiant["kills"] + dire["kills"],
        "total_last_hits": radiant["last_hits"] + dire["last_hits"],
        "total_denies": radiant["denies"] + dire["denies"],
        "total_net_worth": radiant["net_worth"] + dire["net_worth"],
        "total_current_gold": radiant["gold"] + dire["gold"],
        "total_xp": radiant["xp"] + dire["xp"],
    }
    expected_fields = ("game_time_seconds", *history.CURRENT_FEATURE_COLUMNS)
    if tuple(snapshot) != expected_fields:
        raise DotaGSILiveError(
            "Normalized Dota GSI snapshot does not match the live state contract."
        )
    return NormalizedDotaGSIPayload(match_id=match_id, snapshot=snapshot)


def ingest_dota_gsi_payload(
    model_dir: str | Path,
    state_dir: str | Path,
    input_path: str | Path,
    output_path: str | Path,
) -> DotaGSILiveResult:
    normalized = normalize_dota_gsi_payload(load_dota_gsi_payload(input_path))
    destination = Path(output_path)
    if isinstance(normalized, IgnoredDotaGSIPayload):
        response = {
            "schema_version": signal.LIVE_STATE_SCHEMA_VERSION,
            "status": "ignored",
            "reason": normalized.reason,
            "match_id": normalized.match_id,
            "game_time_seconds": normalized.game_time_seconds,
        }
        status = signal.persist_live_state_signal_response(destination, response)
        return IgnoredDotaGSILiveResult(
            status=status,
            response_status="ignored",
            output_path=destination,
        )

    request = {
        "schema_version": signal.LIVE_STATE_SCHEMA_VERSION,
        "match_id": normalized.match_id,
        "snapshot": normalized.snapshot,
    }
    request_path: Path | None = None
    try:
        descriptor, request_name = tempfile.mkstemp(
            prefix=".dota-gsi-live-request.", suffix=".json"
        )
        request_path = Path(request_name)
        with os.fdopen(descriptor, "wb") as file:
            file.write(_render_json(request))
            file.flush()
            os.fsync(file.fileno())
        return signal.ingest_live_state_snapshot(
            model_dir,
            state_dir,
            request_path,
            destination,
        )
    except OSError as exc:
        raise DotaGSILiveError(
            "Normalized Dota GSI request could not be staged."
        ) from exc
    finally:
        if request_path is not None:
            try:
                request_path.unlink(missing_ok=True)
            except OSError:
                pass


def _validate_and_total_teams(
    payload: Mapping[object, object],
) -> dict[str, dict[str, Numeric]]:
    players = _required_mapping(payload, "player", "Dota GSI payload")
    heroes = _required_mapping(payload, "hero", "Dota GSI payload")
    required_teams = {team_key for team_key, _ in TEAM_SPECS}
    if set(players) != required_teams:
        raise DotaGSILiveError(
            "Dota GSI player mapping must contain exactly team2 and team3."
        )
    if set(heroes) != required_teams:
        raise DotaGSILiveError(
            "Dota GSI hero mapping must contain exactly team2 and team3."
        )

    totals: dict[str, dict[str, Numeric]] = {}
    for team_key, expected_team_name in TEAM_SPECS:
        player_team = _required_mapping(players, team_key, "Dota GSI player")
        hero_team = _required_mapping(heroes, team_key, "Dota GSI hero")
        if len(player_team) != 5:
            raise DotaGSILiveError(
                f"Dota GSI player.{team_key} must contain exactly five members."
            )
        if len(hero_team) != 5:
            raise DotaGSILiveError(
                f"Dota GSI hero.{team_key} must contain exactly five members."
            )
        if any(not isinstance(label, str) for label in player_team):
            raise DotaGSILiveError(
                f"Dota GSI player.{team_key} member labels must be strings."
            )
        if any(not isinstance(label, str) for label in hero_team):
            raise DotaGSILiveError(
                f"Dota GSI hero.{team_key} member labels must be strings."
            )
        if set(player_team) != set(hero_team):
            raise DotaGSILiveError(
                f"Dota GSI {team_key} player and hero member labels must match."
            )

        team_totals: dict[str, Numeric] = {
            metric: 0 for metric in (*PLAYER_METRICS, "xp")
        }
        for label in sorted(cast(Mapping[str, object], player_team)):
            player = _required_mapping(
                player_team,
                label,
                f"Dota GSI player.{team_key}",
            )
            hero = _required_mapping(
                hero_team,
                label,
                f"Dota GSI hero.{team_key}",
            )
            team_name = player.get("team_name")
            if team_name != expected_team_name:
                raise DotaGSILiveError(
                    f"Dota GSI player.{team_key}.{label}.team_name must be "
                    f"{expected_team_name}."
                )
            for metric in PLAYER_METRICS:
                value = _validated_cumulative(
                    player.get(metric),
                    f"Dota GSI player.{team_key}.{label}.{metric}",
                )
                team_totals[metric] += value
            xp = _validated_cumulative(
                hero.get("xp"),
                f"Dota GSI hero.{team_key}.{label}.xp",
            )
            team_totals["xp"] += xp
        totals[team_key] = team_totals
    return totals


def _required_mapping(
    payload: Mapping[object, object],
    field: str,
    label: str,
) -> Mapping[object, object]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        raise DotaGSILiveError(f"{label}.{field} must be an object.")
    return value


def _optional_match_id(value: object) -> int | None:
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        match_id = int(value)
        return match_id if match_id > 0 else None
    return None


def _required_match_id(value: object) -> int:
    match_id = _optional_match_id(value)
    if match_id is None:
        raise DotaGSILiveError(
            "Dota GSI map.matchid must be a positive integer or decimal digit string."
        )
    return match_id


def _validated_numeric(value: object, label: str) -> Numeric:
    if type(value) not in (int, float):
        raise DotaGSILiveError(f"{label} must be numeric.")
    numeric = cast(Numeric, value)
    if isinstance(numeric, float) and not math.isfinite(numeric):
        raise DotaGSILiveError(f"{label} must be finite.")
    return numeric


def _validated_cumulative(value: object, label: str) -> Numeric:
    numeric = _validated_numeric(value, label)
    if numeric < 0:
        raise DotaGSILiveError(f"{label} must be non-negative.")
    return numeric


def _render_json(payload: object) -> bytes:
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise DotaGSILiveError(
            "Normalized Dota GSI request could not be rendered."
        ) from exc
    return f"{text}\n".encode("utf-8")
