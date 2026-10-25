import csv
from dataclasses import replace
from decimal import Decimal
import warnings

import numpy as np
import pytest

from tools import evaluate_pov_economy as baseline
from tools.build_pov_economy_transitions import COLUMNS, FEATURES_AT_T, SCOPE


MATCHES = (10, 20, 30, 40)


def dataset():
    rows = []
    for index, mid in enumerate(MATCHES):
        for tick, time in ((2000, Decimal("60.0002")), (5100, Decimal("120.0004"))):
            for horizon in (1, 5):
                for team, sign in (("RADIANT", 1), ("DIRE", -1)):
                    visible = index + (team == "RADIANT")
                    row = dict.fromkeys(COLUMNS, "0")
                    row.update(
                        match_id=str(mid), replay_sha256="abcd"[index] * 64, pov_team=team,
                        economy_game_time_seconds=str(time), economy_replay_tick=str(tick),
                        pov_game_time_seconds=str(time - Decimal("9.9668")), pov_replay_tick=str(tick - 299),
                        pov_age_seconds="9.9668", horizon_steps=str(horizon),
                        future_source_game_time_seconds=str(time + Decimal("60.0003") * horizon),
                        future_replay_tick=str(tick + 1901 * horizon), actual_horizon_seconds=str(Decimal("60.0003") * horizon),
                        scope=SCOPE, own_alive_hero_count="5", own_valid_position_count="5",
                        own_team_spread=str(mid * 100 + tick / 100 + sign), own_team_spread_status="OK",
                        visible_enemy_count=str(visible), visible_enemy_with_position_count=str(visible),
                        hidden_enemy_count=str(5 - visible), nearest_visible_enemy_distance=str(200 + index * 50),
                        nearest_visible_enemy_status="OK",
                        future_delta_own_net_worth_lead=str(sign * (index * 100 + tick // 100) * horizon),
                        future_delta_own_xp_lead=str(sign * (index * 50 - tick // 100) * horizon),
                    )
                    if mid == 10 and team == "DIRE":
                        row.update(nearest_visible_enemy_distance="", nearest_visible_enemy_status="NO_VISIBLE_ENEMY_POSITION")
                    rows.append(row)
    return rows


def save(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.fixture
def source(tmp_path):
    return save(tmp_path / "pov-economy.csv", dataset())


def test_loader_uses_exact_builder_whitelist_and_preserves_missingness(source):
    observations = baseline.load_observations(source)
    assert len(observations) == 32
    assert baseline.FEATURES_AT_T is FEATURES_AT_T
    assert len(FEATURES_AT_T) == 11
    assert not {"match_id", "replay_sha256", "horizon_steps", "actual_horizon_seconds",
                "future_replay_tick", *baseline.FUTURE_TARGETS} & set(FEATURES_AT_T)
    for observation in observations:
        assert len(observation.features) == 11
        if observation.match_id == 10 and observation.pov_team == "DIRE":
            assert np.isnan(observation.features[FEATURES_AT_T.index("nearest_visible_enemy_distance")])
            assert observation.features[FEATURES_AT_T.index("nearest_visible_enemy_status")] == "NO_VISIBLE_ENEMY_POSITION"


@pytest.mark.parametrize("problem", ["duplicate", "unpaired", "sha", "target_sign", "target_nan", "target_text",
                                     "future_time", "future_tick", "scope"])
def test_corrupt_input_fails_explicitly(source, problem):
    rows = dataset()
    if problem == "duplicate":
        rows.append(dict(rows[0]))
    elif problem == "unpaired":
        rows.pop()
    elif problem == "sha":
        rows[0]["replay_sha256"] = "e" * 64
    elif problem == "target_sign":
        rows[0]["future_delta_own_net_worth_lead"] = "999"
    elif problem.startswith("target_"):
        rows[0]["future_delta_own_net_worth_lead"] = "NaN" if problem == "target_nan" else "not a number"
    elif problem == "future_time":
        rows[0]["pov_game_time_seconds"] = "60.0002000000000000000000000001"
    elif problem == "future_tick":
        rows[0]["pov_replay_tick"] = "2001"
    else:
        rows[0]["scope"] = "HISTORICAL_ORACLE"
    with pytest.raises(ValueError):
        baseline.load_observations(save(source, rows))


def test_preprocessing_learns_train_statistics_and_categories_only(source):
    observations = baseline.load_observations(source)
    train = np.asarray([r.features for r in observations if r.match_id == 20 and r.horizon_steps == 1], dtype=object)
    spread, distance = FEATURES_AT_T.index("own_team_spread"), FEATURES_AT_T.index("nearest_visible_enemy_distance")
    train[:, spread] = [10.0, np.nan, 30.0, 50.0]
    train[:, distance] = [0.0, np.nan, 2.0, 3.0]
    estimator = baseline.make_estimator().fit(train, [1.0, -1.0, 2.0, -2.0])
    transformer = estimator.named_steps["features"]
    numeric = transformer.named_transformers_["numeric"]
    union = numeric.named_steps["impute"]
    imputer = dict(union.transformer_list)["values"]
    spread_column = baseline.NUMERIC.index(spread)
    assert imputer.statistics_[spread_column] == 30.0
    imputed_train = union.transform(train[:, baseline.NUMERIC])
    np.testing.assert_allclose(numeric.named_steps["scale"].mean_, imputed_train.mean(axis=0))
    statuses = transformer.named_transformers_["statuses"]
    assert all("TEST_ONLY_STATUS" not in categories for categories in statuses.categories_)
    statistics_before = imputer.statistics_.copy()

    test = train[:2].copy()
    test[:, spread] = 1e12
    test[:, FEATURES_AT_T.index("nearest_visible_enemy_status")] = "TEST_ONLY_STATUS"
    test[:, distance] = [0.0, np.nan]
    imputed_test = union.transform(test[:, baseline.NUMERIC])
    distance_column = baseline.NUMERIC.index(distance)
    assert imputed_test[0, distance_column] == 0.0  # A real zero stays observed.
    assert imputed_test[1, distance_column] == 2.0  # NULL gets the TRAIN median.
    assert imputed_test[0, len(baseline.NUMERIC) + distance_column] == 0
    assert imputed_test[1, len(baseline.NUMERIC) + distance_column] == 1
    assert np.isfinite(estimator.predict(test)).all()
    np.testing.assert_array_equal(imputer.statistics_, statistics_before)


def test_all_null_train_column_is_dropped_without_fictitious_zero(source):
    x = np.asarray([r.features for r in baseline.load_observations(source) if r.horizon_steps == 1], dtype=object)
    distance = FEATURES_AT_T.index("nearest_visible_enemy_distance")
    x[:, distance] = np.nan
    estimator = baseline.make_estimator()
    with pytest.warns(UserWarning, match="Skipping features"):
        estimator.fit(x, np.arange(len(x), dtype=float))
    union = estimator.named_steps["features"].named_transformers_["numeric"].named_steps["impute"]
    imputer = dict(union.transformer_list)["values"]
    assert np.isnan(imputer.statistics_[baseline.NUMERIC.index(distance)])
    probe = x[:2].copy()
    probe[:, distance] = [0.0, np.nan]
    with pytest.warns(UserWarning, match="Skipping features"):
        values = union.transform(probe[:, baseline.NUMERIC])
    assert values.shape[1] == len(baseline.NUMERIC) * 2 - 1
    assert values[0, -1] == 0 and values[1, -1] == 1


def test_leave_one_match_out_keeps_pairs_and_horizons_isolated(source, monkeypatch):
    observations = baseline.load_observations(source)
    calls = []

    class Recorder:
        def fit(self, x, y):
            calls.append((x.copy(), y.copy()))
            return self

        def predict(self, x):
            return np.zeros(len(x))

    monkeypatch.setattr(baseline, "make_estimator", Recorder)
    report = baseline.evaluate(observations)
    assert len(calls) == 16 and len(report["experiments"]) == 4
    for experiment_index, experiment in enumerate(report["experiments"]):
        selected = [r for r in observations if r.horizon_steps == experiment["horizon_steps"]]
        target_index = baseline.FUTURE_TARGETS.index(experiment["target"])
        assert sum(fold["test_rows"] for fold in experiment["folds"]) == len(selected)
        for fold_index, fold in enumerate(experiment["folds"]):
            held_out = fold["match_id"]
            train = [r for r in selected if r.match_id != held_out]
            test = [r for r in selected if r.match_id == held_out]
            assert set(fold["train_matches"]) == set(MATCHES) - {held_out}
            assert fold["train_rows"] == len(train) and fold["test_rows"] == len(test)
            assert {r.pov_team for r in test} == {"RADIANT", "DIRE"}
            actual_x, actual_y = calls[experiment_index * 4 + fold_index]
            expected_x = np.asarray([r.features for r in train], dtype=object)
            np.testing.assert_array_equal(np.asarray(actual_x[:, baseline.NUMERIC], dtype=float),
                                          np.asarray(expected_x[:, baseline.NUMERIC], dtype=float))
            np.testing.assert_array_equal(actual_x[:, baseline.CATEGORICAL], expected_x[:, baseline.CATEGORICAL])
            expected_y = [r.targets[target_index] for r in train]
            np.testing.assert_array_equal(actual_y, expected_y)
            assert fold["train_mean"] == np.mean(expected_y) == 0.0


def test_held_out_targets_do_not_change_held_out_predictions(source, monkeypatch):
    observations = baseline.load_observations(source)
    make_estimator = baseline.make_estimator
    predictions = []

    class Recorder:
        def __init__(self):
            self.estimator = make_estimator()

        def fit(self, x, y):
            self.estimator.fit(x, y)
            return self

        def predict(self, x):
            values = self.estimator.predict(x)
            predictions.append(values.copy())
            return values

    monkeypatch.setattr(baseline, "make_estimator", Recorder)
    baseline.evaluate(observations)
    before = list(predictions)
    predictions.clear()
    changed = [replace(row, targets=tuple(value * 100 for value in row.targets)) if row.match_id == 10 else row
               for row in observations]
    baseline.evaluate(changed)
    # Match 10 is first held-out fold for each of the four separate experiments.
    for index in (0, 4, 8, 12):
        np.testing.assert_allclose(predictions[index], before[index], rtol=0, atol=0)


def test_scores_and_signed_improvements():
    result = baseline.scores([-2.0, 2.0], {
        "ZERO_CHANGE": [0.0, 0.0], "TRAIN_MEAN": [1.0, 1.0], "RIDGE": [-1.0, 1.0],
    })
    assert result["metrics"]["ZERO_CHANGE"] == dict(mae=2.0, rmse=2.0)
    assert result["metrics"]["TRAIN_MEAN"]["rmse"] == pytest.approx(np.sqrt(5))
    assert result["ridge_improvement"]["ZERO_CHANGE"] == dict(mae=1.0, rmse=1.0)
    worse = baseline.scores([0.0], {"ZERO_CHANGE": [0.0], "TRAIN_MEAN": [0.0], "RIDGE": [2.0]})
    assert worse["ridge_improvement"]["TRAIN_MEAN"] == dict(mae=-2.0, rmse=-2.0)


def test_pooled_metrics_are_row_weighted_out_of_fold_scores(source):
    report = baseline.evaluate(baseline.load_observations(source))
    for experiment in report["experiments"]:
        assert experiment["rows"] == 16
        for model in baseline.MODELS:
            folds, count = experiment["folds"], experiment["rows"]
            mae = sum(f["test_rows"] * f["metrics"][model]["mae"] for f in folds) / count
            rmse = np.sqrt(sum(f["test_rows"] * f["metrics"][model]["rmse"] ** 2 for f in folds) / count)
            assert experiment["oof"]["metrics"][model] == pytest.approx(dict(mae=mae, rmse=rmse))
        assert experiment["oof"]["metrics"]["ZERO_CHANGE"] == experiment["oof"]["metrics"]["TRAIN_MEAN"]


def test_four_matches_required_in_each_horizon(source):
    observations = baseline.load_observations(source)
    with pytest.raises(ValueError, match="exactly four"):
        baseline.evaluate([r for r in observations if r.match_id != 40])
    with pytest.raises(ValueError, match="horizon_steps=5"):
        baseline.evaluate([r for r in observations if not (r.match_id == 40 and r.horizon_steps == 5)])


def test_future_metadata_never_changes_features_or_model_predictions(source):
    before = baseline.evaluate(baseline.load_observations(source))
    changed = dataset()
    for row in changed:
        row["future_source_game_time_seconds"] = str(Decimal(row["future_source_game_time_seconds"]) + 17)
        row["actual_horizon_seconds"] = str(Decimal(row["actual_horizon_seconds"]) + 17)
        row["future_replay_tick"] = str(int(row["future_replay_tick"]) + 9000)
    after = baseline.evaluate(baseline.load_observations(save(source, changed)))
    assert before == after


def test_cli_reports_all_experiments_and_writes_nothing(source, monkeypatch, capsys):
    monkeypatch.setattr(baseline, "DEFAULT_INPUT", source)
    before = {path.name: path.read_bytes() for path in source.parent.iterdir()}
    assert baseline.main([]) == 0
    output = capsys.readouterr().out
    assert "rows=32; matches=4" in output
    assert output.count("POOLED OOF") == 4
    assert "horizon_steps=1" in output and "horizon_steps=5" in output
    assert "No model or dataset artifacts were saved" in output
    assert "Only four independent match groups" in output
    assert {path.name: path.read_bytes() for path in source.parent.iterdir()} == before


def test_cli_returns_failure_for_missing_file(tmp_path, capsys):
    assert baseline.main(["--csv-path", str(tmp_path / "missing.csv")]) == 1
    assert "evaluation failed" in capsys.readouterr().err


@pytest.mark.parametrize("finite", [True, False])
def test_numerical_diagnostics_are_reported_and_nonfinite_predictions_fail(source, monkeypatch, finite):
    class Estimator:
        def fit(self, x, y):
            warnings.warn("numerical diagnostic", RuntimeWarning, stacklevel=2)
            return self

        def predict(self, x):
            return np.full(len(x), 0.0 if finite else np.inf)

    monkeypatch.setattr(baseline, "make_estimator", Estimator)
    if finite:
        report = baseline.evaluate(baseline.load_observations(source))
        assert report["diagnostics"] == {"RuntimeWarning: numerical diagnostic": 16}
    else:
        with pytest.raises(ValueError, match="Non-finite OOF predictions"):
            baseline.evaluate(baseline.load_observations(source))
