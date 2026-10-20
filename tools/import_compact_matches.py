"""Import four verified local compact replays into the historical match catalog."""

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

import psycopg

from app.history.domain import HistoricalMatch
from tools.import_historical_matches import apply_matches, validate_matches


ROOT = Path(__file__).resolve().parents[1]
COMPACTS = {
    8960991322: "tools/replay_probe/output/8960991322.compact.json",
    8960882635: "tools/replay_probe/output/independent-8960882635/8960882635.compact.json",
    8958734008: "tools/replay_probe/output/different-teams-8958734008/8958734008.compact.json",
    8957013760: "tools/replay_probe/output/fourth-match-8957013760/8957013760.compact.json",
}
METADATA = (
    "tools/replay_probe/output/different-teams-8958734008/selection.json",
    "tools/replay_probe/output/fourth-match-8957013760/selection.json",
)
# Verified local mapping in app/config/tournament_allowlist.yaml, not an inferred ID.
LEAGUE_ID = 19719
LEAGUE_NAME = "The International 2026"
SOURCE = "replay_clarity"
METADATA_INTEGERS = (
    "match_id",
    "leagueid",
    "start_time",
    "duration",
    "radiant_team_id",
    "dire_team_id",
)


def read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def positive_integer(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"Missing or invalid {field}.")
    return value


def read_metadata(root: Path) -> dict[int, dict]:
    result = {}
    for relative_path in METADATA:
        document = read_object(root / relative_path)
        for group in ("inspected", "candidates", "selected"):
            records = document.get(group, [])
            if group == "selected" and isinstance(records, dict):
                records = [records]
            if not isinstance(records, list):
                raise ValueError(f"Invalid metadata group: {group}.")
            for record in records:
                if not isinstance(record, dict):
                    continue
                if (
                    type(record.get("match_id")) is not int
                    or record["match_id"] not in COMPACTS
                ):
                    continue
                row = {
                    name: positive_integer(record.get(name), name)
                    for name in METADATA_INTEGERS
                }
                if type(record.get("radiant_win")) is not bool:
                    raise ValueError("Missing or invalid radiant_win.")
                row["radiant_win"] = record["radiant_win"]
                mid = row["match_id"]
                if mid in result and result[mid] != row:
                    raise ValueError(f"Conflicting local metadata for match {mid}.")
                result[mid] = row
    return result


def build_matches(
    root: Path, *, ingested_at: datetime | None = None
) -> list[HistoricalMatch]:
    metadata = read_metadata(root)
    observed_at = ingested_at if ingested_at is not None else datetime.now(timezone.utc)
    matches = []
    for mid, relative_path in COMPACTS.items():
        compact = read_object(root / relative_path)
        if positive_integer(compact.get("schema_version"), "schema_version") != 1:
            raise ValueError(f"Unsupported compact schema for match {mid}.")
        if positive_integer(compact.get("match_id"), "match_id") != mid:
            raise ValueError(f"Canonical file match ID mismatch: {mid}.")
        if mid not in metadata:
            raise ValueError(f"No local start_time metadata for match {mid}.")
        row = metadata[mid]
        league_id = positive_integer(compact.get("league_id"), "league_id")
        if league_id != LEAGUE_ID or row["leagueid"] != league_id:
            raise ValueError(f"League metadata mismatch for match {mid}.")
        names, ids = [], []
        for side in ("radiant", "dire"):
            team_id = positive_integer(
                compact.get(f"{side}_team_id"), f"{side}_team_id"
            )
            tag = compact.get(f"{side}_team_tag")
            if not isinstance(tag, str) or not tag.strip():
                raise ValueError(f"Missing {side} team label for match {mid}.")
            if row[f"{side}_team_id"] != team_id:
                raise ValueError(f"Team metadata mismatch for match {mid}.")
            names.append(tag)
            ids.append(str(team_id))
        if ids[0] == ids[1]:
            raise ValueError(f"Duplicate opposing team ID for match {mid}.")
        winner = positive_integer(compact.get("winner"), "winner")
        if winner not in (2, 3) or row["radiant_win"] != (winner == 2):
            raise ValueError(f"Winner metadata mismatch for match {mid}.")
        end = compact.get("game_end_time_seconds")
        if (
            compact.get("clock_normalization_status") != "PROVEN"
            or type(end) not in (int, float)
            or not math.isfinite(end)
            or end <= 0
        ):
            raise ValueError(f"Unverified completed game for match {mid}.")
        if math.floor(end) != row["duration"]:
            raise ValueError(f"Duration metadata mismatch for match {mid}.")
        winning_index = 0 if winner == 2 else 1
        matches.append(
            HistoricalMatch(
                id=f"{SOURCE}:{mid}",
                source=SOURCE,
                source_match_id=str(mid),
                started_at=datetime.fromtimestamp(row["start_time"], timezone.utc),
                ended_at=None,
                team_a_name=names[0],
                team_b_name=names[1],
                team_a_source_id=ids[0],
                team_b_source_id=ids[1],
                winner_name=names[winning_index],
                winner_source_id=ids[winning_index],
                winner_side="team_a" if winner == 2 else "team_b",
                tournament_name=LEAGUE_NAME,
                league_name=LEAGUE_NAME,
                league_source_id=str(league_id),
                status="completed",
                ingested_at=observed_at,
            )
        )
    validate_matches(matches)
    return matches


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Import all four records in one PostgreSQL transaction.",
    )
    args = parser.parse_args(argv)
    try:
        matches = build_matches(ROOT)
        if args.apply:
            inserted = apply_matches(matches)
            print(
                f"APPLY: source={len(matches)}, inserted={inserted}, skipped={len(matches) - inserted}."
            )
        else:
            print(
                f"DRY RUN: {len(matches)} historical matches ready; PostgreSQL was not contacted."
            )
        print(f"source={SOURCE}; start dates come from local selection.json records.")
        print("Team A=Radiant, Team B=Dire; team labels are original replay tags.")
        for match in matches:
            print(
                f"{match.source_match_id}: {match.team_a_name} / {match.team_b_name}; "
                f"started_at={match.started_at.isoformat()}; winner={match.winner_name}; "
                f"league_id={match.league_source_id}"
            )
        print(
            "NULL: ended_at, tournament_source_id, series_name, series_source_id, raw_stage_label, best_of."
        )
        print(
            "Stage/round: unknown. ingested_at is the current import-attempt timestamp."
        )
        return 0
    except (OSError, ValueError, OverflowError, psycopg.Error) as exc:
        print(f"Compact import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
