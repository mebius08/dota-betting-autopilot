import json

import pytest

from app.analytics.historical_experience import HALF_LIFE_SECONDS
from tests.test_historical_experience import NOW, contexts  # noqa: F401 -- shared local-cache fixture
from tools import export_historical_experience as cli


@pytest.fixture
def local(request, monkeypatch):
    make, cache = request.getfixturevalue("contexts")
    output = cache.with_name(f"{cache.name}-output")
    output.mkdir()
    monkeypatch.setattr(cli, "OUTPUT", output)
    monkeypatch.setattr(cli, "DEFAULT_CACHE_DIR", cache)
    return make, cache, output


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_chronological_unique_order_causality_and_half_life(local, capsys):
    make, cache, output = local
    make(90, NOW-HALF_LIFE_SECONDS)
    make(20, NOW)
    make(80, NOW+1)
    make(10, None)
    target = output / "experience.jsonl"
    assert cli.main(["--out", str(target)]) == 0
    report = json.loads(capsys.readouterr().out)
    records = read_jsonl(target)
    assert [r["match_id"] for r in records] == [90, 20, 1000, 80, 10]
    assert len({r["match_id"] for r in records}) == len(records)
    assert report["target_matches"] == 5 and report["team_rows"] == 10 and report["player_rows"] == 50
    for mid in (20, 1000):
        row = next(r for r in records if r["match_id"] == mid)
        assert row["teams"]["RADIANT"]["prior_match_count"] == 1
        assert row["teams"]["RADIANT"]["decayed_prior_match_count"] == 0.5
        assert row["players"][0]["prior_hero_match_count"] == 1
        assert row["players"][0]["decayed_prior_hero_match_count"] == 0.5
        assert row["coverage"]["excluded"]["same_start_time"] == 1
        assert row["coverage"]["excluded"]["future_start_time"] == 1
        assert row["coverage"]["excluded"]["target_match"] == 1
        assert row["coverage"]["excluded"]["unknown_start_time"] == 1
        assert row["coverage"]["available_context_matches"] == 5
        assert row["coverage"]["eligible_prior_matches"] == 1
        assert row["coverage"]["complete_career_history"] is False
        assert "prior_match_ids" not in row["coverage"]
    assert records[0]["teams"]["RADIANT"]["status"] == "NO_OBSERVED_HISTORY"
    assert records[0]["teams"]["RADIANT"]["prior_match_count"] == 0
    assert records[-1]["status"] == "UNKNOWN_TARGET_START_TIME"
    assert records[-1]["players"][0]["prior_match_count"] is None
    assert records[-1]["coverage"]["eligible_prior_matches"] is None


def test_target_selection_and_limit_do_not_truncate_history_and_reuse_builder(local, monkeypatch, capsys):
    make, cache, output = local
    make(1, NOW-HALF_LIFE_SECONDS)
    make(2, NOW+1)
    calls = []
    original = cli.build_historical_experience
    def build(mid, history_ids, *, cache_dir):
        calls.append((mid, list(history_ids)))
        return original(mid, history_ids, cache_dir=cache_dir)
    monkeypatch.setattr(cli, "build_historical_experience", build)
    target = output / "limited.jsonl"
    assert cli.main(["--match-id", "1000", "--match-id", "1000", "--match-id", "2",
                     "--limit", "1", "--out", str(target)]) == 0
    capsys.readouterr()
    assert calls == [(1000, [1, 2, 1000])]
    rows = read_jsonl(target)
    assert len(rows) == 1
    assert rows[0]["coverage"]["available_context_matches"] == 3
    assert rows[0]["players"][0]["decayed_prior_match_count"] == 0.5


def test_nulls_ids_sides_provenance_and_no_outcomes(local):
    make, cache, output = local
    accounts, heroes = list(range(2**53, 2**53+10)), list(range(1, 11))
    accounts[0], heroes[1] = None, None
    make(1000, teams=(None, 20), accounts=accounts, heroes=heroes,
         radiant_win=True, future_target=99999, final_rating=99999)
    target = output / "nulls.jsonl"
    assert cli.main(["--out", str(target)]) == 0
    row, = read_jsonl(target)
    assert row["start_time"] == NOW and row["match_id"] == 1000
    assert row["teams"]["RADIANT"]["prior_match_count"] is None
    assert row["teams"]["RADIANT"]["status"] == "UNKNOWN_ID"
    assert row["players"][0]["account_id"] is row["players"][0]["team_id"] is None
    assert row["players"][1]["account_id"] == 2**53+1
    assert row["players"][1]["hero_id"] is row["players"][1]["prior_hero_match_count"] is None
    assert row["players"][5]["team"] == "DIRE" and row["players"][5]["team_id"] == 20
    assert row["provenance"] == "HISTORICAL_OBSERVED_EXPERIENCE"
    assert row["context_provenance"] == "HISTORICAL_POST_DRAFT_CONTEXT"
    assert all(key not in row for key in ("radiant_win", "future_target", "final_rating"))


def test_dry_run_writes_nothing_and_repeated_export_is_identical(local, capsys):
    make, cache, output = local
    make(1, NOW-HALF_LIFE_SECONDS)
    before = {p: p.read_bytes() for p in cache.glob("*.context.json")}
    assert cli.main(["--limit", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "DRY_RUN" and report["out"] is None
    assert list(output.iterdir()) == []
    assert {p: p.read_bytes() for p in cache.glob("*.context.json")} == before
    target = output / "repeat.jsonl"
    assert cli.main(["--out", str(target)]) == 0
    capsys.readouterr()
    first = target.read_bytes()
    assert cli.main(["--out", str(target)]) == 0
    capsys.readouterr()
    assert target.read_bytes() == first
    assert {p: p.read_bytes() for p in cache.glob("*.context.json")} == before


@pytest.mark.parametrize("case", ["outside_output", "inside_cache", "source_cache", "missing_parent"])
def test_unsafe_output_is_rejected_without_touching_sources(local, case, capsys):
    _, cache, output = local
    if case == "outside_output":
        target = cache.parent / "outside.jsonl"
    elif case == "inside_cache":
        target = cache / "result.jsonl"
    elif case == "source_cache":
        target = cache / "1000.context.json"
    else:
        target = output / "missing/result.jsonl"
    source = (cache / "1000.context.json").read_bytes()
    assert cli.main(["--out", str(target)]) == 1
    assert "--out" in capsys.readouterr().err
    assert (cache / "1000.context.json").read_bytes() == source


@pytest.mark.parametrize("case", ["unknown_target", "zero_limit", "bad_checksum", "duplicate_identity"])
def test_invalid_inputs_leave_previous_result_untouched(local, case, capsys):
    _, cache, output = local
    target = output / "existing.jsonl"
    target.write_text("previous result\n")
    args = ["--out", str(target)]
    if case == "unknown_target":
        args += ["--match-id", "999"]
    elif case == "zero_limit":
        args += ["--limit", "0"]
    elif case == "bad_checksum":
        path = cache / "1000.context.json"
        value = json.loads(path.read_text())
        value["start_time"] += 1
        path.write_text(json.dumps(value))
    else:
        (cache / "01000.context.json").write_bytes((cache / "1000.context.json").read_bytes())
    assert cli.main(args) == 1
    assert "stopped" in capsys.readouterr().err
    assert target.read_text() == "previous result\n"
    assert not list(output.glob("*.tmp"))


def test_atomic_write_failure_preserves_existing_result(local, monkeypatch, capsys):
    _, _, output = local
    target = output / "result.jsonl"
    target.write_text("previous\n")
    def fail(*args):
        raise OSError("write failed")
    monkeypatch.setattr(cli.os, "replace", fail)
    assert cli.main(["--out", str(target)]) == 1
    assert "write failed" in capsys.readouterr().err
    assert target.read_text() == "previous\n"
    assert list(output.iterdir()) == [target]
