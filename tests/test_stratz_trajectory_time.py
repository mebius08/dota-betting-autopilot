from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import cli
from app.draft_history import (
    HistoricalDotaAdvantagePoint,
    HistoricalDotaGame,
    historical_dota_game_id,
)
import app.public_pages as public_pages
from app.public_pages import (
    PublicFieldProvenance,
    PublicHttpResponse,
    PublicPageSource,
    PublicPolicyCheck,
    observations_from_public_state,
)
from app.storage import SQLiteRepository


def test_public_client_mapping_can_confirm_while_preserving_source_index() -> None:
    game = _game("8886013461")
    points = (
        _point(game, "gold", 0, 10.0),
        _point(game, "gold", 1, 20.0),
        _point(game, "xp", 0, 5.0),
        _point(game, "xp", 1, 7.0),
    )
    mapping = public_pages.PublicClientTrajectoryTimeMapping(
        provenance=public_pages.TrajectoryTimeEvidenceProvenance.PUBLIC_CLIENT_INDEX_MAPPING,
        cadence_seconds=60,
        origin_offset_seconds=0,
        covered_metrics=("gold", "xp"),
        evidence_summary="deterministic test mapping",
    )

    assert public_pages.classify_trajectory_time_semantics_with_evidence(
        points,
        public_client_mapping=mapping,
    ) == "TRAJECTORY_TIME_SEMANTICS_CONFIRMED"

    normalized = public_pages.normalize_points_with_public_client_mapping(
        points,
        mapping,
    )

    assert [point.source_index for point in normalized] == [0, 1, 0, 1]
    assert [point.normalized_time_seconds for point in normalized] == [0, 60, 0, 60]
    assert all(point.time_semantics_status == "normalized_seconds" for point in normalized)


def test_mixed_public_client_mapping_is_partial() -> None:
    game = _game("8886013461")
    points = (
        _point(game, "gold", 0, 10.0),
        _point(game, "xp", 0, 5.0),
    )
    mapping = public_pages.PublicClientTrajectoryTimeMapping(
        provenance=public_pages.TrajectoryTimeEvidenceProvenance.PUBLIC_CLIENT_INDEX_MAPPING,
        cadence_seconds=60,
        origin_offset_seconds=0,
        covered_metrics=("gold",),
        evidence_summary="gold-only test mapping",
    )

    assert public_pages.classify_trajectory_time_semantics_with_evidence(
        points,
        public_client_mapping=mapping,
    ) == "TRAJECTORY_TIME_SEMANTICS_PARTIALLY_CONFIRMED"


def test_duration_point_count_correlation_alone_remains_unresolved(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "test.db")
    game = _game("8886013461", duration_seconds=3660)
    repository.upsert_historical_dota_game(game, ())
    repository.replace_historical_dota_advantage_points(
        game.id,
        tuple(
            _point(game, metric, index, float(index))
            for metric in ("gold", "xp")
            for index in range(62)
        ),
    )

    analysis = public_pages.build_point_count_duration_analysis(repository)
    points = repository.list_historical_dota_advantage_points(game.id)

    assert analysis.exact_floor_plus_constant == 1
    assert public_pages.classify_trajectory_time_semantics_with_evidence(
        points,
    ) == "TRAJECTORY_TIME_SEMANTICS_UNRESOLVED"


def test_timed_item_contract_requires_item_time_pairs_for_all_players() -> None:
    final_inventory_only = _state(include_timed_items=False)
    timed_items = _state(include_timed_items=True)

    final_inventory_observations = observations_from_public_state(
        final_inventory_only,
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )
    timed_item_observations = observations_from_public_state(
        timed_items,
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )

    assert "final_items" in final_inventory_observations
    assert "timed_item_data" not in final_inventory_observations
    assert timed_item_observations["timed_item_data"].present is True


def test_ordered_draft_requires_explicit_kind_not_action_number_only() -> None:
    state = _state(include_timed_items=False)
    state["pickBans"] = [
        {
            "actionNumber": index,
            "isRadiant": index <= 5,
            "heroId": 10 + index,
        }
        for index in range(1, 11)
    ]

    observations = observations_from_public_state(
        state,
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )

    assert "draft_action_order" in observations
    assert "draft_action_kind" not in observations
    assert "ordered_draft_actions" not in observations


def test_ordered_draft_can_use_explicit_phase_kind_grouping() -> None:
    state = _state(include_timed_items=False)
    state["pickBans"] = [
        {
            "actionNumber": order,
            "phaseName": "Ban phase 1" if order <= 4 else "Pick phase 1",
            "isRadiant": order % 2 == 1,
            "heroId": 30 + order,
        }
        for order in range(1, 7)
    ]

    observations = observations_from_public_state(
        state,
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )

    assert observations["draft_action_kind"].present is True
    assert observations["ordered_draft_actions"].present is True


def test_trajectory_time_diagnostic_reports_paths_and_is_concise(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = tmp_path / "test.db"
    repository = SQLiteRepository(db_path)
    game = _game("8886013461")
    repository.upsert_historical_dota_game(game, ())
    repository.replace_historical_dota_advantage_points(
        game.id,
        tuple(
            _point(game, metric, index, float(index))
            for metric in ("gold", "xp")
            for index in range(3)
        ),
    )

    state = _state(include_timed_items=True)
    html = _next_data_html(state)
    graph_html = _next_data_html(
        {
            "match": {
                **state,
                "graphs": {
                    "radiantNetworthLeads": [0, 100, 200],
                    "radiantExperienceLeads": [0, 50, 75],
                    "xAxisLabels": ["0", "1", "2"],
                },
            }
        }
    )
    client = _FakePublicClient(
        {
            "https://stratz.com/robots.txt": _response(
                "https://stratz.com/robots.txt",
                200,
                "User-agent: *\nAllow: /\n",
            ),
            public_pages.build_public_match_url(
                PublicPageSource.STRATZ,
                "8886013461",
            ): _response("overview", 200, html),
            "https://stratz.com/matches/8886013461/graphs/networth": _response(
                "graph",
                200,
                graph_html,
            ),
        }
    )

    result = public_pages.build_stratz_trajectory_time_diagnostic(
        repository=repository,
        client=client,  # type: ignore[arg-type]
        match_ids=("8886013461",),
        delay_seconds=0,
    )
    rendered = public_pages.render_stratz_trajectory_time_diagnostic(result)

    assert "path=props.pageProps.match.radiantNetworthLeads" in rendered
    assert "coordinate_candidates=xAxisLabels: list[3], matches_points=True" in rendered
    assert "Point-count versus duration analysis" in rendered
    assert len(rendered) < 6000

    exit_code = cli.main(["stratz-trajectory-time-diagnostic", "--help"])
    help_output = capsys.readouterr().out

    assert exit_code == 0
    assert "stratz-trajectory-time-diagnostic" in help_output


def test_client_asset_inspection_uses_only_overview_referenced_scripts() -> None:
    match_id = "8886013461"
    overview_url = public_pages.build_public_match_url(
        PublicPageSource.STRATZ,
        match_id,
    )
    referenced_asset_url = "https://stratz.com/_next/static/chunks/match.js"
    preloaded_asset_url = "https://stratz.com/_next/static/chunks/route.js"
    client = _FakePublicClient(
        {
            "https://stratz.com/robots.txt": _response(
                "https://stratz.com/robots.txt",
                200,
                "User-agent: *\nDisallow: /matches/\nAllow: /match/\n",
            ),
            overview_url: _response(
                overview_url,
                200,
                (
                    '<script src="/_next/static/chunks/match.js"></script>'
                    '<link href="/_next/static/chunks/route.js" '
                    'rel="preload" as="script">'
                    '<script src="https://cdn.example/chunk.js"></script>'
                    '<script src="/_next/data/build/match.json"></script>'
                ),
            ),
            referenced_asset_url: _response(
                referenced_asset_url,
                200,
                "radiantNetworthLeads minute 60",
            ),
            preloaded_asset_url: _response(
                preloaded_asset_url,
                200,
                "radiantExperienceLeads minute 60",
            ),
        }
    )

    result = public_pages.build_stratz_trajectory_time_diagnostic(
        repository=None,
        client=client,  # type: ignore[arg-type]
        match_ids=(match_id,),
        delay_seconds=0,
        inspect_client_assets=True,
        max_client_assets=4,
    )
    diagnostic = result.match_diagnostics[0]
    rendered = public_pages.render_stratz_trajectory_time_diagnostic(result)

    assert diagnostic.graph.robots_disallowed is True
    assert diagnostic.client_asset_urls == (
        referenced_asset_url,
        preloaded_asset_url,
    )
    assert [asset.url for asset in diagnostic.client_assets] == [
        referenced_asset_url,
        preloaded_asset_url,
    ]
    assert "Public client asset evidence: discovered=2 inspected=2" in rendered
    assert "https://stratz.com/matches/8886013461/graphs/networth" not in client.requests


def _state(*, include_timed_items: bool) -> dict[str, object]:
    players: list[dict[str, object]] = []
    for index in range(10):
        radiant = index < 5
        player: dict[str, object] = {
            "steamAccountId": 1000 + index,
            "playerSlot": index if radiant else 128 + index,
            "isRadiant": radiant,
            "heroId": 10 + index,
            "item0Id": 1,
            "item1Id": 2,
            "item2Id": 3,
            "item3Id": 4,
            "item4Id": 5,
            "item5Id": 6,
        }
        if include_timed_items:
            player["items"] = [{"itemId": 50 + index, "time": 600 + index}]
        players.append(player)
    return {
        "matchId": "8886013461",
        "startDateTime": "2026-07-08T12:00:00Z",
        "endDateTime": "2026-07-08T13:01:00Z",
        "durationSeconds": 3660,
        "didRadiantWin": True,
        "gameVersionId": 182,
        "radiantTeam": {"id": 101, "name": "Radiant"},
        "direTeam": {"id": 202, "name": "Dire"},
        "players": players,
        "radiantNetworthLeads": [0, 100, 200],
        "radiantExperienceLeads": [0, 50, 75],
    }


def _game(
    source_game_id: str,
    *,
    duration_seconds: int = 3660,
) -> HistoricalDotaGame:
    started_at = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    return HistoricalDotaGame(
        id=historical_dota_game_id(public_pages.STRATZ_PUBLIC_SOURCE, source_game_id),
        source=public_pages.STRATZ_PUBLIC_SOURCE,
        source_game_id=source_game_id,
        started_at=started_at,
        ended_at=started_at + timedelta(seconds=duration_seconds),
        team_a_name="Radiant",
        team_b_name="Dire",
        team_a_source_id="101",
        team_b_source_id="202",
        winner_side="team_a",
        team_a_side="radiant",
        patch="182",
    )


def _point(
    game: HistoricalDotaGame,
    metric: str,
    source_index: int,
    value: float,
) -> HistoricalDotaAdvantagePoint:
    return HistoricalDotaAdvantagePoint(
        id=f"{game.id}:advantage:{metric}:{source_index}",
        game_id=game.id,
        source=game.source,
        source_game_id=game.source_game_id,
        metric=metric,  # type: ignore[arg-type]
        source_index=source_index,
        value=value,
    )


def _next_data_html(state: dict[str, object]) -> str:
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"match": state}}})
        + "</script>"
    )


def _response(url: str, status_code: int, text: str) -> PublicHttpResponse:
    return PublicHttpResponse(
        url=url,
        status_code=status_code,
        content_type="text/html",
        body=text.encode("utf-8"),
    )


class _FakePublicClient:
    def __init__(self, responses: dict[str, PublicHttpResponse]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def fetch(self, url: str) -> PublicHttpResponse:
        self.requests.append(url)
        if url == "https://stratz.com/robots.txt":
            return self.responses[url]
        return self.responses.get(url, _response(url, 404, "missing"))


def _unused_policy() -> PublicPolicyCheck:
    return PublicPolicyCheck(
        source=PublicPageSource.STRATZ,
        robots_url="https://stratz.com/robots.txt",
        http_status=200,
        content_type="text/plain",
        byte_size=10,
        checked_path="/match/1",
        path_disallowed=False,
        relevant_rules=(),
        content_signals=(),
    )
