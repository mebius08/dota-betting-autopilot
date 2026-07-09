from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import cli
from app.draft_history import (
    HistoricalDotaAdvantagePoint,
    HistoricalDotaGame,
    HistoricalDotaPlayerFinalStats,
    historical_dota_game_id,
    historical_dota_player_final_stats_id,
)
import app.public_pages as public_pages


def test_stratz_public_trajectory_manifest_is_deterministic_and_bounded() -> None:
    manifest = public_pages.get_stratz_public_backfill_manifest(
        public_pages.STRATZ_PUBLIC_TRAJECTORY_BACKFILL_MANIFEST_NAME
    )
    approved_ids = (
        set(public_pages.STRATZ_PUBLIC_LIVE_EWC_SMOKE_MATCH_IDS)
        | set(public_pages.STRATZ_PUBLIC_LIVE_SOURCE_SHAPE_CANARY_MATCH_IDS)
    )

    assert manifest.version == "1"
    assert manifest.size == 18
    assert manifest.match_ids == tuple(sorted(approved_ids, key=int))
    assert set(manifest.match_ids) == approved_ids
    assert manifest.size <= 200
    assert not set(public_pages.STRATZ_PUBLIC_FIXTURE_ONLY_MATCH_IDS) & set(
        manifest.match_ids
    )


def test_unknown_stratz_public_manifest_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown STRATZ public backfill manifest"):
        public_pages.get_stratz_public_backfill_manifest("missing")


def test_sync_drafts_manifest_uses_existing_stratz_public_sync_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = public_pages.get_stratz_public_backfill_manifest()
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

    def fake_sync_stratz_public_match_pages(**kwargs: object) -> object:
        captured.update(kwargs)
        return public_pages.StratzPublicSyncResult(
            requested_match_ids=manifest.match_ids,
            request_count=0,
            robots_disallowed=False,
            results=(
                public_pages.StratzPublicMatchSyncResult(
                    match_id=manifest.match_ids[0],
                    outcome=public_pages.StratzPublicSyncOutcome.UNCHANGED,
                    storage_result="unchanged",
                ),
            ),
        )

    monkeypatch.setattr(public_pages, "PublicPageHttpClient", FakeClient)
    monkeypatch.setattr(
        public_pages,
        "sync_stratz_public_match_pages",
        fake_sync_stratz_public_match_pages,
    )

    exit_code = cli.main(
        [
            "sync-drafts",
            "--provider",
            "stratz-public",
            "--db",
            str(tmp_path / "test.db"),
            "--manifest",
            public_pages.STRATZ_PUBLIC_TRAJECTORY_BACKFILL_MANIFEST_NAME,
            "--delay-seconds",
            "0",
            "--max-retries",
            "0",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert captured["match_ids"] == manifest.match_ids
    assert captured["delay_seconds"] == 0
    assert captured["max_retries"] == 0
    assert "STRATZ public trajectory backfill manifest" in output
    assert "UNCHANGED" in output


def test_sync_drafts_manifest_rejects_extra_match_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(
        [
            "sync-drafts",
            "--provider",
            "stratz-public",
            "--db",
            str(tmp_path / "test.db"),
            "--manifest",
            public_pages.STRATZ_PUBLIC_TRAJECTORY_BACKFILL_MANIFEST_NAME,
            "--match-id",
            "8886013461",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Use either --manifest or --match-id" in output


def test_trajectory_audit_counts_corpus_shape_and_source_index_anomalies() -> None:
    game_a = _game("8886013461", patch="182")
    game_b = _game(
        "8011794134",
        patch="177",
        league_name="DreamLeague Season 24 powered by Intel",
    )
    game_c = _game(
        "8886013461",
        game_id="duplicate-row",
        patch=None,
        league_name="PGL Wallachia 2025 Season 5",
        team_ids=False,
        winner_side=None,
        ended=False,
    )
    players_by_game = {
        game_a.id: _players(game_a, radiant=5, dire=5),
        game_b.id: _players(game_b, radiant=5, dire=4),
        game_c.id: (),
    }
    points_by_game = {
        game_a.id: (
            _point(game_a, "gold", 0, 100.0),
            _point(game_a, "gold", 1, 200.0),
            _point(game_a, "xp", 0, 50.0),
            _point(game_a, "xp", 1, 75.0),
        ),
        game_b.id: (
            _point(game_b, "gold", 2, 20.0),
            _point(game_b, "gold", 1, 10.0),
            _point(game_b, "xp", 0, 5.0),
        ),
        game_c.id: (
            _point(game_c, "gold", 0, 5.0),
            _point(game_c, "gold", 0, 6.0),
        ),
    }

    audit = public_pages.build_stratz_public_trajectory_corpus_audit_from_records(
        games=(game_a, game_b, game_c),
        players_by_game=players_by_game,
        points_by_game=points_by_game,
    )

    assert audit.game_count == 3
    assert audit.unique_source_game_ids == 2
    assert audit.duplicate_source_game_ids == 1
    assert audit.patch_distribution == {"177": 1, "182": 1, "unknown": 1}
    assert audit.family_distribution["dreamleague"] == 1
    assert audit.family_distribution["pgl"] == 1
    assert audit.games_with_10_players == 1
    assert audit.complete_5v5_compositions == 1
    assert audit.complete_team_identity_games == 2
    assert audit.missing_team_identity_games == 1
    assert audit.missing_or_ambiguous_winner_games == 1
    assert audit.missing_duration_games == 1
    assert audit.gold.games_with_curve == 3
    assert audit.gold.point_count_distribution.min == 2
    assert audit.gold.point_count_distribution.median == 2
    assert audit.gold.games_with_duplicate_source_indices == 1
    assert audit.gold.games_with_non_monotonic_source_indices == 1
    assert audit.gold.repeated_source_index_conflicts == 1
    assert audit.xp.games_with_curve == 2
    assert audit.xp.games_without_curve == 1
    assert audit.pairing.games_with_both_curves == 2
    assert audit.pairing.gold_only_games == 1
    assert audit.pairing.equal_point_count_games == 1
    assert audit.temporal.point_status_counts == {"source_index_unstable": 9}
    assert audit.time_semantics_conclusion is (
        public_pages.TrajectoryTimeSemanticsConclusion.UNRESOLVED
    )
    assert audit.readiness_decision is (
        public_pages.TrajectoryCorpusReadinessDecision.NEEDS_SOURCE_SEMANTICS_WORK
    )


def test_temporal_semantics_classifier_is_conservative() -> None:
    game = _game("8886013461")

    assert public_pages.classify_trajectory_time_semantics(
        (_point(game, "gold", 0, 1.0),)
    ) is public_pages.TrajectoryTimeSemanticsConclusion.UNRESOLVED
    assert public_pages.classify_trajectory_time_semantics(
        (
            _point(
                game,
                "gold",
                0,
                1.0,
                source_time_value="0",
                normalized_time_seconds=0,
            ),
            _point(
                game,
                "gold",
                1,
                2.0,
                source_time_value="60",
                normalized_time_seconds=60,
            ),
        )
    ) is public_pages.TrajectoryTimeSemanticsConclusion.CONFIRMED
    assert public_pages.classify_trajectory_time_semantics(
        (
            _point(
                game,
                "gold",
                0,
                1.0,
                source_time_value="0",
                normalized_time_seconds=0,
            ),
            _point(game, "xp", 0, 2.0),
        )
    ) is public_pages.TrajectoryTimeSemanticsConclusion.PARTIALLY_CONFIRMED


def test_trajectory_audit_counts_explicit_timing_and_ready_decision() -> None:
    game = _game(
        "8011794134",
        patch="177",
        league_name="DreamLeague Season 24 powered by Intel",
    )
    audit = public_pages.build_stratz_public_trajectory_corpus_audit_from_records(
        games=(game,),
        players_by_game={game.id: _players(game, radiant=5, dire=5)},
        points_by_game={
            game.id: (
                _point(
                    game,
                    "gold",
                    0,
                    100.0,
                    source_time_value="0",
                    normalized_time_seconds=0,
                ),
                _point(
                    game,
                    "gold",
                    1,
                    150.0,
                    source_time_value="60",
                    normalized_time_seconds=60,
                ),
                _point(
                    game,
                    "xp",
                    0,
                    40.0,
                    source_time_value="0",
                    normalized_time_seconds=0,
                ),
                _point(
                    game,
                    "xp",
                    1,
                    55.0,
                    source_time_value="60",
                    normalized_time_seconds=60,
                ),
            )
        },
    )

    assert audit.temporal.point_status_counts == {"normalized_seconds": 4}
    assert audit.temporal.explicit_source_time_point_count == 4
    assert audit.temporal.normalized_time_point_count == 4
    assert audit.temporal.games_with_explicit_source_time == 1
    assert audit.temporal.games_with_normalized_time_seconds == 1
    assert audit.time_semantics_conclusion is (
        public_pages.TrajectoryTimeSemanticsConclusion.CONFIRMED
    )
    assert audit.readiness_decision is (
        public_pages.TrajectoryCorpusReadinessDecision.READY_FOR_WINDOW_DESIGN
    )
    assert audit.patch_summaries[0].key == "177"
    assert audit.patch_summaries[0].gold_point_count_median == 2
    assert audit.patch_summaries[0].xp_point_count_median == 2
    assert audit.family_summaries[0].key == "dreamleague"
    assert audit.family_summaries[0].equal_length_rate == 1.0


def test_stratz_trajectory_audit_missing_db_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "missing.db"

    exit_code = cli.main(["stratz-trajectory-audit", "--db", str(db_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Games: 0" in output
    assert "STRATZ_TRAJECTORY_CORPUS_NEEDS_SOURCE_SEMANTICS_WORK" in output
    assert "Database not found" in output
    assert not db_path.exists()


def _game(
    source_game_id: str,
    *,
    game_id: str | None = None,
    patch: str | None = "182",
    league_name: str | None = None,
    team_ids: bool = True,
    winner_side: str | None = "team_a",
    ended: bool = True,
) -> HistoricalDotaGame:
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        minutes=int(source_game_id[-2:])
    )
    return HistoricalDotaGame(
        id=game_id or historical_dota_game_id(
            public_pages.STRATZ_PUBLIC_SOURCE,
            source_game_id,
        ),
        source=public_pages.STRATZ_PUBLIC_SOURCE,
        source_game_id=source_game_id,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=35) if ended else None,
        team_a_name=f"Team A {source_game_id}",
        team_b_name=f"Team B {source_game_id}",
        team_a_source_id=f"a-{source_game_id}" if team_ids else None,
        team_b_source_id=f"b-{source_game_id}" if team_ids else None,
        winner_side=winner_side,  # type: ignore[arg-type]
        team_a_side="radiant",
        patch=patch,
        league_name=league_name,
    )


def _players(
    game: HistoricalDotaGame,
    *,
    radiant: int,
    dire: int,
) -> tuple[HistoricalDotaPlayerFinalStats, ...]:
    rows: list[HistoricalDotaPlayerFinalStats] = []
    for side, count in (("radiant", radiant), ("dire", dire)):
        for index in range(count):
            account_id = f"{game.id}-{side}-{index}"
            rows.append(
                HistoricalDotaPlayerFinalStats(
                    id=historical_dota_player_final_stats_id(game.id, account_id),
                    game_id=game.id,
                    source=game.source,
                    source_game_id=game.source_game_id,
                    account_id=account_id,
                    team_side=side,  # type: ignore[arg-type]
                    team_source_id=game.team_a_source_id
                    if side == "radiant"
                    else game.team_b_source_id,
                    hero_id=10 + len(rows),
                )
            )
    return tuple(rows)


def _point(
    game: HistoricalDotaGame,
    metric: str,
    source_index: int,
    value: float,
    *,
    source_time_value: str | None = None,
    normalized_time_seconds: int | None = None,
) -> HistoricalDotaAdvantagePoint:
    status = (
        "normalized_seconds"
        if normalized_time_seconds is not None
        else "source_index_unstable"
    )
    return HistoricalDotaAdvantagePoint(
        id=f"{game.id}:{metric}:{source_index}:{value}",
        game_id=game.id,
        source=game.source,
        source_game_id=game.source_game_id,
        metric=metric,  # type: ignore[arg-type]
        source_index=source_index,
        value=value,
        source_time_value=source_time_value,
        normalized_time_seconds=normalized_time_seconds,
        time_semantics_status=status,  # type: ignore[arg-type]
    )
