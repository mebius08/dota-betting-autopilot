from datetime import datetime
from pathlib import Path

from app.history import (
    RosterChronologySource,
    RosterContinuityStrength,
    RosterPredecessorResolutionState,
    build_roster_lineage_graph,
)
from app.storage import SQLiteRepository
from tests.history_test_helpers import make_historical_match
from tests.roster_test_helpers import (
    make_coach,
    make_organization,
    make_player,
    make_roster_snapshot,
)


def test_tournament_chronology_uses_match_context_not_provider_id_order(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.upsert_historical_match(
        make_historical_match(
            "later-low-id",
            tournament_source_id="100",
            started_at=_dt("2025-05-01T10:00:00Z"),
            ended_at=_dt("2025-05-01T12:00:00Z"),
        )
    )
    repository.upsert_historical_match(
        make_historical_match(
            "earlier-high-id",
            tournament_source_id="200",
            started_at=_dt("2025-01-01T10:00:00Z"),
            ended_at=_dt("2025-01-01T12:00:00Z"),
        )
    )
    later_low_id = make_roster_snapshot(
        "later-low-id",
        tournament_source_id="100",
        observed_at=_dt("2026-01-01T00:00:00Z"),
    )
    earlier_high_id = make_roster_snapshot(
        "earlier-high-id",
        tournament_source_id="200",
        observed_at=_dt("2026-01-01T00:00:00Z"),
        players=[make_player(f"other-{index}") for index in range(5)],
    )
    _save_snapshots(repository, later_low_id, earlier_high_id)

    graph = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-02-01T00:00:00Z"),
    )

    later_point = graph.chronology_points[later_low_id.id]
    earlier_point = graph.chronology_points[earlier_high_id.id]
    assert later_point.source is RosterChronologySource.TOURNAMENT_MATCH_CONTEXT
    assert earlier_point.source is RosterChronologySource.TOURNAMENT_MATCH_CONTEXT
    assert later_point.context_at == _dt("2025-05-01T10:00:00Z")
    assert earlier_point.context_at == _dt("2025-01-01T10:00:00Z")
    assert earlier_point.context_at < later_point.context_at


def test_valid_from_precedes_tournament_match_context_for_chronology(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.upsert_historical_match(
        make_historical_match(
            "match-1",
            tournament_source_id="300",
            started_at=_dt("2025-01-01T10:00:00Z"),
            ended_at=_dt("2025-01-01T12:00:00Z"),
        )
    )
    snapshot = make_roster_snapshot(
        "explicit-validity",
        tournament_source_id="300",
        observed_at=_dt("2026-01-01T00:00:00Z"),
        valid_from=_dt("2025-03-01T00:00:00Z"),
    )
    repository.upsert_roster_snapshot(snapshot)

    graph = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-02-01T00:00:00Z"),
    )

    point = graph.chronology_points[snapshot.id]
    assert point.source is RosterChronologySource.EXPLICIT_VALID_FROM
    assert point.context_at == _dt("2025-03-01T00:00:00Z")


def test_observed_at_is_chronology_fallback_without_match_context(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    snapshot = make_roster_snapshot(
        "fallback",
        observed_at=_dt("2026-01-05T00:00:00Z"),
        valid_from=None,
    )
    repository.upsert_roster_snapshot(snapshot)

    graph = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-02-01T00:00:00Z"),
    )

    point = graph.chronology_points[snapshot.id]
    assert point.source is RosterChronologySource.OBSERVED_AT_FALLBACK
    assert point.context_at == _dt("2026-01-05T00:00:00Z")


def test_future_completed_tournament_match_does_not_provide_chronology_context(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.upsert_historical_match(
        make_historical_match(
            "future-completed-match",
            tournament_source_id="future-tournament",
            started_at=_dt("2026-07-02T10:00:00Z"),
            ended_at=_dt("2026-07-10T12:00:00Z"),
        )
    )
    snapshot = make_roster_snapshot(
        "future-completed-context",
        tournament_source_id="future-tournament",
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )
    repository.upsert_roster_snapshot(snapshot)

    before_completion = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-07-05T00:00:00Z"),
    )
    after_completion = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-07-11T00:00:00Z"),
    )

    before_point = before_completion.chronology_points[snapshot.id]
    after_point = after_completion.chronology_points[snapshot.id]
    assert before_point.source is RosterChronologySource.OBSERVED_AT_FALLBACK
    assert before_point.context_at == _dt("2026-07-01T00:00:00Z")
    assert after_point.source is RosterChronologySource.TOURNAMENT_MATCH_CONTEXT
    assert after_point.context_at == _dt("2026-07-02T10:00:00Z")


def test_tournament_chronology_uses_strict_completion_boundary(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    as_of = _dt("2026-07-05T00:00:00Z")
    repository.upsert_historical_match(
        make_historical_match(
            "exact-boundary-only",
            tournament_source_id="exact-only",
            started_at=_dt("2026-06-01T10:00:00Z"),
            ended_at=as_of,
        )
    )
    repository.upsert_historical_match(
        make_historical_match(
            "exact-boundary-excluded",
            tournament_source_id="mixed-boundary",
            started_at=_dt("2026-01-01T10:00:00Z"),
            ended_at=as_of,
        )
    )
    repository.upsert_historical_match(
        make_historical_match(
            "before-boundary-eligible",
            tournament_source_id="mixed-boundary",
            started_at=_dt("2026-02-01T10:00:00Z"),
            ended_at=_dt("2026-07-04T23:59:59Z"),
        )
    )
    exact_only = make_roster_snapshot(
        "exact-only",
        tournament_source_id="exact-only",
        observed_at=_dt("2026-07-01T00:00:00Z"),
    )
    mixed_boundary = make_roster_snapshot(
        "mixed-boundary",
        tournament_source_id="mixed-boundary",
        observed_at=_dt("2026-07-01T00:00:00Z"),
        players=[make_player(f"boundary-{index}") for index in range(5)],
    )
    _save_snapshots(repository, exact_only, mixed_boundary)

    graph = build_roster_lineage_graph(repository, as_of=as_of)

    exact_point = graph.chronology_points[exact_only.id]
    mixed_point = graph.chronology_points[mixed_boundary.id]
    assert exact_point.source is RosterChronologySource.OBSERVED_AT_FALLBACK
    assert exact_point.context_at == _dt("2026-07-01T00:00:00Z")
    assert mixed_point.source is RosterChronologySource.TOURNAMENT_MATCH_CONTEXT
    assert mixed_point.context_at == _dt("2026-02-01T10:00:00Z")


def test_chronology_never_bypasses_observed_at_availability(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    repository.upsert_historical_match(
        make_historical_match(
            "old-context",
            tournament_source_id="300",
            started_at=_dt("2025-01-01T10:00:00Z"),
            ended_at=_dt("2025-01-01T12:00:00Z"),
        )
    )
    future_observed = make_roster_snapshot(
        "future-observed",
        tournament_source_id="300",
        observed_at=_dt("2026-07-10T00:00:00Z"),
    )
    exact_cutoff = make_roster_snapshot(
        "exact-cutoff",
        tournament_source_id="300",
        observed_at=_dt("2026-07-05T00:00:00Z"),
        players=[make_player(f"exact-{index}") for index in range(5)],
    )
    _save_snapshots(repository, future_observed, exact_cutoff)

    before_graph = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-07-05T00:00:00Z"),
    )
    after_graph = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-07-11T00:00:00Z"),
    )

    assert future_observed.id not in before_graph.snapshots_by_id
    assert exact_cutoff.id not in before_graph.snapshots_by_id
    assert future_observed.id in after_graph.snapshots_by_id
    assert after_graph.chronology_points[future_observed.id].context_at == _dt(
        "2025-01-01T10:00:00Z"
    )


def test_equal_competitive_context_is_not_a_predecessor(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    previous = make_roster_snapshot(
        "previous",
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    current = make_roster_snapshot(
        "current",
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    _save_snapshots(repository, previous, current)

    graph = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-02-01T00:00:00Z"),
    )

    assert graph.accepted_edges == ()
    assert (
        graph.predecessor_resolutions[current.id].state
        is RosterPredecessorResolutionState.NO_PREDECESSOR
    )


def test_most_recent_eligible_predecessor_wins_before_strength(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    old_exact = make_roster_snapshot(
        "old-exact",
        players=[make_player(f"p{index}") for index in range(1, 6)],
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    recent_strong = make_roster_snapshot(
        "recent-strong",
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p3"),
            make_player("p4"),
            make_player("p6"),
        ],
        valid_from=_dt("2025-03-01T00:00:00Z"),
    )
    current = make_roster_snapshot(
        "current",
        players=[make_player(f"p{index}") for index in range(1, 6)],
        valid_from=_dt("2025-04-01T00:00:00Z"),
    )
    _save_snapshots(repository, old_exact, recent_strong, current)

    graph = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-02-01T00:00:00Z"),
    )

    resolution = graph.predecessor_resolutions[current.id]
    assert resolution.state is RosterPredecessorResolutionState.RESOLVED
    assert resolution.predecessor_snapshot_id == recent_strong.id
    assert resolution.evidence is not None
    assert resolution.evidence.continuity_strength is RosterContinuityStrength.STRONG


def test_equal_context_candidates_use_strength_ranking(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    exact = make_roster_snapshot(
        "exact",
        players=[make_player(f"p{index}") for index in range(1, 6)],
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    strong = make_roster_snapshot(
        "strong",
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p3"),
            make_player("p4"),
            make_player("p6"),
        ],
        valid_from=_dt("2025-01-01T00:00:00Z"),
        organization=make_organization("strong-org", "Strong Org"),
    )
    current = make_roster_snapshot(
        "current",
        players=[make_player(f"p{index}") for index in range(1, 6)],
        valid_from=_dt("2025-02-01T00:00:00Z"),
    )
    _save_snapshots(repository, strong, current, exact)

    graph = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-02-01T00:00:00Z"),
    )

    resolution = graph.predecessor_resolutions[current.id]
    assert resolution.state is RosterPredecessorResolutionState.RESOLVED
    assert resolution.predecessor_snapshot_id == exact.id
    assert resolution.evidence is not None
    assert resolution.evidence.continuity_strength is RosterContinuityStrength.EXACT


def test_substantive_predecessor_tie_is_ambiguous(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    first = make_roster_snapshot(
        "first",
        organization=make_organization("org-a", "Org A"),
        players=[make_player(f"p{index}") for index in range(1, 6)],
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    second = make_roster_snapshot(
        "second",
        organization=make_organization("org-b", "Org B"),
        players=[make_player(f"p{index}") for index in range(1, 6)],
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    current = make_roster_snapshot(
        "current",
        players=[make_player(f"p{index}") for index in range(1, 6)],
        valid_from=_dt("2025-02-01T00:00:00Z"),
    )
    _save_snapshots(repository, current, second, first)

    graph = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-02-01T00:00:00Z"),
    )

    resolution = graph.predecessor_resolutions[current.id]
    assert resolution.state is RosterPredecessorResolutionState.AMBIGUOUS
    assert resolution.predecessor_snapshot_id is None
    assert len(resolution.tied_evidence) == 2
    assert all(edge.current_snapshot_id != current.id for edge in graph.accepted_edges)


def test_changing_insert_order_does_not_change_graph_output(tmp_path: Path) -> None:
    snapshots = _directed_chain_snapshots()
    first_repository = SQLiteRepository(tmp_path / "first.db")
    second_repository = SQLiteRepository(tmp_path / "second.db")
    _save_snapshots(first_repository, *snapshots)
    _save_snapshots(second_repository, *reversed(snapshots))

    first_graph = build_roster_lineage_graph(
        first_repository,
        as_of=_dt("2026-02-01T00:00:00Z"),
    )
    second_graph = build_roster_lineage_graph(
        second_repository,
        as_of=_dt("2026-02-01T00:00:00Z"),
    )

    assert _edge_pairs(first_graph) == _edge_pairs(second_graph)
    assert first_graph.unlinked_snapshot_ids == second_graph.unlinked_snapshot_ids


def test_predecessor_history_is_directional_and_point_in_time(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    a, b, c = _directed_chain_snapshots()
    d = make_roster_snapshot(
        "d",
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p3"),
            make_player("p4"),
            make_player("p8"),
        ],
        observed_at=_dt("2026-07-10T00:00:00Z"),
        valid_from=_dt("2025-04-01T00:00:00Z"),
    )
    _save_snapshots(repository, a, b, c, d)

    before_d = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-07-05T00:00:00Z"),
    )
    after_d = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-07-11T00:00:00Z"),
    )

    assert _ids(before_d.get_predecessor_chain(a.id)) == []
    assert _ids(before_d.get_predecessor_chain(b.id)) == [a.id]
    assert _ids(before_d.get_predecessor_chain(c.id)) == [a.id, b.id]
    assert d.id not in before_d.snapshots_by_id
    assert _ids(after_d.get_predecessor_chain(d.id)) == [a.id, b.id, c.id]
    assert _ids(after_d.get_predecessor_chain(c.id)) == [a.id, b.id]


def test_future_completed_match_does_not_change_retrospective_resolution(
    tmp_path: Path,
) -> None:
    as_of_before_completion = _dt("2026-07-05T00:00:00Z")
    base_repository = SQLiteRepository(tmp_path / "base.db")
    future_match_repository = SQLiteRepository(tmp_path / "with-future-match.db")
    _, base_recent, base_current = _seed_future_match_resolution_case(
        base_repository,
        include_future_match=False,
    )
    future_old, future_recent, future_current = _seed_future_match_resolution_case(
        future_match_repository,
        include_future_match=True,
    )

    base_graph = build_roster_lineage_graph(
        base_repository,
        as_of=as_of_before_completion,
    )
    with_future_graph = build_roster_lineage_graph(
        future_match_repository,
        as_of=as_of_before_completion,
    )
    after_completion_graph = build_roster_lineage_graph(
        future_match_repository,
        as_of=_dt("2026-07-11T00:00:00Z"),
    )

    assert _edge_pairs(base_graph) == _edge_pairs(with_future_graph)
    assert _predecessor_id(base_graph, base_current.id) == base_recent.id
    assert _predecessor_id(with_future_graph, future_current.id) == (
        future_recent.id
    )
    assert (
        with_future_graph.chronology_points[future_old.id].source
        is RosterChronologySource.OBSERVED_AT_FALLBACK
    )
    assert (
        after_completion_graph.chronology_points[future_old.id].source
        is RosterChronologySource.TOURNAMENT_MATCH_CONTEXT
    )
    assert _predecessor_id(after_completion_graph, future_current.id) == (
        future_old.id
    )


def test_tundra_1w_exact_lineage_does_not_attach_unrelated_1w(
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
        valid_from=_dt("2024-01-01T00:00:00Z"),
    )
    tundra = make_roster_snapshot(
        "tundra",
        organization=make_organization("fake-tundra-id", "Tundra Esports"),
        players=transferred_players,
        coach=make_coach("moonmeander", "MoonMeander"),
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    one_w = make_roster_snapshot(
        "one-w",
        organization=make_organization("fake-1w-id", "1W"),
        players=list(reversed(transferred_players)),
        coach=make_coach("moonmeander", "MoonMeander"),
        valid_from=_dt("2025-02-01T00:00:00Z"),
    )
    _save_snapshots(repository, old_one_w, one_w, tundra)

    graph = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-02-01T00:00:00Z"),
    )

    assert repository.count_team_organizations() == 2
    resolution = graph.predecessor_resolutions[one_w.id]
    assert resolution.state is RosterPredecessorResolutionState.RESOLVED
    assert resolution.predecessor_snapshot_id == tundra.id
    assert resolution.evidence is not None
    assert resolution.evidence.continuity_strength is RosterContinuityStrength.EXACT
    assert _ids(graph.get_predecessor_chain(one_w.id)) == [tundra.id]
    assert old_one_w.id not in _ids(graph.get_predecessor_chain(one_w.id))


def test_heroic_lgd_lineage_does_not_attach_ancient_lgd(
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
        valid_from=_dt("2024-01-01T00:00:00Z"),
    )
    heroic = make_roster_snapshot(
        "heroic",
        organization=make_organization("fake-heroic-id", "HEROIC"),
        players=transferred_players,
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    lgd = make_roster_snapshot(
        "lgd-transferred",
        organization=make_organization("fake-lgd-id", "LGD Gaming"),
        players=transferred_players,
        valid_from=_dt("2025-02-01T00:00:00Z"),
    )
    _save_snapshots(repository, ancient_lgd, lgd, heroic)

    graph = build_roster_lineage_graph(
        repository,
        as_of=_dt("2026-02-01T00:00:00Z"),
    )

    assert repository.count_team_organizations() == 2
    resolution = graph.predecessor_resolutions[lgd.id]
    assert resolution.state is RosterPredecessorResolutionState.RESOLVED
    assert resolution.predecessor_snapshot_id == heroic.id
    assert _ids(graph.get_predecessor_chain(lgd.id)) == [heroic.id]
    assert ancient_lgd.id not in _ids(graph.get_predecessor_chain(lgd.id))


def _directed_chain_snapshots():
    a = make_roster_snapshot(
        "a",
        players=[make_player(f"p{index}") for index in range(1, 6)],
        valid_from=_dt("2025-01-01T00:00:00Z"),
    )
    b = make_roster_snapshot(
        "b",
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p3"),
            make_player("p4"),
            make_player("p6"),
        ],
        valid_from=_dt("2025-02-01T00:00:00Z"),
    )
    c = make_roster_snapshot(
        "c",
        players=[
            make_player("p1"),
            make_player("p2"),
            make_player("p3"),
            make_player("p4"),
            make_player("p7"),
        ],
        valid_from=_dt("2025-03-01T00:00:00Z"),
    )
    return a, b, c


def _seed_future_match_resolution_case(
    repository: SQLiteRepository,
    *,
    include_future_match: bool,
):
    if include_future_match:
        repository.upsert_historical_match(
            make_historical_match(
                "future-completed-lineage-context",
                tournament_source_id="future-lineage-context",
                started_at=_dt("2026-01-02T12:00:00Z"),
                ended_at=_dt("2026-07-10T12:00:00Z"),
            )
        )
    players = [make_player(f"p{index}") for index in range(1, 6)]
    old = make_roster_snapshot(
        "old-fallback",
        tournament_source_id="future-lineage-context",
        players=players,
        observed_at=_dt("2026-01-01T00:00:00Z"),
    )
    recent = make_roster_snapshot(
        "recent-fallback",
        tournament_source_id=None,
        players=players,
        observed_at=_dt("2026-01-02T00:00:00Z"),
    )
    current = make_roster_snapshot(
        "current-fallback",
        tournament_source_id=None,
        players=players,
        observed_at=_dt("2026-01-03T00:00:00Z"),
    )
    _save_snapshots(repository, old, recent, current)
    return old, recent, current


def _save_snapshots(repository: SQLiteRepository, *snapshots) -> None:
    for snapshot in snapshots:
        repository.upsert_roster_snapshot(snapshot)


def _edge_pairs(graph) -> list[tuple[str, str]]:
    return [
        (edge.previous_snapshot_id, edge.current_snapshot_id)
        for edge in graph.accepted_edges
    ]


def _ids(snapshots) -> list[str]:
    return [snapshot.id for snapshot in snapshots]


def _predecessor_id(graph, snapshot_id: str) -> str | None:
    return graph.predecessor_resolutions[snapshot_id].predecessor_snapshot_id


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
