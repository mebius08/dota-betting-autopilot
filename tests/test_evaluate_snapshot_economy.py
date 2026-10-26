from dataclasses import replace
from decimal import Decimal, localcontext
import csv

import numpy as np
import pytest

from tools import evaluate_snapshot_economy as cli


MATCHES = (10, 20, 30, 40)


def feature(mid=10, time="50.0334", tick=1700, view="HISTORICAL_GLOBAL", sha=None):
    row = dict.fromkeys(cli.schema.COLUMNS, "")
    team = cli.schema.VIEWS[view]
    row.update(source="replay_clarity", source_match_id=str(mid), replay_sha256=sha or "abcd"[mid // 10 - 1] * 64,
               requested_game_time_seconds=time, requested_replay_tick=str(tick), view=view,
               provenance="HISTORICAL_TEAM_POV" if team else "HISTORICAL_ORACLE", source_statuses="{}")
    for name in cli.schema.FEATURES:
        row[f"feature_status_{name}"] = "NOT_IN_VIEW"
    for channel, origin in cli.schema.CHANNEL_ORIGINS.items():
        row[f"{channel}_status"] = "CHANNEL_ABSENT"
        if channel in cli.CHANNELS_BY_VIEW[view]:
            age = Decimal(10) if team is None else Decimal(0)
            row.update({f"{channel}_status": "AVAILABLE", f"{channel}_origin": origin,
                        f"{channel}_actual_game_time_seconds": str(cli.anchors_source._interval(Decimal(time), age)),
                        f"{channel}_replay_tick": str(tick - 17 if team is None else tick),
                        f"{channel}_age_seconds": str(age)})
    for index, name in enumerate(cli.schema.FEATURES_BY_VIEW[view]):
        row[name] = str(mid * 10 + tick / 100 + index) if not name.endswith("_count") else str(mid // 10)
        row[f"feature_status_{name}"] = "AVAILABLE"
    if mid == 10 and view == "HISTORICAL_RADIANT_POV":
        row["nearest_visible_enemy_distance"] = ""
        row["feature_status_nearest_visible_enemy_distance"] = "MISSING_VALUE"
    return row


def anchor(mid=10, time="60.0002", tick=2000, horizon=1):
    row = dict.fromkeys(cli.anchors_source.ECONOMY_COLUMNS, "0")
    with localcontext() as context:
        context.prec = 100
        future = Decimal(time) + Decimal("60.0003") * horizon
    row.update(
        source="replay_clarity", source_match_id=str(mid), replay_sha256="abcd"[mid // 10 - 1] * 64,
        provenance="HISTORICAL_ORACLE", source_game_time_seconds=time, replay_tick=str(tick),
        future_source_game_time_seconds=str(future), future_replay_tick=str(tick + horizon * 4000),
        horizon_steps=str(horizon), actual_horizon_seconds=str(cli.anchors_source._interval(future, Decimal(time))),
        future_delta_net_worth_lead=str((mid * 5 + tick // 100) * horizon),
        future_delta_xp_lead=str((mid * 2 - tick // 100) * horizon),
    )
    return row


def save(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def paths(tmp_path, features=None, transitions=None):
    if features is None:
        features = [feature(mid, time, tick, view) for mid in MATCHES
                    for time, tick in (("50.0334", 1700), ("110.0334", 4700)) for view in cli.schema.VIEWS]
    if transitions is None:
        transitions = [anchor(mid, time, tick, h) for mid in MATCHES
                       for time, tick in (("60.0002", 2000), ("120.0004", 5000)) for h in (1, 5)]
    return (save(tmp_path / "features.csv", cli.schema.COLUMNS, features),
            save(tmp_path / "transitions.csv", cli.anchors_source.ECONOMY_COLUMNS, transitions))


def test_matching_target_orientation_exact_ages_and_numeric_whitelists(tmp_path):
    examples, report = cli.load_examples(*paths(tmp_path))
    assert len(examples) == 48 and report["anchors"] == 16 and report["excluded"] == []
    assert report["match_ids"] == list(MATCHES)
    for example in examples:
        assert len(example.features) == len(cli.schema.FEATURES_BY_VIEW[example.view])
        assert example.feature_time <= example.anchor_time and example.feature_tick <= example.anchor_tick
        sign = -1 if example.view == "HISTORICAL_DIRE_POV" else 1
        assert example.targets == (
            sign * (example.match_id * 5 + example.anchor_tick // 100) * example.horizon_steps,
            sign * (example.match_id * 2 - example.anchor_tick // 100) * example.horizon_steps,
        )
        assert example.snapshot_age == cli.anchors_source._interval(example.anchor_time, example.feature_time)
        for channel, original, effective in example.observation_ages:
            assert channel in cli.CHANNELS_BY_VIEW[example.view]
            assert original == (10 if example.view == "HISTORICAL_GLOBAL" else 0)
            assert effective == original + example.snapshot_age
    for view in cli.schema.VIEWS:
        coverage = report["coverage"][view][1]
        assert coverage["matched"] == 8
        assert coverage["snapshot_age"]["median_age_seconds"] == Decimal("9.9669")
        assert not set(cli.schema.FEATURES_BY_VIEW[view]) & set(cli.schema.METADATA)


@pytest.mark.parametrize(("time", "tick", "matched"), [
    ("60.0002000000000000000000000000000000000001", 1999, False),
    ("60.0", 2001, False),
    ("60.0002", 2000, True),
])
def test_both_as_of_boundaries_are_inclusive_and_never_approximate(tmp_path, time, tick, matched):
    examples, report = cli.load_examples(*paths(tmp_path, [feature(time=time, tick=tick)], [anchor()]))
    selected = [r for r in examples if r.view == "HISTORICAL_GLOBAL"]
    assert len(selected) == int(matched)
    coverage = report["coverage"]["HISTORICAL_GLOBAL"][1]
    assert coverage["excluded"] == int(not matched)
    if not matched:
        excluded = next(r for r in report["excluded"] if r["view"] == "HISTORICAL_GLOBAL")
        assert excluded["reason"] == "NO_PRECEDING_SNAPSHOT_FEATURE" and excluded["anchor_tick"] == 2000


def test_selects_latest_admissible_frame_instead_of_future_frame(tmp_path):
    features = [feature(), feature(time="60.0003", tick=1999), feature(time="60.0", tick=2001)]
    examples, _ = cli.load_examples(*paths(tmp_path, features, [anchor()]))
    assert examples[0].feature_time == Decimal("50.0334") and examples[0].feature_tick == 1700


def test_foreign_match_is_not_a_fallback_and_missing_view_is_reported(tmp_path):
    examples, report = cli.load_examples(*paths(tmp_path, [feature(mid=20)], [anchor()]))
    assert examples == [] and len(report["excluded"]) == 3
    assert all(row["match_id"] == 10 for row in report["excluded"])


@pytest.mark.parametrize("problem", ["sha", "scope", "oracle_feature", "oracle_metadata", "future_observation_time", "future_observation_tick"])
def test_identity_information_scope_and_observation_causality_are_enforced(tmp_path, problem):
    row = feature(view="HISTORICAL_RADIANT_POV")
    if problem == "sha":
        row["replay_sha256"] = "e" * 64
    elif problem == "scope":
        row["provenance"] = "HISTORICAL_ORACLE"
    elif problem == "oracle_feature":
        row["radiant_net_worth"] = "999999"
    elif problem == "oracle_metadata":
        row["economy_status"] = "AVAILABLE"
    elif problem == "future_observation_time":
        row["pov_actual_game_time_seconds"] = "50.03340000000000000000000000000001"
    else:
        row["pov_replay_tick"] = "1701"
    with pytest.raises(ValueError):
        cli.load_examples(*paths(tmp_path, [row], [anchor()]))


def test_duplicate_features_and_anchors_are_errors(tmp_path):
    with pytest.raises(ValueError, match="Duplicate"):
        cli.load_examples(*paths(tmp_path, [feature(), feature()], [anchor()]))
    with pytest.raises(ValueError, match="Duplicate"):
        cli.load_examples(*paths(tmp_path, [feature()], [anchor(), anchor()]))


def test_future_target_and_economy_values_cannot_enter_features(tmp_path):
    features = [feature(view=view) for view in cli.schema.VIEWS]
    transitions = [anchor()]
    before, _ = cli.load_examples(*paths(tmp_path, features, transitions))
    transitions[0].update(future_delta_net_worth_lead="999999", future_delta_xp_lead="-99999", radiant_net_worth="8888888")
    for row in features:
        row["source_statuses"] = '{"target":123,"future_label":456}'  # Metadata is never flattened.
    after, _ = cli.load_examples(*paths(tmp_path, features, transitions))
    for a, b in zip(before, after, strict=True):
        np.testing.assert_allclose(a.features, b.features, equal_nan=True)
        assert a.targets != b.targets
    path = tmp_path / "features.csv"
    text = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([text[0] + ",target", *[line + ",1" for line in text[1:]]]), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        cli.load_examples(path, tmp_path / "transitions.csv")


def test_decimal_matching_and_effective_observation_age_are_exact(tmp_path):
    time = "60.0002000000000000000000000000000000000001"
    anchor_time = "60.0002000000000000000000000000000000000002"
    inputs = paths(tmp_path, [feature(time=time)], [anchor(time=anchor_time)])
    with localcontext() as context:
        context.prec = 6
        examples, report = cli.load_examples(*inputs)
    example = examples[0]
    assert example.snapshot_age == Decimal("1e-40")
    assert example.observation_ages[0][1:] == (Decimal(10), Decimal("10.0000000000000000000000000000000000000001"))
    assert report["coverage"]["HISTORICAL_GLOBAL"][1]["snapshot_age"]["median_age_seconds"] == Decimal("1e-40")


def test_train_only_imputation_scaling_and_missing_indicators():
    train = np.asarray([[0.0, 0.0], [10.0, np.nan], [30.0, 2.0], [50.0, 3.0]])
    model = cli.make_estimator().fit(train, [1.0, 2.0, 3.0, 4.0])
    union = model.named_steps["impute"]
    imputer = dict(union.transformer_list)["values"]
    np.testing.assert_array_equal(imputer.statistics_, [20.0, 2.0])
    statistics = imputer.statistics_.copy()
    test = np.asarray([[1e12, np.nan], [1e12, 0.0]])
    filled = union.transform(test)
    assert filled[0, 1] == 2.0 and filled[1, 1] == 0.0
    assert filled[0, 3] == 1 and filled[1, 3] == 0
    assert np.isfinite(model.predict(test)).all()
    np.testing.assert_array_equal(imputer.statistics_, statistics)
    np.testing.assert_allclose(model.named_steps["scale"].mean_, union.transform(train).mean(axis=0))


def test_leave_one_match_out_isolates_all_views_horizons_targets_and_train_means(tmp_path, monkeypatch):
    examples, alignment = cli.load_examples(*paths(tmp_path))
    calls = []

    class Recorder:
        def fit(self, x, y):
            calls.append((x.copy(), y.copy()))
            self.mean = y.mean()
            return self

        def predict(self, x):
            return np.full(len(x), self.mean)

    monkeypatch.setattr(cli, "make_estimator", Recorder)
    report = cli.evaluate(examples, alignment["match_ids"])
    assert len(report["experiments"]) == 12 and len(calls) == 48
    for experiment_index, experiment in enumerate(report["experiments"]):
        selected = [r for r in examples if r.view == experiment["view"] and r.horizon_steps == experiment["horizon_steps"]]
        target = cli.TARGETS.index(experiment["target"])
        assert sum(f["test_rows"] for f in experiment["folds"]) == experiment["rows"] == len(selected)
        for fold_index, fold in enumerate(experiment["folds"]):
            train = [r for r in selected if r.match_id != fold["match_id"]]
            actual_x, actual_y = calls[experiment_index * 4 + fold_index]
            np.testing.assert_allclose(actual_x, [r.features for r in train], equal_nan=True)
            np.testing.assert_array_equal(actual_y, [r.targets[target] for r in train])
            assert set(fold["train_matches"]) == set(MATCHES) - {fold["match_id"]}
            assert fold["train_mean"] == np.mean(actual_y)
            assert fold["train_rows"] == len(train)
            assert actual_x.shape[1] == len(cli.schema.FEATURES_BY_VIEW[experiment["view"]])
        assert experiment["oof"]["metrics"]["RIDGE"] == experiment["oof"]["metrics"]["TRAIN_MEAN"]


def test_held_out_targets_never_change_held_out_predictions(tmp_path, monkeypatch):
    examples, alignment = cli.load_examples(*paths(tmp_path))
    factory, predictions = cli.make_estimator, []

    class Recorder:
        def __init__(self):
            self.model = factory()

        def fit(self, x, y):
            self.model.fit(x, y)
            return self

        def predict(self, x):
            values = self.model.predict(x)
            predictions.append(values.copy())
            return values

    monkeypatch.setattr(cli, "make_estimator", Recorder)
    cli.evaluate(examples, alignment["match_ids"])
    before = list(predictions)
    predictions.clear()
    changed = [replace(row, targets=tuple(value * 100 for value in row.targets)) if row.match_id == 10 else row for row in examples]
    cli.evaluate(changed, alignment["match_ids"])
    for index in range(0, 48, 4):
        np.testing.assert_array_equal(predictions[index], before[index])


def test_real_pipeline_oof_metrics_are_finite_and_row_weighted(tmp_path):
    examples, alignment = cli.load_examples(*paths(tmp_path))
    report = cli.evaluate(examples, alignment["match_ids"])
    for experiment in report["experiments"]:
        for name in cli.MODELS:
            folds, n = experiment["folds"], experiment["rows"]
            mae = sum(f["test_rows"] * f["metrics"][name]["mae"] for f in folds) / n
            rmse = np.sqrt(sum(f["test_rows"] * f["metrics"][name]["rmse"] ** 2 for f in folds) / n)
            assert experiment["oof"]["metrics"][name] == pytest.approx(dict(mae=mae, rmse=rmse))
            assert all(np.isfinite(value) and value >= 0 for value in experiment["oof"]["metrics"][name].values())


def test_nonfinite_predictions_are_an_error(tmp_path, monkeypatch):
    examples, alignment = cli.load_examples(*paths(tmp_path))

    class Estimator:
        def fit(self, x, y):
            return self

        def predict(self, x):
            return np.full(len(x), np.inf)

    monkeypatch.setattr(cli, "make_estimator", Estimator)
    with pytest.raises(ValueError, match="Non-finite OOF"):
        cli.evaluate(examples, alignment["match_ids"])


def test_nonfinite_metrics_are_not_reported_as_valid(monkeypatch):
    monkeypatch.setattr(cli, "scores", lambda *args: {"metrics": {"RIDGE": {"rmse": np.inf}}})
    with pytest.raises(ValueError, match="Non-finite evaluation metrics"):
        cli._checked_scores([1.0], {"RIDGE": [1.0]})


def test_missing_match_cannot_turn_into_a_three_match_experiment(tmp_path):
    examples, alignment = cli.load_examples(*paths(tmp_path))
    with pytest.raises(ValueError, match="all four matches"):
        cli.evaluate([r for r in examples if not (r.view == "HISTORICAL_DIRE_POV" and r.match_id == 40)], alignment["match_ids"])


def test_cli_reports_all_experiments_and_creates_no_artifacts(tmp_path, capsys):
    features, transitions = paths(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    assert cli.main(["--features-csv", str(features), "--transitions-csv", str(transitions)]) == 0
    output = capsys.readouterr().out
    assert "Anchors=16; matched anchor/view pairs=48; excluded=0" in output
    assert output.count("POOLED OOF") == 12
    assert "original observation age" in output and "effective age at anchor" in output
    assert "GLOBAL is HISTORICAL_ORACLE" in output and "Only four independent match groups" in output
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
