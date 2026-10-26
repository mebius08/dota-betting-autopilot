"""Evaluate historical snapshot economy regressions without writing artifacts.

Only FEATURES_BY_VIEW enters predictors. Anchors, clocks, ages, provenance and
targets remain metadata/evaluation data. GLOBAL is a historical oracle, never
evidence of LIVE availability. Each view, step horizon and target is separate.
"""

import argparse
from collections import Counter, defaultdict
from collections.abc import Sequence
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys
import warnings

import numpy as np
from sklearn.impute import MissingIndicator, SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.model_selection import LeaveOneGroupOut
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import StandardScaler

from tools import build_pov_economy_transitions as anchors_source
from tools import extract_snapshot_features as schema
from tools.evaluate_pov_economy import LIMITATION, MODELS, _print_scores, scores
from tools.export_snapshot_timeline import _freshness


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEATURES = ROOT / "tools/replay_probe/output/snapshot-features-four-matches.csv"
DEFAULT_TRANSITIONS = ROOT / "tools/replay_probe/output/economy-transitions-four-matches.csv"
TARGETS = ("future_delta_net_worth_lead", "future_delta_xp_lead")
ANCHOR_TARGETS = ("future_delta_own_net_worth_lead", "future_delta_own_xp_lead")
HORIZONS = anchors_source.HORIZONS
CHANNELS_BY_VIEW = {view: ("pov", "pov_enemy_positions") if team else ("economy", "heroes", "spatial_global")
                    for view, team in schema.VIEWS.items()}
FEATURE_CHANNEL = dict(
    **dict.fromkeys(schema.GLOBAL_FEATURES[:6], "economy"),
    **dict.fromkeys(schema.GLOBAL_FEATURES[6:12], "heroes"),
    **dict.fromkeys(schema.GLOBAL_FEATURES[12:], "spatial_global"),
    **dict.fromkeys(schema.POV_FEATURES[:5], "pov"),
    **dict.fromkeys(schema.POV_FEATURES[5:], "pov_enemy_positions"),
)
MISSING_STATUSES = {"MISSING_VALUE", "MISSING_COMPONENT", "NO_CONFIRMED_LAST_SEEN",
                    "NO_PRECEDING_SNAPSHOT", "CHANNEL_ABSENT"}


@dataclass(frozen=True)
class FeatureFrame:
    time: Decimal
    tick: int
    features: tuple[float, ...]
    observations: tuple  # (channel, original observation time or None, original age or None)


@dataclass(frozen=True)
class Example:
    match_id: int
    view: str
    horizon_steps: int
    anchor_time: Decimal
    anchor_tick: int
    feature_time: Decimal
    feature_tick: int
    snapshot_age: Decimal
    observation_ages: tuple  # (channel, age at feature time, age at anchor time)
    features: tuple[float, ...]
    targets: tuple[float, float]


def _feature_frames(path, lineages, replay_matches):
    frames, clocks, seen = defaultdict(list), {}, set()
    for row in anchors_source._read_csv(path, schema.COLUMNS):
        mid, sha = anchors_source._identity(row["source_match_id"], row["replay_sha256"], lineages, replay_matches)
        view = row["view"]
        if view not in schema.VIEWS or row["source"] != "replay_clarity":
            raise ValueError("Invalid snapshot view/source.")
        team = schema.VIEWS[view]
        if row["provenance"] != ("HISTORICAL_TEAM_POV" if team else "HISTORICAL_ORACLE"):
            raise ValueError("Snapshot provenance contradicts its view.")
        time = anchors_source._decimal(row["requested_game_time_seconds"])
        tick = anchors_source._integer(row["requested_replay_tick"])
        identity = (mid, tick, view)
        if identity in seen or clocks.setdefault((mid, tick), time) != time:
            raise ValueError("Duplicate snapshot identity or inconsistent frame time.")
        seen.add(identity)
        observations, channel_statuses = [], {}
        for channel, origin in schema.CHANNEL_ORIGINS.items():
            status = row[f"{channel}_status"]
            values = [row[f"{channel}_{field}"] for field in ("actual_game_time_seconds", "replay_tick", "age_seconds")]
            if team and channel not in CHANNELS_BY_VIEW[view] and status != "CHANNEL_ABSENT":
                raise ValueError("Oracle observation metadata is forbidden in Team POV.")
            if status == "AVAILABLE":
                observed = anchors_source._decimal(values[0])
                observed_tick = anchors_source._integer(values[1])
                age = anchors_source._decimal(values[2])
                if (observed > time or observed_tick > tick or age != anchors_source._interval(time, observed)
                        or row[f"{channel}_origin"] != origin):
                    raise ValueError("Observation time/tick/age/provenance violates its snapshot boundary.")
            elif status in ("NO_PRECEDING_SNAPSHOT", "CHANNEL_ABSENT"):
                if any(values) or row[f"{channel}_origin"] != (origin if status == "NO_PRECEDING_SNAPSHOT" else ""):
                    raise ValueError("Missing observation must have an empty envelope.")
                observed, age = None, None
            else:
                raise ValueError("Unknown observation availability status.")
            channel_statuses[channel] = status
            if channel in CHANNELS_BY_VIEW[view]:
                observations.append((channel, observed, age))
        features = []
        for feature in schema.FEATURES:
            raw, status = row[feature], row[f"feature_status_{feature}"]
            if feature not in schema.FEATURES_BY_VIEW[view]:
                if raw != "" or status != "NOT_IN_VIEW":
                    raise ValueError("Feature from another information scope is forbidden.")
                continue
            channel_status = channel_statuses[FEATURE_CHANNEL[feature]]
            if raw == "":
                if status not in MISSING_STATUSES or (channel_status != "AVAILABLE" and status != channel_status):
                    raise ValueError("NULL feature/status contradicts source availability.")
                value = np.nan
            else:
                if status != "AVAILABLE" or channel_status != "AVAILABLE":
                    raise ValueError("Populated feature lacks an available source.")
                value = float(schema._numeric(raw))
                if not np.isfinite(value):
                    raise ValueError("ML numeric features must be finite or NULL.")
            features.append(value)
        frames[mid, sha, view].append(FeatureFrame(time, tick, tuple(features), tuple(observations)))
    return {key: sorted(records, key=lambda frame: (frame.time, frame.tick)) for key, records in frames.items()}


def _ages(values):
    return _freshness([value for value in values if value is not None]) | {"missing": sum(value is None for value in values)}


def load_examples(features_path: Path, transitions_path: Path) -> tuple[list[Example], dict]:
    lineages, replay_matches = {}, {}
    anchors = anchors_source._anchors(anchors_source._read_csv(transitions_path, anchors_source.ECONOMY_COLUMNS),
                                     lineages, replay_matches)
    frames = _feature_frames(features_path, lineages, replay_matches)
    examples, excluded = [], []
    for anchor in anchors:
        mid, sha = anchor["match_id"], anchor["replay_sha256"]
        time, tick = anchor["economy_game_time_seconds"], anchor["economy_replay_tick"]
        for view, team in schema.VIEWS.items():
            frame = next((frame for frame in reversed(frames.get((mid, sha, view), []))
                          if frame.time <= time and frame.tick <= tick), None)
            if frame is None:
                excluded.append(dict(match_id=int(mid), view=view, horizon_steps=anchor["horizon_steps"],
                                     anchor_time=time, anchor_tick=tick, reason="NO_PRECEDING_SNAPSHOT_FEATURE"))
                continue
            sign = -1 if team == "DIRE" else 1
            examples.append(Example(
                int(mid), view, anchor["horizon_steps"], time, tick, frame.time, frame.tick,
                anchors_source._interval(time, frame.time),
                tuple((channel, age, anchors_source._interval(time, observed) if observed is not None else None)
                      for channel, observed, age in frame.observations),
                frame.features, tuple(float(sign * anchor[target]) for target in ANCHOR_TARGETS),
            ))
    coverage = {}
    for view in schema.VIEWS:
        coverage[view] = {}
        for horizon in HORIZONS:
            selected = [r for r in examples if r.view == view and r.horizon_steps == horizon]
            coverage[view][horizon] = dict(
                anchors=sum(a["horizon_steps"] == horizon for a in anchors), matched=len(selected),
                excluded=sum(r["view"] == view and r["horizon_steps"] == horizon for r in excluded),
                snapshot_age=_ages([r.snapshot_age for r in selected]),
                observation_ages={channel: dict(
                    original_at_snapshot=_ages([next(age for name, age, _ in r.observation_ages if name == channel) for r in selected]),
                    effective_at_anchor=_ages([next(age for name, _, age in r.observation_ages if name == channel) for r in selected]),
                ) for channel in CHANNELS_BY_VIEW[view]},
            )
    return examples, dict(anchors=len(anchors), matched=len(examples), excluded=excluded, coverage=coverage,
                          match_ids=sorted({int(a["match_id"]) for a in anchors}))


def make_estimator() -> Pipeline:
    return Pipeline([
        ("impute", FeatureUnion([
            ("values", SimpleImputer(strategy="median", keep_empty_features=False)),
            ("missing", MissingIndicator(features="all", error_on_new=False)),
        ])),
        ("scale", StandardScaler()),
        ("model", Ridge(alpha=1.0, solver="svd", fit_intercept=True)),
    ])


def _checked_scores(targets, predictions):
    result = scores(targets, predictions)
    if any(not np.isfinite(value) for metrics in result["metrics"].values() for value in metrics.values()):
        raise ValueError("Non-finite evaluation metrics.")
    return result


def evaluate(examples: Sequence[Example], match_ids: Sequence[int]) -> dict:
    if len(set(match_ids)) != 4:
        raise ValueError("Exactly four independent anchor matches are required.")
    experiments, diagnostics = [], Counter()
    for view in schema.VIEWS:
        for horizon in HORIZONS:
            selected = [r for r in examples if r.view == view and r.horizon_steps == horizon]
            if {r.match_id for r in selected} != set(match_ids):
                raise ValueError(f"Cannot evaluate {view}/h={horizon}: matched data is required in all four matches.")
            x = np.asarray([r.features for r in selected], dtype=float)
            groups = np.asarray([r.match_id for r in selected])
            if x.shape[1] != len(schema.FEATURES_BY_VIEW[view]):
                raise ValueError("Estimator matrix must contain only this view's numeric whitelist.")
            for index, target in enumerate(TARGETS):
                y = np.asarray([r.targets[index] for r in selected], dtype=float)
                if not np.isfinite(y).all() or np.isinf(x).any():
                    raise ValueError("Targets/features must be finite, except feature NULLs.")
                predictions = {model: np.full(len(y), np.nan) for model in MODELS}
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
                    mean = float(y[train].mean())
                    predictions["ZERO_CHANGE"][test], predictions["TRAIN_MEAN"][test] = 0.0, mean
                    if any(not np.isfinite(values[test]).all() for values in predictions.values()):
                        raise ValueError(f"Non-finite OOF prediction for {held_out}.")
                    seen[test] += 1
                    folds.append(dict(match_id=held_out, train_matches=sorted(int(mid) for mid in set(groups[train])),
                                      train_rows=len(train), test_rows=len(test), train_mean=mean,
                                      **_checked_scores(y[test], {model: values[test] for model, values in predictions.items()})))
                if not np.all(seen == 1):
                    raise ValueError("Every matched example must receive exactly one OOF prediction.")
                experiments.append(dict(view=view, horizon_steps=horizon, target=target, rows=len(y), folds=folds,
                                        oof=_checked_scores(y, predictions),
                                        null_features=dict(zip(schema.FEATURES_BY_VIEW[view], np.isnan(x).sum(axis=0).tolist(), strict=True))))
    return dict(experiments=experiments, diagnostics=dict(diagnostics))


def _age_text(summary):
    return "/".join(str(summary[key]) for key in ("min_age_seconds", "median_age_seconds", "max_age_seconds")) + f"; missing={summary['missing']}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features-csv", type=Path, default=DEFAULT_FEATURES)
    parser.add_argument("--transitions-csv", type=Path, default=DEFAULT_TRANSITIONS)
    args = parser.parse_args(argv)
    try:
        examples, alignment = load_examples(args.features_csv, args.transitions_csv)
        print(f"Anchors={alignment['anchors']}; matched anchor/view pairs={alignment['matched']}; excluded={len(alignment['excluded'])}.")
        for excluded in alignment["excluded"]:
            print(f"EXCLUDED: {excluded}")
        for view, horizons in alignment["coverage"].items():
            print(f"\n{view}; FEATURES_AT_T=" + ", ".join(schema.FEATURES_BY_VIEW[view]))
            for horizon, coverage in horizons.items():
                print(f"h={horizon}: matched={coverage['matched']}, excluded={coverage['excluded']}; snapshot age min/median/max: {_age_text(coverage['snapshot_age'])}")
                for channel, ages in coverage["observation_ages"].items():
                    print(f"  {channel}: original observation age at snapshot {_age_text(ages['original_at_snapshot'])}; effective age at anchor {_age_text(ages['effective_at_anchor'])}")
        report = evaluate(examples, alignment["match_ids"])
        print("\nTrain-only median imputation, missing indicators for ALL numeric features and scaling.")
        print("Fixed Ridge(alpha=1, solver=svd); ages and all metadata are NOT predictors. No tuning.")
        print("GLOBAL/RADIANT targets are Radiant-minus-Dire; DIRE targets are Dire-minus-Radiant.")
        print("Positive improvement = baseline error minus Ridge error.")
        for experiment in report["experiments"]:
            print(f"\n{experiment['view']} h={experiment['horizon_steps']} target={experiment['target']}; NULLs={experiment['null_features']}")
            print("match_id        n | ZERO_CHANGE MAE RMSE | TRAIN_MEAN MAE RMSE | RIDGE MAE RMSE | improvement vs ZERO MAE RMSE | vs MEAN MAE RMSE")
            for fold in experiment["folds"]:
                _print_scores(str(fold["match_id"]), fold["test_rows"], fold)
            _print_scores("POOLED OOF", experiment["rows"], experiment["oof"])
        if report["diagnostics"]:
            print(f"Estimator diagnostics: {report['diagnostics']}")
        print("WARNING: GLOBAL is HISTORICAL_ORACLE. " + LIMITATION)
        print("No datasets, predictions or model artifacts were saved.")
        return 0
    except (OSError, ValueError, TypeError, KeyError, InvalidOperation, csv.Error) as error:
        print(f"Snapshot economy evaluation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
