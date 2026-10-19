"""Death-only future labels for existing team POV rows; no feature computation.

FEATURE_COLUMNS is an explicit input whitelist, copied unchanged. Everything
appended is a future label or label-audit metadata. Windows are (t, t+h], using
Decimal over saved timestamps, without rounding. Event/delivery disagreement
at either boundary leaves all targets for that horizon missing. In particular,
an already-delivered death is never an unknown future target. CENSORED takes
precedence when t+h exceeds the confirmed match end; ambiguity audit is retained.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
import csv
import json
from pathlib import Path

from app.replay_pov_alignment import TEAMS, _features, _integer, _lineage, _number
from app.replay_pov_features import TEAM_COLUMNS, _header, _players


FEATURE_COLUMNS = TEAM_COLUMNS
HORIZONS = (30, 60)
LABEL_COLUMNS = tuple(name for h in HORIZONS for name in (
    f"own_deaths_next_{h}s", f"enemy_deaths_next_{h}s", f"any_death_next_{h}s",
    f"label_status_{h}s", f"label_window_end_{h}s",
    f"label_ambiguous_event_count_{h}s", f"label_ambiguous_events_{h}s",
))


def _time(value) -> Decimal:
    _number(value)  # Reject booleans/nonfinite/negative values before comparison.
    return Decimal(str(value))


@dataclass(frozen=True)
class _Death:
    id: str
    slot: int
    team: str
    time: Decimal
    delivery_time: Decimal
    delivery_tick: int


def _validated_events(data: Mapping) -> tuple[list[_Death], dict, dict]:
    if (data.get("schema_version") != 1
            or data.get("artifact_kind") != "replay_primary_hero_deaths"
            or data.get("clock_normalization_status") != "PROVEN"
            or data.get("rejected_hero_deaths")):
        raise ValueError("Expected verified, complete primary hero-death data")
    _lineage(data)
    end = _time(data["game_end_normalized_time_seconds"])
    _integer(data["game_end_replay_tick"])
    roster = {}
    for player in data["eof_players"]:
        slot = _integer(player["player_slot"])
        if (slot in roster or player["team"] not in TEAMS
                or player.get("primary_identity_verified") is not True
                or player.get("active_clone_ambiguity") is not False):
            raise ValueError("Unverified or duplicate EOF player identity")
        roster[slot] = player
    if len(roster) != 10 or Counter(p["team"] for p in roster.values()) != {t: 5 for t in TEAMS}:
        raise ValueError("Expected ten primary players, five per team")
    result, seen = [], {}
    reincarnations = duplicates = 0
    for event in data["hero_deaths"]:
        slot = _integer(event["player_slot"])
        player = roster.get(slot)
        if (player is None or event["victim_team"] != player["team"]
                or event["hero_id"] != player["hero_id"]
                or event["victim_unit_name"] != player["unit_name"]
                or event.get("identity_status") != "UNIQUE_FOOTER_PLAYER_TEAM_SELECTED_PRIMARY_NONCLONE"
                or event.get("combat_log_target_hero") is not True
                or event.get("combat_log_target_illusion") is not False
                or event.get("timestamp_semantics_status") != "ENGINE_TIMESTAMP_MINUS_GAME_START_CORROBORATED_AT_DELIVERY"
                or event.get("frame_clock_proven") is not True
                or event.get("frame_synthetic") is not False
                or type(event.get("will_reincarnate")) is not bool):
            raise ValueError("Unverified death identity, timestamp, or reincarnation flag")
        when = _time(event["event_game_time_seconds"])
        delivered = _time(event["frame_game_time_seconds"])
        tick = _integer(event["replay_tick"])
        if tick != _integer(event["frame_replay_tick"]) or when > end:
            raise ValueError("Invalid death delivery tick or event past match end")
        key = (slot, when)
        if key in seen:
            if seen[key] != event:
                raise ValueError("Conflicting duplicate death event")
            duplicates += 1
            continue
        seen[key] = dict(event)
        if event["will_reincarnate"]:
            reincarnations += 1
            continue
        result.append(_Death(f"{slot}@{when}", slot, player["team"], when, delivered, tick))
    per_player = Counter(e.slot for e in result)
    for slot, player in roster.items():
        if per_player[slot] != _integer(player["deaths"]):
            raise ValueError(f"Death events disagree with EOF counter for player {slot}")
    audit = {"source_deaths": dict(Counter(e.team for e in result)),
             "reincarnations_excluded": reincarnations, "duplicates_excluded": duplicates}
    return result, roster, audit


def build_future_death_rows(
    pov_rows: Sequence[Mapping], death_data: Mapping, visibility: Mapping,
) -> tuple[list[dict], dict]:
    events, roster, audit = _validated_events(death_data)
    _header(visibility, "replay_hero_visibility")
    lineage = _lineage(death_data)
    if _lineage(visibility) != lineage:
        raise ValueError("POV sidecar match/replay mismatch")
    for key in ("clock_normalization_method", "zero_replay_tick", "pause_ticks", "pause_witnessed"):
        if visibility[key] != death_data[key]:
            raise ValueError(f"Clock metadata mismatch: {key}")
    end = _time(death_data["game_end_normalized_time_seconds"])
    if _time(visibility["game_end_time_seconds"]) != end:
        raise ValueError("Confirmed match ends disagree")
    expected = set()
    previous_time, previous_tick = Decimal(-1), -1
    for frame in visibility["snapshots"]:
        time, tick = _time(frame["game_time_seconds"]), _integer(frame["replay_tick"])
        if (time <= previous_time or tick <= previous_tick or time > end
                or _time(frame["source_game_time_seconds"]) != time
                or frame.get("synthetic") is True or frame.get("game_paused") is True):
            raise ValueError("Invalid actual POV frame time/tick")
        players = _players(frame)
        if players.keys() != roster.keys() or any(
            (p["team"], p["hero_id"]) != (roster[slot]["team"], roster[slot]["hero_id"])
            for slot, p in players.items()
        ):
            raise ValueError("Sidecar primary roster differs from death roster")
        expected.update((time, tick, team) for team in TEAMS)
        previous_time, previous_tick = time, tick
    result, seen = [], set()
    for pov in pov_rows:
        time, tick, team = _time(pov["game_time_seconds"]), _integer(pov["replay_tick"]), pov["pov_team"]
        key = (time, tick, team)
        if _lineage(pov) != lineage or key not in expected or key in seen:
            raise ValueError("POV identity/time/tick mismatch or duplicate row")
        seen.add(key)
        _features(pov)  # Validation only: preserve original feature cell values.
        row = {name: pov[name] for name in FEATURE_COLUMNS}
        for h in HORIZONS:
            upper = time + h
            conflicts, future = [], []
            for event in events:
                reasons = []
                if (event.time > time) != (event.delivery_tick > tick):
                    reasons.append("LEFT_EVENT_VS_DELIVERY_TICK")
                if (event.delivery_time > time) != (event.delivery_tick > tick):
                    reasons.append("LEFT_DELIVERY_CLOCK_CONFLICT")
                if (event.time <= upper) != (event.delivery_time <= upper):
                    reasons.append("RIGHT_EVENT_VS_DELIVERY_TIME")
                if reasons:
                    conflicts.append({"event_id": event.id, "reasons": reasons})
                if time < event.time <= upper and event.delivery_tick > tick:
                    future.append(event)
            status = ("CENSORED" if upper > end else
                      "AMBIGUOUS_TIME_BOUNDARY" if conflicts else "VALID")
            own = sum(e.team == team for e in future) if status == "VALID" else None
            enemy = sum(e.team != team for e in future) if status == "VALID" else None
            row.update({
                f"own_deaths_next_{h}s": own, f"enemy_deaths_next_{h}s": enemy,
                f"any_death_next_{h}s": int(own + enemy > 0) if status == "VALID" else None,
                f"label_status_{h}s": status, f"label_window_end_{h}s": str(upper),
                f"label_ambiguous_event_count_{h}s": len(conflicts),
                f"label_ambiguous_events_{h}s": json.dumps(conflicts, separators=(",", ":")),
            })
        result.append(row)
    if seen != expected:
        raise ValueError("Missing POV team/frame rows")
    return result, audit


def write_future_deaths(pov_path: Path, deaths_path: Path, visibility_path: Path, output: Path):
    if output.resolve() in {p.resolve() for p in (pov_path, deaths_path, visibility_path)}:
        raise ValueError("Output must not overwrite an input")
    with pov_path.open(encoding="utf-8", newline="") as stream:
        pov = list(csv.DictReader(stream))
    deaths = json.loads(deaths_path.read_text(encoding="utf-8"))
    visibility = json.loads(visibility_path.read_text(encoding="utf-8"))
    rows, audit = build_future_death_rows(pov, deaths, visibility)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FEATURE_COLUMNS + LABEL_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return rows, audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pov", "deaths", "visibility", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        rows, audit = write_future_deaths(args.pov, args.deaths, args.visibility, args.output)
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(f"WROTE rows={len(rows)} source_audit={audit} output={args.output}")
    for h in HORIZONS:
        counts = dict(Counter(row[f"label_status_{h}s"] for row in rows))
        positives = sum(row[f"any_death_next_{h}s"] == 1 for row in rows)
        print(f"horizon={h} statuses={counts} positive_rows={positives}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
