import csv
from decimal import Decimal, localcontext
import json
from pathlib import Path

import pytest

from tools import build_pov_economy_transitions as builder


def anchor(mid="10", sha="a" * 64, time="60.0002", tick=2000, horizon=1,
           future_time="120.0004", future_tick=4000, nw=-20, xp=85):
    row = dict.fromkeys(builder.ECONOMY_COLUMNS, "0")
    row.update(
        source="replay_clarity", source_match_id=mid, replay_sha256=sha, provenance="HISTORICAL_ORACLE",
        source_game_time_seconds=time, replay_tick=str(tick), horizon_steps=str(horizon),
        future_source_game_time_seconds=future_time, future_replay_tick=str(future_tick),
        actual_horizon_seconds=str(builder._interval(Decimal(future_time), Decimal(time))),
        future_delta_net_worth_lead=str(nw), future_delta_xp_lead=str(xp),
    )
    return row


def pair(time="50.0334", tick=1700, mid="10", sha="a" * 64):
    rows = []
    for team, visible in (("RADIANT", 3), ("DIRE", 1)):
        row = dict.fromkeys(builder.POV_COLUMNS, "0")
        row.update(
            match_id=mid, replay_sha256=sha, game_time_seconds=time, replay_tick=str(tick), pov_team=team,
            own_alive_hero_count="5", own_valid_position_count="5", own_unknown_life_state_count="0",
            own_team_spread="123.45678901234567890123456789", own_team_spread_status="OK",
            visible_enemy_count=str(visible), visible_enemy_with_position_count=str(visible),
            hidden_enemy_count=str(5 - visible), unknown_enemy_count="0",
            nearest_visible_enemy_distance="42.125", nearest_visible_enemy_status="OK",
        )
        for horizon in (30, 60):
            row.update({
                f"label_status_{horizon}s": "VALID",
                f"label_window_end_{horizon}s": str(builder._interval(Decimal(time), Decimal(-horizon))),
                f"label_ambiguous_events_{horizon}s": "[]",
            })
        rows.append(row)
    return rows


def save(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def sources(tmp_path, anchors=None, observations=None):
    return (
        save(tmp_path / "economy.csv", builder.ECONOMY_COLUMNS, [anchor()] if anchors is None else anchors),
        save(tmp_path / "pov.csv", builder.POV_COLUMNS, pair() if observations is None else observations),
    )


@pytest.mark.parametrize(("time", "tick", "selected_tick"), [
    ("60.000200000000000000000000001", 1900, 1700),  # Future time, admissible tick.
    ("60.0", 2001, 1700),  # Past time, future tick.
    ("60.0002", 2000, 2000),  # Both boundaries are inclusive.
])
def test_as_of_requires_both_exact_time_and_tick(tmp_path, time, tick, selected_tick):
    rows, report = builder.build_transitions(*sources(tmp_path, observations=pair() + pair(time, tick)))
    assert len(rows) == 2 and report["skipped_team_rows"] == 0
    assert {row["pov_replay_tick"] for row in rows} == {selected_tick}
    assert {row["pov_team"] for row in rows} == {"RADIANT", "DIRE"}
    for row in rows:
        assert row["pov_game_time_seconds"] <= row["economy_game_time_seconds"]
        assert row["pov_replay_tick"] <= row["economy_replay_tick"]


def test_targets_are_oriented_to_own_team_and_features_keep_each_teams_view(tmp_path):
    rows, _ = builder.build_transitions(*sources(tmp_path))
    radiant, dire = rows
    assert radiant["future_delta_own_net_worth_lead"] == -20
    assert dire["future_delta_own_net_worth_lead"] == 20
    assert radiant["future_delta_own_xp_lead"] == 85
    assert dire["future_delta_own_xp_lead"] == -85
    assert radiant["visible_enemy_count"] == "3" and dire["visible_enemy_count"] == "1"
    assert radiant["scope"] == dire["scope"] == builder.SCOPE


@pytest.mark.parametrize(("mid", "sha"), [("10", "b" * 64), ("11", "a" * 64)])
def test_match_and_sha_conflicts_fail_instead_of_cross_replay_join(tmp_path, mid, sha):
    with pytest.raises(ValueError, match="identity conflict"):
        builder.build_transitions(*sources(tmp_path, observations=pair(mid=mid, sha=sha)))


@pytest.mark.parametrize("observations", [[], pair(time="61.0", tick=1900), pair(time="59.0", tick=2001),
                                         pair(mid="11", sha="b" * 64)])
def test_no_eligible_pov_skips_both_rows_with_reason(tmp_path, observations):
    rows, report = builder.build_transitions(*sources(tmp_path, observations=observations))
    assert rows == [] and report["match_count"] == 1
    assert report["skipped_anchor_rows"] == 1 and report["skipped_team_rows"] == 2
    assert report["skip_reasons"] == {"NO_PRECEDING_POV": 2}
    assert report["min_pov_age_seconds"] is report["median_pov_age_seconds"] is report["max_pov_age_seconds"] is None
    assert report["matches"][0]["horizons"]["1"]["skip_reasons"] == {"NO_PRECEDING_POV": 2}


def test_allowlist_excludes_all_oracle_values_future_labels_and_metadata(tmp_path):
    paths = sources(tmp_path)
    before, report = builder.build_transitions(*paths)
    changed_economy = anchor()
    for name in ("radiant_net_worth", "dire_net_worth", "radiant_total_xp", "dire_total_xp",
                 "net_worth_lead_radiant_minus_dire", "past_delta_net_worth_lead"):
        changed_economy[name] = "9999999999999999999"
    changed_pov = pair()
    for row in changed_pov:
        for name in set(builder.POV_COLUMNS) - set(builder.FEATURES_AT_T) - {
                "match_id", "replay_sha256", "pov_team", "game_time_seconds", "replay_tick"}:
            row[name] = "unused future death label"
    after, _ = builder.build_transitions(*sources(tmp_path, [changed_economy], changed_pov))
    assert before == after  # Death-label validity is irrelevant to economy targets.
    assert set(builder.FEATURES_AT_T) == {
        "own_alive_hero_count", "own_valid_position_count", "own_unknown_life_state_count",
        "own_team_spread", "visible_enemy_count", "visible_enemy_with_position_count",
        "hidden_enemy_count", "unknown_enemy_count", "nearest_visible_enemy_distance",
        "own_team_spread_status", "nearest_visible_enemy_status",
    }
    assert set(builder.FEATURES_AT_T).isdisjoint(builder.METADATA + builder.FUTURE_TARGETS)
    assert report["column_groups"]["FEATURES_AT_T"] == builder.FEATURES_AT_T
    assert all(set(row) == set(builder.COLUMNS) for row in after)
    assert "match_id" in report["evaluation_note"] and "not independent" in report["evaluation_note"]


def test_null_cells_statuses_and_exact_feature_tokens_are_preserved(tmp_path):
    observations = pair()
    observations[0].update(nearest_visible_enemy_distance="", nearest_visible_enemy_status="NO_VISIBLE_ENEMY_POSITION")
    observations[1].update(own_team_spread="", own_team_spread_status="INSUFFICIENT_OWN_POSITIONS")
    rows, _ = builder.build_transitions(*sources(tmp_path, observations=observations))
    for output, original in zip(rows, observations, strict=True):
        assert {name: output[name] for name in builder.FEATURES_AT_T} == {
            name: original[name] for name in builder.FEATURES_AT_T}
    out = tmp_path / "out.csv"
    builder.write_csv(out, rows)
    with out.open(newline="") as stream:
        saved = list(csv.DictReader(stream))
    assert saved[0]["nearest_visible_enemy_distance"] == ""
    assert saved[0]["nearest_visible_enemy_status"] == "NO_VISIBLE_ENEMY_POSITION"
    assert saved[1]["own_team_spread"] == ""


@pytest.mark.parametrize("problem", ["duplicate", "missing_team", "time", "clock_order", "team"])
def test_corrupted_or_unpaired_pov_is_an_explicit_error(tmp_path, problem):
    observations = pair()
    if problem == "duplicate":
        observations.append(dict(observations[0]))
    elif problem == "missing_team":
        observations.pop()
    elif problem == "time":
        observations[1]["game_time_seconds"] = "50.0335"
    elif problem == "clock_order":
        observations.extend(pair(time="49.0", tick=1900))
    else:
        observations[0]["pov_team"] = "GLOBAL"
    with pytest.raises(ValueError):
        builder.build_transitions(*sources(tmp_path, observations=observations))


def test_duplicate_economy_identity_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="Duplicate"):
        builder.build_transitions(*sources(tmp_path, [anchor(), anchor()]))


@pytest.mark.parametrize("field", ["actual_horizon_seconds", "future_replay_tick", "future_source_game_time_seconds"])
def test_invalid_transition_clock_is_an_error(tmp_path, field):
    economy = anchor()
    economy[field] = "0"
    with pytest.raises(ValueError, match="Invalid economy transition"):
        builder.build_transitions(*sources(tmp_path, [economy]))


def test_future_pov_changes_never_affect_past_features(tmp_path):
    future = pair(time="70.0002", tick=2200)
    before, _ = builder.build_transitions(*sources(tmp_path, observations=pair() + future))
    for row in future:
        row.update(visible_enemy_count="5", own_team_spread="999999")
    after, _ = builder.build_transitions(*sources(tmp_path, observations=pair() + future + pair("80.0", 5000)))
    assert before == after
    changed, _ = builder.build_transitions(*sources(tmp_path, [anchor(nw=123, xp=-45)], pair() + future))
    for original, revised in zip(before, changed, strict=True):
        assert {k: original[k] for k in builder.FEATURES_AT_T} == {k: revised[k] for k in builder.FEATURES_AT_T}
        assert original[builder.FUTURE_TARGETS[0]] != revised[builder.FUTURE_TARGETS[0]]


def test_decimal_times_ages_median_and_ticks_do_not_round(tmp_path):
    time = "60.0002000000000000000000000000000000000002"
    pov_time = "60.0002000000000000000000000000000000000001"
    future_time = "120.0004000000000000000000000000000000000003"
    paths = sources(tmp_path, [anchor(time=time, future_time=future_time)], pair(pov_time, 1999))
    with localcontext() as context:
        context.prec = 6
        rows, report = builder.build_transitions(*paths)
    assert rows[0]["economy_game_time_seconds"] == Decimal(time)
    assert rows[0]["pov_game_time_seconds"] == Decimal(pov_time)
    assert rows[0]["future_source_game_time_seconds"] == Decimal(future_time)
    assert rows[0]["pov_age_seconds"] == report["median_pov_age_seconds"] == Decimal("1e-40")
    assert rows[0]["actual_horizon_seconds"] == Decimal("60.0002000000000000000000000000000000000001")
    assert rows[0]["pov_replay_tick"] == 1999 and rows[0]["economy_replay_tick"] == 2000
    out = tmp_path / "out.csv"
    builder.write_csv(out, rows)
    with out.open(newline="") as stream:
        saved = next(csv.DictReader(stream))
    assert saved["economy_game_time_seconds"] == time and saved["pov_game_time_seconds"] == pov_time


def test_matches_horizons_unique_rows_and_stable_csv_order(tmp_path):
    anchors = [anchor(horizon=5), anchor(), anchor(mid="20", sha="b" * 64)]
    observations = pair() + pair(mid="20", sha="b" * 64)
    first, report = builder.build_transitions(*sources(tmp_path, anchors, observations))
    second, _ = builder.build_transitions(*sources(tmp_path, list(reversed(anchors)), list(reversed(observations))))
    assert first == second and len(first) == 6 and report["match_count"] == 2
    keys = [(r["match_id"], r["economy_replay_tick"], r["pov_team"], r["horizon_steps"]) for r in first]
    assert len(keys) == len(set(keys))
    assert [(r["match_id"], r["horizon_steps"], r["pov_team"]) for r in first] == [
        ("10", 1, "RADIANT"), ("10", 1, "DIRE"), ("10", 5, "RADIANT"), ("10", 5, "DIRE"),
        ("20", 1, "RADIANT"), ("20", 1, "DIRE"),
    ]
    for row in first:
        assert row["replay_sha256"] == ("a" if row["match_id"] == "10" else "b") * 64
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    builder.write_csv(a, first)
    builder.write_csv(b, second)
    assert a.read_bytes() == b.read_bytes()


def configure_cli(tmp_path, monkeypatch):
    paths = sources(tmp_path)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "ECONOMY_PATH", Path("economy.csv"))
    monkeypatch.setattr(builder, "POV_PATH", Path("pov.csv"))
    return paths


def test_cli_dry_run_creates_no_file(tmp_path, monkeypatch, capsys):
    configure_cli(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    assert builder.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "DRY_RUN" and report["out"] is None and report["rows"] == 2
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_cli_exports_only_explicit_path_and_preserves_sources(tmp_path, monkeypatch, capsys):
    inputs = configure_cli(tmp_path, monkeypatch)
    before = [p.read_bytes() for p in inputs]
    out = tmp_path / "selected.csv"
    assert builder.main(["--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "EXPORTED"
    with out.open(newline="") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == list(builder.COLUMNS) and len(list(reader)) == 2
    assert [p.read_bytes() for p in inputs] == before
    for path in inputs:
        assert builder.main(["--out", str(path)]) == 1
        assert "must not overwrite" in capsys.readouterr().err
    assert [p.read_bytes() for p in inputs] == before


def test_failed_write_does_not_publish_partial_file(tmp_path, monkeypatch, capsys):
    configure_cli(tmp_path, monkeypatch)
    out = tmp_path / "out.csv"
    out.write_text("previous complete file", encoding="utf-8")

    def fail_replace(*args):
        raise OSError("write denied")

    monkeypatch.setattr(builder.os, "replace", fail_replace)
    assert builder.main(["--out", str(out)]) == 1
    assert "write denied" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "previous complete file"
    assert not list(tmp_path.glob(".out.csv.*"))
