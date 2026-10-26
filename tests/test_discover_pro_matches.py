from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json

import httpx
import psycopg
import pytest

from tools import discover_pro_matches as cli


AS_OF = 2_000_000_000
CUTOFF = AS_OF - 366 * 86400


def match(mid, start=AS_OF - 60, **extras):
    return dict(match_id=mid, start_time=start, duration=1234, leagueid=987654,
                radiant_team_id=100, dire_team_id=200, radiant_name="Radiant team", dire_name="Dire team",
                radiant_win=True, **extras)


def client_for(pages, requests=None):
    def handler(request):
        assert request.url.path == "/api/proMatches" and "api_key" not in request.url.params
        cursor = request.url.params.get("less_than_match_id")
        if requests is not None:
            requests.append(cursor)
        return httpx.Response(200, json=pages[cursor])
    return httpx.Client(transport=httpx.MockTransport(handler))


def run(client, path, **kwargs):
    return cli.discover(client, path, as_of=AS_OF, delay=0, sleep=lambda _: None, **kwargs)


class Connection:
    def __init__(self):
        self.rows = {}
        self.events = []
        self.fail_on = None

    @contextmanager
    def transaction(self):
        before = deepcopy(self.rows)
        try:
            yield
        except Exception:
            self.rows = before
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    def execute(self, query, params):
        assert query == cli.UPSERT  # No old tables are written.
        row = dict(zip(cli.COLUMNS, params, strict=True))
        mid = row["match_id"]
        row["raw_metadata"] = row["raw_metadata"].obj
        if mid == self.fail_on:
            raise psycopg.OperationalError("write failed")
        previous = self.rows.get(mid)
        if previous and previous["raw_metadata"] == row["raw_metadata"]:
            returned = None
        else:
            self.rows[mid] = (previous or {}) | row
            returned = {"inserted": previous is None}

        class Result:
            def fetchone(self):
                return returned
        return Result()


def test_pagination_covers_window_beyond_smoke_and_resumes_without_gaps(tmp_path):
    requests = []
    pages = {None: [match(500), match(400)], "400": [match(300, CUTOFF - 1), match(200, CUTOFF)],
             "200": [match(100, CUTOFF - 2)]}
    path = tmp_path / "checkpoint.json"
    with client_for(pages, requests) as client:
        smoke = run(client, path, max_pages=1)
        assert smoke["pages"] == 1 and smoke["next_cursor"] == 400 and not smoke["complete"]
        assert smoke["counts"]["window_matches"] == 2
        complete = run(client, path)
        assert complete["complete"] and complete["completion_reason"] == "OLDER_PAGE"
        assert complete["counts"]["window_matches"] == 3 and complete["counts"]["older_matches"] == 2
        assert complete["run_pages"] == 2 and complete["min_start_time"] == CUTOFF
        repeated = run(client, path)
        assert repeated["run_pages"] == 0 and all(n == 0 for n in repeated["run_counts"].values())
    assert requests == [None, "400", "200"]


def test_inclusive_window_unknown_fields_and_missing_optional_values():
    raw = dict(match_id=1, start_time=CUTOFF, new_provider_field={"nested": [1, "text"]})
    row = cli.parse_match(raw)
    assert row["radiant_team_id"] is row["radiant_team_name"] is row["radiant_win"] is None
    assert row["raw_metadata"] == raw
    payload = [match(4, AS_OF + 1), match(3, AS_OF), match(2, CUTOFF), match(1, CUTOFF - 1)]
    selected, window, cursor, info, reason = cli.parse_page(payload, None, CUTOFF, AS_OF)
    assert {r["match_id"] for r in selected} == {3, 2} and selected == window
    assert cursor == 1 and info["older_matches"] == info["newer_matches"] == 1 and reason is None


def test_unknown_date_is_retained_but_never_counted_as_within_window():
    selected, window, _, info, reason = cli.parse_page([match(3, None), match(2, 0), match(1, CUTOFF - 10)], None, CUTOFF, AS_OF)
    assert len(selected) == 2 and not window and info["unknown_date_matches"] == 2 and reason is None
    assert all(r["start_time"] is None for r in selected)
    assert selected[1]["raw_metadata"]["start_time"] == 0


def test_duplicate_ids_within_page_and_at_cursor_do_not_duplicate_matches():
    selected, _, cursor, info, _ = cli.parse_page([match(10), match(9), match(9)], 10, CUTOFF, AS_OF)
    assert len(selected) == 1 and cursor == 9 and info["duplicates"] == 2
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        cli.parse_page([match(10), match(10, duration_change=1)], None, CUTOFF, AS_OF)
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        cli.parse_page([match(10, unknown_field=True), match(10, unknown_field=1)], None, CUTOFF, AS_OF)


@pytest.mark.parametrize("payload", [[match(10)], [match(11)], {"unexpected": True}])
def test_ignored_or_nonprogressing_cursor_is_a_diagnostic_error(payload):
    with pytest.raises(ValueError):
        cli.parse_page(payload, 10, CUTOFF, AS_OF)


@pytest.mark.parametrize("change", [dict(match_id=None), dict(match_id=True), dict(start_time="bad"),
                                    dict(radiant_team_id=-1), dict(radiant_win=1)])
def test_invalid_known_fields_fail_before_any_write(change):
    raw = match(10) | change
    with pytest.raises(ValueError):
        cli.parse_match(raw)


@pytest.mark.parametrize(("retry_after", "required"), [("7", 7), ("Fri, 18 May 2033 03:33:30 GMT", 10)])
def test_http_429_respects_retry_after_and_never_sends_api_key(retry_after, required):
    calls, waits = [], []
    now = datetime.fromtimestamp(AS_OF, timezone.utc)

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_after}) if len(calls) == 1 else httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert cli.fetch_page(client, 123, delay=1.2, sleep=waits.append, now=now) == []
    assert len(calls) == 2 and waits == [1.2, required]
    assert calls[0].url.params["less_than_match_id"] == "123"
    assert "api_key" not in calls[0].url.params


def test_long_retry_after_stops_instead_of_retrying_early():
    requests, waits = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="rather than retrying early"):
            cli.fetch_page(client, None, sleep=waits.append)
    assert len(requests) == 1 and waits == [1.2]


@pytest.mark.parametrize("failure", ["network", "server", "json"])
def test_retryable_failures_have_bounded_attempts(failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "network":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(503) if failure == "server" else httpx.Response(200, content=b"broken JSON")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="after 3 attempts"):
            cli.fetch_page(client, None, sleep=lambda _: None)
    assert len(calls) == 3


def test_api_error_preserves_last_committed_cursor_for_resume(tmp_path):
    path = tmp_path / "checkpoint.json"
    with client_for({None: [match(200)]}) as client:
        run(client, path, max_pages=1)
    state = json.loads(path.read_text())
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        with pytest.raises(RuntimeError):
            run(client, path)
    failed = json.loads(path.read_text())
    assert failed["next_cursor"] == state["next_cursor"] == 200
    assert failed["counts"] == state["counts"] and failed["last_error"]
    with client_for({"200": [match(100, CUTOFF - 1)]}) as client:
        assert run(client, path)["complete"]


def test_transactional_idempotent_upsert_updates_metadata_and_preserves_source_statuses():
    connection = Connection()
    rows = [cli.parse_match(match(1)), cli.parse_match(match(2))]
    assert cli.upsert_page(connection, rows) == dict(inserted=2, updated=0, skipped=0)
    connection.rows[1].update(dem_status="DOWNLOADED", game_state_status="PARTIAL")
    assert cli.upsert_page(connection, rows) == dict(inserted=0, updated=0, skipped=2)
    rows[0] = cli.parse_match(match(1, new_provider_field="changed"))
    assert cli.upsert_page(connection, rows) == dict(inserted=0, updated=1, skipped=1)
    assert connection.rows[1]["dem_status"] == "DOWNLOADED" and connection.rows[1]["game_state_status"] == "PARTIAL"
    assert "dem_status" not in cli.UPSERT and "game_state_status" not in cli.UPSERT


def test_write_failure_rolls_back_whole_page_and_does_not_advance_checkpoint(tmp_path):
    connection = Connection()
    connection.fail_on = 100
    path = tmp_path / "checkpoint.json"
    with client_for({None: [match(200), match(100)]}) as client:
        with pytest.raises(psycopg.Error):
            run(client, path, apply=True, connection=connection)
    assert not connection.rows and "rollback" in connection.events
    state = json.loads(path.read_text())
    assert state["pages"] == 0 and state["next_cursor"] is None and state["counts"]["inserted"] == 0


def test_db_commit_precedes_atomic_cursor_save_and_replay_is_safe(tmp_path, monkeypatch):
    connection, path = Connection(), tmp_path / "checkpoint.json"
    save = cli.save_checkpoint

    def fail_after_commit(path, state):
        if state["pages"] == 1:
            assert "commit" in connection.events
            raise OSError("disk failure")
        save(path, state)

    monkeypatch.setattr(cli, "save_checkpoint", fail_after_commit)
    with client_for({None: [match(100)]}) as client:
        with pytest.raises(OSError):
            run(client, path, apply=True, connection=connection, max_pages=1)
    assert 100 in connection.rows and json.loads(path.read_text())["next_cursor"] is None
    monkeypatch.setattr(cli, "save_checkpoint", save)
    with client_for({None: [match(100)]}) as client:
        resumed = run(client, path, apply=True, connection=connection, max_pages=1)
    assert len(connection.rows) == 1 and resumed["run_counts"]["skipped"] == 1


def test_checkpoint_scope_cannot_silently_change_and_restart_keeps_db(tmp_path):
    path = tmp_path / "checkpoint.json"
    with client_for({None: [match(100)]}) as client:
        run(client, path, max_pages=1)
    with pytest.raises(ValueError, match="mode mismatch"):
        cli.load_checkpoint(path, apply=True)
    with pytest.raises(ValueError, match="mode mismatch"):
        cli.load_checkpoint(path, apply=False, as_of=AS_OF + 1)
    connection = Connection()
    for iteration in range(2):
        with client_for({None: [match(100)]}) as client:
            state = run(client, path, apply=True, connection=connection, restart=True, max_pages=1)
        assert state["run_counts"]["inserted" if iteration == 0 else "skipped"] == 1
    assert len(connection.rows) == 1


def test_empty_response_means_provider_exhaustion_not_fabricated_window_coverage(tmp_path):
    with client_for({None: []}) as client:
        state = run(client, tmp_path / "checkpoint.json")
    report = cli.summary(state)
    assert report["complete"] and report["completion_reason"] == "API_EXHAUSTED"
    assert report["window_boundary_reached"] is False
    assert report["counts_cumulative"]["window_matches"] == 0
    assert report["observed_dates"] == dict(from_utc=None, through_utc=None)


def test_dry_run_never_connects_to_postgres(tmp_path, monkeypatch, capsys):
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: real_client(transport=transport))
    monkeypatch.setattr(cli.psycopg, "connect", lambda **kwargs: pytest.fail("Unexpected database access"))
    assert cli.main(["--checkpoint", str(tmp_path / "preview.json"), "--max-pages", "3"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "DRY_RUN" and report["counts_this_run"]["inserted"] == 0
