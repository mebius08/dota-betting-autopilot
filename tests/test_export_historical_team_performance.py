from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from tests.test_historical_experience import NOW, contexts  # noqa: F401 -- shared local caches
from tests.test_historical_team_performance import match
from tools import export_historical_experience as output_helpers
from tools import export_historical_team_performance as cli


@pytest.fixture
def local(request, monkeypatch):
    make, cache = request.getfixturevalue("contexts")
    output = cache.with_name(f"{cache.name}-output")
    output.mkdir()
    checkpoint = cache / "manifest.json"
    state = dict(matches={"1000": dict(match_id=1000, status="PARSE_OK", dem_sha256="a" * 64)})
    def add(mid, time=NOW, status="PARSE_OK", **kwargs):
        make(mid, time, **kwargs)
        state["matches"][str(mid)] = dict(match_id=mid, status=status, dem_sha256="a" * 64)
        checkpoint.write_text(json.dumps(state))
    add(1000)
    monkeypatch.setattr(cli, "CHECKPOINT", checkpoint)
    monkeypatch.setattr(cli, "DEFAULT_CACHE_DIR", cache)
    monkeypatch.setattr(output_helpers, "OUTPUT", output)
    return add, cache, output, state, checkpoint


@pytest.fixture
def database(monkeypatch):
    data = dict(rows=[match(1, win=False), match(2, end=NOW-12*3600)], connections=[], queries=[])
    monkeypatch.setattr(cli, "postgres_connection_options_from_env", lambda: dict(host="unused", dbname="existing"))
    @contextmanager
    def connect(**options):
        data["connections"].append(options)
        def execute(query):
            data["queries"].append(query)
            return SimpleNamespace(fetchall=lambda: deepcopy(data["rows"]))
        yield SimpleNamespace(execute=execute)
    monkeypatch.setattr(cli.psycopg, "connect", connect)
    return data


def read_records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_one_read_only_query_dry_run_parse_ok_selection_and_limit(local, database, capsys):
    add, cache, output, _, _ = local
    add(2000, NOW+10)
    add(3000, NOW+20)
    add(4000, NOW+30)
    add(5, NOW-1, status="PARSE_ERROR")
    before = {p.name: p.read_bytes() for p in cache.iterdir()}
    assert cli.main(["--limit", "3"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "DRY_RUN" and report["out"] is None and report["target_matches"] == 3
    assert [row["match_id"] for row in report["targets"]] == [1000, 2000, 3000]
    assert database["queries"] == [cli.QUERY]
    assert cli.QUERY.startswith("SELECT ") and "public.opendota_pro_matches" in cli.QUERY
    assert len(database["connections"]) == 1
    assert "default_transaction_read_only=on" in database["connections"][0]["options"]
    assert database["connections"][0]["connect_timeout"] == 5
    assert list(output.iterdir()) == []
    assert {p.name: p.read_bytes() for p in cache.iterdir()} == before


def test_buffer_false_result_unknown_result_and_unchanged_decay(local, database, capsys):
    _, _, output, _, _ = local
    database["rows"] += [match(3, win=None), match(4, end=NOW+1),
                         match(5, end=NOW+7200), dict(match(1000), start_time=NOW),
                         dict(match(6), duration_seconds=None)]
    path = output / "performance.jsonl"
    assert cli.main(["--limit", "3", "--out", str(path)]) == 0
    capsys.readouterr()
    record, = read_records(path)
    radiant = record["teams"]["RADIANT"]
    assert radiant["prior_match_count"] == 1 and radiant["losses"] == 1 and radiant["wins"] == 0
    assert radiant["weighted_matches"] == 0.5  # True target time, not target minus buffer.
    assert radiant["weighted_win_fraction"] == 0
    assert record["teams"]["DIRE"]["weighted_win_fraction"] == 1
    assert record["coverage"]["excluded"]["unknown_result"] == 1
    assert record["buffer_coverage"]["excluded"] == dict(
        target_match=1, unknown_timing=1, estimated_end_not_before_target=2, within_buffer=1)
    assert record["catalog_coverage"]["fields"]["radiant_win"]["known"] == 6
    assert record["catalog_coverage"]["fields"]["radiant_win"]["null"] == 1
    assert record["temporal_policy"]["buffer_seconds"] == 86400
    assert record["temporal_policy"]["result_availability_proven"] is False
    assert record["temporal_policy"]["estimated_end_is_confirmed_wall_clock"] is False
    assert record["context_provenance"] == "HISTORICAL_POST_DRAFT_CONTEXT"
    assert "radiant_win" not in record and "radiant_win" not in radiant


def test_strict_buffer_boundary_and_custom_buffer(local):
    _, cache, _, _, checkpoint = local
    targets = cli.load_targets(checkpoint, cache, 3)
    rows = [match(1, end=NOW-86400), match(2, end=NOW-86401), match(3, end=NOW-7200)]
    records, _ = cli.build_records(targets, rows)
    assert records[0]["teams"]["RADIANT"]["prior_match_count"] == 1
    assert records[0]["buffer_coverage"]["excluded"]["within_buffer"] == 2
    records, _ = cli.build_records(targets, rows, buffer_seconds=3600)
    assert records[0]["teams"]["RADIANT"]["prior_match_count"] == 3


def test_factual_nullable_field_coverage_and_duplicate_counts():
    rows = [match(1, win=False), dict(match(2), start_time=None),
            dict(match(3), duration_seconds=0, radiant_team_id=0, radiant_win=None), match(1, win=False)]
    unique, summary = cli.catalog_coverage(rows)
    assert len(unique) == 3 and summary["rows_read"] == 4 and summary["duplicate_rows"] == 1
    assert summary["usable_result_records"] == 1
    assert summary["radiant_win_false"] == 1
    assert summary["fields"]["start_time"] == dict(known=2, null=1, zero_unknown=0)
    assert summary["fields"]["duration_seconds"] == dict(known=2, null=0, zero_unknown=1)


def test_null_target_time_and_team_are_not_filled_from_catalog(local, database, capsys):
    add, _, output, _, _ = local
    add(1000, None, teams=(None, 20))
    database["rows"] += [dict(match(1000), start_time=NOW)]
    path = output / "nulls.jsonl"
    assert cli.main(["--limit", "3", "--out", str(path)]) == 0
    capsys.readouterr()
    record, = read_records(path)
    assert record["start_time"] is None
    assert record["teams"]["RADIANT"]["team_id"] is None
    assert record["teams"]["RADIANT"]["prior_match_count"] is None
    assert record["buffer_coverage"]["time_eligible_records"] is None


def test_empty_usable_history_preserves_unknown_fraction(local, database, capsys):
    database["rows"] = [match(1, end=NOW-3600), match(2, win=None)]
    path = local[2] / "empty.jsonl"
    assert cli.main(["--out", str(path)]) == 0
    capsys.readouterr()
    record, = read_records(path)
    assert record["teams"]["RADIANT"]["prior_match_count"] == 0
    assert record["teams"]["RADIANT"]["weighted_win_fraction"] is None
    assert record["teams"]["RADIANT"]["status"] == "NO_OBSERVED_HISTORY"


def test_duplicate_deterministic_atomic_export_without_target_outcome_leak(local, database, capsys):
    add, _, output, _, _ = local
    add(900, NOW)
    database["rows"] += [match(1, win=False)]
    path = output / "repeat.jsonl"
    assert cli.main(["--out", str(path)]) == 0
    capsys.readouterr()
    first = path.read_bytes()
    records = read_records(path)
    assert [r["match_id"] for r in records] == [900, 1000]
    assert len({r["match_id"] for r in records}) == 2
    database["rows"].reverse()
    assert cli.main(["--out", str(path)]) == 0
    capsys.readouterr()
    assert path.read_bytes() == first
    assert records[0]["teams"]["RADIANT"]["prior_match_count"] == 1


@pytest.mark.parametrize("damage", ["checkpoint_sha", "catalog_identity", "duplicate_result"])
def test_conflicting_sources_leave_existing_output_intact(local, database, capsys, damage):
    _, _, output, state, checkpoint = local
    path = output / "existing.jsonl"
    path.write_text("previous\n")
    if damage == "checkpoint_sha":
        state["matches"]["1000"]["dem_sha256"] = "b" * 64
        checkpoint.write_text(json.dumps(state))
    elif damage == "catalog_identity":
        database["rows"] += [dict(match(1000), start_time=NOW+1)]
    else:
        database["rows"] += [match(1, win=True)]
    assert cli.main(["--out", str(path)]) == 1
    assert "stopped" in capsys.readouterr().err
    assert path.read_text() == "previous\n"
    assert list(output.iterdir()) == [path]


@pytest.mark.parametrize("args", [["--limit", "0"], ["--buffer-hours", "0"], ["--buffer-hours", "-1"]])
def test_bad_arguments_fail_before_database_access(local, database, args, capsys):
    assert cli.main(args) == 1
    assert "positive" in capsys.readouterr().err
    assert database["connections"] == []


def test_database_failure_has_no_fallback_or_partial_result(local, monkeypatch, capsys):
    path = local[2] / "failed.jsonl"
    def unavailable():
        raise cli.psycopg.OperationalError("Existing PostgreSQL is unavailable")
    monkeypatch.setattr(cli, "read_catalog", unavailable)
    assert cli.main(["--limit", "3", "--out", str(path)]) == 1
    assert "PostgreSQL is unavailable" in capsys.readouterr().err
    assert not path.exists()


def test_cache_output_path_is_rejected_before_query(local, database, capsys):
    assert cli.main(["--out", str(local[1] / "result.jsonl")]) == 1
    assert "--out" in capsys.readouterr().err
    assert database["connections"] == []
