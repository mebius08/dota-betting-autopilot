from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest

import app.public_pages as public_pages
from app.public_pages import (
    OrderedDraftEvidenceStatus,
    PublicHttpResponse,
    PublicFieldProvenance,
    PublicPageSource,
    PublicMatchSemanticFingerprint,
    PublicSourceCoverageClassification,
    STRATZ_PUBLIC_SOURCE,
    StratzPublicLiveCanaryEvidenceStatus,
    StratzPublicMatchSyncResult,
    StratzPublicSyncOutcome,
    StratzPublicSyncResult,
    aggregate_public_field_coverage,
    build_public_match_url,
    build_public_source_contract,
    extract_embedded_public_states,
    extract_public_match_semantics,
    extract_public_match_semantics_from_page,
    evaluate_stratz_public_live_canary_evidence,
    normalize_stratz_public_match_page,
    normalize_stratz_public_match_state,
    observations_from_public_state,
    public_match_semantic_fingerprint,
    render_stratz_public_sync_result,
    sync_stratz_public_match_pages,
)
from app.storage import SQLiteRepository


def test_normalization_preserves_composition_without_fabricating_draft_order() -> None:
    state = _public_state(
        "8886013461",
        competition_name="Esports World Cup 2026",
        ordered_draft=False,
        advantage_mode="mixed",
    )

    result = normalize_stratz_public_match_page(
        requested_match_id="8886013461",
        html=_next_data_html(state),
    )

    assert result.normalized is not None
    normalized = result.normalized
    assert normalized.game.source == STRATZ_PUBLIC_SOURCE
    assert normalized.game.source_game_id == "8886013461"
    assert normalized.composition_complete is True
    assert len(normalized.player_final_stats) == 10
    assert normalized.ordered_draft_actions == ()
    assert (
        normalized.ordered_draft_evidence_status
        is OrderedDraftEvidenceStatus.MISSING_OR_AMBIGUOUS
    )
    assert normalized.gold_advantage_points == 3
    assert normalized.xp_advantage_points == 2
    assert normalized.trajectory_time_semantics_status == "mixed"
    gold_points = [
        point for point in normalized.advantage_points if point.metric == "gold"
    ]
    xp_points = [point for point in normalized.advantage_points if point.metric == "xp"]
    assert all(point.normalized_time_seconds is None for point in gold_points)
    assert all(
        point.time_semantics_status == "source_index_unstable"
        for point in gold_points
    )
    assert [point.normalized_time_seconds for point in xp_points] == [0, 60]


def test_duplicate_player_records_are_ingestion_critical() -> None:
    state = _public_state("8886013461", competition_name="Esports World Cup 2026")
    players = cast(list[dict[str, object]], state["players"])
    players[1]["steamAccountId"] = players[0]["steamAccountId"]

    result = normalize_stratz_public_match_page(
        requested_match_id="8886013461",
        html=_next_data_html(state),
    )

    assert result.normalized is None
    assert "duplicate player account identity" in result.invariant_failures


def test_real_shape_next_flight_regression_fixture_decodes_and_normalizes() -> None:
    html = _next_flight_html(_real_shape_state())

    states, findings = extract_embedded_public_states(html)
    assert "embedded public state parsed from Next flight stream" in findings
    semantics = extract_public_match_semantics(states[0])

    assert semantics.match_id == "8886013461"
    assert len({player.account_id for player in semantics.players}) == 10
    assert sum(1 for player in semantics.players if player.team_side == "radiant") == 5
    assert sum(1 for player in semantics.players if player.team_side == "dire") == 5
    assert {player.hero_id for player in semantics.players} == set(range(10, 20))
    assert semantics.has_complete_5v5_composition is True
    assert semantics.radiant_team_id == "101"
    assert semantics.dire_team_id == "202"
    assert semantics.patch == "176"
    assert semantics.players[0].kills == 1
    assert semantics.players[0].net_worth == 12_000
    assert semantics.players[0].gpm == 500
    assert semantics.players[0].final_item_ids == (1, 2, 3, 4, 5, 6)
    assert sum(1 for point in semantics.advantage_points if point.metric == "gold") == 3
    assert sum(1 for point in semantics.advantage_points if point.metric == "xp") == 2

    result = normalize_stratz_public_match_page(
        requested_match_id="8886013461",
        html=html,
    )

    assert result.normalized is not None
    normalized = result.normalized
    assert len(normalized.player_final_stats) == 10
    assert normalized.composition_complete is True
    assert normalized.game.team_a_source_id == "101"
    assert normalized.game.team_b_source_id == "202"
    assert normalized.game.team_a_name == "Real Radiant"
    assert normalized.game.team_b_name == "Real Dire"
    assert normalized.game.patch == "176"
    assert normalized.player_final_stats[0].kills == 1
    assert normalized.player_final_stats[0].final_item_ids == (1, 2, 3, 4, 5, 6)
    assert normalized.gold_advantage_points == 3
    assert normalized.xp_advantage_points == 2
    assert all(
        point.time_semantics_status == "source_index_unstable"
        and point.normalized_time_seconds is None
        for point in normalized.advantage_points
    )


def test_feasibility_and_ingestion_share_real_shape_semantics() -> None:
    state = _real_shape_state()
    observations = observations_from_public_state(
        state,
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )
    result = normalize_stratz_public_match_state(
        requested_match_id="8886013461",
        state=state,
    )

    assert result.normalized is not None
    for key in (
        "player_account_ids",
        "player_sides",
        "player_hero_ids",
        "complete_5v5_picks",
        "team_ids",
        "team_display_names",
        "patch_id",
        "individual_kills",
        "final_items",
        "gold_advantage_timeline",
        "xp_advantage_timeline",
    ):
        assert observations[key].present is True
    assert len(result.normalized.player_final_stats) == 10
    assert result.normalized.gold_advantage_points == 3
    assert result.normalized.xp_advantage_points == 2


def test_same_page_feasibility_and_ingestion_share_page_semantics() -> None:
    match_id = "8886013461"
    html = _next_flight_html_for_states(
        _shallow_live_match_root(match_id),
        _real_shape_state(),
    )

    states, _ = extract_embedded_public_states(html)
    old_selected_root_fingerprint = public_match_semantic_fingerprint(
        extract_public_match_semantics(states[0])
    )
    page_semantics = extract_public_match_semantics_from_page(
        html=html,
        requested_match_id=match_id,
    )
    analysis, _ = public_pages.analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id=match_id,
        url=build_public_match_url(PublicPageSource.STRATZ, match_id),
        response=PublicHttpResponse(
            url=build_public_match_url(PublicPageSource.STRATZ, match_id),
            status_code=200,
            content_type="text/html",
            body=html.encode("utf-8"),
        ),
    )
    coverage = {row.key: row for row in aggregate_public_field_coverage((analysis,))}
    result = normalize_stratz_public_match_page(
        requested_match_id=match_id,
        html=html,
    )

    assert old_selected_root_fingerprint == PublicMatchSemanticFingerprint(
        match_id=match_id,
        patch_id=None,
        radiant_team_id=None,
        dire_team_id=None,
        player_count=0,
        radiant_player_count=0,
        dire_player_count=0,
        player_account_id_count=0,
        player_hero_id_count=0,
        gold_advantage_point_count=0,
        xp_advantage_point_count=0,
        draft_action_count=0,
        ban_count=0,
    )
    assert page_semantics.fingerprint == PublicMatchSemanticFingerprint(
        match_id=match_id,
        patch_id="176",
        radiant_team_id="101",
        dire_team_id="202",
        player_count=10,
        radiant_player_count=5,
        dire_player_count=5,
        player_account_id_count=10,
        player_hero_id_count=10,
        gold_advantage_point_count=3,
        xp_advantage_point_count=2,
        draft_action_count=12,
        ban_count=2,
    )

    assert coverage["player_account_ids"].present_count == 1
    assert coverage["player_sides"].present_count == 1
    assert coverage["player_hero_ids"].present_count == 1
    assert coverage["team_ids"].present_count == 1
    assert coverage["patch_id"].present_count == 1
    assert coverage["gold_advantage_timeline"].present_count == 1
    assert coverage["xp_advantage_timeline"].present_count == 1

    assert result.normalized is not None
    normalized = result.normalized
    players = normalized.player_final_stats
    assert len(players) == 10
    assert sum(1 for player in players if player.team_side == "radiant") == 5
    assert sum(1 for player in players if player.team_side == "dire") == 5
    assert len({player.hero_id for player in players}) == 10
    assert normalized.game.team_a_source_id == "101"
    assert normalized.game.team_b_source_id == "202"
    assert normalized.game.patch == "176"
    assert normalized.gold_advantage_points == 3
    assert normalized.xp_advantage_points == 2
    assert all(player.kills is not None for player in players)
    assert all(player.deaths is not None for player in players)
    assert all(player.assists is not None for player in players)
    assert all(player.net_worth is not None for player in players)
    assert all(player.gpm is not None and player.xpm is not None for player in players)
    assert all(player.final_item_ids for player in players)


def test_player_side_support_does_not_promote_role_position_contract() -> None:
    html = _next_data_html(_public_state("1", competition_name="Esports World Cup"))
    analysis, _ = public_pages.analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="1",
        url="https://stratz.com/match/1",
        response=PublicHttpResponse(
            url="https://stratz.com/match/1",
            status_code=200,
            content_type="text/html",
            body=html.encode("utf-8"),
        ),
    )
    coverage = aggregate_public_field_coverage((analysis,))
    contract = build_public_source_contract(
        public_pages.PublicPageProbeResult(
            source=PublicPageSource.STRATZ,
            probe_started_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
            request_count=1,
            policy=public_pages.PublicPolicyCheck(
                source=PublicPageSource.STRATZ,
                robots_url="https://stratz.com/robots.txt",
                http_status=200,
                content_type="text/plain",
                byte_size=0,
                checked_path="/match/1",
                path_disallowed=False,
                relevant_rules=(),
                content_signals=(),
            ),
            analyses=(analysis,),
            coverage=coverage,
            recommendation=public_pages.determine_public_source_recommendation(
                (analysis,),
                coverage,
            ),
        )
    )
    rows = {row.key: row for row in contract.coverage}

    assert rows["player_side_association"].classification is (
        PublicSourceCoverageClassification.SUPPORTED
    )
    assert rows["player_slot_position"].classification is (
        PublicSourceCoverageClassification.MISSING
    )


def test_sync_is_idempotent_and_partial_page_does_not_erase_stronger_fields(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    match_id = "8886013461"
    rich_state = _public_state(
        match_id,
        competition_name="Esports World Cup 2026",
        ordered_draft=True,
        include_player_stats=True,
        include_items=True,
    )
    partial_state = _public_state(
        match_id,
        competition_name=None,
        ordered_draft=False,
        include_team_identity=False,
        include_player_stats=False,
        include_items=False,
        include_advantage=False,
    )

    first = sync_stratz_public_match_pages(
        repository=repository,
        match_ids=(match_id,),
        client=_FakePublicClient.for_states({match_id: rich_state}),
        delay_seconds=0,
        max_retries=0,
        fetch_referenced_resources=False,
        sleep_func=lambda _: None,
    )
    second = sync_stratz_public_match_pages(
        repository=repository,
        match_ids=(match_id,),
        client=_FakePublicClient.for_states({match_id: partial_state}),
        delay_seconds=0,
        max_retries=0,
        fetch_referenced_resources=False,
        sleep_func=lambda _: None,
    )

    assert first.results[0].outcome is StratzPublicSyncOutcome.INGESTED
    assert second.results[0].outcome is StratzPublicSyncOutcome.UNCHANGED
    game = repository.list_historical_dota_games()[0]
    assert game.team_a_name == "Radiant 8886013461"
    assert game.team_a_source_id == "101"
    assert game.tournament_name == "Esports World Cup 2026"
    assert game.patch == "176"
    assert len(repository.list_historical_draft_actions(game.id)) == 12
    player = repository.list_historical_dota_player_final_stats(game.id)[0]
    assert player.team_source_id in {"101", "202"}
    assert player.kills is not None
    assert player.final_item_ids
    assert len(repository.list_historical_dota_advantage_points(game.id)) == 5


def test_identical_reingestion_does_not_duplicate_rows(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    match_id = "8886013461"
    state = _public_state(
        match_id,
        competition_name="Esports World Cup 2026",
        ordered_draft=False,
        include_player_stats=True,
        include_items=True,
        advantage_mode="mixed",
    )

    first = sync_stratz_public_match_pages(
        repository=repository,
        match_ids=(match_id,),
        client=_FakePublicClient.for_states({match_id: state}),
        delay_seconds=0,
        max_retries=0,
        fetch_referenced_resources=False,
        sleep_func=lambda _: None,
    )
    second = sync_stratz_public_match_pages(
        repository=repository,
        match_ids=(match_id,),
        client=_FakePublicClient.for_states({match_id: state}),
        delay_seconds=0,
        max_retries=0,
        fetch_referenced_resources=False,
        sleep_func=lambda _: None,
    )

    assert first.results[0].outcome is StratzPublicSyncOutcome.INGESTED
    assert second.results[0].outcome is StratzPublicSyncOutcome.UNCHANGED
    games = repository.list_historical_dota_games()
    assert len(games) == 1
    players = repository.list_historical_dota_player_final_stats(games[0].id)
    advantage_points = repository.list_historical_dota_advantage_points(games[0].id)
    assert len(players) == 10
    assert len([point for point in advantage_points if point.metric == "gold"]) == 3
    assert len([point for point in advantage_points if point.metric == "xp"]) == 2


def test_referenced_json_resource_is_fetched_once_and_ingested(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    match_id = "7770000001"
    page_url = build_public_match_url(PublicPageSource.STRATZ, match_id)
    resource_url = "https://stratz.com/_next/data/build/match.json"
    html = "<html><link rel='preload' href='/_next/data/build/match.json'></html>"
    client = _FakePublicClient(
        {
            _ROBOTS_URL: [_response(_ROBOTS_URL, 200, "User-agent: *\nAllow: /\n")],
            page_url: [_response(page_url, 200, html)],
            resource_url: [
                _response(
                    resource_url,
                    200,
                    json.dumps(
                        {
                            "match": _public_state(
                                match_id,
                                competition_name="DreamLeague Season 28",
                            )
                        }
                    ),
                    content_type="application/json",
                )
            ],
        }
    )

    result = sync_stratz_public_match_pages(
        repository=repository,
        match_ids=(match_id,),
        client=client,
        delay_seconds=0,
        max_retries=0,
        sleep_func=lambda _: None,
    )

    assert result.results[0].outcome is StratzPublicSyncOutcome.INGESTED
    assert result.request_count == 3
    assert client.requested_urls.count(resource_url) == 1
    assert repository.list_historical_dota_games()[0].league_name == (
        "DreamLeague Season 28"
    )


def test_retryable_and_permanent_fetch_outcomes_do_not_roll_back_success(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    successful = "8886013461"
    missing = "6660000001"
    success_url = build_public_match_url(PublicPageSource.STRATZ, successful)
    missing_url = build_public_match_url(PublicPageSource.STRATZ, missing)
    client = _FakePublicClient(
        {
            _ROBOTS_URL: [_response(_ROBOTS_URL, 200, "User-agent: *\nAllow: /\n")],
            success_url: [
                _response(success_url, 500, "temporary"),
                _response(
                    success_url,
                    200,
                    _next_data_html(
                        _public_state(
                            successful,
                            competition_name="Esports World Cup 2026",
                        )
                    ),
                ),
            ],
            missing_url: [_response(missing_url, 404, "missing")],
        }
    )

    result = sync_stratz_public_match_pages(
        repository=repository,
        match_ids=(successful, missing),
        client=client,
        delay_seconds=0,
        max_retries=1,
        retry_backoff_seconds=0,
        fetch_referenced_resources=False,
        sleep_func=lambda _: None,
    )

    assert [row.outcome for row in result.results] == [
        StratzPublicSyncOutcome.INGESTED,
        StratzPublicSyncOutcome.NOT_FOUND,
    ]
    assert client.requested_urls.count(success_url) == 2
    assert client.requested_urls.count(missing_url) == 1
    assert result.request_count == 4
    assert len(repository.list_historical_dota_games()) == 1


def test_deterministic_multi_family_canary_report_remains_live_blocked(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    states = {
        "8886013461": _public_state(
            "8886013461",
            competition_name="Esports World Cup 2026",
            patch="176",
        ),
        "7770000001": _public_state(
            "7770000001",
            competition_name="DreamLeague Season 28",
            patch="175",
            advantage_mode="normalized",
        ),
        "6660000001": _public_state(
            "6660000001",
            competition_name="FISSURE Playground 3",
            patch="174",
            include_team_identity=False,
        ),
    }

    result = sync_stratz_public_match_pages(
        repository=repository,
        match_ids=tuple(states),
        client=_FakePublicClient.for_states(states),
        delay_seconds=0,
        max_retries=0,
        fetch_referenced_resources=False,
        sleep_func=lambda _: None,
    )
    report = render_stratz_public_sync_result(result)

    assert result.storage_successes == 3
    assert {row.competition_family for row in result.results} == {
        "esports_world_cup",
        "dreamleague",
        "fissure_playground",
    }
    assert any("partial team identity" in row.known_limitations for row in result.results)
    assert "LIVE_REQUEST_EXECUTED" in report
    assert "LIVE_SINGLE_OR_HOMOGENEOUS_SAMPLE" in report
    assert "LIVE_CANARY_NOT_EXECUTED" not in report
    assert "STRATZ_PUBLIC_CANARY_BLOCKED" in report
    assert (
        "Blocker: multi-family live canary not yet completed"
    ) in report


def test_live_request_success_blocks_only_on_missing_multi_family_canary() -> None:
    result = StratzPublicSyncResult(
        requested_match_ids=("8886013461",),
        request_count=2,
        robots_disallowed=False,
        results=(
            StratzPublicMatchSyncResult(
                match_id="8886013461",
                outcome=StratzPublicSyncOutcome.INGESTED,
                http_status=200,
                storage_result="inserted",
                patch="182",
                composition_complete=True,
                player_count=10,
                team_identity_status="complete",
                gold_advantage_points=63,
                xp_advantage_points=63,
                trajectory_time_semantics_status="source_index_unstable",
            ),
        ),
    )

    report = render_stratz_public_sync_result(result)

    assert "LIVE_SINGLE_OR_HOMOGENEOUS_SAMPLE" in report
    assert "STRATZ_PUBLIC_CANARY_BLOCKED" in report
    assert "Blocker: multi-family live canary not yet completed" in report
    assert "production ingestion loses semantics" not in report


def test_actual_extraction_regression_evidence_uses_regression_blocker() -> None:
    result = StratzPublicSyncResult(
        requested_match_ids=("8886013461",),
        request_count=2,
        robots_disallowed=False,
        results=(
            StratzPublicMatchSyncResult(
                match_id="8886013461",
                outcome=StratzPublicSyncOutcome.SOURCE_INCOMPLETE,
                http_status=200,
                composition_complete=False,
                player_count=0,
                team_identity_status="unknown",
                invariant_failures=(
                    "ambiguous player side assignment",
                    "incomplete 5v5 hero composition",
                ),
            ),
        ),
    )

    report = render_stratz_public_sync_result(result)

    assert "STRATZ_PUBLIC_CANARY_BLOCKED" in report
    assert (
        "Blocker: production ingestion loses semantics that the shared feasibility "
        "path sees on the same real Next Flight page"
    ) in report


def test_observed_live_source_shape_canary_selects_ready_decision() -> None:
    result = StratzPublicSyncResult(
        requested_match_ids=(
            "8886013461",
            "8655240937",
            "8639790960",
            "8358745059",
            "8346430978",
            "8327632578",
            "8011794134",
        ),
        request_count=8,
        robots_disallowed=False,
        results=(
            _live_success(
                "8886013461",
                outcome=StratzPublicSyncOutcome.UNCHANGED,
                storage_result="unchanged",
                family="unknown",
                patch="182",
                gold_points=63,
                xp_points=63,
            ),
            _live_success(
                "8655240937",
                family="unknown",
                patch="182",
                gold_points=38,
                xp_points=38,
            ),
            _live_success(
                "8639790960",
                family="unknown",
                patch="182",
                gold_points=22,
                xp_points=22,
            ),
            _live_success(
                "8358745059",
                family="unknown",
                patch="180",
                gold_points=48,
                xp_points=48,
            ),
            _live_success(
                "8346430978",
                family="pgl",
                patch="180",
                gold_points=38,
                xp_points=38,
            ),
            _live_success(
                "8327632578",
                family="the_international",
                patch="180",
                gold_points=26,
                xp_points=26,
            ),
            _live_success(
                "8011794134",
                family="dreamleague",
                patch="177",
                gold_points=33,
                xp_points=33,
            ),
        ),
    )

    evidence = evaluate_stratz_public_live_canary_evidence(result)
    report = render_stratz_public_sync_result(result)

    assert evidence.status is (
        StratzPublicLiveCanaryEvidenceStatus.LIVE_MULTI_FAMILY_CANARY_COMPLETED
    )
    assert evidence.explicit_patches == ("177", "180", "182")
    assert evidence.recognized_families == (
        "dreamleague",
        "pgl",
        "the_international",
    )
    assert "LIVE_MULTI_FAMILY_CANARY_COMPLETED" in report
    assert "STRATZ_PUBLIC_READY_FOR_BOUNDED_BACKFILL" in report
    assert "Blocker:" not in report


def test_unknown_family_does_not_invalidate_patch_and_family_diversity() -> None:
    result = StratzPublicSyncResult(
        requested_match_ids=tuple(str(9000 + index) for index in range(5)),
        request_count=6,
        robots_disallowed=False,
        results=(
            _live_success("9000", family="unknown", patch="182"),
            _live_success("9001", family="unknown", patch="182"),
            _live_success("9002", family="pgl", patch="180"),
            _live_success("9003", family="dreamleague", patch="180"),
            _live_success("9004", family="unknown", patch="177"),
        ),
    )

    evidence = evaluate_stratz_public_live_canary_evidence(result)

    assert evidence.status is (
        StratzPublicLiveCanaryEvidenceStatus.LIVE_MULTI_FAMILY_CANARY_COMPLETED
    )
    assert evidence.recognized_families == ("dreamleague", "pgl")


def test_explicit_patch_diversity_can_complete_canary_without_family_diversity() -> None:
    result = StratzPublicSyncResult(
        requested_match_ids=tuple(str(9100 + index) for index in range(5)),
        request_count=6,
        robots_disallowed=False,
        results=(
            _live_success("9100", family="unknown", patch="177"),
            _live_success("9101", family="unknown", patch="177"),
            _live_success("9102", family="unknown", patch="180"),
            _live_success("9103", family="unknown", patch="180"),
            _live_success("9104", family="unknown", patch="182"),
        ),
    )

    evidence = evaluate_stratz_public_live_canary_evidence(result)

    assert evidence.status is (
        StratzPublicLiveCanaryEvidenceStatus.LIVE_MULTI_FAMILY_CANARY_COMPLETED
    )
    assert evidence.explicit_patches == ("177", "180", "182")


def test_homogeneous_successful_sample_remains_blocked() -> None:
    result = StratzPublicSyncResult(
        requested_match_ids=tuple(str(9200 + index) for index in range(5)),
        request_count=6,
        robots_disallowed=False,
        results=tuple(
            _live_success(str(9200 + index), family="unknown", patch="182")
            for index in range(5)
        ),
    )

    evidence = evaluate_stratz_public_live_canary_evidence(result)
    report = render_stratz_public_sync_result(result)

    assert evidence.status is (
        StratzPublicLiveCanaryEvidenceStatus.LIVE_SINGLE_OR_HOMOGENEOUS_SAMPLE
    )
    assert "STRATZ_PUBLIC_CANARY_BLOCKED" in report
    assert "Blocker: multi-family live canary not yet completed" in report


def test_systematic_parse_failure_blocks_canary() -> None:
    result = StratzPublicSyncResult(
        requested_match_ids=("9300", "9301", "9302", "9303", "9304"),
        request_count=6,
        robots_disallowed=False,
        results=(
            _live_success("9300", family="pgl", patch="182"),
            _live_success("9301", family="dreamleague", patch="180"),
            StratzPublicMatchSyncResult(
                match_id="9302",
                outcome=StratzPublicSyncOutcome.PARSE_FAILED,
                http_status=200,
                invariant_failures=("no embedded public state parsed",),
            ),
            StratzPublicMatchSyncResult(
                match_id="9303",
                outcome=StratzPublicSyncOutcome.PARSE_FAILED,
                http_status=200,
                invariant_failures=("no embedded public state parsed",),
            ),
            _live_success("9304", family="the_international", patch="177"),
        ),
    )

    evidence = evaluate_stratz_public_live_canary_evidence(result)
    report = render_stratz_public_sync_result(result)

    assert evidence.status is (
        StratzPublicLiveCanaryEvidenceStatus.LIVE_CANARY_CRITICAL_FAILURE
    )
    assert "STRATZ_PUBLIC_CANARY_BLOCKED" in report
    assert "Blocker: multi-family live canary has parse failures" in report


def test_cli_sync_drafts_stratz_public_renders_bounded_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app import cli

    def fake_sync(**kwargs: object) -> StratzPublicSyncResult:
        assert kwargs["match_ids"] == ("8886013461",)
        assert kwargs["delay_seconds"] == 0
        assert kwargs["max_retries"] == 0
        assert kwargs["fetch_referenced_resources"] is False
        return StratzPublicSyncResult(
            requested_match_ids=("8886013461",),
            request_count=2,
            robots_disallowed=False,
            results=(
                StratzPublicMatchSyncResult(
                    match_id="8886013461",
                    outcome=StratzPublicSyncOutcome.INGESTED,
                    http_status=200,
                    storage_result="inserted",
                    competition_family="esports_world_cup",
                    patch="176",
                    composition_complete=True,
                    player_count=10,
                    gold_advantage_points=3,
                    xp_advantage_points=2,
                    trajectory_time_semantics_status="source_index_unstable",
                ),
            ),
        )

    monkeypatch.setattr(public_pages, "PublicPageHttpClient", _CliFakeClient)
    monkeypatch.setattr(public_pages, "sync_stratz_public_match_pages", fake_sync)

    exit_code = cli.main(
        [
            "sync-drafts",
            "--provider",
            "stratz-public",
            "--db",
            str(tmp_path / "test.db"),
            "--match-id",
            "8886013461",
            "--delay-seconds",
            "0",
            "--max-retries",
            "0",
            "--skip-referenced-resources",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "STRATZ public-page historical ingestion" in output
    assert "8886013461 | INGESTED" in output
    assert "LIVE_REQUEST_EXECUTED" in output
    assert "LIVE_SINGLE_OR_HOMOGENEOUS_SAMPLE" in output
    assert "LIVE_CANARY_NOT_EXECUTED" not in output
    assert "STRATZ_PUBLIC_CANARY_BLOCKED" in output


_ROBOTS_URL = "https://stratz.com/robots.txt"


def _live_success(
    match_id: str,
    *,
    outcome: StratzPublicSyncOutcome = StratzPublicSyncOutcome.INGESTED,
    storage_result: str = "inserted",
    family: str,
    patch: str,
    gold_points: int = 10,
    xp_points: int = 10,
) -> StratzPublicMatchSyncResult:
    return StratzPublicMatchSyncResult(
        match_id=match_id,
        outcome=outcome,
        http_status=200,
        storage_result=storage_result,
        competition_family=family,
        patch=patch,
        composition_complete=True,
        player_count=10,
        team_identity_status="complete",
        gold_advantage_points=gold_points,
        xp_advantage_points=xp_points,
        trajectory_time_semantics_status="source_index_unstable",
    )


class _CliFakeClient:
    def __init__(self, *, timeout: float) -> None:
        assert timeout == 10.0


class _FakePublicClient:
    def __init__(
        self,
        responses: dict[str, list[PublicHttpResponse]],
    ) -> None:
        self.responses = responses
        self.requested_urls: list[str] = []

    @classmethod
    def for_states(cls, states: dict[str, dict[str, object]]) -> "_FakePublicClient":
        responses: dict[str, list[PublicHttpResponse]] = {
            _ROBOTS_URL: [_response(_ROBOTS_URL, 200, "User-agent: *\nAllow: /\n")]
        }
        for match_id, state in states.items():
            url = build_public_match_url(PublicPageSource.STRATZ, match_id)
            responses[url] = [_response(url, 200, _next_data_html(state))]
        return cls(responses)

    def fetch(self, url: str) -> PublicHttpResponse:
        self.requested_urls.append(url)
        queue = self.responses.get(url)
        if not queue:
            return _response(url, 404, "missing")
        if len(queue) == 1:
            return queue[0]
        return queue.pop(0)


def _response(
    url: str,
    status_code: int,
    text: str,
    *,
    content_type: str = "text/html",
) -> PublicHttpResponse:
    return PublicHttpResponse(
        url=url,
        status_code=status_code,
        content_type=content_type,
        body=text.encode("utf-8"),
    )


def _next_data_html(state: dict[str, object]) -> str:
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"match": state}}})
        + "</script>"
    )


def _next_flight_html(state: dict[str, object]) -> str:
    return _next_flight_html_for_states(state)


def _next_flight_html_for_states(*states: dict[str, object]) -> str:
    chunks = []
    for index, state in enumerate(states, start=15):
        flight_payload = json.dumps(["$", f"$L{index}", None, {"children": state}])
        chunks.append(f"{index}:{flight_payload}\n")
    return (
        "<script>"
        + "self.__next_f.push("
        + json.dumps([1, *chunks])
        + ")"
        + "</script>"
    )


def _shallow_live_match_root(match_id: str) -> dict[str, object]:
    return {
        "match": {
            "matchId": match_id,
            "players": [
                {
                    "steamAccountId": 8000 + index,
                    "heroId": 80 + index,
                }
                for index in range(10)
            ],
        }
    }


def _real_shape_state() -> dict[str, object]:
    player_matches: list[dict[str, object]] = []
    for index in range(10):
        radiant = index < 5
        player_matches.append(
            {
                "steamAccountId": 9000 + index,
                "playerSlot": index if radiant else 128 + index,
                "heroId": 10 + index,
                "kills": index + 1,
                "deaths": 1,
                "assists": 6,
                "netWorth": 12_000 + index,
                "numLastHits": 120 + index,
                "numDenies": 8,
                "goldPerMinute": 500,
                "experiencePerMinute": 600,
                "level": 20,
                "heroDamage": 15_000,
                "towerDamage": 1_000,
                "heroHealing": 50,
                "item0Id": 1,
                "item1Id": 2,
                "item2Id": 3,
                "item3Id": 4,
                "item4Id": 5,
                "item5Id": 6,
            }
        )
    return {
        "routeTree": {
            "match": {
                "id": "8886013461",
                "endDateTime": "2026-07-08T12:45:00Z",
                "durationSeconds": 2700,
                "didRadiantWin": True,
                "gameVersionId": 176,
                "radiantTeam": {"id": 101, "name": "Real Radiant"},
                "direTeam": {"id": 202, "name": "Real Dire"},
                "series": {"id": "series-real", "type": "BO3", "gameNumber": 1},
                "scoreboard": {"playerMatches": player_matches},
                "draft": {"pickBans": _pick_bans(ordered=False)},
                "graphs": {
                    "radiantNetworthLeads": [0, 100, 1000],
                    "radiantExperienceLeads": [0, 50],
                },
            },
            "sidebar": {
                "id": "not-the-match",
                "players": [{"id": 1}, {"id": 2}],
            },
        }
    }


def _public_state(
    match_id: str,
    *,
    competition_name: str | None,
    patch: str = "176",
    ordered_draft: bool = True,
    include_team_identity: bool = True,
    include_player_stats: bool = True,
    include_items: bool = True,
    include_advantage: bool = True,
    advantage_mode: str = "unstable",
) -> dict[str, object]:
    players: list[dict[str, object]] = []
    for index in range(10):
        radiant = index < 5
        player: dict[str, object] = {
            "steamAccountId": 1000 + index,
            "playerSlot": index if radiant else 128 + index,
            "isRadiant": radiant,
            "heroId": 10 + index,
        }
        if include_player_stats:
            player.update(
                {
                    "kills": index,
                    "deaths": 1,
                    "assists": 5,
                    "netWorth": 12000 + index,
                    "numLastHits": 100 + index,
                    "numDenies": 10,
                    "goldPerMinute": 500,
                    "experiencePerMinute": 600,
                    "level": 20,
                    "heroDamage": 15000,
                    "towerDamage": 1000,
                    "heroHealing": 50,
                }
            )
        if include_items:
            player.update(
                {
                    "item0Id": 1,
                    "item1Id": 2,
                    "item2Id": 3,
                    "item3Id": 4,
                    "item4Id": 5,
                    "item5Id": 6,
                }
            )
        players.append(player)

    state: dict[str, object] = {
        "matchId": match_id,
        "startDateTime": "2026-07-08T12:00:00Z",
        "endDateTime": "2026-07-08T12:45:00Z",
        "durationSeconds": 2700,
        "didRadiantWin": True,
        "gameVersionId": patch,
        "series": {"id": f"series-{match_id}", "type": "BO3", "gameNumber": 1},
        "players": players,
        "pickBans": _pick_bans(ordered=ordered_draft),
    }
    if include_team_identity:
        state.update(
            {
                "radiantTeam": {"id": 101, "name": f"Radiant {match_id}"},
                "direTeam": {"id": 202, "name": f"Dire {match_id}"},
                "radiantTeamId": 101,
                "direTeamId": 202,
            }
        )
    if competition_name is not None:
        state["league"] = {"id": f"league-{match_id}", "name": competition_name}
    if include_advantage:
        if advantage_mode == "normalized":
            state["radiantNetworthLeads"] = [
                {"time": 0, "value": 0},
                {"time": 60, "value": 100},
            ]
            state["radiantExperienceLeads"] = [
                {"time": 0, "value": 0},
                {"time": 60, "value": 50},
            ]
        elif advantage_mode == "mixed":
            state["radiantNetworthLeads"] = [0, 100, 1000]
            state["radiantExperienceLeads"] = [
                {"time": 0, "value": 0},
                {"time": 60, "value": 50},
            ]
        else:
            state["radiantNetworthLeads"] = [0, 100, 1000]
            state["radiantExperienceLeads"] = [0, 50]
    return state


def _pick_bans(*, ordered: bool) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for order, hero_id in enumerate(range(10, 20), start=1):
        row: dict[str, object] = {
            "isPick": True,
            "isRadiant": order <= 5,
            "heroId": hero_id,
        }
        row["order" if ordered else "displayOrder"] = order
        rows.append(row)
    for order, hero_id in enumerate(range(30, 32), start=11):
        row = {
            "isPick": False,
            "team": 0 if order % 2 else 1,
            "heroId": hero_id,
        }
        row["order" if ordered else "displayOrder"] = order
        rows.append(row)
    return rows
