"""Extract explicit numeric features from historical snapshot timeline views.

FEATURES_BY_VIEW is the predictor allowlist. All other CSV columns are metadata,
including source/feature statuses and observation ages. Oracle and Team POV
features occupy disjoint columns. Inventory counts describe exported entries,
not unique item types, purchases or changes. No LIVE availability is implied.
Missing components invalidate an aggregate; they are never treated as zero.
"""

import argparse
from collections.abc import Sequence
import csv
from decimal import Decimal, InvalidOperation, localcontext
import json
import os
from pathlib import Path
import sys
import tempfile

from app.analytics.historical_snapshot import (
    HistoricalObservation, HistoricalSnapshot, MatchIdentity, _elapsed_seconds, _sha256,
)
from app.analytics.snapshot_views import (
    CHANNEL_ORIGINS, ECONOMY_FIELDS, ENEMY_FIELDS, HERO_FIELDS, POV_FIELDS,
    SPATIAL_FIELDS, SPATIAL_REQUIRED, STRING_FIELDS, VIEWS, project_snapshot,
)
from app.replay_trajectory import _reject_non_finite_constant, _unique_json_object


TEAMS = ("RADIANT", "DIRE")
GLOBAL_FEATURES = (
    "radiant_net_worth", "dire_net_worth", "net_worth_lead_radiant_minus_dire",
    "radiant_total_xp", "dire_total_xp", "xp_lead_radiant_minus_dire",
    "radiant_level_sum", "dire_level_sum", "radiant_last_hits_sum", "dire_last_hits_sum",
    "radiant_item_entry_count", "dire_item_entry_count",
    "radiant_observed_global_position_count", "dire_observed_global_position_count",
)
POV_FEATURES = (
    "own_team_spread", "visible_enemy_count", "visible_enemy_with_position_count", "hidden_enemy_count",
    "nearest_visible_enemy_distance", "enemy_last_seen_position_count", "max_enemy_last_seen_age_seconds",
)
FEATURES = (*GLOBAL_FEATURES, *POV_FEATURES)
FEATURES_BY_VIEW = {view: POV_FEATURES if team else GLOBAL_FEATURES for view, team in VIEWS.items()}
ENVELOPE = ("origin", "status", "actual_game_time_seconds", "replay_tick", "age_seconds", "values")
METADATA = (
    "source", "source_match_id", "replay_sha256", "requested_game_time_seconds", "requested_replay_tick",
    "view", "provenance", "source_statuses",
    *(f"{channel}_{field}" for channel in CHANNEL_ORIGINS for field in ENVELOPE if field != "values"),
    *(f"feature_status_{feature}" for feature in FEATURES),
)
COLUMNS = (*METADATA, *FEATURES)


def _numeric(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Expected an exact finite number or NULL.")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Expected an exact finite number or NULL.") from error
    if not number.is_finite():
        raise ValueError("Expected an exact finite number or NULL.")
    return value if isinstance(value, int) else number


def _integer(value, *, minimum=0):
    number = _numeric(value)
    if number is None or number < minimum or number != int(number):
        raise ValueError("Expected a nonnegative integer identity/count.")
    return int(number)


def _fields(data, allowed, *, required=None):
    required = allowed if required is None else required
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        raise ValueError("Payload contradicts the explicit field allowlist.")
    return {key: value if key in STRING_FIELDS or key.endswith("_status") or key == "match_id" else _numeric(value)
            for key, value in data.items()}


def _decode_payload(channel, values, team):
    if values is None:
        return None
    if channel == "economy":
        return _fields(values, ECONOMY_FIELDS)
    if channel in ("heroes", "spatial_global"):
        allowed = {"players"} | ({"replay_sha256"} if channel == "spatial_global" else set())
        if not isinstance(values, dict) or set(values) - allowed or not isinstance(values.get("players"), list):
            raise ValueError("Expected an explicit hero/position player list.")
        players = []
        for player in values["players"]:
            if channel == "spatial_global":
                players.append(_fields(player, SPATIAL_FIELDS, required=SPATIAL_REQUIRED))
            else:
                if not isinstance(player, dict) or "items" not in player:
                    raise ValueError("Hero inventory must be explicit, including NULL.")
                copied = _fields({key: value for key, value in player.items() if key != "items"}, HERO_FIELDS)
                items = player["items"]
                if items is not None and not isinstance(items, list):
                    raise ValueError("Hero inventory must be a list or NULL.")
                copied["items"] = None if items is None else [_fields(item, ("inventory_slot", "item_name")) for item in items]
                players.append(copied)
        return {key: value for key, value in values.items() if key != "players"} | {"players": players}
    if not isinstance(values, dict) or set(values) != ({team} if team else set(TEAMS)):
        raise ValueError("Team payload contains the wrong observer or an opposite-team observation.")
    if channel == "pov":
        return {own: _fields(features, POV_FIELDS) for own, features in values.items()}
    if any(not isinstance(enemies, list) for enemies in values.values()):
        raise ValueError("Enemy observations must be lists.")
    return {own: [_fields(enemy, ENEMY_FIELDS) for enemy in enemies] for own, enemies in values.items()}


def _validated(record):
    expected = {"match", "replay_sha256", "requested_game_time_seconds", "requested_replay_tick",
                "snapshot_kind", "view", "observations"}
    if not isinstance(record, dict) or set(record) != expected or record["view"] not in VIEWS:
        raise ValueError("Expected the exact historical timeline contract and a known view.")
    if not isinstance(record["match"], dict) or set(record["match"]) != {"source", "source_match_id"}:
        raise ValueError("Missing explicit match identity.")
    match = MatchIdentity(**record["match"])
    sha = _sha256(record["replay_sha256"])
    time = _numeric(record["requested_game_time_seconds"])
    if time is None:
        raise ValueError("Requested time cannot be NULL.")
    time, tick = Decimal(time), _integer(record["requested_replay_tick"])
    view, observations = record["view"], record["observations"]
    team = VIEWS[view]
    allowed = {"pov", "pov_enemy_positions"} if team else set(CHANNEL_ORIGINS)
    if not isinstance(observations, dict) or set(observations) - allowed:
        raise ValueError("Observation channels violate the requested information scope.")
    typed = {}
    for channel, observation in observations.items():
        if not isinstance(observation, dict) or set(observation) != set(ENVELOPE):
            raise ValueError("Invalid observation envelope.")
        fields = dict(observation)
        for field in ("actual_game_time_seconds", "age_seconds"):
            number = _numeric(fields[field])
            fields[field] = Decimal(number) if number is not None else None
        fields["replay_tick"] = None if fields["replay_tick"] is None else _integer(fields["replay_tick"])
        fields["values"] = _decode_payload(channel, fields["values"], team)
        typed[channel] = HistoricalObservation(**fields)
    snapshot = HistoricalSnapshot(match, time, typed, record["snapshot_kind"])
    # Reuse existing provenance, as-of, last-seen and safe-payload validation.
    return project_snapshot(snapshot, view, requested_replay_tick=tick) | {"replay_sha256": sha}


def _sum(values):
    if any(value is None for value in values):
        return None
    numbers = [Decimal(value) for value in values]
    with localcontext() as context:
        context.prec = max(v.adjusted() for v in numbers) - min(v.as_tuple().exponent for v in numbers) + len(str(len(numbers))) + 2
        return sum(numbers, Decimal(0))


def _roster(players, record):
    if len(players) != 10:
        raise ValueError("Global aggregates require a complete ten-hero roster.")
    for field, minimum in (("player_slot", 0), ("hero_id", 1)):
        if len({_integer(player[field], minimum=minimum) for player in players}) != 10:
            raise ValueError("Global hero identities must be unique.")
    if any(sum(player["team"] == team for player in players) != 5 for team in TEAMS):
        raise ValueError("Global roster must contain five Radiant and five Dire heroes.")
    if any((p["source"], p["source_match_id"]) != (record["match"]["source"], record["match"]["source_match_id"]) for p in players):
        raise ValueError("Global player belongs to another match.")
    return {team: [player for player in players if player["team"] == team] for team in TEAMS}


def extract_record(raw: dict) -> dict:
    record = _validated(raw)
    view, observations = record["view"], record["observations"]
    row = dict.fromkeys(COLUMNS)
    row.update(record["match"], replay_sha256=record["replay_sha256"], view=view,
               requested_game_time_seconds=record["requested_game_time_seconds"],
               requested_replay_tick=record["requested_replay_tick"],
               provenance="HISTORICAL_TEAM_POV" if VIEWS[view] else "HISTORICAL_ORACLE")
    for feature in FEATURES:
        row[f"feature_status_{feature}"] = "MISSING_VALUE" if feature in FEATURES_BY_VIEW[view] else "NOT_IN_VIEW"
    for channel in CHANNEL_ORIGINS:
        observation = observations.get(channel)
        for field in ENVELOPE[:-1]:
            row[f"{channel}_{field}"] = observation[field] if observation else ("CHANNEL_ABSENT" if field == "status" else None)
    source_statuses = {}

    def payload(channel, features):
        observation = observations.get(channel)
        status = observation["status"] if observation else "CHANNEL_ABSENT"
        if status != "AVAILABLE":
            for feature in features:
                row[f"feature_status_{feature}"] = status
            return None
        return observation["values"]

    def put(feature, value, missing="MISSING_COMPONENT"):
        row[feature] = value
        row[f"feature_status_{feature}"] = "AVAILABLE" if value is not None else missing

    team = VIEWS[view]
    if team is None:
        economy = payload("economy", GLOBAL_FEATURES[:6])
        if economy is not None:
            for field in ("radiant_net_worth", "dire_net_worth", "radiant_total_xp", "dire_total_xp"):
                value = economy[field]
                if value is not None and value < 0:
                    raise ValueError("Economic totals cannot be negative.")
                put(field, value, "MISSING_VALUE")
            for radiant, dire, lead in (("radiant_net_worth", "dire_net_worth", GLOBAL_FEATURES[2]),
                                        ("radiant_total_xp", "dire_total_xp", GLOBAL_FEATURES[5])):
                put(lead, _elapsed_seconds(Decimal(economy[radiant]), Decimal(economy[dire]))
                    if economy[radiant] is not None and economy[dire] is not None else None)
        heroes = payload("heroes", GLOBAL_FEATURES[6:12])
        if heroes is not None:
            for own, players in _roster(heroes["players"], record).items():
                for field in ("level", "last_hits"):
                    values = [None if p[field] is None else _integer(p[field]) for p in players]
                    put(f"{own.lower()}_{'level' if field == 'level' else 'last_hits'}_sum", _sum(values))
                for player in players:
                    if player["items"] is not None:
                        slots = [_integer(item["inventory_slot"]) for item in player["items"]]
                        if len(slots) != len(set(slots)):
                            raise ValueError("Duplicate inventory slot in one hero snapshot.")
                put(f"{own.lower()}_item_entry_count", None if any(p["items"] is None for p in players)
                    else sum(len(p["items"]) for p in players))
        spatial = payload("spatial_global", GLOBAL_FEATURES[12:])
        if spatial is not None:
            if "replay_sha256" in spatial and _sha256(spatial["replay_sha256"]) != record["replay_sha256"]:
                raise ValueError("Spatial replay SHA differs from the timeline identity.")
            for own, players in _roster(spatial["players"], record).items():
                for player in players:
                    x, y, status, life = (player[field] for field in ("x", "y", "position_status", "life_state"))
                    if (x is None) != (y is None) or (status == "OBSERVED") != (x is not None) or (x is not None and life != 0):
                        raise ValueError("Inconsistent GLOBAL position/life status.")
                    if life is not None:
                        _integer(life)
                    if status == "HERO_NOT_ALIVE" and life in (None, 0):
                        raise ValueError("HERO_NOT_ALIVE requires a nonzero life state.")
                put(f"{own.lower()}_observed_global_position_count", sum(p["position_status"] == "OBSERVED" for p in players))
            source_statuses["spatial_global"] = [{"player_slot": p["player_slot"], "position_status": p["position_status"]}
                                                  for p in spatial["players"]]
    else:
        pov = payload("pov", POV_FEATURES[:5])
        if pov is not None:
            for feature in POV_FEATURES[:5]:
                value = pov[team][feature]
                if value is not None and (value < 0 or (feature.endswith("_count") and _integer(value) > 5)):
                    raise ValueError("Invalid Team POV count/distance.")
                put(feature, value, "MISSING_VALUE")
            source_statuses["pov"] = {field: pov[team][field] for field in ("own_team_spread_status", "nearest_visible_enemy_status")}
        enemies = payload("pov_enemy_positions", POV_FEATURES[5:])
        if enemies is not None:
            if any(_sha256(enemy["replay_sha256"]) != record["replay_sha256"] for enemy in enemies[team]):
                raise ValueError("Enemy observation replay SHA differs from the timeline identity.")
            seen = [enemy for enemy in enemies[team] if enemy["last_seen_status"] in ("LAST_SEEN", "OBSERVED_NOW")]
            put("enemy_last_seen_position_count", len(seen))
            put("max_enemy_last_seen_age_seconds", max((e["last_seen_age_at_requested_time_seconds"] for e in seen), default=None),
                "NO_CONFIRMED_LAST_SEEN")
            source_statuses["pov_enemy_positions"] = [{field: enemy[field] for field in (
                "enemy_player_slot", "visibility_state", "current_position_status", "last_seen_status",
            )} for enemy in enemies[team]]
    row["source_statuses"] = json.dumps(source_statuses, sort_keys=True, separators=(",", ":"))
    return row


def _coverage(rows):
    return {view: dict(rows=sum(row["view"] == view for row in rows), features={feature: dict(
        filled=sum(row["view"] == view and row[feature] is not None for row in rows),
        null=sum(row["view"] == view and row[feature] is None for row in rows),
    ) for feature in FEATURES}) for view in VIEWS}


def extract_features(timeline: Path, out: Path | None = None) -> dict:
    if out is not None and out.resolve() == timeline.resolve():
        raise ValueError("Output must not overwrite the source timeline.")
    rows, identities, lineages, replay_matches = [], set(), {}, {}
    with timeline.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line, parse_float=Decimal, parse_constant=_reject_non_finite_constant,
                                 object_pairs_hook=_unique_json_object)
                row = extract_record(raw)
                match = (row["source"], row["source_match_id"])
                sha = row["replay_sha256"]
                if lineages.setdefault(match, sha) != sha or replay_matches.setdefault(sha, match) != match:
                    raise ValueError("Conflicting match/replay identity.")
                identity = (*match, row["requested_game_time_seconds"], row["requested_replay_tick"], row["view"])
                if identity in identities:
                    raise ValueError("Duplicate match/time/tick/view identity.")
                identities.add(identity)
                rows.append(row)
            except (ValueError, TypeError, KeyError, InvalidOperation) as error:
                raise ValueError(f"{timeline}:{line_number}: {error}") from error
    rows.sort(key=lambda row: (row["source"], row["source_match_id"], row["requested_game_time_seconds"],
                               row["requested_replay_tick"], tuple(VIEWS).index(row["view"])))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=out.parent,
                                             prefix=f".{out.name}.", delete=False) as stream:
                temporary = Path(stream.name)
                writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            os.replace(temporary, out)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return dict(rows=len(rows), mode="EXPORTED" if out is not None else "DRY_RUN", coverage=_coverage(rows),
                features_by_view=FEATURES_BY_VIEW, metadata_columns=METADATA,
                matches={f"{source}/{mid}": _coverage([row for row in rows if (row["source"], row["source_match_id"]) == (source, mid)])
                         for source, mid in sorted(lineages)})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="Explicit CSV destination; omission performs a read-only dry-run.")
    args = parser.parse_args(argv)
    try:
        report = extract_features(args.timeline, args.out)
        print(f"{report['mode']}: rows={report['rows']}; matches={len(report['matches'])}; out={args.out}")
        print("Numeric predictors use FEATURES_BY_VIEW only; all other columns are metadata.")
        print("Coverage cells are filled/NULL; GLOBAL | RADIANT_POV | DIRE_POV.")
        for label, coverage in (("ALL", report["coverage"]), *report["matches"].items()):
            print(f"\n{label}; rows by view: " + ", ".join(str(coverage[view]["rows"]) for view in VIEWS))
            for feature in FEATURES:
                print(feature + " | " + " | ".join(f"{coverage[view]['features'][feature]['filled']}/{coverage[view]['features'][feature]['null']}" for view in VIEWS))
    except (OSError, ValueError, TypeError, KeyError, InvalidOperation) as error:
        print(f"Snapshot feature extraction failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
