"""Offline leave-one-match-out economy regression from historical Team POV.

Each observation-step horizon and target is evaluated separately. Only the
builder's FEATURES_AT_T enters predictors; targets are retrospective oracle
outcomes. No production models or dataset artifacts are written.
"""

import argparse
from collections import Counter
from collections.abc import Sequence
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys
import warnings

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.impute import MissingIndicator, SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.model_selection import LeaveOneGroupOut
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from app.replay_pov_alignment import TEAMS, _integer
from tools.build_pov_economy_transitions import (
    FEATURES_AT_T, FUTURE_TARGETS, HORIZONS, ROOT, SCOPE,
    _decimal, _features, _identity, _interval, _read_csv, COLUMNS,
)


DEFAULT_INPUT = ROOT / "tools/replay_probe/output/pov-economy-transitions-four-matches.csv"
NUMERIC = tuple(i for i, name in enumerate(FEATURES_AT_T) if not name.endswith("_status"))
CATEGORICAL = tuple(i for i, name in enumerate(FEATURES_AT_T) if name.endswith("_status"))
MODELS = ("ZERO_CHANGE", "TRAIN_MEAN", "RIDGE")
LIMITATION = (
    "Only four independent match groups. Paired team rows and overlapping "
    "transitions are correlated; team rows are not independent matches. "
    "Pooled OOF metrics are row-weighted and cannot establish generalization. "
    "Horizons count observations, not fixed 60/300-second windows. "
    "Historical Team POV does not establish LIVE availability."
)


@dataclass(frozen=True)
class Observation:
    match_id: int
    economy_replay_tick: int
    pov_team: str
    horizon_steps: int
    features: tuple
    targets: tuple[float, float]


def load_observations(path: Path) -> list[Observation]:
    """Validate paired identity/causality; never pass metadata to the estimator."""
    frames, lineages, replay_matches = {}, {}, {}
    for row in _read_csv(path, COLUMNS):
        mid, sha = _identity(row["match_id"], row["replay_sha256"], lineages, replay_matches)
        team, horizon = row["pov_team"], _integer(row["horizon_steps"])
        if team not in TEAMS or horizon not in HORIZONS or row["scope"] != SCOPE:
            raise ValueError("Invalid POV team, horizon or historical scope.")
        time, tick = _decimal(row["economy_game_time_seconds"]), _integer(row["economy_replay_tick"])
        pov_time, pov_tick = _decimal(row["pov_game_time_seconds"]), _integer(row["pov_replay_tick"])
        future_time, future_tick = _decimal(row["future_source_game_time_seconds"]), _integer(row["future_replay_tick"])
        age, interval = _decimal(row["pov_age_seconds"]), _decimal(row["actual_horizon_seconds"])
        if (pov_time > time or pov_tick > tick or future_time <= time or future_tick <= tick
                or age != _interval(time, pov_time) or interval != _interval(future_time, time)):
            raise ValueError("Invalid transition time/tick or future POV observation.")
        validated = _features(row)
        features = tuple(validated[name] if i in CATEGORICAL else (
            np.nan if validated[name] == "" else float(validated[name])
        ) for i, name in enumerate(FEATURES_AT_T))
        if any(not np.isfinite(features[i]) for i in NUMERIC if not np.isnan(features[i])):
            raise ValueError("Non-finite numeric POV feature.")
        try:
            targets = tuple(Decimal(row[name]) for name in FUTURE_TARGETS)
        except InvalidOperation as error:
            raise ValueError("Targets must be finite numeric changes.") from error
        if any(not value.is_finite() or not np.isfinite(float(value)) for value in targets):
            raise ValueError("Targets must be finite numeric changes.")
        observation = Observation(int(mid), tick, team, horizon, features, tuple(map(float, targets)))
        metadata = (sha, time, pov_time, pov_tick, age, future_time, future_tick, interval)
        frame = frames.setdefault((int(mid), tick, horizon), dict(metadata=metadata, teams={}))
        if frame["metadata"] != metadata or team in frame["teams"]:
            raise ValueError("Duplicate transition identity or inconsistent paired metadata.")
        frame["teams"][team] = (observation, targets)
    observations = []
    for frame in (frames[key] for key in sorted(frames)):
        if set(frame["teams"]) != set(TEAMS):
            raise ValueError("Each transition must contain paired Radiant/Dire rows.")
        radiant, dire = (frame["teams"][team][1] for team in TEAMS)
        if any(r != d.copy_negate() for r, d in zip(radiant, dire, strict=True)):
            raise ValueError("Paired own-team economic targets must have opposite signs.")
        observations.extend(frame["teams"][team][0] for team in TEAMS)
    return observations


def make_estimator() -> Pipeline:
    # Missing indicators cover ALL numeric fields, including NULLs first seen
    # in test. All-missing train value columns are dropped, not filled with zero.
    numeric = Pipeline([
        ("impute", FeatureUnion([
            ("values", SimpleImputer(strategy="median", keep_empty_features=False)),
            ("missing", MissingIndicator(features="all", error_on_new=False)),
        ])),
        ("scale", StandardScaler()),
    ])
    return Pipeline([
        ("features", ColumnTransformer([
            ("numeric", numeric, list(NUMERIC)),
            ("statuses", OneHotEncoder(handle_unknown="ignore", sparse_output=False), list(CATEGORICAL)),
        ], remainder="drop")),
        ("model", Ridge(alpha=1.0, solver="svd", fit_intercept=True)),
    ])


def scores(targets, predictions: dict) -> dict:
    metrics = {name: dict(mae=float(mean_absolute_error(targets, values)),
                          rmse=float(np.sqrt(mean_squared_error(targets, values))))
               for name, values in predictions.items()}
    return dict(
        metrics=metrics,
        ridge_improvement={baseline: {metric: metrics[baseline][metric] - metrics["RIDGE"][metric]
                                     for metric in ("mae", "rmse")}
                           for baseline in ("ZERO_CHANGE", "TRAIN_MEAN")},
    )


def evaluate(observations: Sequence[Observation]) -> dict:
    matches = {row.match_id for row in observations}
    if len(matches) != 4:
        raise ValueError("This experiment requires exactly four independent matches.")
    experiments, diagnostics = [], Counter()
    for horizon in HORIZONS:
        selected = [row for row in observations if row.horizon_steps == horizon]
        if {row.match_id for row in selected} != matches:
            raise ValueError(f"All four matches need rows for horizon_steps={horizon}.")
        x = np.asarray([row.features for row in selected], dtype=object)
        groups = np.asarray([row.match_id for row in selected])
        for target_index, target in enumerate(FUTURE_TARGETS):
            y = np.asarray([row.targets[target_index] for row in selected], dtype=float)
            predictions = {name: np.full(len(y), np.nan) for name in MODELS}
            seen, folds = np.zeros(len(y), dtype=int), []
            for train, test in LeaveOneGroupOut().split(x, y, groups):
                held_out = int(groups[test[0]])
                estimator = make_estimator()
                with warnings.catch_warnings(record=True) as notices:
                    warnings.simplefilter("always")
                    estimator.fit(x[train], y[train])
                    predictions["RIDGE"][test] = estimator.predict(x[test])
                for notice in notices:
                    diagnostics[f"{notice.category.__name__}: {notice.message}"] += 1
                train_mean = float(y[train].mean())
                predictions["ZERO_CHANGE"][test] = 0.0
                predictions["TRAIN_MEAN"][test] = train_mean
                if any(not np.isfinite(values[test]).all() for values in predictions.values()):
                    raise ValueError(f"Non-finite OOF predictions for {held_out}.")
                seen[test] += 1
                folds.append(dict(
                    match_id=held_out, train_matches=sorted(int(mid) for mid in set(groups[train])),
                    train_rows=len(train), test_rows=len(test), train_mean=train_mean,
                    **scores(y[test], {name: values[test] for name, values in predictions.items()}),
                ))
            if not np.all(seen == 1):
                raise ValueError("Every observation must receive exactly one OOF prediction.")
            experiments.append(dict(
                horizon_steps=horizon, target=target, rows=len(y), folds=folds,
                missing_values={FEATURES_AT_T[i]: int(np.isnan(np.asarray(x[:, i], dtype=float)).sum())
                                for i in NUMERIC},
                oof=scores(y, predictions),
            ))
    return dict(rows=len(observations), matches=len(matches), experiments=experiments, diagnostics=dict(diagnostics))


def _print_scores(label: str, count: int, result: dict) -> None:
    metrics = " | ".join(f"{result['metrics'][name]['mae']:.6f} {result['metrics'][name]['rmse']:.6f}"
                         for name in MODELS)
    changes = " | ".join(f"{values['mae']:+.6f} {values['rmse']:+.6f}"
                         for values in result["ridge_improvement"].values())
    print(f"{label:12s} {count:4d} | {metrics} | {changes}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-path", type=Path, default=DEFAULT_INPUT)
    args = parser.parse_args(argv)
    try:
        report = evaluate(load_observations(args.csv_path))
        print(f"Source: {args.csv_path}; rows={report['rows']}; matches={report['matches']}.")
        print("FEATURES_AT_T: " + ", ".join(FEATURES_AT_T))
        print("Train-only median imputation, all-numeric missing indicators, scaling and status one-hot encoding.")
        print("Fixed Ridge: alpha=1.0, solver=svd, fit_intercept=True; no random components or tuning.")
        print("Targets: positive means improvement of the observing team's advantage.")
        print("Improvement = baseline error minus Ridge error; positive is better, negative is worse.")
        for experiment in report["experiments"]:
            print(f"\nhorizon_steps={experiment['horizon_steps']}; target={experiment['target']}")
            print(f"NULL numeric features: {experiment['missing_values']}")
            print("match_id        n | ZERO_CHANGE MAE RMSE | TRAIN_MEAN MAE RMSE | RIDGE MAE RMSE | improvement vs ZERO MAE RMSE | vs MEAN MAE RMSE")
            for fold in experiment["folds"]:
                _print_scores(str(fold["match_id"]), fold["test_rows"], fold)
            _print_scores("POOLED OOF", experiment["rows"], experiment["oof"])
        print("\nWith complete opposite-target team pairs, each train-only target mean is zero.")
        if report["diagnostics"]:
            print(f"Estimator diagnostics: {report['diagnostics']}")
        print("WARNING: " + LIMITATION)
        print("No model or dataset artifacts were saved.")
        return 0
    except (OSError, ValueError, csv.Error) as error:
        print(f"Offline POV economy evaluation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
