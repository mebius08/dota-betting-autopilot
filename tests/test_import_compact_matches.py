from datetime import datetime, timezone
import json
from unittest.mock import MagicMock

import psycopg
import pytest

from app.history.domain import HistoricalMatch
from app.tournaments import CompetitiveStage, TournamentRound
from tools import import_compact_matches as importer


# Small headers reproduce the verified local metadata, without loading replays.
FACTS = (
    (8960991322, 1787486918, 3862.367, 9572001, 7119388, "VSN", "TSpirit", 3),
    (8960882635, 1787482181, 2658.8335, 9572001, 7119388, "VSN", "TSpirit", 2),
    (8958734008, 1787383043, 2610.6003, 8255888, 10136357, "BB", "NGX", 2),
    (8957013760, 1787298340, 2057.6, 9247354, 2163, "FLCN", "Liquid", 3),
)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    metadata = []
    for mid, start, duration, radiant, dire, radiant_tag, dire_tag, winner in FACTS:
        compact = dict(
            schema_version=1,
            match_id=mid,
            league_id=19719,
            radiant_team_id=radiant,
            dire_team_id=dire,
            radiant_team_tag=radiant_tag,
            dire_team_tag=dire_tag,
            winner=winner,
            game_end_time_seconds=duration,
            clock_normalization_status="PROVEN",
        )
        write_json(tmp_path / importer.COMPACTS[mid], compact)
        metadata.append(
            dict(
                match_id=mid,
                start_time=start,
                duration=int(duration),
                radiant_team_id=radiant,
                dire_team_id=dire,
                leagueid=19719,
                radiant_win=winner == 2,
            )
        )
    write_json(
        tmp_path / importer.METADATA[0],
        {"inspected": metadata[:3], "selected": metadata[2]},
    )
    write_json(
        tmp_path / importer.METADATA[1],
        {"candidates": [metadata[3]], "selected": metadata[3]},
    )
    monkeypatch.setattr(importer, "ROOT", tmp_path)
    connect = MagicMock(
        side_effect=AssertionError("Real PostgreSQL must not be contacted")
    )
    monkeypatch.setattr(psycopg, "connect", connect)
    return tmp_path, connect


def test_four_canonical_headers_and_local_dates_produce_existing_model(inputs):
    root, connect = inputs
    imported_at = datetime(2026, 10, 20, tzinfo=timezone.utc)
    matches = importer.build_matches(root, ingested_at=imported_at)
    assert len(matches) == 4
    assert len({match.id for match in matches}) == 4
    for match, (mid, start, _, radiant, dire, radiant_tag, dire_tag, winner) in zip(
        matches, FACTS
    ):
        assert isinstance(match, HistoricalMatch)
        assert match.id == f"replay_clarity:{mid}"
        assert match.source == "replay_clarity"
        assert match.source_match_id == str(mid)
        assert match.started_at == datetime.fromtimestamp(start, timezone.utc)
        assert match.ingested_at == imported_at
        assert (match.team_a_name, match.team_b_name) == (radiant_tag, dire_tag)
        assert (match.team_a_source_id, match.team_b_source_id) == (
            str(radiant),
            str(dire),
        )
        assert match.winner_side == ("team_a" if winner == 2 else "team_b")
        assert match.winner_name == (radiant_tag if winner == 2 else dire_tag)
        assert match.winner_source_id == str(radiant if winner == 2 else dire)
        assert match.status == "completed"
        assert match.league_source_id == "19719"
        assert match.tournament_name == match.league_name == "The International 2026"
        assert all(
            getattr(match, name) is None
            for name in (
                "ended_at",
                "tournament_source_id",
                "series_name",
                "series_source_id",
                "raw_stage_label",
                "best_of",
            )
        )
        assert match.competitive_stage == CompetitiveStage.UNKNOWN
        assert match.normalized_round == TournamentRound.UNKNOWN
    connect.assert_not_called()


def test_dry_run_never_calls_writer_or_connects(inputs, monkeypatch, capsys):
    _, connect = inputs
    writer = MagicMock(side_effect=AssertionError("--apply was not requested"))
    monkeypatch.setattr(importer, "apply_matches", writer)
    assert importer.main([]) == 0
    output = capsys.readouterr().out
    assert "DRY RUN: 4 historical matches ready" in output
    assert "PostgreSQL was not contacted" in output
    for mid in importer.COMPACTS:
        assert str(mid) in output
    writer.assert_not_called()
    connect.assert_not_called()


def test_apply_delegates_all_four_to_existing_idempotent_writer(
    inputs, monkeypatch, capsys
):
    _, connect = inputs
    writer = MagicMock(return_value=4)
    monkeypatch.setattr(importer, "apply_matches", writer)
    assert importer.main(["--apply"]) == 0
    writer.assert_called_once()
    matches = writer.call_args.args[0]
    assert [match.source_match_id for match in matches] == [
        str(row[0]) for row in FACTS
    ]
    assert "source=4, inserted=4, skipped=0" in capsys.readouterr().out
    connect.assert_not_called()


def test_writer_failure_is_reported_without_success(inputs, monkeypatch, capsys):
    monkeypatch.setattr(
        importer,
        "apply_matches",
        MagicMock(side_effect=psycopg.OperationalError("database unavailable")),
    )
    assert importer.main(["--apply"]) == 1
    captured = capsys.readouterr()
    assert "database unavailable" in captured.err
    assert "APPLY:" not in captured.out


def test_missing_start_time_blocks_entire_import_without_inference(
    inputs, monkeypatch, capsys
):
    root, connect = inputs
    path = root / importer.METADATA[0]
    document = json.loads(path.read_text())
    del document["inspected"][0]["start_time"]
    write_json(path, document)
    writer = MagicMock(
        side_effect=AssertionError("Invalid records must not be applied")
    )
    monkeypatch.setattr(importer, "apply_matches", writer)
    assert importer.main(["--apply"]) == 1
    assert "start_time" in capsys.readouterr().err
    writer.assert_not_called()
    connect.assert_not_called()


def test_conflicting_local_dates_are_rejected(inputs, capsys):
    root, connect = inputs
    first = json.loads((root / importer.METADATA[0]).read_text())["inspected"][0]
    first["start_time"] += 1
    path = root / importer.METADATA[1]
    document = json.loads(path.read_text())
    document["candidates"].append(first)
    write_json(path, document)
    assert importer.main([]) == 1
    assert "Conflicting local metadata" in capsys.readouterr().err
    connect.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("match_id", 123, "match ID mismatch"),
        ("radiant_team_id", 123, "Team metadata mismatch"),
        ("winner", 2, "Winner metadata mismatch"),
        ("radiant_team_tag", "", "Missing radiant team label"),
        ("clock_normalization_status", "UNVERIFIED", "Unverified completed game"),
    ],
)
def test_inconsistent_compact_blocks_apply(
    inputs, monkeypatch, capsys, field, value, message
):
    root, connect = inputs
    path = root / importer.COMPACTS[8960991322]
    document = json.loads(path.read_text())
    document[field] = value
    write_json(path, document)
    writer = MagicMock()
    monkeypatch.setattr(importer, "apply_matches", writer)
    assert importer.main(["--apply"]) == 1
    assert message in capsys.readouterr().err
    writer.assert_not_called()
    connect.assert_not_called()


def test_noncanonical_copy_does_not_replace_missing_canonical_file(inputs, capsys):
    root, connect = inputs
    canonical = root / importer.COMPACTS[8960991322]
    canonical.rename(canonical.with_name("alias.compact.json"))
    assert importer.main([]) == 1
    assert "Compact import failed" in capsys.readouterr().err
    connect.assert_not_called()
