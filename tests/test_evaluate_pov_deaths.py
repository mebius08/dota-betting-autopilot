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
    observations = baseline.load_observations(source[0])
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
            return self.model.predict_proba(x)

    monkeypatch.setattr(baseline, "make_estimator", RecordedEstimator)
    report = baseline.evaluate(observations)
    assert len(records) == len(report["folds"]) == 4
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
