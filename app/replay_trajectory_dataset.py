from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from typing import Literal, assert_never

from app.replay_trajectory import (
    AGGREGATE_FIELDS,
    SUPPORTED_SCHEMA_VERSION,
    AggregateField,
    ReplaySnapshot,
    ReplayTeam,
    ReplayTrajectory,
    ReplayTrajectoryError,
    load_replay_trajectory,
)


DATASET_SCHEMA_VERSION = 1
HORIZONS_SECONDS = (120, 180, 300)
DECISION_FILENAME = "team_decisions.csv"
OUTCOME_FILENAME = "team_outcomes.csv"
MANIFEST_FILENAME = "manifest.json"
OUTPUT_FILENAMES = (
    DECISION_FILENAME,
    OUTCOME_FILENAME,
    MANIFEST_FILENAME,
)

# This tuple is the documented serialization order. It intentionally contains
# only source lineage and state available at the decision timestamp.
DECISION_COLUMNS = (
    "match_id",
    "replay_sha256",
    "game_mode",
    "league_id",
    "game_time_seconds",
    "team",
    "team_id",
    "team_tag",
    "opponent_team",
    "opponent_team_id",
    "opponent_team_tag",
    "team_kills",
    "team_deaths",
    "team_assists",
    "team_last_hits",
    "team_denies",
    "team_net_worth",
    "team_current_gold",
    "team_total_xp",
    "opponent_kills",
    "opponent_deaths",
    "opponent_assists",
    "opponent_last_hits",
    "opponent_denies",
    "opponent_net_worth",
    "opponent_current_gold",
    "opponent_total_xp",
    "kill_diff",
    "death_diff",
    "assist_diff",
    "last_hit_diff",
    "deny_diff",
    "net_worth_diff",
    "current_gold_diff",
    "total_xp_diff",
)

# Outcomes are serialized by decision key, exact horizon, future state, change,
# and finally the outcome-only winner label.
OUTCOME_COLUMNS = (
    "match_id",
    "game_time_seconds",
    "team",
    "horizon_seconds",
    "future_game_time_seconds",
    "future_team_kills",
    "future_team_deaths",
    "future_team_assists",
    "future_team_last_hits",
    "future_team_denies",
    "future_team_net_worth",
    "future_team_current_gold",
    "future_team_total_xp",
    "future_opponent_kills",
    "future_opponent_deaths",
    "future_opponent_assists",
    "future_opponent_last_hits",
    "future_opponent_denies",
    "future_opponent_net_worth",
    "future_opponent_current_gold",
    "future_opponent_total_xp",
    "future_kill_diff",
    "future_death_diff",
    "future_assist_diff",
    "future_last_hit_diff",
    "future_deny_diff",
    "future_net_worth_diff",
    "future_current_gold_diff",
    "future_total_xp_diff",
    "kill_diff_change",
    "death_diff_change",
    "assist_diff_change",
    "last_hit_diff_change",
    "deny_diff_change",
    "net_worth_diff_change",
    "current_gold_diff_change",
    "total_xp_diff_change",
    "team_won",
)

DIFF_COLUMNS: tuple[tuple[AggregateField, str], ...] = (
    ("kills", "kill"),
    ("deaths", "death"),
    ("assists", "assist"),
    ("last_hits", "last_hit"),
    ("denies", "deny"),
    ("net_worth", "net_worth"),
    ("current_gold", "current_gold"),
    ("total_xp", "total_xp"),
)
TEAM_ORIENTATIONS: tuple[TeamName, ...] = ("RADIANT", "DIRE")
DECISION_LEAKAGE_COLUMNS = {
    "winner",
    "team_won",
    "horizon_seconds",
    "game_end_time_seconds",
    "remaining_game_time",
    "last_snapshot_indicator",
}

TeamName = Literal["RADIANT", "DIRE"]
CsvScalar = str | int | bool | None
CsvRow = dict[str, CsvScalar]
DecisionKey = tuple[int, int, str]
OutcomeKey = tuple[int, int, str, int, int]


class ReplayTrajectoryDatasetError(ValueError):
    """Raised when a replay trajectory dataset cannot be built safely."""


@dataclass(frozen=True)
class ReplayTrajectoryDatasetBuildResult:
    status: Literal["BUILT", "UNCHANGED"]
    output_dir: Path
    match_count: int
    snapshot_count: int
    decision_row_count: int
    outcome_row_count: int
    outcome_rows_by_horizon: dict[int, int]
    decision_sha256: str
    outcome_sha256: str
    manifest_sha256: str


@dataclass(frozen=True)
class _SourceArtifact:
    path: Path
    trajectory: ReplayTrajectory


@dataclass(frozen=True)
class _DatasetRows:
    decisions: list[CsvRow]
    outcomes: list[CsvRow]
    snapshot_count: int
    outcome_rows_by_horizon: dict[int, int]


def build_replay_trajectory_dataset(
    input_dir: str | Path,
    output_dir: str | Path,
) -> ReplayTrajectoryDatasetBuildResult:
    sources = _load_sources(Path(input_dir))
    rows = _construct_rows(sources)
    decision_bytes = _render_csv(DECISION_COLUMNS, rows.decisions)
    outcome_bytes = _render_csv(OUTCOME_COLUMNS, rows.outcomes)
    decision_sha256 = _sha256(decision_bytes)
    outcome_sha256 = _sha256(outcome_bytes)
    manifest_bytes = _render_manifest(
        sources,
        rows,
        decision_sha256=decision_sha256,
        outcome_sha256=outcome_sha256,
    )
    outputs = {
        DECISION_FILENAME: decision_bytes,
        OUTCOME_FILENAME: outcome_bytes,
        MANIFEST_FILENAME: manifest_bytes,
    }

    destination = Path(output_dir)
    if _existing_output_set_is_identical(destination, outputs):
        status: Literal["BUILT", "UNCHANGED"] = "UNCHANGED"
    else:
        _replace_output_set(destination, outputs)
        status = "BUILT"

    return ReplayTrajectoryDatasetBuildResult(
        status=status,
        output_dir=destination,
        match_count=len(sources),
        snapshot_count=rows.snapshot_count,
        decision_row_count=len(rows.decisions),
        outcome_row_count=len(rows.outcomes),
        outcome_rows_by_horizon=dict(rows.outcome_rows_by_horizon),
        decision_sha256=decision_sha256,
        outcome_sha256=outcome_sha256,
        manifest_sha256=_sha256(manifest_bytes),
    )


def _load_sources(input_dir: Path) -> list[_SourceArtifact]:
    if not input_dir.exists():
        raise ReplayTrajectoryDatasetError(
            f"Input directory does not exist: {input_dir.as_posix()}."
        )
    if not input_dir.is_dir():
        raise ReplayTrajectoryDatasetError(
            f"Input path is not a directory: {input_dir.as_posix()}."
        )

    try:
        source_paths = sorted(
            (
                path
                for path in input_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".json"
            ),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as exc:
        raise ReplayTrajectoryDatasetError(
            "Cannot scan the replay trajectory input directory."
        ) from exc

    if not source_paths:
        raise ReplayTrajectoryDatasetError(
            "Input directory contains no replay trajectory JSON files."
        )

    sources: list[_SourceArtifact] = []
    for path in source_paths:
        try:
            trajectory = load_replay_trajectory(path)
        except ReplayTrajectoryError as exc:
            raise ReplayTrajectoryDatasetError(
                f"Invalid source artifact {path.name}: {exc}"
            ) from exc
        sources.append(_SourceArtifact(path=path, trajectory=trajectory))

    paths_by_match_id: dict[int, list[str]] = {}
    for source in sources:
        match_id = source.trajectory["match_id"]
        paths_by_match_id.setdefault(match_id, []).append(source.path.name)
    duplicate_ids = sorted(
        match_id
        for match_id, names in paths_by_match_id.items()
        if len(names) > 1
    )
    if duplicate_ids:
        match_id = duplicate_ids[0]
        names = ", ".join(sorted(paths_by_match_id[match_id]))
        raise ReplayTrajectoryDatasetError(
            f"Duplicate match_id {match_id} in source artifacts: {names}."
        )

    for source in sources:
        expected_name = f"{source.trajectory['match_id']}.json"
        if source.path.name != expected_name:
            raise ReplayTrajectoryDatasetError(
                f"Source filename {source.path.name} does not match canonical "
                f"filename {expected_name}."
            )

    return sorted(sources, key=lambda source: source.trajectory["match_id"])


def _construct_rows(sources: list[_SourceArtifact]) -> _DatasetRows:
    _validate_decision_schema()
    decisions: list[CsvRow] = []
    outcomes: list[CsvRow] = []
    decision_keys: set[DecisionKey] = set()
    outcome_keys: set[OutcomeKey] = set()
    snapshot_count = 0
    outcome_rows_by_horizon = {horizon: 0 for horizon in HORIZONS_SECONDS}

    for source in sources:
        trajectory = source.trajectory
        match_id = trajectory["match_id"]
        snapshots_by_time = {
            snapshot["game_time_seconds"]: snapshot
            for snapshot in trajectory["snapshots"]
        }
        snapshot_count += len(snapshots_by_time)

        for snapshot in trajectory["snapshots"]:
            game_time = snapshot["game_time_seconds"]
            decision_rows_by_team: dict[TeamName, CsvRow] = {}
            for team in TEAM_ORIENTATIONS:
                row = _decision_row(trajectory, snapshot, team)
                decision_key = (match_id, game_time, team)
                if decision_key in decision_keys:
                    raise ReplayTrajectoryDatasetError(
                        f"Duplicate decision key generated: {decision_key}."
                    )
                decision_keys.add(decision_key)
                decision_rows_by_team[team] = row
                decisions.append(row)

            _validate_decision_pair(decision_rows_by_team, match_id, game_time)

            outcome_rows_for_snapshot: dict[tuple[TeamName, int], CsvRow] = {}
            for team in TEAM_ORIENTATIONS:
                for horizon in HORIZONS_SECONDS:
                    future_time = game_time + horizon
                    future_snapshot = snapshots_by_time.get(future_time)
                    if future_snapshot is None:
                        continue
                    if future_snapshot["game_time_seconds"] != future_time:
                        raise ReplayTrajectoryDatasetError(
                            "Outcome snapshot must match the exact future horizon."
                        )
                    row = _outcome_row(
                        trajectory,
                        decision_rows_by_team[team],
                        future_snapshot,
                        team,
                        horizon,
                    )
                    outcome_key = (
                        match_id,
                        game_time,
                        team,
                        horizon,
                        future_time,
                    )
                    if outcome_key in outcome_keys:
                        raise ReplayTrajectoryDatasetError(
                            f"Duplicate outcome key generated: {outcome_key}."
                        )
                    if outcome_key[:3] not in decision_keys:
                        raise ReplayTrajectoryDatasetError(
                            "Outcome key has no matching decision key: "
                            f"{outcome_key}."
                        )
                    outcome_keys.add(outcome_key)
                    outcome_rows_for_snapshot[(team, horizon)] = row
                    outcome_rows_by_horizon[horizon] += 1
                    outcomes.append(row)

            _validate_outcome_pairs(
                outcome_rows_for_snapshot,
                match_id,
                game_time,
                snapshots_by_time,
            )

    expected_decisions = snapshot_count * len(TEAM_ORIENTATIONS)
    if len(decisions) != expected_decisions:
        raise ReplayTrajectoryDatasetError(
            "Each source snapshot must produce exactly two decision rows."
        )
    if len(decision_keys) != len(decisions):
        raise ReplayTrajectoryDatasetError("Decision keys must be unique.")
    if len(outcome_keys) != len(outcomes):
        raise ReplayTrajectoryDatasetError("Outcome keys must be unique.")

    return _DatasetRows(
        decisions=decisions,
        outcomes=outcomes,
        snapshot_count=snapshot_count,
        outcome_rows_by_horizon=outcome_rows_by_horizon,
    )


def _decision_row(
    trajectory: ReplayTrajectory,
    snapshot: ReplaySnapshot,
    team: TeamName,
) -> CsvRow:
    opponent = _opponent(team)
    teams = _teams_by_name(snapshot)
    team_values = teams[team]
    opponent_values = teams[opponent]
    team_id, team_tag = _team_metadata(trajectory, team)
    opponent_id, opponent_tag = _team_metadata(trajectory, opponent)
    row: CsvRow = {
        "match_id": trajectory["match_id"],
        "replay_sha256": trajectory["replay_sha256"],
        "game_mode": trajectory["game_mode"],
        "league_id": trajectory["league_id"],
        "game_time_seconds": snapshot["game_time_seconds"],
        "team": team,
        "team_id": team_id,
        "team_tag": team_tag,
        "opponent_team": opponent,
        "opponent_team_id": opponent_id,
        "opponent_team_tag": opponent_tag,
    }
    for field in AGGREGATE_FIELDS:
        row[f"team_{field}"] = _metric_value(team_values, field)
        row[f"opponent_{field}"] = _metric_value(opponent_values, field)
    for field, column_stem in DIFF_COLUMNS:
        row[f"{column_stem}_diff"] = (
            _metric_value(team_values, field)
            - _metric_value(opponent_values, field)
        )
    return row


def _outcome_row(
    trajectory: ReplayTrajectory,
    decision: CsvRow,
    future_snapshot: ReplaySnapshot,
    team: TeamName,
    horizon: int,
) -> CsvRow:
    opponent = _opponent(team)
    future_teams = _teams_by_name(future_snapshot)
    future_team = future_teams[team]
    future_opponent = future_teams[opponent]
    decision_time = _integer_cell(decision, "game_time_seconds")
    future_time = future_snapshot["game_time_seconds"]
    row: CsvRow = {
        "match_id": trajectory["match_id"],
        "game_time_seconds": decision_time,
        "team": team,
        "horizon_seconds": horizon,
        "future_game_time_seconds": future_time,
    }
    for field in AGGREGATE_FIELDS:
        row[f"future_team_{field}"] = _metric_value(future_team, field)
        row[f"future_opponent_{field}"] = _metric_value(
            future_opponent, field
        )
    for field, column_stem in DIFF_COLUMNS:
        future_diff = (
            _metric_value(future_team, field)
            - _metric_value(future_opponent, field)
        )
        current_diff = _integer_cell(decision, f"{column_stem}_diff")
        change = future_diff - current_diff
        row[f"future_{column_stem}_diff"] = future_diff
        row[f"{column_stem}_diff_change"] = change
        if change != future_diff - current_diff:
            raise ReplayTrajectoryDatasetError(
                "Outcome change must equal future minus decision-time difference."
            )
    winner_team = "RADIANT" if trajectory["winner"] == 2 else "DIRE"
    row["team_won"] = team == winner_team
    return row


def _validate_decision_schema() -> None:
    leakage = sorted(
        column
        for column in DECISION_COLUMNS
        if column in DECISION_LEAKAGE_COLUMNS or column.startswith("future_")
    )
    if leakage:
        raise ReplayTrajectoryDatasetError(
            "Decision schema contains leakage column(s): "
            f"{', '.join(leakage)}."
        )


def _validate_decision_pair(
    rows: dict[TeamName, CsvRow],
    match_id: int,
    game_time: int,
) -> None:
    if set(rows) != set(TEAM_ORIENTATIONS) or len(rows) != 2:
        raise ReplayTrajectoryDatasetError(
            "Each snapshot must contain one Radiant and one Dire orientation."
        )
    radiant = rows["RADIANT"]
    dire = rows["DIRE"]
    for _, column_stem in DIFF_COLUMNS:
        column = f"{column_stem}_diff"
        if _integer_cell(radiant, column) != -_integer_cell(dire, column):
            raise ReplayTrajectoryDatasetError(
                f"Decision difference {column} is not mirrored for "
                f"match_id {match_id} at {game_time}."
            )


def _validate_outcome_pairs(
    rows: dict[tuple[TeamName, int], CsvRow],
    match_id: int,
    game_time: int,
    snapshots_by_time: dict[int, ReplaySnapshot],
) -> None:
    for horizon in HORIZONS_SECONDS:
        future_time = game_time + horizon
        has_future = future_time in snapshots_by_time
        radiant = rows.get(("RADIANT", horizon))
        dire = rows.get(("DIRE", horizon))
        if not has_future:
            if radiant is not None or dire is not None:
                raise ReplayTrajectoryDatasetError(
                    "An outcome was generated without an exact future snapshot."
                )
            continue
        if radiant is None or dire is None:
            raise ReplayTrajectoryDatasetError(
                "An exact future snapshot must produce both team outcomes."
            )
        if (
            _integer_cell(radiant, "future_game_time_seconds") != future_time
            or _integer_cell(dire, "future_game_time_seconds") != future_time
        ):
            raise ReplayTrajectoryDatasetError(
                "Outcome future time does not match the exact horizon."
            )
        for _, column_stem in DIFF_COLUMNS:
            future_column = f"future_{column_stem}_diff"
            change_column = f"{column_stem}_diff_change"
            if _integer_cell(radiant, future_column) != -_integer_cell(
                dire, future_column
            ):
                raise ReplayTrajectoryDatasetError(
                    f"Outcome difference {future_column} is not mirrored for "
                    f"match_id {match_id} at {game_time}."
                )
            if _integer_cell(radiant, change_column) != -_integer_cell(
                dire, change_column
            ):
                raise ReplayTrajectoryDatasetError(
                    f"Outcome change {change_column} is not mirrored for "
                    f"match_id {match_id} at {game_time}."
                )
        radiant_won = _boolean_cell(radiant, "team_won")
        dire_won = _boolean_cell(dire, "team_won")
        if radiant_won == dire_won:
            raise ReplayTrajectoryDatasetError(
                "team_won must be mirrored between Radiant and Dire."
            )


def _render_csv(columns: tuple[str, ...], rows: list[CsvRow]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=",", lineterminator="\n")
    writer.writerow(columns)
    expected = set(columns)
    for index, row in enumerate(rows):
        actual = set(row)
        if actual != expected:
            missing = ", ".join(sorted(expected - actual))
            unexpected = ", ".join(sorted(actual - expected))
            raise ReplayTrajectoryDatasetError(
                f"CSV row {index} does not match its fixed schema; "
                f"missing=[{missing}] unexpected=[{unexpected}]."
            )
        writer.writerow(_csv_text(row[column]) for column in columns)
    return stream.getvalue().encode("utf-8")


def _render_manifest(
    sources: list[_SourceArtifact],
    rows: _DatasetRows,
    *,
    decision_sha256: str,
    outcome_sha256: str,
) -> bytes:
    manifest = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "source_schema_version": SUPPORTED_SCHEMA_VERSION,
        "horizons_seconds": list(HORIZONS_SECONDS),
        "match_count": len(sources),
        "match_ids": [source.trajectory["match_id"] for source in sources],
        "source_artifacts": [
            {
                "match_id": source.trajectory["match_id"],
                "replay_sha256": source.trajectory["replay_sha256"],
                "snapshot_count": len(source.trajectory["snapshots"]),
            }
            for source in sources
        ],
        "snapshot_count": rows.snapshot_count,
        "decision_row_count": len(rows.decisions),
        "outcome_row_count": len(rows.outcomes),
        "decision_file": {
            "filename": DECISION_FILENAME,
            "row_count": len(rows.decisions),
            "sha256": decision_sha256,
        },
        "outcome_file": {
            "filename": OUTCOME_FILENAME,
            "row_count": len(rows.outcomes),
            "sha256": outcome_sha256,
        },
    }
    text = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    return f"{text}\n".encode("utf-8")


def _existing_output_set_is_identical(
    output_dir: Path,
    outputs: dict[str, bytes],
) -> bool:
    if not output_dir.exists():
        return False
    if not output_dir.is_dir():
        raise ReplayTrajectoryDatasetError(
            f"Output path is not a directory: {output_dir.as_posix()}."
        )
    _reject_unexpected_output_entries(output_dir)
    try:
        return all(
            (output_dir / filename).is_file()
            and (output_dir / filename).read_bytes() == content
            for filename, content in outputs.items()
        )
    except OSError as exc:
        raise ReplayTrajectoryDatasetError(
            "Existing replay trajectory dataset outputs cannot be read."
        ) from exc


def _replace_output_set(output_dir: Path, outputs: dict[str, bytes]) -> None:
    parent = output_dir.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReplayTrajectoryDatasetError(
            "Cannot create the replay trajectory dataset parent directory."
        ) from exc

    if output_dir.exists():
        if not output_dir.is_dir():
            raise ReplayTrajectoryDatasetError(
                f"Output path is not a directory: {output_dir.as_posix()}."
            )
        _reject_unexpected_output_entries(output_dir)

    transaction: Path | None = None
    old_output: Path | None = None
    installed_new = False
    rollback_failed = False
    try:
        transaction = Path(
            tempfile.mkdtemp(
                dir=parent,
                prefix=f".{output_dir.name}.transaction.",
            )
        )
        new_output = transaction / "new"
        new_output.mkdir()
        for filename, content in outputs.items():
            _write_durable(new_output / filename, content)

        old_output = transaction / "old"
        if output_dir.exists():
            os.replace(output_dir, old_output)
        else:
            old_output = None
        try:
            os.replace(new_output, output_dir)
            installed_new = True
            if old_output is not None:
                shutil.rmtree(old_output)
                old_output = None
        except OSError:
            if installed_new and output_dir.exists():
                os.replace(output_dir, transaction / "failed-new")
                installed_new = False
            if old_output is not None and old_output.exists():
                os.replace(old_output, output_dir)
                old_output = None
            raise
    except OSError as exc:
        if old_output is not None and old_output.exists() and not output_dir.exists():
            try:
                os.replace(old_output, output_dir)
                old_output = None
            except OSError:
                rollback_failed = True
        message = "Could not atomically replace the replay trajectory dataset."
        if rollback_failed:
            message += " Automatic rollback also failed."
        raise ReplayTrajectoryDatasetError(message) from exc
    finally:
        if transaction is not None and transaction.exists() and not rollback_failed:
            try:
                shutil.rmtree(transaction)
            except OSError as exc:
                if installed_new:
                    raise ReplayTrajectoryDatasetError(
                        "Dataset was built but temporary output cleanup failed."
                    ) from exc


def _reject_unexpected_output_entries(output_dir: Path) -> None:
    try:
        unexpected = sorted(
            path.name
            for path in output_dir.iterdir()
            if path.name not in OUTPUT_FILENAMES
        )
    except OSError as exc:
        raise ReplayTrajectoryDatasetError(
            "Cannot inspect the replay trajectory dataset output directory."
        ) from exc
    if unexpected:
        raise ReplayTrajectoryDatasetError(
            "Output directory contains unexpected entries; refusing to replace "
            f"it: {', '.join(unexpected)}."
        )


def _write_durable(path: Path, content: bytes) -> None:
    with path.open("wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def _teams_by_name(snapshot: ReplaySnapshot) -> dict[TeamName, ReplayTeam]:
    result: dict[TeamName, ReplayTeam] = {}
    for row in snapshot["teams"]:
        if row["team"] == "RADIANT":
            result["RADIANT"] = row
        elif row["team"] == "DIRE":
            result["DIRE"] = row
    if set(result) != set(TEAM_ORIENTATIONS):
        raise ReplayTrajectoryDatasetError(
            "Validated snapshot is missing a team aggregate orientation."
        )
    return result


def _team_metadata(
    trajectory: ReplayTrajectory,
    team: TeamName,
) -> tuple[int | None, str | None]:
    if team == "RADIANT":
        return trajectory["radiant_team_id"], trajectory["radiant_team_tag"]
    return trajectory["dire_team_id"], trajectory["dire_team_tag"]


def _opponent(team: TeamName) -> TeamName:
    if team == "RADIANT":
        return "DIRE"
    return "RADIANT"


def _metric_value(row: ReplayTeam, field: AggregateField) -> int:
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


def _integer_cell(row: CsvRow, column: str) -> int:
    value = row.get(column)
    if type(value) is not int:
        raise ReplayTrajectoryDatasetError(
            f"Internal dataset column {column} must be an integer."
        )
    return value


def _boolean_cell(row: CsvRow, column: str) -> bool:
    value = row.get(column)
    if type(value) is not bool:
        raise ReplayTrajectoryDatasetError(
            f"Internal dataset column {column} must be a boolean."
        )
    return value


def _csv_text(value: CsvScalar) -> str:
    if value is None:
        return ""
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
