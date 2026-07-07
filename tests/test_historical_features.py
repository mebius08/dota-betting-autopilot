from datetime import datetime
from pathlib import Path

import pytest

from app.history import (
    HistoricalPredictionContext,
    build_historical_match_features,
)
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match
from tests.roster_test_helpers import (
    make_coach,
    make_organization,
    make_player,
    make_roster_snapshot,
)


def test_feature_history_uses_strict_cutoff_and_self_exclusion(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    prediction_timestamp = _dt("2026-01-10T12:00:00Z")
    included = make_historical_match(
        "included",
        source_match_id="included",
        started_at=_dt("2026-01-01T10:00:00Z"),
        ended_at=_dt("2026-01-01T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="opponent",
        winner_side="team_a",
    )
    exact_cutoff = make_historical_match(
        "exact-cutoff",
        started_at=_dt("2026-01-10T10:00:00Z"),
        ended_at=prediction_timestamp,
        team_a_source_id="team-a",
        team_b_source_id="opponent",
        winner_side="team_a",
    )
    started_before_ended_after = make_historical_match(
        "ended-after",
        started_at=_dt("2026-01-10T10:00:00Z"),
        ended_at=_dt("2026-01-10T13:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="opponent",
        winner_side="team_a",
    )
    future = make_historical_match(
        "future",
        started_at=_dt("2026-01-11T10:00:00Z"),
        ended_at=_dt("2026-01-11T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="opponent",
        winner_side="team_a",
    )
    malformed_target_self = make_historical_match(
        "target-row",
        source_match_id="target-source",
        started_at=_dt("2026-01-01T09:00:00Z"),
        ended_at=_dt("2026-01-01T10:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="opponent",
        winner_side="team_a",
    )
    for match in (
        included,
        exact_cutoff,
        started_before_ended_after,
        future,
        malformed_target_self,
    ):
        repository.save_historical_match(match)

    row = build_historical_match_features(
        repository,
        HistoricalPredictionContext(
            source="pandascore",
            source_match_id="target-source",
            prediction_timestamp=prediction_timestamp,
            team_a_source_id="team-a",
            team_b_source_id="team-b",
            target_match_id="target-row",
        ),
    )

    assert row.team_a_history_matches == 1
    assert row.team_a_history_wins == 1


def test_later_data_does_not_alter_old_feature_row(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    context = HistoricalPredictionContext(
        source="pandascore",
        source_match_id="target",
        prediction_timestamp=_dt("2026-01-10T12:00:00Z"),
        team_a_source_id="team-a",
        team_b_source_id="team-b",
    )
    repository.save_historical_match(
        make_historical_match(
            "old",
            started_at=_dt("2026-01-01T10:00:00Z"),
            ended_at=_dt("2026-01-01T12:00:00Z"),
            team_a_source_id="team-a",
            team_b_source_id="opponent",
            winner_side="team_a",
        )
    )

    before = build_historical_match_features(repository, context)
    repository.save_historical_match(
        make_historical_match(
            "future",
            started_at=_dt("2026-01-11T10:00:00Z"),
            ended_at=_dt("2026-01-11T12:00:00Z"),
            team_a_source_id="team-a",
            team_b_source_id="team-b",
            winner_side="team_b",
        )
    )
    after = build_historical_match_features(repository, context)

    assert after == before


def test_raw_form_excludes_unknown_winner_and_uses_source_ids(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_historical_match(
        make_historical_match(
            "win",
            team_a_name="Same Display",
            team_b_name="Opponent",
            team_a_source_id="team-a",
            team_b_source_id="opponent-1",
            winner_side="team_a",
        )
    )
    repository.save_historical_match(
        make_historical_match(
            "loss",
            team_a_name="Renamed Team",
            team_b_name="Opponent",
            team_a_source_id="team-a",
            team_b_source_id="opponent-2",
            winner_side="team_b",
        )
    )
    repository.save_historical_match(
        make_historical_match(
            "unknown-winner",
            team_a_source_id="team-a",
            team_b_source_id="opponent-3",
            winner_side=None,
        )
    )
    repository.save_historical_match(
        make_historical_match(
            "same-name-different-id",
            team_a_name="Same Display",
            team_b_name="Opponent",
            team_a_source_id="different-team",
            team_b_source_id="opponent-4",
            winner_side="team_a",
        )
    )

    row = build_historical_match_features(
        repository,
        HistoricalPredictionContext(
            source="pandascore",
            source_match_id="target",
            prediction_timestamp=_dt("2026-01-02T00:00:00Z"),
            team_a_source_id="team-a",
            team_b_source_id="team-b",
        ),
    )

    assert row.team_a_history_matches == 2
    assert row.team_a_history_wins == 1
    assert row.team_a_history_losses == 1
    assert row.team_a_raw_win_rate == 0.5


def test_feature_row_swap_symmetry(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.save_historical_match(
        make_historical_match(
            "team-a-win",
            team_a_source_id="team-a",
            team_b_source_id="opponent-a",
            winner_side="team_a",
        )
    )
    repository.save_historical_match(
        make_historical_match(
            "team-b-loss",
            team_a_source_id="team-b",
            team_b_source_id="opponent-b",
            winner_side="team_b",
        )
    )
    prediction_timestamp = _dt("2026-01-02T00:00:00Z")

    a_vs_b = build_historical_match_features(
        repository,
        HistoricalPredictionContext(
            source="pandascore",
            source_match_id="target-a-b",
            prediction_timestamp=prediction_timestamp,
            team_a_source_id="team-a",
            team_b_source_id="team-b",
        ),
    )
    b_vs_a = build_historical_match_features(
        repository,
        HistoricalPredictionContext(
            source="pandascore",
            source_match_id="target-b-a",
            prediction_timestamp=prediction_timestamp,
            team_a_source_id="team-b",
            team_b_source_id="team-a",
        ),
    )

    assert a_vs_b.team_a_history_matches == b_vs_a.team_b_history_matches
    assert a_vs_b.team_b_history_matches == b_vs_a.team_a_history_matches
    assert a_vs_b.team_a_raw_win_rate == b_vs_a.team_b_raw_win_rate
    assert a_vs_b.team_b_raw_win_rate == b_vs_a.team_a_raw_win_rate
    assert a_vs_b.team_a_opponent_adjusted_strength == (
        b_vs_a.team_b_opponent_adjusted_strength
    )
    assert a_vs_b.team_b_opponent_adjusted_strength == (
        b_vs_a.team_a_opponent_adjusted_strength
    )
    assert a_vs_b.raw_win_rate_diff == -b_vs_a.raw_win_rate_diff
    assert a_vs_b.recency_weighted_win_rate_diff == pytest.approx(
        -b_vs_a.recency_weighted_win_rate_diff
    )
    assert a_vs_b.opponent_adjusted_strength_diff == pytest.approx(
        -b_vs_a.opponent_adjusted_strength_diff
    )
    assert a_vs_b.history_matches_diff == -b_vs_a.history_matches_diff


def test_tundra_to_1w_lineage_bridge_excludes_old_unrelated_1w(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    transferred_players = [
        make_player("pure", "Pure"),
        make_player("bzm", "bzm"),
        make_player("33", "33"),
        make_player("ari", "Ari"),
        make_player("whitemon", "Whitemon"),
    ]
    old_one_w = make_roster_snapshot(
        "old-1w",
        organization=make_organization("fake-1w-id", "1W"),
        players=[make_player(f"old-1w-{index}") for index in range(5)],
        observed_at=_dt("2025-02-15T00:00:00Z"),
        valid_from=_dt("2024-01-01T00:00:00Z"),
    )
    tundra = make_roster_snapshot(
        "tundra",
        organization=make_organization("fake-tundra-id", "Tundra Esports"),
        players=transferred_players,
        coach=make_coach("moonmeander", "MoonMeander"),
        observed_at=_dt("2025-02-15T00:00:00Z"),
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    one_w = make_roster_snapshot(
        "one-w",
        organization=make_organization("fake-1w-id", "1W"),
        players=list(reversed(transferred_players)),
        coach=make_coach("moonmeander", "MoonMeander"),
        observed_at=_dt("2025-02-15T00:00:00Z"),
        valid_from=_dt("2025-02-01T00:00:00Z"),
    )
    for snapshot in (old_one_w, tundra, one_w):
        repository.upsert_roster_snapshot(snapshot)
    repository.save_historical_match(
        make_historical_match(
            "old-1w-loss",
            started_at=_dt("2024-06-01T10:00:00Z"),
            ended_at=_dt("2024-06-01T12:00:00Z"),
            team_a_source_id="fake-1w-id",
            team_b_source_id="opponent",
            winner_side="team_b",
        )
    )
    repository.save_historical_match(
        make_historical_match(
            "tundra-win",
            started_at=_dt("2025-01-15T10:00:00Z"),
            ended_at=_dt("2025-01-15T12:00:00Z"),
            team_a_source_id="fake-tundra-id",
            team_b_source_id="opponent",
            winner_side="team_a",
        )
    )

    row = build_historical_match_features(
        repository,
        HistoricalPredictionContext(
            source="pandascore",
            source_match_id="target",
            prediction_timestamp=_dt("2025-03-01T00:00:00Z"),
            team_a_source_id="fake-1w-id",
            team_b_source_id="target-opponent",
        ),
    )

    assert row.team_a_history_bridge == "lineage_chronology_windows"
    assert row.team_a_history_matches == 1
    assert row.team_a_history_wins == 1


def test_heroic_to_lgd_lineage_bridge_excludes_ancient_unrelated_lgd(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    transferred_players = [
        make_player("yuma", "Yuma"),
        make_player("tailung", "TaiLung"),
        make_player("wisper", "Wisper"),
        make_player("thiolicor", "Thiolicor"),
        make_player("kj", "KJ"),
    ]
    ancient_lgd = make_roster_snapshot(
        "ancient-lgd",
        organization=make_organization("fake-lgd-id", "LGD Gaming"),
        players=[make_player(f"ancient-lgd-{index}") for index in range(5)],
        observed_at=_dt("2025-02-15T00:00:00Z"),
        valid_from=_dt("2024-01-01T00:00:00Z"),
    )
    heroic = make_roster_snapshot(
        "heroic",
        organization=make_organization("fake-heroic-id", "HEROIC"),
        players=transferred_players,
        observed_at=_dt("2025-02-15T00:00:00Z"),
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    lgd = make_roster_snapshot(
        "lgd-transferred",
        organization=make_organization("fake-lgd-id", "LGD Gaming"),
        players=transferred_players,
        observed_at=_dt("2025-02-15T00:00:00Z"),
        valid_from=_dt("2025-02-01T00:00:00Z"),
    )
    for snapshot in (ancient_lgd, heroic, lgd):
        repository.upsert_roster_snapshot(snapshot)
    repository.save_historical_match(
        make_historical_match(
            "ancient-lgd-loss",
            started_at=_dt("2024-06-01T10:00:00Z"),
            ended_at=_dt("2024-06-01T12:00:00Z"),
            team_a_source_id="fake-lgd-id",
            team_b_source_id="opponent",
            winner_side="team_b",
        )
    )
    repository.save_historical_match(
        make_historical_match(
            "heroic-win",
            started_at=_dt("2025-01-15T10:00:00Z"),
            ended_at=_dt("2025-01-15T12:00:00Z"),
            team_a_source_id="fake-heroic-id",
            team_b_source_id="opponent",
            winner_side="team_a",
        )
    )

    row = build_historical_match_features(
        repository,
        HistoricalPredictionContext(
            source="pandascore",
            source_match_id="target",
            prediction_timestamp=_dt("2025-03-01T00:00:00Z"),
            team_a_source_id="fake-lgd-id",
            team_b_source_id="target-opponent",
        ),
    )

    assert row.team_a_history_bridge == "lineage_chronology_windows"
    assert row.team_a_history_matches == 1
    assert row.team_a_history_wins == 1


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
