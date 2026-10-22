from collections import Counter
import csv
from decimal import Decimal
from hashlib import sha256
import warnings

import numpy as np
import pytest

from tools import evaluate_pov_deaths as baseline


def write_source(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=baseline.COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def source(tmp_path):
    rows = []
    for mid in range(1, 5):
        for frame, status in enumerate(("VALID", "VALID", "CENSORED", "AMBIGUOUS_TIME_BOUNDARY")):
            time = Decimal(frame * 10) + Decimal("0.0334")
            for team in baseline.TEAMS:
                own = int((team == "RADIANT") == (frame == 0))
                enemy = 1 - own
                missing = team == "DIRE" and frame == 0
                row = dict(
                    match_id=str(mid), replay_sha256=f"{mid:064x}",
                    game_time_seconds=str(time), replay_tick=str(100 + 300 * frame),
                    pov_team=team, own_alive_hero_count="5", own_valid_position_count="5",
                    own_unknown_life_state_count="0", own_team_spread=str(100 * mid + frame),
                    visible_enemy_count="3", visible_enemy_with_position_count="0" if missing else "2",
                    hidden_enemy_count="2", unknown_enemy_count="0",
                    nearest_visible_enemy_distance="" if missing else str(1000 * mid + frame),
                    own_team_spread_status="OK",
                    nearest_visible_enemy_status="NO_VISIBLE_ENEMY_POSITION" if missing else "OK",
                )
                for horizon in (30, 60):
                    # A non-VALID 60s target must not remove a VALID 30s row.
                    current = status if horizon == 30 else "CENSORED"
                    ambiguous = current == "AMBIGUOUS_TIME_BOUNDARY"
                    row.update({
                        f"own_deaths_next_{horizon}s": str(own) if current == "VALID" else "",
                        f"enemy_deaths_next_{horizon}s": str(enemy) if current == "VALID" else "",
                        f"any_death_next_{horizon}s": "1" if current == "VALID" else "",
                        f"label_status_{horizon}s": current,
                        f"label_window_end_{horizon}s": str(time + horizon),
                        f"label_ambiguous_event_count_{horizon}s": "1" if ambiguous else "0",
                        f"label_ambiguous_events_{horizon}s": '[{"event_id":"example","reasons":["RIGHT_EVENT_VS_DELIVERY_TIME"]}]' if ambiguous else '[]',
                    })
                rows.append(row)
    path = tmp_path / "corpus.csv"
    write_source(path, rows)
    return path, rows


def test_own_target_and_30s_status_are_used_without_imputing_labels(source):
    path, _ = source
    observations = baseline.load_observations(path)
    assert len(observations) == 32
    assert Counter(row.status for row in observations) == {
        "VALID": 16, "CENSORED": 8, "AMBIGUOUS_TIME_BOUNDARY": 8,
    }
    valid = [row for row in observations if row.status == "VALID"]
    assert len(valid) == 16 and sum(row.target for row in valid) == 8
    assert all(row.target is None for row in observations if row.status != "VALID")
    assert any(np.isnan(row.features[-1]) for row in valid)
    assert valid[0].actual_time == Decimal("0.0334")


def test_leave_one_match_out_never_fits_on_test_or_nonvalid_rows(source, monkeypatch):
    path, rows = source
    # Vary prevalence by match so a global/in-sample constant cannot pass.
    rows[0]["enemy_deaths_next_30s"] = "1"
    rows[1]["own_deaths_next_30s"] = "1"
    write_source(path, rows)
    observations = baseline.load_observations(path)
    valid = [row for row in observations if row.status == "VALID"]
    records = []
    factory = baseline.make_estimator

    class RecordedEstimator:
        def __init__(self):
            self.model = factory()
            self.record = {}
            records.append(self.record)

        def fit(self, x, y):
            self.record.update(train_x=x.copy(), train_y=y.copy())
            self.model.fit(x, y)
            return self

        def predict_proba(self, x):
            self.record["test_x"] = x.copy()
            probabilities = self.model.predict_proba(x)
            self.record["test_probabilities"] = probabilities[:, 1].copy()
            return probabilities

    monkeypatch.setattr(baseline, "make_estimator", RecordedEstimator)
    report = baseline.evaluate(observations)
    assert len(records) == len(report["folds"]) == 4
    exported = {
        (row["match_id"], row["replay_tick"], row["pov_team"]): row
        for row in report["oof_rows"]
    }
    for record, fold in zip(records, report["folds"], strict=True):
        train = [row for row in valid if row.match_id != fold["match_id"]]
        test = [row for row in valid if row.match_id == fold["match_id"]]
        assert fold["match_id"] not in fold["train_matches"]
        assert len(fold["train_matches"]) == 3
        np.testing.assert_array_equal(record["train_x"], [row.features for row in train])
        np.testing.assert_array_equal(record["test_x"], [row.features for row in test])
        np.testing.assert_array_equal(record["train_y"], [row.target for row in train])
        assert record["train_x"].shape[1] == len(baseline.FEATURES) == 5
        assert fold["train_positive_rate"] == np.mean([row.target for row in train])
        assert fold["test_rows"] == 4
        assert fold["excluded_censored"] == fold["excluded_ambiguous"] == 2
        for row, probability in zip(test, record["test_probabilities"], strict=True):
            forecast = exported[row.match_id, row.replay_tick, row.pov_team]
            assert forecast["logistic_probability"] == probability
            assert forecast["constant_probability"] == fold["train_positive_rate"]
    assert report["valid_rows"] == 16
    assert report["excluded"] == {"CENSORED": 8, "AMBIGUOUS_TIME_BOUNDARY": 8}


def test_imputation_and_scaling_are_train_only_and_null_is_not_a_real_zero():
    train = np.array([
        [1, 1, 4, 10, 100], [2, 1, 3, 20, 200],
        [3, 2, 2, np.nan, np.nan], [4, 3, 1, 30, 300],
    ])
    estimator = baseline.make_estimator().fit(train, [0, 1, 0, 1])
    features = estimator.named_steps["features"]
    imputer = dict(features.transformer_list)["values"]
    medians = imputer.statistics_.copy()
    means = estimator.named_steps["scale"].mean_.copy()
    test = np.array([[3, 1, 2, np.nan, np.nan], [3, 1, 2, 0, 0], [3, 1, 2, 1e9, 1e9]])
    transformed = features.transform(test)
    np.testing.assert_array_equal(transformed[0, 3:5], [20, 200])
    np.testing.assert_array_equal(transformed[0, 5:], [0, 0, 0, 1, 1])
    np.testing.assert_array_equal(transformed[1, 3:5], [0, 0])
    np.testing.assert_array_equal(transformed[1, 5:], [0, 0, 0, 0, 0])
    assert np.isfinite(estimator.predict_proba(test)).all()
    np.testing.assert_array_equal(imputer.statistics_, medians)
    np.testing.assert_array_equal(estimator.named_steps["scale"].mean_, means)
    # Missingness first encountered at test time still gets its own indicator.
    complete = baseline.make_estimator().fit(train[[0, 1, 3]], [0, 1, 1])
    np.testing.assert_array_equal(
        complete.named_steps["features"].transform(test[:1])[0, 5:], [0, 0, 0, 1, 1]
    )


@pytest.mark.parametrize("problem", ["duplicate", "unpaired", "nonvalid_target"])
def test_inconsistent_source_is_rejected(source, problem):
    path, rows = source
    if problem == "duplicate":
        rows.append(rows[0].copy())
    elif problem == "unpaired":
        rows.pop(1)
    else:
        next(row for row in rows if row["label_status_30s"] == "CENSORED")["own_deaths_next_30s"] = "0"
    write_source(path, rows)
    with pytest.raises(ValueError):
        baseline.load_observations(path)


def test_cli_reports_oof_without_saving_models_or_changing_source(source, capsys):
    path, _ = source
    digest = sha256(path.read_bytes()).hexdigest()
    before = set(path.parent.iterdir())
    assert baseline.main(["--csv-path", str(path)]) == 0
    output = capsys.readouterr().out
    assert "POOLED OOF CONSTANT" in output and "POOLED OOF LOGISTIC" in output
    assert "WARNING: Four matches" in output
    assert "No model or dataset artifacts were saved." in output
    assert set(path.parent.iterdir()) == before
    assert sha256(path.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("converged", [False, True])
def test_estimator_warnings_are_reported_and_nonconvergence_fails(source, monkeypatch, converged):
    factory = baseline.make_estimator

    def warned_estimator():
        estimator = factory()
        fit = estimator.fit

        def warned_fit(x, y):
            category = RuntimeWarning if converged else baseline.ConvergenceWarning
            warnings.warn("test diagnostic", category)
            return fit(x, y)

        estimator.fit = warned_fit
        return estimator

    monkeypatch.setattr(baseline, "make_estimator", warned_estimator)
    observations = baseline.load_observations(source[0])
    if converged:
        report = baseline.evaluate(observations)
        assert report["diagnostics"] == {"RuntimeWarning: test diagnostic": 4}
    else:
        with pytest.raises(ValueError, match="did not converge"):
            baseline.evaluate(observations)


def test_oof_export_has_one_exact_ordered_row_per_valid_example(source, tmp_path):
    path, rows = source
    precise = Decimal("0.033400000000000000123")
    for row in rows[:2]:
        row["game_time_seconds"] = str(precise)
        for horizon in (30, 60):
            row[f"label_window_end_{horizon}s"] = str(precise + horizon)
    # Preserve a binary target, rather than exporting this count as if it were binary.
    rows[0]["own_deaths_next_30s"] = rows[1]["enemy_deaths_next_30s"] = "3"
    write_source(path, rows)
    observations = baseline.load_observations(path)
    report = baseline.evaluate(list(reversed(observations)))
    output = tmp_path / "oof.csv"
    baseline.write_oof(output, report["oof_rows"])
    with output.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        forecasts = list(reader)
        assert reader.fieldnames == list(baseline.OOF_COLUMNS)
    expected = {
        (row["match_id"], row["replay_tick"], row["pov_team"]): row
        for row in rows if row["label_status_30s"] == "VALID"
    }
    keys = [(row["match_id"], row["replay_tick"], row["pov_team"]) for row in forecasts]
    assert len(forecasts) == len(set(keys)) == len(expected) == 16
    assert set(keys) == set(expected)
    assert keys == sorted(keys, key=lambda key: (
        int(key[0]), Decimal(expected[key]["game_time_seconds"]), int(key[1]),
        baseline.TEAMS.index(key[2]),
    ))
    assert forecasts[0]["game_time_seconds"] == str(precise)
    for row, key in zip(forecasts, keys, strict=True):
        assert row["game_time_seconds"] == expected[key]["game_time_seconds"]
        assert row["horizon_seconds"] == "30"
        assert int(row["target"]) == int(int(expected[key]["own_deaths_next_30s"]) > 0)
        for field in ("constant_probability", "logistic_probability"):
            probability = float(row[field])
            assert np.isfinite(probability) and 0 <= probability <= 1
    for model, field in (("constant", "constant_probability"), ("logistic", "logistic_probability")):
        recomputed = baseline.scores(
            [int(row["target"]) for row in forecasts],
            [float(row[field]) for row in forecasts],
        )
        assert recomputed == pytest.approx(report[model], abs=1e-12)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_atomic_export_failure_leaves_no_partial_file(tmp_path, monkeypatch, existing, failure):
    output = tmp_path / "oof.csv"
    if existing:
        output.write_bytes(b"previous complete result\n")
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    row = dict(zip(baseline.OOF_COLUMNS, (1, 100, "0.0334", "RADIANT", 30, .5, .25, 1)))

    def fail(*args, **kwargs):
        raise OSError("simulated write failure")

    if failure == "write":
        original = baseline.csv.DictWriter

        def partial_writer(*args, **kwargs):
            writer = original(*args, **kwargs)

            def write_part_then_fail(rows):
                writer.writerow(rows[0])
                fail()

            writer.writerows = write_part_then_fail
            return writer

        monkeypatch.setattr(baseline.csv, "DictWriter", partial_writer)
    else:
        monkeypatch.setattr(baseline.os, failure, fail)
    with pytest.raises(OSError, match="simulated write failure"):
        baseline.write_oof(output, [row, row])
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_cli_refuses_to_overwrite_source_before_training(source, monkeypatch, capsys):
    path, _ = source
    digest = sha256(path.read_bytes()).hexdigest()

    def forbidden(*args):
        pytest.fail("Training must not run when output is the source")

    monkeypatch.setattr(baseline, "evaluate", forbidden)
    assert baseline.main(["--csv-path", str(path), "--out-oof", str(path)]) == 1
    assert "must not overwrite the source" in capsys.readouterr().err
    assert sha256(path.read_bytes()).hexdigest() == digest


def test_cli_reports_export_error_without_claiming_success(source, tmp_path, monkeypatch, capsys):
    def fail(*args):
        raise OSError("destination unavailable")

    monkeypatch.setattr(baseline, "write_oof", fail)
    output = tmp_path / "oof.csv"
    assert baseline.main(["--csv-path", str(source[0]), "--out-oof", str(output)]) == 1
    captured = capsys.readouterr()
    assert "destination unavailable" in captured.err
    assert "OOF CSV:" not in captured.out
    assert not output.exists()


@pytest.mark.parametrize("probability", [np.nan, -0.1, 1.1])
def test_invalid_probabilities_cannot_be_exported(source, monkeypatch, probability):
    class BadEstimator:
        def fit(self, x, y):
            return self

        def predict_proba(self, x):
            return np.tile([1 - probability, probability], (len(x), 1))

    monkeypatch.setattr(baseline, "make_estimator", BadEstimator)
    with pytest.raises(ValueError, match="Invalid OOF model probabilities"):
        baseline.evaluate(baseline.load_observations(source[0]))


@pytest.mark.skipif(not baseline.DEFAULT_INPUT.is_file(), reason="Local canonical four-match corpus required")
def test_real_export_reproduces_original_oof_metrics(tmp_path, capsys):
    digest = sha256(baseline.DEFAULT_INPUT.read_bytes()).hexdigest()
    output = tmp_path / "real-oof.csv"
    assert baseline.main(["--out-oof", str(output)]) == 0
    with output.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2206
    with baseline.DEFAULT_INPUT.open(newline="", encoding="utf-8") as stream:
        valid = {(row["match_id"], row["replay_tick"], row["pov_team"]): row
                 for row in csv.DictReader(stream) if row["label_status_30s"] == "VALID"}
    keys = [(row["match_id"], row["replay_tick"], row["pov_team"]) for row in rows]
    assert len(set(keys)) == len(valid) == len(rows) and set(keys) == set(valid)
    for key, row in zip(keys, rows, strict=True):
        assert row["game_time_seconds"] == valid[key]["game_time_seconds"]
        assert int(row["target"]) == int(int(valid[key]["own_deaths_next_30s"]) > 0)
        assert row["horizon_seconds"] == "30"
    y = [int(row["target"]) for row in rows]
    for field, brier, loss in (
        ("constant_probability", .152303, .484136),
        ("logistic_probability", .149765, .482304),
    ):
        probabilities = np.asarray([float(row[field]) for row in rows])
        assert np.isfinite(probabilities).all() and np.all((0 <= probabilities) & (probabilities <= 1))
        result = baseline.scores(y, probabilities)
        assert result == pytest.approx({"brier": brier, "log_loss": loss}, abs=1e-6)
    assert "rows=2206" in capsys.readouterr().out
    assert sha256(baseline.DEFAULT_INPUT.read_bytes()).hexdigest() == digest
