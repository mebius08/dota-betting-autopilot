"""Offline leave-one-match-out baseline for own-team death in (t, t+30s]."""

import argparse
from collections import Counter
from collections.abc import Sequence
import csv
from dataclasses import dataclass
from decimal import Decimal
import os
from pathlib import Path
import sys
import tempfile
import warnings

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import MissingIndicator, SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss
from sklearn.model_selection import LeaveOneGroupOut
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import StandardScaler

from app.replay_pov_alignment import TEAMS, _features, _integer, _lineage
from app.replay_pov_corpus import COLUMNS, _decimal, _labels


DEFAULT_INPUT = Path(__file__).resolve().parents[1] / (
    "tools/replay_probe/output/pov-death-corpus-four-matches/pov-death-corpus.csv"
)
FEATURES = (
    "visible_enemy_count", "visible_enemy_with_position_count", "hidden_enemy_count",
    "own_team_spread", "nearest_visible_enemy_distance",
)
RANDOM_STATE = 42
OOF_COLUMNS = (
    "match_id", "replay_tick", "game_time_seconds", "pov_team", "horizon_seconds",
    "constant_probability", "logistic_probability", "target",
)
LIMITATION = (
    "Four matches, paired team views and overlapping 30-second windows do not "
    "provide a reliable estimate of generalization. Pooled OOF metrics are row-weighted, "
    "not independent-trial evidence. Raw clocks/visibility and death-event boundaries "
    "remain upstream guarantees; CSV validation does not re-prove them."
)


@dataclass(frozen=True)
class Observation:
    match_id: int
    replay_tick: int
    pov_team: str
    actual_time: Decimal
    status: str
    target: int | None
    features: tuple[float, ...]


def load_observations(path: Path) -> list[Observation]:
    frames, lineages, replays = {}, {}, {}
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(COLUMNS):
            raise ValueError("Expected the existing combined POV death corpus schema.")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("Malformed corpus row.")
            mid, sha = _lineage(row)
            if lineages.setdefault(mid, sha) != sha or replays.setdefault(sha, mid) != mid:
                raise ValueError("Conflicting match/replay identity.")
            tick, team = _integer(row["replay_tick"]), row["pov_team"]
            time = _decimal(row["game_time_seconds"])
            if team not in TEAMS:
                raise ValueError("Invalid POV team.")
            features = _features(row)
            labels = _labels(row, 30)
            status, (own, _, _), _, _ = labels
            observation = Observation(
                mid, tick, team, time, status,
                int(own > 0) if status == "VALID" else None,
                tuple(np.nan if features[name] is None else float(features[name])
                      for name in FEATURES),
            )
            frame = frames.setdefault((mid, tick), {"time": time, "teams": {}})
            if frame["time"] != time or team in frame["teams"]:
                raise ValueError("Duplicate match/tick/team or inconsistent actual time.")
            frame["teams"][team] = (observation, labels)
    if len(lineages) != 4:
        raise ValueError("This experiment requires exactly four independent matches.")
    observations, previous = [], {}
    for (mid, tick), frame in sorted(frames.items(), key=lambda item: (item[0][0], item[1]["time"])):
        if set(frame["teams"]) != set(TEAMS):
            raise ValueError("Each frame must have exactly one Radiant and one Dire POV.")
        old_time, old_tick = previous.get(mid, (Decimal(-1), -1))
        if frame["time"] <= old_time or tick <= old_tick:
            raise ValueError("Actual frame times and ticks must strictly increase.")
        previous[mid] = frame["time"], tick
        radiant, dire = (frame["teams"][team][1] for team in TEAMS)
        if radiant[0] != dire[0] or radiant[2:] != dire[2:]:
            raise ValueError("Paired label statuses/ambiguity audits disagree.")
        if radiant[0] == "VALID" and radiant[1] != (dire[1][1], dire[1][0], dire[1][2]):
            raise ValueError("Paired own/enemy death targets disagree.")
        observations.extend(frame["teams"][team][0] for team in TEAMS)
    return observations


def make_estimator() -> Pipeline:
    # Empty train columns are dropped by the median imputer, never assigned a
    # fictitious physical zero. All five missing indicators always exist, even
    # when a feature is first missing in a held-out match.
    return Pipeline([
        ("features", FeatureUnion([
            ("values", SimpleImputer(strategy="median", keep_empty_features=False)),
            ("missing", MissingIndicator(features="all", error_on_new=False)),
        ])),
        ("scale", StandardScaler()),
        ("model", LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000,
                                     random_state=RANDOM_STATE)),
    ])


def scores(targets, probabilities) -> dict:
    return {
        "brier": float(brier_score_loss(targets, probabilities)),
        "log_loss": float(log_loss(targets, probabilities, labels=[0, 1])),
    }


def evaluate(observations: Sequence[Observation]) -> dict:
    matches = sorted({row.match_id for row in observations})
    valid = [row for row in observations if row.status == "VALID"]
    if len(matches) != 4 or {row.match_id for row in valid} != set(matches):
        raise ValueError("All four matches must contain VALID 30-second targets.")
    x = np.asarray([row.features for row in valid], dtype=float)
    y = np.asarray([row.target for row in valid], dtype=int)
    groups = np.asarray([row.match_id for row in valid])
    constant_oof, logistic_oof = np.full(len(y), np.nan), np.full(len(y), np.nan)
    seen = np.zeros(len(y), dtype=int)
    folds, diagnostics = [], Counter()
    for train, test in LeaveOneGroupOut().split(x, y, groups):
        held_out = int(groups[test[0]])
        if len(np.unique(y[train])) != 2:
            raise ValueError(f"Training fold for {held_out} needs both target classes.")
        estimator = make_estimator()
        with warnings.catch_warnings(record=True) as notices:
            warnings.simplefilter("always")
            estimator.fit(x[train], y[train])
            logistic_oof[test] = estimator.predict_proba(x[test])[:, 1]
        for notice in notices:
            if issubclass(notice.category, ConvergenceWarning):
                raise ValueError(f"LogisticRegression did not converge for {held_out}.")
            diagnostics[f"{notice.category.__name__}: {notice.message}"] += 1
        prior = float(y[train].mean())
        constant_oof[test] = prior
        if not np.isfinite(logistic_oof[test]).all() or np.any(
            (logistic_oof[test] < 0) | (logistic_oof[test] > 1)
        ):
            raise ValueError(f"Invalid OOF model probabilities for {held_out}.")
        seen[test] += 1
        statuses = Counter(row.status for row in observations if row.match_id == held_out)
        folds.append({
            "match_id": held_out,
            "train_matches": sorted(int(mid) for mid in np.unique(groups[train])),
            "train_rows": len(train), "train_positive_rate": prior,
            "test_rows": len(test), "positives": int(y[test].sum()),
            "positive_rate": float(y[test].mean()),
            "excluded_censored": statuses["CENSORED"],
            "excluded_ambiguous": statuses["AMBIGUOUS_TIME_BOUNDARY"],
            "constant": scores(y[test], constant_oof[test]),
            "logistic": scores(y[test], logistic_oof[test]),
        })
    if not np.all(seen == 1) or not np.isfinite(logistic_oof).all():
        raise ValueError("Incomplete or invalid out-of-fold predictions.")
    for probabilities in (constant_oof, logistic_oof):
        if not np.isfinite(probabilities).all() or np.any((probabilities < 0) | (probabilities > 1)):
            raise ValueError("OOF probabilities must be finite and within [0, 1].")
    ordered = sorted(range(len(valid)), key=lambda index: (
        valid[index].match_id, valid[index].actual_time, valid[index].replay_tick,
        TEAMS.index(valid[index].pov_team),
    ))
    oof_rows, identities = [], set()
    for index in ordered:
        row = valid[index]
        identity = (row.match_id, row.replay_tick, row.pov_team)
        if identity in identities:
            raise ValueError("Duplicate OOF match/tick/team identity.")
        identities.add(identity)
        oof_rows.append(dict(
            match_id=row.match_id, replay_tick=row.replay_tick,
            game_time_seconds=str(row.actual_time), pov_team=row.pov_team,
            horizon_seconds=30, constant_probability=float(constant_oof[index]),
            logistic_probability=float(logistic_oof[index]), target=int(y[index]),
        ))
    return {
        "folds": folds, "valid_rows": len(valid), "positive_rate": float(y.mean()),
        "excluded": dict(Counter(row.status for row in observations if row.status != "VALID")),
        "constant": scores(y, constant_oof), "logistic": scores(y, logistic_oof),
        "diagnostics": dict(diagnostics),
        "missing_values": {name: int(np.isnan(x[:, index]).sum())
                           for index, name in enumerate(FEATURES)},
        "oof_rows": oof_rows,
    }


def write_oof(path: Path, rows: Sequence[dict]) -> None:
    """Replace the explicit destination only after a complete, durable CSV write."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=OOF_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-path", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--out-oof", type=Path,
        help="Write OOF CSV atomically to this explicit path; parent must exist.",
    )
    args = parser.parse_args(argv)
    try:
        if args.out_oof is not None and args.out_oof.resolve() == args.csv_path.resolve():
            raise ValueError("OOF output must not overwrite the source corpus.")
        observations = load_observations(args.csv_path)
        report = evaluate(observations)
        if args.out_oof is not None:
            write_oof(args.out_oof, report["oof_rows"])
        print(f"Source: {args.csv_path}; rows={len(observations)}; matches=4.")
        print("Target: own_deaths_next_30s > 0 in (t, t+30s], not shared any_death_next_30s.")
        print("Features: " + ", ".join(FEATURES) + "; plus one missing indicator per feature.")
        print(f"VALID feature missingness: {report['missing_values']}.")
        print("Train-only median imputation and scaling; LogisticRegression C=1, lbfgs, max_iter=1000, random_state=42; no tuning.")
        print("match_id     test_rows positive_rate excluded_C excluded_A | constant Brier log_loss | logistic Brier log_loss | train_prior")
        for fold in report["folds"]:
            print(
                f"{fold['match_id']} {fold['test_rows']:9d} {fold['positive_rate']:.6f} "
                f"{fold['excluded_censored']:10d} {fold['excluded_ambiguous']:10d} | "
                f"{fold['constant']['brier']:.6f} {fold['constant']['log_loss']:.6f} | "
                f"{fold['logistic']['brier']:.6f} {fold['logistic']['log_loss']:.6f} | "
                f"{fold['train_positive_rate']:.6f}"
            )
        for model in ("constant", "logistic"):
            print(f"POOLED OOF {model.upper()}: n={report['valid_rows']}, positive_rate={report['positive_rate']:.6f}, "
                  f"Brier={report[model]['brier']:.6f}, log_loss={report[model]['log_loss']:.6f}.")
        print(f"Excluded by 30s status: {report['excluded']}.")
        if report["diagnostics"]:
            print(f"WARNING: estimator diagnostics: {report['diagnostics']}.")
        print("WARNING: " + LIMITATION)
        if args.out_oof is None:
            print("No model or dataset artifacts were saved.")
        else:
            print(f"OOF CSV: {args.out_oof}; rows={len(report['oof_rows'])}.")
            print("game_time_seconds is actual source time; target = own_deaths_next_30s > 0, for evaluation only.")
            print("No model artifacts were saved.")
        return 0
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Offline baseline failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
