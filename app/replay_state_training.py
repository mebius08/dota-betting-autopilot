from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Literal, cast

from app.replay_trajectory_dataset import DECISION_COLUMNS, OUTCOME_COLUMNS


TRAINING_SCHEMA_VERSION = 1
REQUIRED_HORIZON_SECONDS = 180
REQUIRED_LAGS_SECONDS = (60, 120, 180)
TRAINING_FILENAME = "replay_state_training.csv"
MANIFEST_FILENAME = "manifest.json"
OUTPUT_FILENAMES = (TRAINING_FILENAME, MANIFEST_FILENAME)

CURRENT_DIFF_COLUMNS = (
    "kill_diff",
    "death_diff",
    "assist_diff",
    "last_hit_diff",
    "deny_diff",
    "net_worth_diff",
    "current_gold_diff",
    "total_xp_diff",
)
TOTAL_COLUMNS = (
    ("total_kills", "kills"),
    ("total_last_hits", "last_hits"),
    ("total_denies", "denies"),
    ("total_net_worth", "net_worth"),
    ("total_current_gold", "current_gold"),
    ("total_xp", "total_xp"),
)
MOMENTUM_DIFF_COLUMNS = (
    "net_worth_diff",
    "total_xp_diff",
    "kill_diff",
    "last_hit_diff",
)
TARGET_COLUMNS = (
    "target_net_worth_diff_change_180",
    "target_total_xp_diff_change_180",
    "target_kill_diff_change_180",
)
TRAINING_COLUMNS = (
    "match_id",
    "game_time_seconds",
    *CURRENT_DIFF_COLUMNS,
    *(column for column, _ in TOTAL_COLUMNS),
    *(
        f"{column}_change_prev_{lag}"
        for lag in REQUIRED_LAGS_SECONDS
        for column in MOMENTUM_DIFF_COLUMNS
    ),
    *TARGET_COLUMNS,
)
FORBIDDEN_COLUMNS = {
    "team_won",
    "team_id",
    "team_tag",
    "opponent_team_id",
    "opponent_team_tag",
    "replay_sha256",
    "league_id",
    "game_mode",
}
AGGREGATE_STEMS = (
    "kills",
    "deaths",
    "assists",
    "last_hits",
    "denies",
    "net_worth",
    "current_gold",
    "total_xp",
)
DIFF_BY_AGGREGATE = dict(zip(AGGREGATE_STEMS, CURRENT_DIFF_COLUMNS, strict=True))
_INTEGER_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")

TeamName = Literal["RADIANT", "DIRE"]
DecisionKey = tuple[int, int, TeamName]
SnapshotKey = tuple[int, int]
OutcomeKey = tuple[int, int, TeamName, int]
TrainingKey = tuple[int, int]
TrainingRow = dict[str, int]


class ReplayStateTrainingError(ValueError):
    """Raised when replay state training data cannot be built safely."""


@dataclass(frozen=True)
class ReplayStateTrainingBuildResult:
    status: Literal["BUILT", "UNCHANGED"]
    output_dir: Path
    source_match_count: int
    source_snapshot_count: int
    source_decision_row_count: int
    source_outcome_row_count: int
    training_row_count: int
    training_sha256: str
    manifest_sha256: str


@dataclass(frozen=True)
class _SourceManifest:
    sha256: str
    match_count: int
    snapshot_count: int
    decision_row_count: int
    outcome_row_count: int
    decision_sha256: str
    outcome_sha256: str


@dataclass(frozen=True)
class _Decision:
    match_id: int
    game_time_seconds: int
    team: TeamName
    values: dict[str, int]


@dataclass(frozen=True)
class _Outcome:
    match_id: int
    game_time_seconds: int
    team: TeamName
    horizon_seconds: int
    values: dict[str, int]


def build_replay_state_training_set(
    dataset_dir: str | Path,
    output_dir: str | Path,
) -> ReplayStateTrainingBuildResult:
    source_dir = Path(dataset_dir)
    manifest_bytes = _read_source_file(source_dir, MANIFEST_FILENAME)
    source_manifest = _load_source_manifest(manifest_bytes)
    decision_bytes = _read_source_file(source_dir, "team_decisions.csv")
    outcome_bytes = _read_source_file(source_dir, "team_outcomes.csv")
    _validate_source_hash(
        "team_decisions.csv", decision_bytes, source_manifest.decision_sha256
    )
    _validate_source_hash(
        "team_outcomes.csv", outcome_bytes, source_manifest.outcome_sha256
    )

    decisions = _load_decisions(decision_bytes, source_manifest)
    outcomes = _load_outcomes(outcome_bytes, source_manifest, decisions)
    _validate_required_outcome_coverage(decisions, outcomes)
    rows = _construct_training_rows(decisions, outcomes)
    training_bytes = _render_training_csv(rows)
    training_sha256 = _sha256(training_bytes)
    output_manifest_bytes = _render_output_manifest(
        source_manifest,
        training_row_count=len(rows),
        training_sha256=training_sha256,
    )
    outputs = {
        TRAINING_FILENAME: training_bytes,
        MANIFEST_FILENAME: output_manifest_bytes,
    }

    destination = Path(output_dir)
    if _existing_output_set_is_identical(destination, outputs):
        status: Literal["BUILT", "UNCHANGED"] = "UNCHANGED"
    else:
        _replace_output_set(destination, outputs)
        status = "BUILT"

    return ReplayStateTrainingBuildResult(
        status=status,
        output_dir=destination,
        source_match_count=source_manifest.match_count,
        source_snapshot_count=source_manifest.snapshot_count,
        source_decision_row_count=source_manifest.decision_row_count,
        source_outcome_row_count=source_manifest.outcome_row_count,
        training_row_count=len(rows),
        training_sha256=training_sha256,
        manifest_sha256=_sha256(output_manifest_bytes),
    )


def _read_source_file(dataset_dir: Path, filename: str) -> bytes:
    if not dataset_dir.exists():
        raise ReplayStateTrainingError(
            f"Dataset directory does not exist: {dataset_dir.as_posix()}."
        )
    if not dataset_dir.is_dir():
        raise ReplayStateTrainingError(
            f"Dataset path is not a directory: {dataset_dir.as_posix()}."
        )
    path = dataset_dir / filename
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ReplayStateTrainingError(
            f"Cannot read required source file {filename}."
        ) from exc


def _load_source_manifest(content: bytes) -> _SourceManifest:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayStateTrainingError("Source manifest is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise ReplayStateTrainingError("Source manifest root must be an object.")

    schema_version = _manifest_int(payload, "schema_version")
    if schema_version != 1:
        raise ReplayStateTrainingError(
            f"Unsupported source manifest schema_version {schema_version}."
        )
    match_count = _manifest_int(payload, "match_count")
    snapshot_count = _manifest_int(payload, "snapshot_count")
    decision_row_count = _manifest_int(payload, "decision_row_count")
    outcome_row_count = _manifest_int(payload, "outcome_row_count")
    horizons = payload.get("horizons_seconds")
    if (
        not isinstance(horizons, list)
        or REQUIRED_HORIZON_SECONDS not in horizons
        or any(type(value) is not int for value in horizons)
    ):
        raise ReplayStateTrainingError(
            "Source manifest does not declare the required 180-second horizon."
        )

    decision_file = _manifest_file(payload, "decision_file")
    outcome_file = _manifest_file(payload, "outcome_file")
    if decision_file[0] != "team_decisions.csv":
        raise ReplayStateTrainingError(
            "Source manifest decision filename must be team_decisions.csv."
        )
    if outcome_file[0] != "team_outcomes.csv":
        raise ReplayStateTrainingError(
            "Source manifest outcome filename must be team_outcomes.csv."
        )
    if decision_file[1] != decision_row_count:
        raise ReplayStateTrainingError(
            "Source manifest decision row counts are inconsistent."
        )
    if outcome_file[1] != outcome_row_count:
        raise ReplayStateTrainingError(
            "Source manifest outcome row counts are inconsistent."
        )

    return _SourceManifest(
        sha256=_sha256(content),
        match_count=match_count,
        snapshot_count=snapshot_count,
        decision_row_count=decision_row_count,
        outcome_row_count=outcome_row_count,
        decision_sha256=decision_file[2],
        outcome_sha256=outcome_file[2],
    )


def _manifest_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise ReplayStateTrainingError(
            f"Source manifest {key} must be a non-negative integer."
        )
    return value


def _manifest_file(
    payload: dict[str, Any], key: str
) -> tuple[str, int, str]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ReplayStateTrainingError(f"Source manifest {key} must be an object.")
    filename = value.get("filename")
    row_count = value.get("row_count")
    sha256 = value.get("sha256")
    if not isinstance(filename, str):
        raise ReplayStateTrainingError(
            f"Source manifest {key}.filename must be a string."
        )
    if type(row_count) is not int or row_count < 0:
        raise ReplayStateTrainingError(
            f"Source manifest {key}.row_count must be a non-negative integer."
        )
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise ReplayStateTrainingError(
            f"Source manifest {key}.sha256 must be a lowercase SHA-256 digest."
        )
    return filename, row_count, sha256


def _validate_source_hash(filename: str, content: bytes, expected: str) -> None:
    actual = _sha256(content)
    if actual != expected:
        raise ReplayStateTrainingError(
            f"Source hash mismatch for {filename}: expected {expected}, got {actual}."
        )


def _load_decisions(
    content: bytes,
    manifest: _SourceManifest,
) -> dict[DecisionKey, _Decision]:
    rows = _read_csv(content, "team_decisions.csv", DECISION_COLUMNS)
    if len(rows) != manifest.decision_row_count:
        raise ReplayStateTrainingError(
            "Source row-count mismatch for team_decisions.csv: "
            f"expected {manifest.decision_row_count}, got {len(rows)}."
        )

    decisions: dict[DecisionKey, _Decision] = {}
    snapshots: dict[SnapshotKey, dict[TeamName, _Decision]] = {}
    for row_number, row in enumerate(rows, start=2):
        match_id = _csv_int(row, "match_id", "team_decisions.csv", row_number)
        game_time = _csv_int(
            row, "game_time_seconds", "team_decisions.csv", row_number
        )
        team = _team_cell(row, "team_decisions.csv", row_number)
        opponent_team = row["opponent_team"]
        expected_opponent = "DIRE" if team == "RADIANT" else "RADIANT"
        if opponent_team != expected_opponent:
            raise ReplayStateTrainingError(
                f"Invalid opponent_team in team_decisions.csv row {row_number}."
            )

        values: dict[str, int] = {}
        for stem in AGGREGATE_STEMS:
            team_column = f"team_{stem}"
            opponent_column = f"opponent_{stem}"
            values[team_column] = _csv_int(
                row, team_column, "team_decisions.csv", row_number
            )
            values[opponent_column] = _csv_int(
                row, opponent_column, "team_decisions.csv", row_number
            )
            diff_column = DIFF_BY_AGGREGATE[stem]
            values[diff_column] = _csv_int(
                row, diff_column, "team_decisions.csv", row_number
            )
            expected_diff = values[team_column] - values[opponent_column]
            if values[diff_column] != expected_diff:
                raise ReplayStateTrainingError(
                    f"Inconsistent current {diff_column} in team_decisions.csv "
                    f"row {row_number}."
                )

        key = (match_id, game_time, team)
        if key in decisions:
            raise ReplayStateTrainingError(f"Duplicate decision key: {key}.")
        decision = _Decision(match_id, game_time, team, values)
        decisions[key] = decision
        snapshots.setdefault((match_id, game_time), {})[team] = decision

    if len({key[0] for key in snapshots}) != manifest.match_count:
        raise ReplayStateTrainingError(
            "Source match-count mismatch in team_decisions.csv."
        )
    if len(snapshots) != manifest.snapshot_count:
        raise ReplayStateTrainingError(
            "Source snapshot-count mismatch in team_decisions.csv: "
            f"expected {manifest.snapshot_count}, got {len(snapshots)}."
        )
    _validate_snapshot_pairs(snapshots)
    return decisions


def _validate_snapshot_pairs(
    snapshots: dict[SnapshotKey, dict[TeamName, _Decision]],
) -> None:
    for snapshot_key, teams in snapshots.items():
        if set(teams) != {"RADIANT", "DIRE"}:
            raise ReplayStateTrainingError(
                f"Source snapshot {snapshot_key} must contain both team orientations."
            )
        radiant = teams["RADIANT"]
        dire = teams["DIRE"]
        for stem in AGGREGATE_STEMS:
            if (
                radiant.values[f"team_{stem}"]
                != dire.values[f"opponent_{stem}"]
                or radiant.values[f"opponent_{stem}"]
                != dire.values[f"team_{stem}"]
            ):
                raise ReplayStateTrainingError(
                    f"Inconsistent mirrored totals for snapshot {snapshot_key}."
                )


def _load_outcomes(
    content: bytes,
    manifest: _SourceManifest,
    decisions: dict[DecisionKey, _Decision],
) -> dict[OutcomeKey, _Outcome]:
    rows = _read_csv(content, "team_outcomes.csv", OUTCOME_COLUMNS)
    if len(rows) != manifest.outcome_row_count:
        raise ReplayStateTrainingError(
            "Source row-count mismatch for team_outcomes.csv: "
            f"expected {manifest.outcome_row_count}, got {len(rows)}."
        )

    outcomes: dict[OutcomeKey, _Outcome] = {}
    for row_number, row in enumerate(rows, start=2):
        match_id = _csv_int(row, "match_id", "team_outcomes.csv", row_number)
        game_time = _csv_int(
            row, "game_time_seconds", "team_outcomes.csv", row_number
        )
        team = _team_cell(row, "team_outcomes.csv", row_number)
        horizon = _csv_int(
            row, "horizon_seconds", "team_outcomes.csv", row_number
        )
        future_time = _csv_int(
            row, "future_game_time_seconds", "team_outcomes.csv", row_number
        )
        key = (match_id, game_time, team, horizon)
        if key in outcomes:
            raise ReplayStateTrainingError(f"Duplicate outcome key: {key}.")
        decision_key = (match_id, game_time, team)
        future_key = (match_id, future_time, team)
        decision = decisions.get(decision_key)
        future = decisions.get(future_key)
        if decision is None:
            raise ReplayStateTrainingError(
                f"Outcome key has no matching decision key: {decision_key}."
            )
        if future_time != game_time + horizon or future is None:
            raise ReplayStateTrainingError(
                f"Outcome key has no exact future snapshot: {key}."
            )

        values: dict[str, int] = {}
        for stem in AGGREGATE_STEMS:
            diff_column = DIFF_BY_AGGREGATE[stem]
            future_team_column = f"future_team_{stem}"
            future_opponent_column = f"future_opponent_{stem}"
            future_diff_column = f"future_{diff_column}"
            change_column = f"{diff_column}_change"
            values[future_team_column] = _csv_int(
                row, future_team_column, "team_outcomes.csv", row_number
            )
            values[future_opponent_column] = _csv_int(
                row, future_opponent_column, "team_outcomes.csv", row_number
            )
            values[future_diff_column] = _csv_int(
                row, future_diff_column, "team_outcomes.csv", row_number
            )
            values[change_column] = _csv_int(
                row, change_column, "team_outcomes.csv", row_number
            )
            if (
                values[future_team_column] != future.values[f"team_{stem}"]
                or values[future_opponent_column]
                != future.values[f"opponent_{stem}"]
            ):
                raise ReplayStateTrainingError(
                    f"Outcome future {stem} totals do not match their exact "
                    f"future decision in row {row_number}."
                )
            if values[future_diff_column] != future.values[diff_column]:
                raise ReplayStateTrainingError(
                    f"Outcome {future_diff_column} does not match its exact "
                    f"future decision in row {row_number}."
                )
            expected_change = future.values[diff_column] - decision.values[diff_column]
            if values[change_column] != expected_change:
                raise ReplayStateTrainingError(
                    f"Outcome {change_column} is inconsistent in row {row_number}."
                )
        outcomes[key] = _Outcome(match_id, game_time, team, horizon, values)
    return outcomes


def _validate_required_outcome_coverage(
    decisions: dict[DecisionKey, _Decision],
    outcomes: dict[OutcomeKey, _Outcome],
) -> None:
    for decision in decisions.values():
        future_key = (
            decision.match_id,
            decision.game_time_seconds + REQUIRED_HORIZON_SECONDS,
            decision.team,
        )
        if future_key not in decisions:
            continue
        outcome_key = (
            decision.match_id,
            decision.game_time_seconds,
            decision.team,
            REQUIRED_HORIZON_SECONDS,
        )
        if outcome_key not in outcomes:
            raise ReplayStateTrainingError(
                f"Missing required exact outcome for decision key {outcome_key[:3]}."
            )


def _read_csv(
    content: bytes,
    filename: str,
    expected_columns: tuple[str, ...],
) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReplayStateTrainingError(f"{filename} is not valid UTF-8.") from exc
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=",")
        header = next(reader)
        if tuple(header) != expected_columns:
            raise ReplayStateTrainingError(
                f"{filename} does not match the required source schema."
            )
        rows: list[dict[str, str]] = []
        for row_number, cells in enumerate(reader, start=2):
            if len(cells) != len(header):
                raise ReplayStateTrainingError(
                    f"Malformed {filename} row {row_number}."
                )
            rows.append(dict(zip(header, cells, strict=True)))
        return rows
    except StopIteration as exc:
        raise ReplayStateTrainingError(f"{filename} is empty.") from exc
    except csv.Error as exc:
        raise ReplayStateTrainingError(f"Cannot parse {filename}.") from exc


def _csv_int(
    row: dict[str, str],
    column: str,
    filename: str,
    row_number: int,
) -> int:
    value = row[column]
    if _INTEGER_PATTERN.fullmatch(value) is None:
        raise ReplayStateTrainingError(
            f"{filename} row {row_number} column {column} must be an integer."
        )
    return int(value)


def _team_cell(
    row: dict[str, str], filename: str, row_number: int
) -> TeamName:
    value = row["team"]
    if value not in {"RADIANT", "DIRE"}:
        raise ReplayStateTrainingError(
            f"{filename} row {row_number} has invalid team {value!r}."
        )
    return cast(TeamName, value)


def _construct_training_rows(
    decisions: dict[DecisionKey, _Decision],
    outcomes: dict[OutcomeKey, _Outcome],
) -> list[TrainingRow]:
    _validate_training_schema()
    rows: list[TrainingRow] = []
    training_keys: set[TrainingKey] = set()
    radiant_decisions = sorted(
        (decision for decision in decisions.values() if decision.team == "RADIANT"),
        key=lambda decision: (decision.match_id, decision.game_time_seconds),
    )
    for current in radiant_decisions:
        match_id = current.match_id
        game_time = current.game_time_seconds
        previous = {
            lag: decisions.get((match_id, game_time - lag, "RADIANT"))
            for lag in REQUIRED_LAGS_SECONDS
        }
        future = decisions.get(
            (match_id, game_time + REQUIRED_HORIZON_SECONDS, "RADIANT")
        )
        outcome = outcomes.get(
            (match_id, game_time, "RADIANT", REQUIRED_HORIZON_SECONDS)
        )
        if any(value is None for value in previous.values()):
            continue
        if future is None or outcome is None:
            continue

        row: TrainingRow = {
            "match_id": match_id,
            "game_time_seconds": game_time,
        }
        for column in CURRENT_DIFF_COLUMNS:
            row[column] = current.values[column]
        for total_column, stem in TOTAL_COLUMNS:
            row[total_column] = (
                current.values[f"team_{stem}"]
                + current.values[f"opponent_{stem}"]
            )
        for lag in REQUIRED_LAGS_SECONDS:
            lagged = previous[lag]
            if lagged is None:
                raise ReplayStateTrainingError("Missing exact lag decision.")
            for column in MOMENTUM_DIFF_COLUMNS:
                row[f"{column}_change_prev_{lag}"] = (
                    current.values[column] - lagged.values[column]
                )
        row["target_net_worth_diff_change_180"] = outcome.values[
            "net_worth_diff_change"
        ]
        row["target_total_xp_diff_change_180"] = outcome.values[
            "total_xp_diff_change"
        ]
        row["target_kill_diff_change_180"] = outcome.values["kill_diff_change"]

        key = (match_id, game_time)
        if key in training_keys:
            raise ReplayStateTrainingError(f"Duplicate training key: {key}.")
        if current.team != "RADIANT":
            raise ReplayStateTrainingError("Dire row reached the training output.")
        training_keys.add(key)
        rows.append(row)
    return rows


def _validate_training_schema() -> None:
    if len(TRAINING_COLUMNS) != len(set(TRAINING_COLUMNS)):
        raise ReplayStateTrainingError("Training schema contains duplicate columns.")
    leakage = sorted(
        column
        for column in TRAINING_COLUMNS
        if column in FORBIDDEN_COLUMNS or column.startswith("future_")
    )
    if leakage:
        raise ReplayStateTrainingError(
            f"Training schema contains forbidden columns: {', '.join(leakage)}."
        )
    unexpected_targets = sorted(
        column
        for column in TRAINING_COLUMNS
        if column.startswith("target_") and column not in TARGET_COLUMNS
    )
    if unexpected_targets:
        raise ReplayStateTrainingError(
            "Training schema contains an unapproved target column."
        )


def _render_training_csv(rows: list[TrainingRow]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=",", lineterminator="\n")
    writer.writerow(TRAINING_COLUMNS)
    expected = set(TRAINING_COLUMNS)
    previous_key: TrainingKey | None = None
    for index, row in enumerate(rows):
        if set(row) != expected:
            raise ReplayStateTrainingError(
                f"Training row {index} does not match the fixed schema."
            )
        key = (row["match_id"], row["game_time_seconds"])
        if previous_key is not None and key <= previous_key:
            raise ReplayStateTrainingError("Training keys are not strictly ordered.")
        previous_key = key
        writer.writerow(row[column] for column in TRAINING_COLUMNS)
    return stream.getvalue().encode("utf-8")


def _render_output_manifest(
    source: _SourceManifest,
    *,
    training_row_count: int,
    training_sha256: str,
) -> bytes:
    manifest = {
        "schema_version": TRAINING_SCHEMA_VERSION,
        "source_manifest_sha256": source.sha256,
        "source_match_count": source.match_count,
        "source_snapshot_count": source.snapshot_count,
        "source_decision_row_count": source.decision_row_count,
        "source_outcome_row_count": source.outcome_row_count,
        "required_horizon_seconds": REQUIRED_HORIZON_SECONDS,
        "required_lags_seconds": list(REQUIRED_LAGS_SECONDS),
        "training_row_count": training_row_count,
        "training_file": {
            "filename": TRAINING_FILENAME,
            "row_count": training_row_count,
            "sha256": training_sha256,
        },
    }
    text = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False)
    return f"{text}\n".encode("utf-8")


def _existing_output_set_is_identical(
    output_dir: Path,
    outputs: dict[str, bytes],
) -> bool:
    if not output_dir.exists():
        return False
    if not output_dir.is_dir():
        raise ReplayStateTrainingError(
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
        raise ReplayStateTrainingError(
            "Existing replay state training outputs cannot be read."
        ) from exc


def _replace_output_set(output_dir: Path, outputs: dict[str, bytes]) -> None:
    parent = output_dir.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReplayStateTrainingError(
            "Cannot create the replay state training parent directory."
        ) from exc
    if output_dir.exists():
        if not output_dir.is_dir():
            raise ReplayStateTrainingError(
                f"Output path is not a directory: {output_dir.as_posix()}."
            )
        _reject_unexpected_output_entries(output_dir)

    transaction: Path | None = None
    old_output: Path | None = None
    installed_new = False
    rollback_failed = False
    try:
        transaction = Path(
            tempfile.mkdtemp(dir=parent, prefix=f".{output_dir.name}.transaction.")
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
        message = "Could not atomically replace replay state training outputs."
        if rollback_failed:
            message += " Automatic rollback also failed."
        raise ReplayStateTrainingError(message) from exc
    finally:
        if transaction is not None and transaction.exists() and not rollback_failed:
            try:
                shutil.rmtree(transaction)
            except OSError as exc:
                if installed_new:
                    raise ReplayStateTrainingError(
                        "Training set was built but temporary output cleanup failed."
                    ) from exc


def _reject_unexpected_output_entries(output_dir: Path) -> None:
    try:
        unexpected = sorted(
            path.name
            for path in output_dir.iterdir()
            if path.name not in OUTPUT_FILENAMES
        )
    except OSError as exc:
        raise ReplayStateTrainingError(
            "Cannot inspect the replay state training output directory."
        ) from exc
    if unexpected:
        raise ReplayStateTrainingError(
            "Output directory contains unexpected entries; refusing to replace it: "
            f"{', '.join(unexpected)}."
        )


def _write_durable(path: Path, content: bytes) -> None:
    with path.open("wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
