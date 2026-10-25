import csv
from decimal import Decimal, localcontext
import json
from pathlib import Path

import pytest

from app.replay_trajectory import AGGREGATE_FIELDS
from tools import build_economy_transitions as builder


TIMES = (0.0, 60.0002, 120.0001, 180.0005, 240.0, 300.0003, 360.0001)
TICKS = (100, 1901, 5500, 7301, 9102, 16000, 17801)


def compact(match_id=10, count=7, sha="a" * 64):
    snapshots = []
    for index in range(count):
        players = []
        for slot in range(10):
            radiant = slot < 5
            players.append(dict(
                player_slot=slot, team="RADIANT" if radiant else "DIRE", hero_id=slot + 1,
                hero_name=f"hero_{slot}", level=1, kills=0, deaths=0, assists=0, last_hits=0, denies=0,
                net_worth=(100 + index * 10 if radiant else 200 + index * 4) + slot,
                current_gold=10, total_xp=(10 + index * 3 if radiant else 20 + index * 6) + slot,
                items=[],
            ))
        teams = [dict(team=team, **{field:sum(player[field] for player in players if player["team"] == team)
                                   for field in AGGREGATE_FIELDS}) for team in ("RADIANT", "DIRE")]
        snapshots.append(dict(game_time_seconds=index * 60, source_game_time_seconds=TIMES[index],
                              replay_tick=TICKS[index], game_state=5, players=players, teams=teams))
    return dict(
        schema_version=1, match_id=match_id, replay_sha256=sha, clarity_version="4.0.1", game_mode=2,
        league_id=19719, winner=2, radiant_team_id=None, radiant_team_tag=None, dire_team_id=None,
        dire_team_tag=None, picks_bans=[], clock_normalization_status="PROVEN",
        clock_normalization_method="FIXTURE_CLOCK", zero_replay_tick=100,
        game_end_time_seconds=(count - 1) * 60 + 30, pause_ticks=0, pause_seconds=0.0,
        pause_witnessed=False, snapshots=snapshots,
    )


def save(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def source(tmp_path):
    return save(tmp_path / "10.compact.json", compact())


def test_exact_targets_lead_identities_steps_and_tail_coverage(source):
    rows, report = builder.build_transitions({"10": source})
    assert len(rows) == 8 and report["rows"] == 8
    for row in rows:
        h = row["horizon_steps"]
        assert h in (1, 5)
        index = TICKS.index(row["replay_tick"])
        assert row["future_replay_tick"] == TICKS[index + h]
        assert row["future_source_game_time_seconds"] == Decimal(str(TIMES[index + h]))
        assert row["future_delta_radiant_net_worth"] == 50 * h
        assert row["future_delta_dire_net_worth"] == 20 * h
        assert row["future_delta_net_worth_lead"] == 30 * h
        assert row["future_delta_radiant_total_xp"] == 15 * h
        assert row["future_delta_dire_total_xp"] == 30 * h
        assert row["future_delta_xp_lead"] == -15 * h
        assert row["future_delta_net_worth_lead"] == row["future_delta_radiant_net_worth"] - row["future_delta_dire_net_worth"]
        assert row["future_delta_xp_lead"] == row["future_delta_radiant_total_xp"] - row["future_delta_dire_total_xp"]
        assert row["actual_horizon_seconds"] != Decimal(60 * h)
    assert report["horizons"]["1"]["rows"] == 6 and report["horizons"]["1"]["excluded_tail_frames"] == 1
    assert report["horizons"]["5"]["rows"] == 2 and report["horizons"]["5"]["excluded_tail_frames"] == 5
    assert max(row["replay_tick"] for row in rows) == TICKS[-2]
    assert max(row["replay_tick"] for row in rows if row["horizon_steps"] == 5) == TICKS[1]


def test_first_previous_features_are_null_and_later_features_use_only_the_past(source):
    rows, report = builder.build_transitions({"10": source})
    for row in rows:
        index = TICKS.index(row["replay_tick"])
        if index == 0:
            assert row["past_delta_net_worth_lead"] is row["previous_observation_interval_seconds"] is None
        else:
            assert row["past_delta_net_worth_lead"] == 30
            assert row["previous_observation_interval_seconds"] == Decimal(str(TIMES[index])) - Decimal(str(TIMES[index - 1]))
    assert report["horizons"]["1"]["null_past_delta_net_worth_lead"] == 1
    assert report["horizons"]["5"]["null_previous_observation_interval_seconds"] == 1
    assert set(builder.FEATURES_AT_T).isdisjoint(builder.METADATA)
    assert set(builder.FEATURES_AT_T).isdisjoint(builder.FUTURE_TARGETS)
    assert not {"future_source_game_time_seconds", "future_replay_tick", "horizon_steps", "actual_horizon_seconds"} & set(builder.FEATURES_AT_T)


def test_changing_future_values_changes_only_future_targets_for_a_past_row(source):
    rows, _ = builder.build_transitions({"10": source})
    data = compact()
    data["snapshots"][2]["players"][0]["net_worth"] += 500
    data["snapshots"][2]["teams"][0]["net_worth"] += 500
    data["winner"] = 3
    save(source, data)
    changed, _ = builder.build_transitions({"10": source})
    before = next(row for row in rows if row["replay_tick"] == TICKS[1] and row["horizon_steps"] == 1)
    after = next(row for row in changed if row["replay_tick"] == TICKS[1] and row["horizon_steps"] == 1)
    assert {field: before[field] for field in builder.FEATURES_AT_T} == {field: after[field] for field in builder.FEATURES_AT_T}
    assert after["future_delta_radiant_net_worth"] == before["future_delta_radiant_net_worth"] + 500
    assert not {"winner", "league_id", "game_end_time_seconds"} & after.keys()


def test_appending_frames_does_not_change_features_or_existing_targets(source):
    save(source, compact(count=6))
    before, _ = builder.build_transitions({"10": source})
    save(source, compact(count=7))
    after, _ = builder.build_transitions({"10": source})
    by_key = {(row["replay_tick"], row["horizon_steps"]): row for row in after}
    assert all(row == by_key[row["replay_tick"], row["horizon_steps"]] for row in before)


def test_decimal_json_clock_tokens_and_intervals_are_not_rounded(source):
    exact = "60.0002000000000000000000000000000000000001"
    source.write_text(json.dumps(compact()).replace("60.0002", exact), encoding="utf-8")
    with localcontext() as context:
        context.prec = 6
        rows, _ = builder.build_transitions({"10": source})
    row = next(row for row in rows if row["replay_tick"] == TICKS[1] and row["horizon_steps"] == 1)
    assert row["source_game_time_seconds"] == Decimal(exact)
    assert row["previous_observation_interval_seconds"] == Decimal(exact)
    with localcontext() as context:
        context.prec = 100
        assert row["actual_horizon_seconds"] == Decimal("120.0001") - Decimal(exact)
    assert row["replay_tick"] == 1901 and row["future_replay_tick"] == 5500


def test_two_matches_keep_separate_rows_shas_and_stable_order(tmp_path, source):
    other = save(tmp_path / "20.compact.json", compact(match_id=20, sha="b" * 64))
    rows, report = builder.build_transitions({"20": other, "10": source})
    reverse, _ = builder.build_transitions({"10": source, "20": other})
    assert rows == reverse and report["match_count"] == 2 and len(rows) == 16
    identities = [(int(row["source_match_id"]), row["replay_tick"], row["horizon_steps"]) for row in rows]
    assert identities == sorted(identities) and len(identities) == len(set(identities))
    assert {row["replay_sha256"] for row in rows if row["source_match_id"] == "10"} == {"a" * 64}
    assert {row["replay_sha256"] for row in rows if row["source_match_id"] == "20"} == {"b" * 64}
    with pytest.raises(ValueError, match="expected match 20"):
        builder.build_transitions({"20": source})
    save(other, compact(match_id=20))
    with pytest.raises(ValueError, match="reused across matches"):
        builder.build_transitions({"10": source, "20": other})


@pytest.mark.parametrize("count", [1, 2, 5])
def test_short_matches_have_no_invented_future_rows(source, count):
    save(source, compact(count=count))
    rows, report = builder.build_transitions({"10": source})
    for h in (1, 5):
        coverage = report["horizons"][str(h)]
        assert coverage["rows"] == max(0, count - h)
        assert coverage["excluded_tail_frames"] == min(h, count)
        if count <= h:
            assert coverage["min_actual_horizon_seconds"] is coverage["max_actual_horizon_seconds"] is None
    assert len(rows) == max(0, count - 1) + max(0, count - 5)


@pytest.mark.parametrize("problem", ["tick", "time", "player_slot", "team", "aggregate", "duplicate_team"])
def test_corrupted_compacts_fail_explicitly(source, problem):
    data = compact()
    frame = data["snapshots"][1]
    if problem == "tick":
        frame["replay_tick"] = TICKS[0]
    elif problem == "time":
        frame["source_game_time_seconds"] = 60.02
    elif problem == "player_slot":
        frame["players"][1]["player_slot"] = 0
    elif problem == "team":
        frame["players"][0]["team"] = "DIRE"
    elif problem == "aggregate":
        frame["teams"][0]["net_worth"] += 1
    else:
        frame["teams"][1]["team"] = "RADIANT"
    save(source, data)
    with pytest.raises(ValueError):
        builder.build_transitions({"10": source})


def test_dry_run_creates_no_files_and_export_has_stable_columns_nulls_and_order(tmp_path, source, monkeypatch, capsys):
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "INPUTS", {"10": Path(source.name)})
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert builder.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "DRY_RUN" and report["out"] is None and report["rows"] == 8
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert report["column_groups"]["FEATURES_AT_T"] == list(builder.FEATURES_AT_T)
    out = tmp_path / "generated" / "economy.csv"
    assert builder.main(["--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "EXPORTED"
    with out.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(builder.COLUMNS)
        rows = list(reader)
    assert len(rows) == 8 and all(row["provenance"] == "HISTORICAL_ORACLE" for row in rows)
    assert rows[0]["past_delta_net_worth_lead"] == rows[0]["previous_observation_interval_seconds"] == ""
    first = out.read_bytes()
    assert builder.main(["--out", str(out)]) == 0
    assert out.read_bytes() == first
    assert builder.main(["--out", str(source)]) == 1
    captured = capsys.readouterr()
    assert "must not overwrite an input compact" in captured.err


def test_failed_export_preserves_destination_and_removes_temporary_file(tmp_path, source, monkeypatch):
    rows, _ = builder.build_transitions({"10": source})
    out = tmp_path / "existing.csv"
    out.write_text("preserve", encoding="utf-8")
    before = set(tmp_path.iterdir())
    def fail(*args):
        raise OSError("simulated write failure")
    monkeypatch.setattr(builder.os, "replace", fail)
    with pytest.raises(OSError, match="simulated write failure"):
        builder.write_csv(out, rows)
    assert out.read_text(encoding="utf-8") == "preserve" and set(tmp_path.iterdir()) == before
