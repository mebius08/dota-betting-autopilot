from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import cast
from urllib.request import Request

from app.public_pages import (
    PUBLIC_PAGE_USER_AGENT,
    PublicArchitectureDecision,
    PublicDataUsage,
    PublicFieldProvenance,
    PublicHttpResponse,
    PublicMatchPageProbe,
    PublicPageAccessStatus,
    PublicPageHttpClient,
    PublicPageAnalysis,
    PublicPageProbeResult,
    PublicPageSource,
    PublicPolicyCheck,
    PublicSourceCoverageClassification,
    PublicWorkloadSuitability,
    build_public_source_contract,
    build_public_match_url,
    determine_public_source_recommendation,
    evaluate_robots_path,
    extract_embedded_public_states,
    extract_public_referenced_resource_urls,
    extract_visible_html_values,
)
from app.public_pages.feasibility import (
    PUBLIC_FIELD_DEFINITIONS,
    aggregate_public_field_coverage,
    analyze_public_match_page_response,
    observations_from_public_state,
)


def test_build_stratz_public_match_url() -> None:
    assert (
        build_public_match_url(PublicPageSource.STRATZ, "8886013461")
        == "https://stratz.com/match/8886013461"
    )


def test_http_client_uses_honest_project_user_agent_and_no_auth_header() -> None:
    opener = _FakeUrlOpen({"https://stratz.com/robots.txt": _ok("User-agent: *\n")})
    client = PublicPageHttpClient(urlopen_func=opener)

    client.fetch("https://stratz.com/robots.txt")

    request = opener.requests[0]
    assert request.get_header("User-agent") == PUBLIC_PAGE_USER_AGENT
    assert request.get_header("Authorization") is None


def test_robots_policy_path_matching_distinguishes_match_from_matches() -> None:
    robots = """
User-agent: *
Allow: /matches/live
Disallow: /matches/*
"""

    singular_disallowed, singular_rules = evaluate_robots_path(
        robots,
        user_agent="*",
        path="/match/8886013461",
    )
    plural_disallowed, plural_rules = evaluate_robots_path(
        robots,
        user_agent="*",
        path="/matches/8886013461",
    )

    assert singular_disallowed is False
    assert singular_rules == ()
    assert plural_disallowed is True
    assert plural_rules == ("Disallow: /matches/*",)


def test_robots_disallowed_match_path_prevents_page_fetch() -> None:
    opener = _FakeUrlOpen(
        {
            "https://stratz.com/robots.txt": _ok(
                "User-agent: *\nDisallow: /match/*\n"
            ),
            "https://stratz.com/match/8886013461": _ok("<html></html>"),
        }
    )
    client = PublicPageHttpClient(urlopen_func=opener)
    probe = PublicMatchPageProbe(client, sleep_func=lambda _: None)

    result = probe.run(match_ids=("8886013461",), delay_seconds=0)

    assert result.policy.path_disallowed is True
    assert result.analyses[0].access_status is PublicPageAccessStatus.ROBOTS_PATH_DISALLOWED
    assert [request.full_url for request in opener.requests] == [
        "https://stratz.com/robots.txt"
    ]


def test_successful_public_html_fetch_extracts_static_fields() -> None:
    html = """
<html>
  <span data-field="match-id">8886013461</span>
  <span data-field="radiant-team-id">101</span>
  <span data-field="dire-team-id">202</span>
  <span data-field="radiant-team">LGD Gaming</span>
  <span data-field="dire-team">Virtus.pro</span>
  <span data-radiant-pick="1"></span>
  <span data-radiant-pick="2"></span>
  <span data-radiant-pick="3"></span>
  <span data-radiant-pick="4"></span>
  <span data-radiant-pick="5"></span>
  <span data-dire-pick="6"></span>
  <span data-dire-pick="7"></span>
  <span data-dire-pick="8"></span>
  <span data-dire-pick="9"></span>
  <span data-dire-pick="10"></span>
</html>
"""
    values = extract_visible_html_values(html)

    analysis, requests = analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="8886013461",
        url="https://stratz.com/match/8886013461",
        response=PublicHttpResponse(
            url="https://stratz.com/match/8886013461",
            status_code=200,
            content_type="text/html",
            body=html.encode("utf-8"),
        ),
    )

    assert requests == 0
    assert values["match_id"] == ("8886013461",)
    assert analysis.access_status is PublicPageAccessStatus.PUBLIC_PAGE_AVAILABLE
    assert analysis.observations["stable_match_id"].provenance is PublicFieldProvenance.VISIBLE_HTML
    assert analysis.observations["complete_5v5_picks"].present is True


def test_embedded_json_state_detection_and_field_extraction() -> None:
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"match": _complete_public_state()}}})
        + "</script>"
    )

    states, findings = extract_embedded_public_states(html)
    observations = observations_from_public_state(
        states[0],
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )

    assert "embedded public state parsed from __NEXT_DATA__" in findings
    assert observations["player_account_ids"].present is True
    assert observations["complete_5v5_picks"].present is True
    assert observations["ordered_draft_actions"].present is True
    assert observations["first_pick_side"].provenance is PublicFieldProvenance.DERIVED_FROM_PUBLIC_FIELDS
    assert observations["timed_item_data"].present is True
    assert observations["advantage_timeline"].present is True


def test_next_flight_stream_state_detection() -> None:
    flight_payload = json.dumps(
        [
            "$",
            "$L1a",
            None,
            {"data": {"match": _complete_public_state()}},
        ]
    )
    html = (
        "<script>"
        + "self.__next_f.push("
        + json.dumps([1, f"15:{flight_payload}\n"])
        + ")"
        + "</script>"
    )

    states, findings = extract_embedded_public_states(html)
    observations = observations_from_public_state(
        states[0],
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )

    assert states
    assert "embedded public state parsed from Next flight stream" in findings
    assert observations["complete_5v5_picks"].present is True


def test_malformed_embedded_json_is_reported_without_crashing() -> None:
    states, findings = extract_embedded_public_states(
        '<script id="__NEXT_DATA__" type="application/json">{bad</script>'
    )

    assert states == ()
    assert any("malformed embedded JSON" in finding for finding in findings)


def test_sparse_js_page_is_not_called_access_control_required() -> None:
    html = "<html><script src='/app.js'></script><div id='root'></div></html>"

    analysis, _ = analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="8886013461",
        url="https://stratz.com/match/8886013461",
        response=PublicHttpResponse(
            url="https://stratz.com/match/8886013461",
            status_code=200,
            content_type="text/html",
            body=html.encode("utf-8"),
        ),
    )

    assert analysis.access_status is PublicPageAccessStatus.PAGE_AVAILABLE_DATA_NOT_STATIC
    assert analysis.access_status is not PublicPageAccessStatus.ACCESS_CONTROL_REQUIRED


def test_public_referenced_json_resource_is_detected_and_parsed() -> None:
    html = "<html><link rel='preload' href='/_next/data/build/match.json'></html>"
    opener = _FakeUrlOpen(
        {
            "https://stratz.com/_next/data/build/match.json": _ok(
                json.dumps(_complete_public_state()),
                content_type="application/json",
            )
        }
    )
    urls = extract_public_referenced_resource_urls(
        html,
        "https://stratz.com/match/8886013461",
    )

    analysis, requests = analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="8886013461",
        url="https://stratz.com/match/8886013461",
        response=PublicHttpResponse(
            url="https://stratz.com/match/8886013461",
            status_code=200,
            content_type="text/html",
            body=html.encode("utf-8"),
        ),
        client=PublicPageHttpClient(urlopen_func=opener),
        fetch_referenced_resources=True,
    )

    assert urls == ("https://stratz.com/_next/data/build/match.json",)
    assert requests == 1
    assert analysis.observations["stable_match_id"].provenance is PublicFieldProvenance.PUBLIC_PAGE_REFERENCED_RESOURCE


def test_incomplete_picks_do_not_count_as_complete_5v5() -> None:
    state = _complete_public_state()
    players = cast(list[dict[str, object]], state["players"])
    state["players"] = players[:8]
    state["pickBans"] = []

    observations = observations_from_public_state(
        state,
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )

    assert "complete_5v5_picks" not in observations
    assert "radiant_picks" in observations
    assert "dire_picks" not in observations


def test_display_order_is_not_treated_as_draft_order_without_semantics() -> None:
    state = _complete_public_state()
    state["pickBans"] = [
        {
            "displayOrder": index,
            "isPick": True,
            "isRadiant": index <= 5,
            "heroId": index,
        }
        for index in range(1, 11)
    ]

    observations = observations_from_public_state(
        state,
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )

    assert "ordered_draft_actions" not in observations
    assert "draft_action_order" not in observations
    assert observations["complete_5v5_picks"].present is True


def test_banned_hero_fields_count_as_explicit_bans() -> None:
    state = _complete_public_state()
    state["pickBans"] = [
        {
            "order": 0,
            "heroId": 85,
            "bannedHeroId": 85,
            "wasBannedSuccessfully": True,
            "isRadiant": True,
        }
    ]

    observations = observations_from_public_state(
        state,
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )

    assert observations["bans"].present is True
    assert "ordered_draft_actions" not in observations


def test_player_stable_id_handling_does_not_require_display_names() -> None:
    state = _complete_public_state()
    for player in cast(list[dict[str, object]], state["players"]):
        player.pop("name")

    observations = observations_from_public_state(
        state,
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )

    assert observations["player_account_ids"].present is True
    assert "player_display_names" not in observations


def test_post_game_fields_keep_leakage_usage_classification() -> None:
    by_key = {definition.key: definition for definition in PUBLIC_FIELD_DEFINITIONS}

    assert PublicDataUsage.POST_GAME_TARGET_OR_LABEL in by_key["duration"].usage
    assert PublicDataUsage.POST_GAME_TARGET_OR_LABEL in by_key["winner_side"].usage
    assert PublicDataUsage.POST_GAME_TARGET_OR_LABEL in by_key["individual_kills"].usage
    assert PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT in by_key["complete_5v5_picks"].usage


def test_mixed_field_coverage_records_provenance() -> None:
    complete, _ = analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="1",
        url="https://stratz.com/match/1",
        response=PublicHttpResponse(
            url="https://stratz.com/match/1",
            status_code=200,
            content_type="text/html",
            body=(
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps({"match": _complete_public_state()})
                + "</script>"
            ).encode("utf-8"),
        ),
    )
    sparse, _ = analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="2",
        url="https://stratz.com/match/2",
        response=PublicHttpResponse(
            url="https://stratz.com/match/2",
            status_code=200,
            content_type="text/html",
            body=b"<html></html>",
        ),
    )

    coverage = {
        row.key: row for row in aggregate_public_field_coverage([complete, sparse])
    }

    assert coverage["complete_5v5_picks"].present_count == 1
    assert coverage["complete_5v5_picks"].applicable_count == 2
    assert (
        PublicFieldProvenance.DERIVED_FROM_PUBLIC_FIELDS
        in coverage["complete_5v5_picks"].provenance
    )


def test_source_contract_classifies_supported_derivable_missing_and_unstable() -> None:
    state = _complete_public_state()
    state.pop("startDateTime")
    analysis, _ = analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="1",
        url="https://stratz.com/match/1",
        response=PublicHttpResponse(
            url="https://stratz.com/match/1",
            status_code=200,
            content_type="text/html",
            body=(
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps({"match": state})
                + "</script>"
            ).encode("utf-8"),
        ),
    )

    contract = build_public_source_contract(_probe_result((analysis,)))
    rows = {row.key: row for row in contract.coverage}

    assert (
        rows["hero_picks"].classification
        is PublicSourceCoverageClassification.DERIVABLE
    )
    assert (
        rows["start_time"].classification
        is PublicSourceCoverageClassification.DERIVABLE
    )
    assert (
        rows["winner_result"].classification
        is PublicSourceCoverageClassification.SUPPORTED
    )
    assert (
        rows["time_series_resolution"].classification
        is PublicSourceCoverageClassification.UNSTABLE
    )
    assert (
        rows["buybacks"].classification
        is PublicSourceCoverageClassification.MISSING
    )


def test_source_contract_records_partial_coverage_across_sample() -> None:
    complete, _ = analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="1",
        url="https://stratz.com/match/1",
        response=PublicHttpResponse(
            url="https://stratz.com/match/1",
            status_code=200,
            content_type="text/html",
            body=(
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps({"match": _complete_public_state()})
                + "</script>"
            ).encode("utf-8"),
        ),
    )
    sparse, _ = analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="2",
        url="https://stratz.com/match/2",
        response=PublicHttpResponse(
            url="https://stratz.com/match/2",
            status_code=200,
            content_type="text/html",
            body=b"<html></html>",
        ),
    )

    contract = build_public_source_contract(_probe_result((complete, sparse)))
    rows = {row.key: row for row in contract.coverage}

    assert (
        rows["hero_picks"].classification
        is PublicSourceCoverageClassification.PARTIAL
    )


def test_source_contract_assesses_workloads_and_architecture_decision() -> None:
    analysis, _ = analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="1",
        url="https://stratz.com/match/1",
        response=PublicHttpResponse(
            url="https://stratz.com/match/1",
            status_code=200,
            content_type="text/html",
            body=(
                '<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps({"match": _complete_public_state()})
                + "</script>"
            ).encode("utf-8"),
        ),
    )

    contract = build_public_source_contract(_probe_result((analysis,)))
    workloads = {row.workload: row for row in contract.workloads}

    assert (
        workloads["POST_DRAFT win probability"].suitability
        is PublicWorkloadSuitability.SUFFICIENT_WITH_LIMITATIONS
    )
    assert (
        workloads["live state estimation"].suitability
        is PublicWorkloadSuitability.INSUFFICIENT
    )
    assert (
        contract.architecture_decision
        is PublicArchitectureDecision.STRATZ_PUBLIC_SUFFICIENT
    )


def test_suggestive_event_key_without_event_semantics_is_not_supported() -> None:
    observations = observations_from_public_state(
        {
            "matchId": 1,
            "killEvents": [{"label": "killEvents"}],
            "roshanEvents": [{"label": "roshanEvents"}],
            "buildingEvents": [{"label": "buildingEvents"}],
        },
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE,
    )

    assert "kill_events" not in observations
    assert "roshan_objectives" not in observations
    assert "tower_barracks_objectives" not in observations


def test_http_403_and_429_have_distinct_access_semantics() -> None:
    forbidden, _ = analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="1",
        url="https://stratz.com/match/1",
        response=PublicHttpResponse(
            url="https://stratz.com/match/1",
            status_code=403,
            content_type="text/html",
            body=b"",
        ),
    )
    limited, _ = analyze_public_match_page_response(
        source=PublicPageSource.STRATZ,
        match_id="2",
        url="https://stratz.com/match/2",
        response=PublicHttpResponse(
            url="https://stratz.com/match/2",
            status_code=429,
            content_type="text/html",
            body=b"",
        ),
    )

    assert forbidden.access_status is PublicPageAccessStatus.HTTP_FORBIDDEN
    assert limited.access_status is PublicPageAccessStatus.HTTP_RATE_LIMITED


def test_probe_does_not_use_stratz_token_or_database(monkeypatch) -> None:
    monkeypatch.setenv("STRATZ_TOKEN", "secret-token")
    opener = _FakeUrlOpen(
        {
            "https://stratz.com/robots.txt": _ok("User-agent: *\nAllow: /\n"),
            "https://stratz.com/match/8886013461": _ok("<html></html>"),
        }
    )
    client = PublicPageHttpClient(urlopen_func=opener)
    probe = PublicMatchPageProbe(client, sleep_func=lambda _: None)

    result = probe.run(match_ids=("8886013461",), delay_seconds=0)

    assert result.request_count == 2
    assert all(request.get_header("Authorization") is None for request in opener.requests)
    assert os.environ["STRATZ_TOKEN"] == "secret-token"


class _FakeResponse:
    def __init__(self, body: bytes, *, content_type: str = "text/html") -> None:
        self.body = body
        self.status = 200
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        return False


class _FakeUrlOpen:
    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[Request] = []

    def __call__(
        self,
        request: Request,
        data: object | None = None,
        timeout: float = 10.0,
    ) -> _FakeResponse:
        self.requests.append(request)
        if data is not None:
            raise AssertionError("public page probe must not pass positional data")
        return self.responses[request.full_url]


def _ok(value: str, *, content_type: str = "text/html") -> _FakeResponse:
    return _FakeResponse(value.encode("utf-8"), content_type=content_type)


def _probe_result(
    analyses: tuple[PublicPageAnalysis, ...],
) -> PublicPageProbeResult:
    coverage = aggregate_public_field_coverage(analyses)
    return PublicPageProbeResult(
        source=PublicPageSource.STRATZ,
        probe_started_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        request_count=len(analyses),
        policy=PublicPolicyCheck(
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
        analyses=analyses,
        coverage=coverage,
        recommendation=determine_public_source_recommendation(analyses, coverage),
    )


def _complete_public_state() -> dict[str, object]:
    players: list[dict[str, object]] = []
    for index in range(10):
        radiant = index < 5
        players.append(
            {
                "steamAccountId": 1000 + index,
                "playerSlot": index if radiant else 128 + index,
                "isRadiant": radiant,
                "heroId": 10 + index,
                "name": f"Player {index}",
                "kills": index,
                "deaths": 1,
                "assists": 5,
                "netWorth": 12000,
                "numLastHits": 100,
                "numDenies": 10,
                "goldPerMinute": 500,
                "experiencePerMinute": 600,
                "level": 20,
                "heroDamage": 15000,
                "towerDamage": 1000,
                "heroHealing": 50,
                "item0Id": 1,
                "item1Id": 2,
                "item2Id": 3,
                "item3Id": 4,
                "item4Id": 5,
                "item5Id": 6,
                "items": [{"itemId": 50 + index, "time": 600}],
            }
        )
    pick_bans: list[dict[str, object]] = []
    for order, hero_id in enumerate(range(10, 20), start=1):
        pick_bans.append(
            {
                "order": order,
                "isPick": True,
                "isRadiant": order <= 5,
                "heroId": hero_id,
            }
        )
    for order, hero_id in enumerate(range(30, 34), start=11):
        pick_bans.append(
            {
                "order": order,
                "isPick": False,
                "team": 0 if order % 2 else 1,
                "heroId": hero_id,
            }
        )
    return {
        "matchId": 8886013461,
        "startDateTime": "2026-07-08T12:00:00Z",
        "endDateTime": "2026-07-08T12:45:00Z",
        "durationSeconds": 2700,
        "didRadiantWin": True,
        "gameVersionId": 176,
        "league": {"id": 1, "displayName": "Esports World Cup 2026"},
        "series": {"id": 2, "type": "BO3", "gameNumber": 1},
        "radiantTeam": {"id": 101, "name": "LGD Gaming"},
        "direTeam": {"id": 202, "name": "Virtus.pro"},
        "radiantTeamId": 101,
        "direTeamId": 202,
        "radiantKills": 30,
        "direKills": 20,
        "towerStatusRadiant": 1,
        "towerStatusDire": 2,
        "barracksStatusRadiant": 3,
        "barracksStatusDire": 4,
        "radiantNetworthLeads": [0, 100, 1000],
        "radiantExperienceLeads": [0, 50, 500],
        "killEvents": [{"time": 120, "killer": 1000, "victim": 1005}],
        "roshanEvents": [{"time": 1800, "team": "radiant"}],
        "tormentorEvents": [{"time": 1500, "team": "dire"}],
        "players": players,
        "pickBans": pick_bans,
    }
