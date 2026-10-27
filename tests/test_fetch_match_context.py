from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from app.replay_fetcher import OpenDotaRateLimitError, OpenDotaRequestError, ReplayFetcher
from tests.test_import_compact_economy import compact_fixture
from tools import fetch_match_context as cli


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "OUTPUT", output)
    monkeypatch.setattr(cli, "DESTINATION", output / "match-context")
    checkpoint = output / "manifest.json"
    monkeypatch.setattr(cli, "CHECKPOINT", checkpoint)
    state = dict(candidates=[], matches={})
    payloads = {}

    def make(mid, *, local=False, status="PARSE_OK"):
        folder = output / str(mid)
        folder.mkdir()
        compact = compact_fixture(mid)
        compact["picks_bans"] = [dict(is_pick=True, team=2 if i < 5 else 3, hero_id=i+1)
                                  for i in range(10)] + [dict(is_pick=False, team=2, hero_id=20)]
        meta = dict(match_id=mid, start_time=1780000000, leagueid=19719,
                    radiant_team_id=101, dire_team_id=202, patch=60,
                    players=[dict(player_slot=i if i < 5 else 128+i-5,
                                  hero_id=i+1, account_id=1000+i, final_kda=999)
                             for i in range(10)],
                    picks_bans=[dict(r, team=r["team"]-2, order=i)
                                for i, r in enumerate(compact["picks_bans"])],
                    radiant_win=True, future_targets=[999])
        payloads[mid] = meta
        path = folder / f"{mid}.compact.json"
        path.write_text(json.dumps(compact))
        artifacts = [dict(path=str(path.relative_to(tmp_path)), sha256=cli.sha256(path))]
        if local:
            source = folder / "opendota.json"
            source.write_text(json.dumps(meta))
            artifacts.append(dict(path=str(source.relative_to(tmp_path)), sha256=cli.sha256(source)))
        entry = dict(match_id=mid, league_id=19719, status=status,
                     dem_sha256=compact["replay_sha256"], artifacts=artifacts)
        catalog = dict(match_id=mid, start_time=1780000000, league_id=19719)
        state["matches"][str(mid)] = entry
        state["candidates"].append(catalog)
        checkpoint.write_text(json.dumps(state))
        return dict(entry=entry, catalog=catalog, compact=artifacts[0],
                    metadata=artifacts[1] if local else None), compact, meta

    return make, payloads, state


@pytest.fixture
def api(inputs, monkeypatch):
    _, payloads, _ = inputs
    calls = []
    def client(**kwargs):
        assert kwargs["api_key"] == "" and kwargs["request_delay_seconds"] >= 3
        def get(endpoint, *, max_attempts):
            assert endpoint.startswith("matches/") and max_attempts == 1
            mid = int(endpoint.split('/')[-1])
            calls.append(mid)
            value = payloads[mid]
            if isinstance(value, Exception):
                raise value
            return SimpleNamespace(payload=deepcopy(value))
        return SimpleNamespace(_fetch_opendota_json=get)
    monkeypatch.setattr(cli, "ReplayFetcher", client)
    return calls


def result(capsys):
    return json.loads(capsys.readouterr().out)


def test_dry_run_is_local_only_and_reports_extra_invalid_and_duplicate_compacts(inputs, api, capsys):
    make, _, _ = inputs
    record, _, _ = make(1, local=True)
    make(2)
    make(3, status="INVALID_ARTIFACT")
    copy = cli.OUTPUT / "diagnostic"
    copy.mkdir()
    (copy / "1.compact.json").write_bytes(cli.source_path(record["compact"]["path"]).read_bytes())
    before = cli.sha256(cli.CHECKPOINT)
    assert cli.main([]) == 0
    report = result(capsys)
    assert report["statuses"] == {"LOCAL": 1, "NEEDS_API": 1}
    assert report["requests"] == 0 and api == [] and not cli.DESTINATION.exists()
    assert report["input_audit"]["parse_ok_matches"] == 2
    assert report["input_audit"]["compact_files"] == 4
    assert report["input_audit"]["unique_compact_match_ids"] == 3
    assert {e["reason"] for e in report["input_audit"]["noncanonical_compacts"]} == {"INVALID_ARTIFACT", "DUPLICATE_NONCANONICAL_COPY"}
    assert cli.sha256(cli.CHECKPOINT) == before


def test_limit_reuse_cache_resume_and_explicit_whitelist(inputs, api, capsys):
    make, _, _ = inputs
    record, _, _ = make(1, local=True)
    make(2)
    make(3)
    original = cli.source_path(record["metadata"]["path"]).read_bytes()
    assert cli.main(["--run", "--limit", "1"]) == 0
    report = result(capsys)
    assert api == [2] and report["stop_reason"] == "LIMIT_REACHED"
    assert report["statuses"] == {"LOCAL": 1, "FETCHED": 1, "DEFERRED": 1}
    saved = {p.name: p.read_bytes() for p in cli.DESTINATION.glob("*.context.json")}
    one = json.loads(saved["1.context.json"])
    assert set(one) == set(cli.CORE) | {"schema_version", "provenance", "content_sha256"}
    assert one["players"][5] == dict(player_slot=5, opendota_player_slot=128,
        team="DIRE", hero_id=6, account_id=1005)
    assert one["picks_bans"][5]["order"] == 5 and one["patch"] == 60
    assert "final_kda" not in one["players"][0] and "future_targets" not in one
    assert len(saved["1.context.json"]) < 20000
    assert cli.main(["--run", "--limit", "1"]) == 0
    result(capsys)
    assert api == [2, 3]
    assert all((cli.DESTINATION / name).read_bytes() == data for name, data in saved.items())
    assert cli.main(["--run"]) == 0
    report = result(capsys)
    assert api == [2, 3] and report["statuses"] == {"CACHED": 3}
    assert json.loads((cli.DESTINATION / "report.json").read_text())["coverage_totals"] == report["coverage_totals"]
    assert cli.source_path(record["metadata"]["path"]).read_bytes() == original


def test_nulls_are_not_zero_and_missing_draft_is_not_invented(inputs):
    make, _, _ = inputs
    record, compact, meta = make(1)
    meta.update(start_time=None, radiant_team_id=None, dire_team_id=None, patch=None, picks_bans=None)
    compact.update(radiant_team_id=None, dire_team_id=None)
    record["catalog"]["start_time"] = None
    for p in meta["players"]:
        p["account_id"] = None
    meta["players"][0]["account_id"] = 4294967295
    core = cli.normalize(meta, compact, record)
    assert core["start_time"] is core["patch"] is core["radiant_team_id"] is None
    assert all(p["account_id"] is None for p in core["players"])
    assert all(p["order"] is None for p in core["picks_bans"])
    compact["picks_bans"] = []
    meta["players"] = None
    core = cli.normalize(meta, compact, record)
    assert core["picks_bans"] is None and cli.coverage(core)["draft_null"] == 1
    assert all(p["opendota_player_slot"] is None for p in core["players"])


@pytest.mark.parametrize("damage", ["match", "league", "team", "time", "hero", "duplicate_slot", "slot", "order", "draft", "side", "shape"])
def test_source_mismatches_are_explicit_and_never_replaced(inputs, damage):
    make, _, _ = inputs
    record, compact, meta = make(1)
    if damage == "match":
        meta["match_id"] = 2
    elif damage == "league":
        meta["leagueid"] = 999
    elif damage == "team":
        meta["radiant_team_id"] = 999
    elif damage == "time":
        meta["start_time"] += 1
    elif damage == "hero":
        meta["players"][0]["hero_id"] = 999
    elif damage == "duplicate_slot":
        meta["players"][1]["player_slot"] = 0
    elif damage == "slot":
        meta["players"][0]["player_slot"] = 6
    elif damage == "order":
        meta["picks_bans"][1]["order"] = 0
    elif damage == "draft":
        meta["picks_bans"][0]["hero_id"] = 999
    elif damage == "side":
        meta["players"][0]["isRadiant"] = False
    else:
        meta["players"] = {"invalid": "shape"}
    with pytest.raises(ValueError):
        cli.normalize(meta, compact, record)


def test_bad_local_sha_is_reported_without_api_fallback(inputs, api, capsys):
    make, _, _ = inputs
    record, _, _ = make(1, local=True)
    source = cli.source_path(record["metadata"]["path"])
    source.write_text('{}')
    assert cli.main(["--run"]) == 1
    report = result(capsys)
    assert "SHA mismatch" in report["matches"][0]["error"] and not api
    assert not (cli.DESTINATION / "1.context.json").exists()


@pytest.mark.parametrize("damage", ["checksum", "identity", "target"])
def test_invalid_cache_is_not_refetched_or_overwritten(inputs, api, capsys, damage):
    make, _, _ = inputs
    make(1, local=True)
    assert cli.main(["--run"]) == 0
    result(capsys)
    path = cli.DESTINATION / "1.context.json"
    value = json.loads(path.read_text())
    if damage == "target":
        value["target"] = 1
    else:
        value["players"][0]["hero_id"] = 999
    if damage != "checksum":
        value["content_sha256"] = cli.digest({k: v for k,v in value.items() if k != "content_sha256"})
    path.write_text(json.dumps(value))
    original = path.read_bytes()
    assert cli.main(["--run"]) == 1
    assert result(capsys)["matches"][0]["status"] == "ERROR"
    assert not api and path.read_bytes() == original


def test_429_stops_without_extra_requests_and_preserves_retry_after(inputs, api, capsys, monkeypatch):
    make, payloads, _ = inputs
    make(1, local=True)
    make(2)
    make(3)
    payloads[2] = OpenDotaRateLimitError("900", attempts=1)
    monkeypatch.setattr(cli.time, "time", lambda: 1000)
    assert cli.main(["--run"]) == 1
    report = result(capsys)
    assert api == [2] and report["stop_reason"] == "HTTP_429"
    assert json.loads((cli.DESTINATION / "state.json").read_text())["resume_after_epoch"] == 1900
    assert cli.main(["--run"]) == 1
    assert result(capsys)["stop_reason"] == "COOLDOWN" and api == [2]


@pytest.mark.parametrize("status,requests,reason", [(None,3,"SOURCE_UNAVAILABLE"),(403,1,"HTTP_403")])
def test_systemic_or_access_errors_stop_without_bypasses(inputs, api, capsys, status, requests, reason):
    make, payloads, _ = inputs
    for mid in range(1,6):
        make(mid)
        payloads[mid] = OpenDotaRequestError("unavailable",reason="request_timeout",attempts=1,http_status=status)
    assert cli.main(["--run"]) == 1
    report = result(capsys)
    assert len(api) == requests and report["stop_reason"] == reason
    assert all(m["status"] == "DEFERRED" for m in report["matches"][requests:])


def test_disk_floor_warning_and_mid_request_stop(inputs, api, capsys, monkeypatch):
    make, _, _ = inputs
    make(1)
    free = cli.FLOOR + cli.WRITE_RESERVE - 1
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda _: SimpleNamespace(free=free))
    assert cli.main(["--run"]) == 1 and not api and not cli.DESTINATION.exists()
    assert "LOW_DISK_SPACE" in capsys.readouterr().err
    free = cli.FLOOR + cli.WRITE_RESERVE + 128 * 1024
    assert cli.main([]) == 0 and result(capsys)["warnings"]
    def client(**kwargs):
        def get(*args, **kwargs):
            nonlocal free
            free = cli.FLOOR + 1
            return SimpleNamespace(payload=inputs[1][1])
        return SimpleNamespace(_fetch_opendota_json=get)
    monkeypatch.setattr(cli, "ReplayFetcher", client)
    assert cli.main(["--run"]) == 1
    assert result(capsys)["stop_reason"] == "LOW_DISK_SPACE"
    assert not (cli.DESTINATION / "1.context.json").exists()


def test_reused_get_client_uses_no_key_even_when_environment_has_one(inputs, capsys, monkeypatch):
    make, payloads, _ = inputs
    make(1)
    monkeypatch.setenv("OPENDOTA_API_KEY", "must-not-be-used")
    requests = []
    def opener(request, **kwargs):
        requests.append(request)
        return BytesIO(json.dumps(payloads[1]).encode())
    def client(**kwargs):
        assert kwargs["api_key"] == "" and kwargs["request_delay_seconds"] == 3
        return ReplayFetcher(api_key="", request_delay_seconds=0, urlopen_func=opener)
    monkeypatch.setattr(cli, "ReplayFetcher", client)
    assert cli.main(["--run"]) == 0
    assert result(capsys)["requests"] == 1
    assert requests[0].get_method() == "GET"
    assert requests[0].full_url == "https://api.opendota.com/api/matches/1"


def test_invalid_api_payloads_stop_after_three_without_caching_them(inputs, api, capsys):
    make, payloads, _ = inputs
    for mid in range(1, 6):
        make(mid)
        payloads[mid] = []
    assert cli.main(["--run"]) == 1
    report = result(capsys)
    assert api == [1, 2, 3] and report["stop_reason"] == "SOURCE_UNAVAILABLE"
    assert not list(cli.DESTINATION.glob("*.context.json"))


def test_atomic_write_failure_stops_without_requests_or_source_changes(inputs, api, capsys, monkeypatch):
    make, _, _ = inputs
    record, _, _ = make(1, local=True)
    make(2)
    source = cli.source_path(record["metadata"]["path"])
    original = source.read_bytes()
    def fail(*args):
        raise OSError("mock write failure")
    monkeypatch.setattr(cli, "save", fail)
    assert cli.main(["--run"]) == 1
    report = result(capsys)
    assert report["stop_reason"] == "WRITE_ERROR" and not api
    assert report["matches"][1]["status"] == "DEFERRED"
    assert source.read_bytes() == original and not (cli.DESTINATION / "1.context.json").exists()


def test_retry_after_http_date_is_not_shortened(monkeypatch):
    monkeypatch.setattr(cli.time, "time", lambda: 1000)
    error = SimpleNamespace(retry_after="Thu, 01 Jan 1970 00:31:40 GMT")
    assert cli.cooldown(error) == 1900
