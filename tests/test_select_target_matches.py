from collections import Counter
from contextlib import contextmanager
import json

import psycopg
import pytest

from app.tournament_allowlist import load_tournament_allowlist
from tools import select_target_matches as cli


AS_OF_TEXT = "2026-10-26T01:52:53Z"
AS_OF = cli.parse_as_of(AS_OF_TEXT)
CUTOFF = AS_OF - 366 * 86400


@pytest.fixture
def allowlist(tmp_path):
    path = tmp_path / "allowlist.yaml"
    path.write_text(
        "version: 1\nleagues:\n"
        "  - league_id: 101\n    canonical_series: Selected event\n"
        "    edition: '2026'\n    stage: main\n"
        "  - league_id: 202\n    canonical_series: Selected qualifier\n"
        "    edition: '2026'\n    stage: ti_closed_qualifier\n"
        "  - league_id: 303\n    canonical_series: Empty event\n"
        "    edition: '2026'\n    stage: main\n",
        encoding="utf-8",
    )
    return load_tournament_allowlist(path)


def match(mid, league_id=101, start_time=AS_OF):
    return dict(match_id=mid, league_id=league_id, start_time=start_time)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.events = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        yield
        self.events.append("end")

    def execute(self, query, params=None):
        self.events.append((query, params))
        if query == cli.READ_ONLY:
            assert self.events[-2] == "begin"
            return Result([])
        assert query.startswith("SELECT ")
        assert "FROM opendota_pro_matches" in query
        if query == cli.TOTALS:
            return Result([{"matches": len(self.rows)}])
        cutoff, as_of = params[:2]
        window = [
            row for row in self.rows
            if row["start_time"] is not None
            and cutoff <= row["start_time"] <= as_of
        ]
        if query == cli.LEAGUE_COUNTS:
            return Result([
                dict(league_id=league_id, matches=count)
                for league_id, count in Counter(
                    row["league_id"] for row in window
                ).items()
            ])
        assert query == cli.TARGET_IDS
        assert "league_id = ANY(%s)" in query
        return Result([
            {"match_id": row["match_id"]}
            for row in sorted(window, key=lambda row: row["match_id"])
            if row["league_id"] in params[2]
        ])


def test_allowlist_and_inclusive_window_filter_without_mutation(allowlist):
    rows = [
        match(3, 101, CUTOFF), match(1, 202), match(2, 999),
        match(4, 101, CUTOFF - 1), match(5, 101, AS_OF + 1),
        match(6, 101, None), match(7, None), match(8, 0),
    ]
    before = [dict(row) for row in rows]
    connection = Connection(rows)
    report, ids = cli.audit_catalog(connection, allowlist, as_of=AS_OF)
    assert ids == [1, 3]  # Includes the configured closed qualifier.
    assert report["selected_matches"] == 2
    assert report["excluded_matches"] == 3
    assert report["excluded_leagues"] == 1
    assert report["unknown_league_matches"] == 2
    assert report["catalog_matches"] == 8
    assert report["window_matches"] == 5
    assert report["outside_window_or_unknown_date_matches"] == 3
    assert report["configured_leagues"] == 3
    assert report["selected_leagues"] == 2
    assert [row["matches"] for row in report["leagues"]] == [1, 1, 0]
    assert report["from_utc"] == "2025-10-25T01:52:53+00:00"
    assert rows == before
    assert connection.events[1] == (cli.READ_ONLY, None)
    assert connection.events[-1] == "end"


def test_real_configuration_is_used_without_a_copied_id_list():
    allowlist = load_tournament_allowlist()
    assert allowlist.entry_for(19719) is not None
    assert allowlist.entry_for(20395) is None
    report, ids = cli.audit_catalog(
        Connection([match(8960991322, 19719), match(9061429763, 20395)]),
        allowlist, as_of=AS_OF,
    )
    assert ids == [8960991322]
    assert [row["league_id"] for row in report["leagues"]] == list(
        allowlist.league_ids
    )


def test_repeat_returns_same_unique_sorted_ids(allowlist):
    connection = Connection([match(3), match(1, 202), match(2)])
    first = cli.audit_catalog(connection, allowlist, as_of=AS_OF)
    second = cli.audit_catalog(connection, allowlist, as_of=AS_OF)
    assert first == second
    assert first[1] == [1, 2, 3]


def test_duplicate_identity_is_an_error(allowlist):
    with pytest.raises(ValueError, match="Inconsistent target IDs"):
        cli.audit_catalog(Connection([match(1), match(1)]), allowlist, as_of=AS_OF)


def test_empty_allowlist_never_falls_back_to_all_matches(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("version: 1\nleagues: []\n", encoding="utf-8")
    connection = Connection([match(1)])
    with pytest.raises(ValueError, match="allowlist is empty"):
        cli.audit_catalog(
            connection, load_tournament_allowlist(path), as_of=AS_OF
        )
    assert connection.events == []


def setup_cli(monkeypatch, allowlist, connection):
    monkeypatch.setattr(cli, "load_tournament_allowlist", lambda: allowlist)
    monkeypatch.setattr(cli, "postgres_connection_options_from_env", lambda: {})
    calls = []

    def connect(**options):
        calls.append(options)
        return connection

    monkeypatch.setattr(cli.psycopg, "connect", connect)
    return calls


def test_default_cli_json_and_database_read_only(monkeypatch, capsys, allowlist):
    calls = setup_cli(monkeypatch, allowlist, Connection([match(2), match(1, 999)]))
    assert cli.main(["--as-of", AS_OF_TEXT]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["selected_matches"] == report["excluded_matches"] == 1
    assert "match_ids" not in report
    assert report["dem_availability"] == "NOT_CHECKED_BY_THIS_SELECTOR"
    assert output.err == ""
    assert calls[0]["autocommit"] is True
    assert calls[0]["options"] == "-c default_transaction_read_only=on"


def test_ids_only_has_no_non_id_stdout(monkeypatch, capsys, allowlist):
    setup_cli(monkeypatch, allowlist, Connection([match(3), match(1, 202), match(2, 999)]))
    assert cli.main(["--as-of", AS_OF_TEXT, "--ids-only"]) == 0
    output = capsys.readouterr()
    assert output.out == "1\n3\n"
    assert output.err == ""


def test_database_error_does_not_emit_target_ids(monkeypatch, capsys, allowlist):
    setup_cli(monkeypatch, allowlist, Connection([match(1)]))

    def failed_connect(**options):
        raise psycopg.OperationalError("database unavailable")

    monkeypatch.setattr(cli.psycopg, "connect", failed_connect)
    assert cli.main(["--as-of", AS_OF_TEXT, "--ids-only"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "database unavailable" in output.err


def test_missing_configuration_fails_without_connecting(monkeypatch, capsys, tmp_path):
    def missing():
        return load_tournament_allowlist(tmp_path / "missing.yaml")

    def no_connect(**options):
        pytest.fail("Must not query an unfiltered catalog")

    monkeypatch.setattr(cli, "load_tournament_allowlist", missing)
    monkeypatch.setattr(cli.psycopg, "connect", no_connect)
    assert cli.main(["--as-of", AS_OF_TEXT]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "tournament_allowlist_unavailable" in output.err


@pytest.mark.parametrize("value", ["yesterday", "2026-10-26", "2026-10-26T00:00:00.1Z"])
def test_invalid_or_ambiguous_window_fails(value):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--as-of", value])
    assert exc.value.code == 2
