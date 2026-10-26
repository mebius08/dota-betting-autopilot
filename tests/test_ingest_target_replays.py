from copy import deepcopy
import json
import signal
from types import SimpleNamespace
import subprocess
import time
from urllib.error import HTTPError

import pytest

from app.replay_fetcher import OpenDotaRateLimitError, ReplayDownloadError
from app.replay_trajectory import AGGREGATE_FIELDS
from app.tournament_allowlist import load_tournament_allowlist
from tools import ingest_target_replays as cli


def candidate(mid, league=19719, start=100):
    return dict(match_id=mid, league_id=league, league_name="Selected", start_time=start)


def test_newest_first_and_reproducible_league_diversity():
    rows = [candidate(5, 1, 90), candidate(6, 1, 100),
            candidate(3, 2, 70), candidate(4, 2, 80), candidate(2, 3, 60)]
    assert [r["match_id"] for r in cli.diverse_order(rows)] == [6, 4, 2, 5, 3]
    assert cli.diverse_order(rows) == cli.diverse_order(list(reversed(rows)))
    with pytest.raises(ValueError, match="Duplicate"):
        cli.diverse_order(rows + [rows[0]])


@pytest.mark.parametrize("damage", ["league", "time", "identity"])
def test_candidate_selection_remains_read_only_and_allowlisted(monkeypatch, damage):
    from contextlib import contextmanager

    row = candidate(1, start=2_000_000_000)
    if damage == "league":
        row["league_id"] = 20053
    elif damage == "time":
        row["start_time"] += 1
    else:
        row["match_id"] = 2
    monkeypatch.setattr(cli, "audit_catalog", lambda *a, **k: ({}, [1]))

    class Connection:
        @contextmanager
        def transaction(self):
            yield

        def execute(self, query, params=None):
            assert query == cli.READ_ONLY or query.startswith("SELECT ")
            if params is not None:
                assert params == ([1],)
            return SimpleNamespace(fetchall=lambda: [row])

    with pytest.raises(ValueError):
        cli.candidates(Connection(), load_tournament_allowlist(), 2_000_000_000)


@pytest.fixture
def batch(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    path = tmp_path / "batch/manifest.json"
    path.parent.mkdir()
    state = dict(matches={}, candidates=[candidate(1), candidate(2)], as_of=100)
    return state, path


class Fetcher:
    def __init__(self):
        self.details = []
        self.downloads = []
        self.no_url = False
        self.failure = None

    def _fetch_opendota_json(self, endpoint, *, max_attempts):
        self.details.append(endpoint)
        assert max_attempts == 1  # No capped Retry-After retry on HTTP 429.
        mid = int(endpoint.split("/")[-1])
        return SimpleNamespace(payload=dict(
            match_id=mid, leagueid=19719,
            replay_url=None if self.no_url else f"http://replay1.valve.net/570/{mid}_1.dem.bz2",
        ))

    def _download_replay(self, *, replay_url, match_id, destination, progress_callback):
        self.downloads.append(match_id)
        if self.failure:
            raise self.failure
        destination.write_bytes(b"PBDEMS2\0" + str(match_id).encode())
        progress_callback(SimpleNamespace(downloaded_bytes=123))


@pytest.fixture
def parsed(monkeypatch):
    def parse(directory, mid, league, sha):
        for suffix in ("compact.json", "compact.spatial.json", "compact.visibility.json"):
            (directory / f"{mid}.{suffix}").write_text("{}")
        (directory / "pov").mkdir(exist_ok=True)
        for name in ("team_pov_features.csv", "enemy_observations.csv"):
            (directory / "pov" / name).write_text("header\n")
        return dict(economy_snapshots=1, spatial_snapshots=6, pov_team_rows=12)
    monkeypatch.setattr(cli, "parse_replay", parse)
    return parse


def test_bounded_run_and_resume_never_redownload_completed(batch, parsed):
    state, path = batch
    fetcher = Fetcher()
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert state["matches"]["1"]["status"] == "PARSE_OK"
    assert state["stop_reason"] == "LIMIT_REACHED"
    saved = json.loads(path.read_text())
    cli.verify_saved(saved["matches"]["1"])
    cli.execute_batch(saved, path, fetcher, limit=1, min_free_bytes=0)
    assert fetcher.downloads == [1, 2]
    assert all(e["status"] == "PARSE_OK" for e in saved["matches"].values())
    cli.execute_batch(saved, path, fetcher, limit=100, min_free_bytes=0)
    assert saved["last_run_attempted"] == 0
    assert fetcher.downloads == [1, 2]


def test_parse_failure_resumes_download_ok_without_download(batch, parsed, monkeypatch):
    state, path = batch
    state["candidates"] = state["candidates"][:1]
    fetcher = Fetcher()

    def failed(*args):
        raise subprocess.TimeoutExpired("Clarity", 600)

    monkeypatch.setattr(cli, "parse_replay", failed)
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert state["matches"]["1"]["status"] == "RETRYABLE_ERROR"
    assert state["matches"]["1"]["download_status"] == "DOWNLOAD_OK"
    monkeypatch.setattr(cli, "parse_replay", parsed)
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert state["matches"]["1"]["status"] == "PARSE_OK"
    assert fetcher.downloads == [1] and len(fetcher.details) == 1
    assert "reason" not in state["matches"]["1"]


def test_parser_error_preserves_dem_and_is_not_retried_forever(batch, monkeypatch):
    state, path = batch
    state["candidates"] = state["candidates"][:1]
    fetcher = Fetcher()

    def failed(*args):
        raise subprocess.CalledProcessError(1, "Clarity")

    monkeypatch.setattr(cli, "parse_replay", failed)
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert state["matches"]["1"]["status"] == "PARSE_ERROR"
    assert state["matches"]["1"]["dem_sha256"]
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert fetcher.downloads == [1]


@pytest.mark.parametrize("http_status", [404, 410])
def test_expired_replay_is_terminal_and_not_retried(batch, http_status):
    state, path = batch
    fetcher = Fetcher()
    fetcher.failure = ReplayDownloadError("expired", http_status=http_status)
    cli.execute_batch(state, path, fetcher, limit=2, min_free_bytes=0)
    assert {e["status"] for e in state["matches"].values()} == {"REPLAY_UNAVAILABLE"}
    cli.execute_batch(state, path, fetcher, limit=2, min_free_bytes=0)
    assert len(fetcher.downloads) == 2


def test_absent_url_does_not_download(batch):
    state, path = batch
    fetcher = Fetcher()
    fetcher.no_url = True
    cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert state["matches"]["1"]["reason"] == "NO_REPLAY_URL"
    assert fetcher.downloads == []


def test_retryable_match_has_bounded_attempts(batch):
    state, path = batch
    state["candidates"] = state["candidates"][:1]
    fetcher = Fetcher()
    fetcher.failure = ReplayDownloadError("network error")
    for _ in range(4):
        cli.execute_batch(state, path, fetcher, limit=1, min_free_bytes=0)
    assert fetcher.downloads == [1, 1, 1]
    assert state["matches"]["1"]["attempts"] == 3
    assert state["last_run_attempted"] == 0


def test_429_stops_immediately_and_preserves_full_retry_after(batch, monkeypatch):
    state, path = batch
    fetcher = Fetcher()

    def rate_limited(*args, **kwargs):
        raise OpenDotaRateLimitError("900", attempts=1)

    monkeypatch.setattr(fetcher, "_fetch_opendota_json", rate_limited)
    monkeypatch.setattr(cli.time, "time", lambda: 1000)
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=0)
    assert state["stop_reason"] == "HTTP_429"
    assert state["resume_after_epoch"] == 1900
    assert len(state["matches"]) == 1
    assert json.loads(path.read_text())["matches"]["1"]["status"] == "RETRYABLE_ERROR"


def test_valve_429_also_preserves_original_retry_after(batch, monkeypatch):
    state, path = batch
    fetcher = Fetcher()
    fetcher.failure = ReplayDownloadError("rate limited", http_status=429)
    fetcher.failure.__cause__ = HTTPError(
        "http://replay1.valve.net/570/1_1.dem.bz2", 429, "rate limited",
        {"Retry-After": "900"}, None,
    )
    monkeypatch.setattr(cli.time, "time", lambda: 1000)
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=0)
    assert state["resume_after_epoch"] == 1900
    assert state["stop_reason"] == "HTTP_429"
    assert fetcher.downloads == [1]


def test_disk_and_repeated_network_errors_stop_safely(batch, monkeypatch):
    state, path = batch
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda p: SimpleNamespace(free=0))
    fetcher = Fetcher()
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=1)
    assert state["stop_reason"] == "LOW_DISK_SPACE" and not fetcher.details
    state["candidates"] = [candidate(i) for i in range(1, 10)]
    fetcher.failure = ReplayDownloadError("network failed")
    cli.execute_batch(state, path, fetcher, limit=100, min_free_bytes=0)
    assert state["stop_reason"] == "REPEATED_ERRORS"
    assert len(fetcher.downloads) == 3


def test_sha_tampering_and_identity_mismatch_are_explicit(batch, parsed):
    state, path = batch
    cli.execute_batch(state, path, Fetcher(), limit=1, min_free_bytes=0)
    (path.parent / "1/1.dem").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA mismatch"):
        cli.verify_saved(state["matches"]["1"])
    entry = candidate(3, league=20395)
    with pytest.raises(ValueError, match="metadata conflicts"):
        cli.process(entry, path.parent / "3", Fetcher(), lambda: None)


@pytest.mark.parametrize("url", [
    "https://example.org/570/1_2.dem.bz2", "http://replay1.valve.net/570/2_2.dem.bz2",
    "file:///570/1_2.dem.bz2", "http://evilvalve.net/570/1_2.dem.bz2",
])
def test_never_substitutes_another_source_or_match(url):
    with pytest.raises(ValueError, match="selected match"):
        cli.check_url(url, 1)


def test_confirmed_opendota_chinese_replay_cdn_is_supported():
    cli.check_url("http://replay413.dota2.com.cn/570/8960762254_1790012770.dem.bz2", 8960762254)


def test_atomic_checkpoint_does_not_destroy_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    cli.save(path, {"saved": True})

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(cli.os, "replace", fail)
    with pytest.raises(OSError):
        cli.save(path, {"saved": False})
    assert json.loads(path.read_text()) == {"saved": True}
    assert list(tmp_path.iterdir()) == [path]


def test_streaming_deadline_raises_and_restores_signal_handler():
    handler = signal.getsignal(signal.SIGALRM)
    with pytest.raises(ReplayDownloadError, match="deadline_exceeded"):
        with cli.download_deadline(0.01):
            time.sleep(1)
    assert signal.getsignal(signal.SIGALRM) == handler
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def sources():
    header = dict(schema_version=1, match_id=1, replay_sha256="a" * 64,
                  clarity_version="4.0.1", clock_normalization_status="PROVEN",
                  clock_normalization_method="GAME_START_TIME_AND_PAUSE_TICKS",
                  zero_replay_tick=1000, game_end_time_seconds=65.0,
                  pause_ticks=0, pause_seconds=0.0, pause_witnessed=False)
    players = [dict(player_slot=i, team="RADIANT" if i < 5 else "DIRE",
                    hero_id=i+1, hero_name=f"hero_{i}", level=1, items=[],
                    **{f: 0 for f in AGGREGATE_FIELDS}) for i in range(10)]
    compact = dict(header, game_mode=2, league_id=19719, winner=2,
                   radiant_team_id=None, radiant_team_tag=None,
                   dire_team_id=None, dire_team_tag=None, picks_bans=[], snapshots=[])
    spatial = dict(header, artifact_kind="replay_hero_positions", snapshot_interval_seconds=10,
                   coordinate_system={"name": "SOURCE2_NATIVE_CELL_GRID"}, snapshots=[])
    visibility = dict(header, artifact_kind="replay_hero_visibility",
                      snapshot_interval_seconds=10, snapshots=[])
    for actual, tick, nominal in ((0.0, 1000, 0), (60.0002, 2800, 60)):
        frame = dict(source_game_time_seconds=actual, replay_tick=tick)
        compact["snapshots"].append(dict(frame, game_time_seconds=nominal, game_state=5,
            players=deepcopy(players), teams=[dict(team=team, **{f: 0 for f in AGGREGATE_FIELDS})
                                            for team in ("RADIANT", "DIRE")]))
        sf, vf = dict(frame, game_time_seconds=actual, players=[]), dict(frame, game_time_seconds=actual, players=[])
        for p in players:
            identity = dict(player_slot=p["player_slot"], team=p["team"], hero_id=p["hero_id"],
                            selected_hero_handle=p["hero_id"], hero_entity_handle=p["hero_id"],
                            hero_entity_class="CDOTA_Unit_Hero_Test")
            sf["players"].append(dict(identity, life_state=2, position_status="HERO_NOT_ALIVE",
                                      x=None, y=None))
            vf["players"].append(dict(identity, visible_to_radiant="HIDDEN", visible_to_dire="VISIBLE"))
        spatial["snapshots"].append(sf)
        visibility["snapshots"].append(vf)
    return compact, spatial, visibility


def write_sources(directory, data):
    for suffix, payload in zip(("compact.json", "compact.spatial.json", "compact.visibility.json"), data):
        (directory / f"1.{suffix}").write_text(json.dumps(payload))


def test_actual_channel_validation_preserves_dead_nulls_and_gold_limitation(tmp_path):
    write_sources(tmp_path, sources())
    result = cli.validate_parsed(tmp_path, 1, 19719, "a" * 64)
    assert result["economy_snapshots"] == 2
    assert result["spatial_snapshots"] == 2 and result["pov_team_rows"] == 4
    assert result["position_status_counts"] == {"HERO_NOT_ALIVE": 20}
    assert result["hidden_current_coordinates_absent"]
    assert result["current_gold"]["absence_of_zero_substitution_proven"] is False


@pytest.mark.parametrize("damage", ["sha", "roster", "tick", "coordinate", "observed_null"])
def test_corrupt_channels_never_become_parse_ok(tmp_path, damage):
    compact, spatial, visibility = sources()
    if damage == "sha":
        spatial["replay_sha256"] = "b" * 64
    elif damage == "roster":
        spatial["snapshots"][0]["players"][0]["hero_id"] = 2
    elif damage == "tick":
        spatial["snapshots"][1]["replay_tick"] = 999
    elif damage == "coordinate":
        spatial["snapshots"][0]["players"][0]["x"] = 0
    else:
        spatial["snapshots"][0]["players"][0]["position_status"] = "OBSERVED"
    write_sources(tmp_path, (compact, spatial, visibility))
    with pytest.raises(ValueError):
        cli.validate_parsed(tmp_path, 1, 19719, "a" * 64)
