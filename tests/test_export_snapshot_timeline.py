import csv
from dataclasses import replace
from decimal import Decimal, localcontext
import json
from pathlib import Path

import pytest

from tools import export_snapshot_timeline as cli


INSPECTOR = cli.inspector
MATCH = "8960991322"
SHA = "a" * 64
SPREAD = "123.456789012345678901234567890123456789"
ANCHORS = ((10.0334, 201), (20.0334, 301))


def write_sources(root, match_id=MATCH, sha=SHA, anchors=ANCHORS):
    """Small actual-shape artifacts, independent of all gitignored replay data."""
    players, positions = [], []
    for slot in range(10):
        identity = dict(player_slot=slot, team="RADIANT" if slot < 5 else "DIRE", hero_id=slot + 1)
        players.append(dict(
            **identity, hero_name=f"hero_{slot}", level=1, net_worth=100, current_gold=10, total_xp=0,
            kills=0, deaths=0, assists=0, last_hits=0, denies=0,
            items=[dict(inventory_slot=0, item_name="item_fixture")], target=99,
        ))
        positions.append(dict(**identity, x=slot + 1.125, y=slot + 2.25, position_status="OBSERVED", life_state=0))
    positions[-1].update(x=None, y=None, position_status="HERO_NOT_ALIVE", life_state=2)
    compact = [dict(
        game_time_seconds=0, source_game_time_seconds=0.0002, replay_tick=100, players=players,
        teams=[dict(team=team, net_worth=500, current_gold=50 if team == "RADIANT" else None, total_xp=0)
               for team in INSPECTOR.TEAMS],
    )]
    spatial, pov, enemies = [], [], []
    for time, tick in anchors:
        spatial.append(dict(source_game_time_seconds=time, replay_tick=tick, players=positions))
        for team in INSPECTOR.TEAMS:
            lineage = dict(match_id=match_id, replay_sha256=sha, game_time_seconds=str(time),
                           replay_tick=str(tick), pov_team=team)
            pov.append(dict(
                **lineage, own_alive_hero_count="5", own_valid_position_count="5", own_unknown_life_state_count="0",
                own_team_spread=SPREAD, visible_enemy_count="0", visible_enemy_with_position_count="0",
                hidden_enemy_count="5", unknown_enemy_count="0", nearest_visible_enemy_distance="",
                own_team_spread_status="OK", nearest_visible_enemy_status="NO_VISIBLE_ENEMY_POSITION",
            ))
            for slot in (range(5, 10) if team == "RADIANT" else range(5)):
                enemies.append(dict(
                    **lineage, enemy_player_slot=str(slot), enemy_hero_id=str(slot + 1),
                    enemy_team="DIRE" if team == "RADIANT" else "RADIANT",
                    visibility_state="HIDDEN", current_position_status="HIDDEN", current_x="", current_y="",
                    last_seen_status="LAST_SEEN", last_seen_game_time_seconds="0.0002",
                    last_seen_age_seconds=str(Decimal(str(time)) - Decimal("0.0002")),
                    last_seen_x="1.125", last_seen_y="2.25",
                ))
    paths = INSPECTOR.MATCH_SOURCES[match_id]
    for path, frames, extra in (
        (paths[0], compact, dict(winner="DIRE", own_deaths_next_30s=99, future_delta_own_xp_lead=123)),
        (paths[1], spatial, dict(artifact_kind="replay_hero_positions")),
    ):
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(dict(
            match_id=int(match_id), replay_sha256=sha, clock_normalization_status="PROVEN", snapshots=frames, **extra,
        )), encoding="utf-8")
    for path, columns, rows in ((paths[2], INSPECTOR.TEAM_COLUMNS, pov),
                                (paths[3], INSPECTOR.ENEMY_OBSERVATION_FIELDS, enemies)):
        save_csv(root / path, columns, rows)


def save_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def sources(tmp_path, monkeypatch):
    monkeypatch.setattr(INSPECTOR, "ROOT", tmp_path)
    monkeypatch.setattr(INSPECTOR, "MATCH_SOURCES", {MATCH: INSPECTOR.MATCH_SOURCES[MATCH]})
    write_sources(tmp_path)
    return tmp_path


def nested_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from nested_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_keys(child)


def test_one_anchor_builds_once_and_projects_three_views_with_no_information_leak(sources, monkeypatch):
    write_sources(sources, anchors=ANCHORS[:1])
    original_build, original_project = cli.build_historical_snapshot, cli.project_snapshot
    builds, views = [], []

    def build(*args, **kwargs):
        builds.append(kwargs["requested_replay_tick"])
        return original_build(*args, **kwargs)

    def project(snapshot, view, **kwargs):
        views.append((view, kwargs["requested_replay_tick"]))
        return original_project(snapshot, view, **kwargs)

    monkeypatch.setattr(cli, "build_historical_snapshot", build)
    monkeypatch.setattr(cli, "project_snapshot", project)
    records = list(cli.iter_timeline_records())
    assert builds == [201] and views == [(view, 201) for view in cli.VIEWS]
    assert len(records) == 3
    assert set(records[0]["observations"]) == set(cli.CHANNEL_ORIGINS)
    for record in records:
        assert record["match"] == dict(source="replay_clarity", source_match_id=MATCH)
        assert record["replay_sha256"] == SHA and record["requested_replay_tick"] == 201
        assert record["requested_game_time_seconds"] == Decimal("10.0334")
        team = cli.VIEWS[record["view"]]
        if team is not None:
            assert set(record["observations"]) == {"pov", "pov_enemy_positions"}
            assert all(set(o["values"]) == {team} for o in record["observations"].values())
        for observation in record["observations"].values():
            assert observation["status"] == "AVAILABLE"
            assert observation["actual_game_time_seconds"] <= record["requested_game_time_seconds"]
            assert observation["replay_tick"] <= record["requested_replay_tick"]
        for enemies in record["observations"]["pov_enemy_positions"]["values"].values():
            for enemy in enemies:
                assert enemy["current_x"] is enemy["current_y"] is None
                assert enemy["last_seen_x"] == Decimal("1.125")
                assert enemy["last_seen_game_time_seconds"] == Decimal("0.0002")
                assert enemy["last_seen_age_at_requested_time_seconds"] == Decimal("10.0332")
                assert enemy["pov_team"] == (team or enemy["pov_team"])
        assert not {"target", "winner", "own_deaths_next_30s", "future_delta_own_xp_lead", "logistic_probability",
                    "changes", "predictions"} & set(nested_keys(record))
    assert records[0]["observations"]["economy"]["values"]["dire_current_gold"] is None
    assert records[0]["observations"]["spatial_global"]["values"]["players"][-1]["x"] is None


def test_files_are_loaded_once_per_match_not_per_anchor(sources, monkeypatch):
    original_json, original_csv = INSPECTOR._json_source, INSPECTOR._csv_source
    reads = []

    def read_json(path, *args):
        reads.append(path)
        return original_json(path, *args)

    def read_csv(path, *args):
        reads.append(path)
        return original_csv(path, *args)

    monkeypatch.setattr(INSPECTOR, "_json_source", read_json)
    monkeypatch.setattr(INSPECTOR, "_csv_source", read_csv)
    assert len(list(cli.iter_timeline_records())) == 6
    assert len(reads) == len(set(reads)) == 4


def test_multiple_matches_frames_and_views_have_stable_order_and_unique_keys(sources, monkeypatch):
    other = "8957013760"
    paths = tuple(Path(f"other/{name}") for name in ("compact.json", "spatial.json", "pov.csv", "enemy.csv"))
    monkeypatch.setattr(INSPECTOR, "MATCH_SOURCES", {MATCH: INSPECTOR.MATCH_SOURCES[MATCH], other: paths})
    write_sources(sources, other, "b" * 64, tuple(reversed(ANCHORS)))
    records = list(cli.iter_timeline_records())
    keys = [(int(r["match"]["source_match_id"]), r["requested_game_time_seconds"],
             r["requested_replay_tick"], tuple(cli.VIEWS).index(r["view"])) for r in records]
    assert len(keys) == len(set(keys)) == 12 and keys == sorted(keys)


@pytest.mark.parametrize("problem", ["duplicate", "missing_team", "different_time"])
def test_bad_pov_pairs_are_not_used_as_anchors(sources, problem):
    path = sources / INSPECTOR.MATCH_SOURCES[MATCH][2]
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    if problem == "duplicate":
        rows.append(dict(rows[0]))
    elif problem == "missing_team":
        rows.pop()
    else:
        rows[1]["game_time_seconds"] = "10.0335"
    save_csv(path, INSPECTOR.TEAM_COLUMNS, rows)
    with pytest.raises(ValueError, match="POV|pairs"):
        cli.export_timeline()


@pytest.mark.parametrize("boundary", ["time", "tick"])
def test_projection_rejects_future_observations_even_if_builder_is_faulty(sources, monkeypatch, boundary):
    original = cli.build_historical_snapshot

    def faulty(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        observation = snapshot.observations["economy"]
        bad = replace(observation, **(
            dict(actual_game_time_seconds=snapshot.requested_game_time_seconds + Decimal("0.0000000001"))
            if boundary == "time" else dict(replay_tick=kwargs["requested_replay_tick"] + 1)
        ))
        return replace(snapshot, observations=snapshot.observations | {"economy": bad})

    monkeypatch.setattr(cli, "build_historical_snapshot", faulty)
    with pytest.raises(ValueError, match="time, tick or age"):
        cli.export_timeline()


@pytest.mark.parametrize(("time", "tick"), [(10.0335, 200), (10.0333, 202)])
def test_real_builder_excludes_each_future_boundary(sources, time, tick):
    write_sources(sources, anchors=ANCHORS[:1])
    path = sources / INSPECTOR.MATCH_SOURCES[MATCH][0]
    compact = json.loads(path.read_text(encoding="utf-8"))
    future = dict(compact["snapshots"][0], source_game_time_seconds=time, replay_tick=tick)
    compact["snapshots"].append(future)
    path.write_text(json.dumps(compact), encoding="utf-8")
    record = next(cli.iter_timeline_records())
    for channel in ("economy", "heroes"):
        assert record["observations"][channel]["actual_game_time_seconds"] == Decimal("0.0002")
        assert record["observations"][channel]["replay_tick"] == 100


def test_decimal_json_null_age_and_tick_are_exact_under_small_context(sources):
    write_sources(sources, anchors=ANCHORS[:1])
    exact = "10.0334000000000000000000000000000000000001"
    for path in INSPECTOR.MATCH_SOURCES[MATCH]:
        full = sources / path
        full.write_text(full.read_text(encoding="utf-8").replace("10.0334", exact), encoding="utf-8")
    out = sources / "timeline.jsonl"
    with localcontext() as context:
        context.prec = 6
        report = cli.export_timeline(out)
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 3
    record = records[0]
    assert record["requested_game_time_seconds"] == exact and record["requested_replay_tick"] == 201
    assert record["observations"]["economy"]["age_seconds"] == "10.0332000000000000000000000000000000000001"
    assert record["observations"]["economy"]["values"]["dire_current_gold"] is None
    assert record["observations"]["pov"]["values"]["RADIANT"]["own_team_spread"] == SPREAD
    assert record["observations"]["pov"]["values"]["RADIANT"]["nearest_visible_enemy_distance"] is None
    assert report["freshness"]["economy"]["median_age_seconds"] == Decimal("10.0332000000000000000000000000000000000001")


def test_missing_sources_keep_empty_envelopes_and_are_counted(sources):
    path = sources / INSPECTOR.MATCH_SOURCES[MATCH][0]
    compact = json.loads(path.read_text(encoding="utf-8"))
    compact["snapshots"] = []
    path.write_text(json.dumps(compact), encoding="utf-8")
    report = cli.export_timeline()
    assert report["anchors"] == 2 and report["records"] == 6
    assert report["channel_statuses"]["economy"] == dict(AVAILABLE=0, NO_PRECEDING_SNAPSHOT=2)
    assert report["channel_statuses"]["heroes"] == dict(AVAILABLE=0, NO_PRECEDING_SNAPSHOT=2)
    assert report["channel_statuses"]["pov"] == dict(AVAILABLE=2, NO_PRECEDING_SNAPSHOT=0)
    assert report["freshness"]["economy"] == dict(
        observations=0, min_age_seconds=None, median_age_seconds=None, max_age_seconds=None,
    )
    for record in cli.iter_timeline_records():
        if record["view"] == "HISTORICAL_GLOBAL":
            observation = record["observations"]["economy"]
            assert observation["status"] == "NO_PRECEDING_SNAPSHOT"
            assert all(observation[k] is None for k in ("actual_game_time_seconds", "replay_tick", "age_seconds", "values"))


def test_cli_dry_run_creates_no_file(sources, capsys):
    before = {p: p.read_bytes() for p in sources.rglob("*") if p.is_file()}
    assert cli.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "DRY_RUN" and report["out"] is None
    assert report["anchors"] == 2 and report["records"] == 6
    assert report["records_by_view"] == dict.fromkeys(cli.VIEWS, 2)
    assert {p: p.read_bytes() for p in sources.rglob("*") if p.is_file()} == before


def test_repeated_exports_are_identical_and_preserve_sources(sources):
    before = {p: p.read_bytes() for p in sources.rglob("*") if p.is_file()}
    first, second = sources / "first.jsonl", sources / "second.jsonl"
    report = cli.export_timeline(first)
    again = cli.export_timeline(second)
    assert first.read_bytes() == second.read_bytes()
    assert report | {"out": None} == again | {"out": None}
    assert all(path.read_bytes() == original for path, original in before.items())
    assert cli.export_timeline()["freshness"]["economy"] == dict(
        observations=2, min_age_seconds=Decimal("10.0332"), median_age_seconds=Decimal("15.0332"),
        max_age_seconds=Decimal("20.0332"),
    )


def test_duplicate_records_do_not_replace_a_complete_output(sources, monkeypatch):
    payload = next(cli.iter_timeline_records())
    monkeypatch.setattr(cli, "iter_timeline_records", lambda: iter([payload, payload]))
    out = sources / "out.jsonl"
    out.write_text("previous complete output", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate timeline identity"):
        cli.export_timeline(out)
    assert out.read_text(encoding="utf-8") == "previous complete output"
    assert not list(sources.glob(".out.jsonl.*"))


@pytest.mark.parametrize("problem", ["missing_file", "sha", "identity"])
def test_source_errors_are_explicit_and_publish_nothing(sources, capsys, problem):
    path = sources / INSPECTOR.MATCH_SOURCES[MATCH][1]
    if problem == "missing_file":
        path.unlink()
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["replay_sha256" if problem == "sha" else "match_id"] = "b" * 64 if problem == "sha" else 999
        path.write_text(json.dumps(data), encoding="utf-8")
    out = sources / "out.jsonl"
    assert cli.main(["--out", str(out)]) == 1
    captured = capsys.readouterr()
    assert not out.exists() and captured.out == "" and "Historical timeline error" in captured.err
    assert not list(sources.glob(".out.jsonl.*"))


def test_output_cannot_overwrite_any_source(sources):
    for relative in INSPECTOR.MATCH_SOURCES[MATCH]:
        path = sources / relative
        before = path.read_bytes()
        with pytest.raises(ValueError, match="overwrite"):
            cli.export_timeline(path)
        assert path.read_bytes() == before
