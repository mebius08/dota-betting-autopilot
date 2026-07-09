from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.request import Request

import pytest

from app.history import HistoricalCompetitionFamily
from app.stratz import (
    STRATZ_TOKEN_ENV,
    AccessStatus,
    CoverageClassification,
    GraphQLErrorClassification,
    SourceVerdict,
    StratzAccessCapability,
    StratzConfigurationError,
    StratzFeasibilityProbe,
    StratzFieldGroupAccess,
    StratzGraphQLError,
    StratzGraphQLClient,
    StratzProbeResult,
    StratzSchemaSnapshot,
    GraphQLTypeDefinition,
    aggregate_field_coverage,
    analyze_stratz_match_payload,
    apply_access_capability_to_query_plan,
    build_stratz_query_plan,
    chunk_match_ids,
    classify_graphql_error,
    coverage_by_key,
    determine_source_verdict,
    extract_query_field_names,
    inspect_stratz_schema,
    parse_graphql_type_definition,
    parse_graphql_type_ref,
    parse_match_candidates,
    probe_stratz_access_capability,
    render_graphql_type,
    render_probe_result,
    select_representative_candidates,
)


def test_stratz_graphql_request_uses_bearer_token_and_keyword_timeout() -> None:
    urlopen = _StdlibShapedFakeOpen(b'{"data":{"ok":true}}')
    client = StratzGraphQLClient(
        token="secret-token",
        timeout=2.5,
        urlopen_func=urlopen,
    )

    response = client.execute("query Test { ok }", {"take": 1})

    assert response.data == {"ok": True}
    request = _assert_single_stratz_request(urlopen, expected_timeout=2.5)
    assert request.full_url == "https://api.stratz.com/graphql"
    assert request.get_header("Authorization") == "Bearer secret-token"
    assert request.get_header("Content-type") == "application/json"
    body = json.loads((request.data or b"").decode("utf-8"))
    assert body == {"query": "query Test { ok }", "variables": {"take": 1}}


def test_stratz_missing_token_names_env_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(STRATZ_TOKEN_ENV, raising=False)
    urlopen = _StdlibShapedFakeOpen(b'{"data":{"ok":true}}')
    client = StratzGraphQLClient(urlopen_func=urlopen)

    with pytest.raises(StratzConfigurationError) as exc_info:
        client.execute("query Test { ok }")

    assert STRATZ_TOKEN_ENV in str(exc_info.value)
    assert urlopen.requests == []


def test_stratz_graphql_errors_are_failures_and_redact_token() -> None:
    urlopen = _StdlibShapedFakeOpen(
        b'{"errors":[{"message":"bad secret-token"}],"data":{"match":null}}'
    )
    client = StratzGraphQLClient(
        token="secret-token",
        urlopen_func=urlopen,
    )

    with pytest.raises(StratzGraphQLError) as exc_info:
        client.execute("query Test { match { id } }")

    assert "STRATZ GraphQL errors" in str(exc_info.value)
    assert "secret-token" not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)


def test_extract_query_field_names_from_introspection_shape() -> None:
    names = extract_query_field_names(
        {
            "__type": {
                "name": "DotaQuery",
                "kind": "OBJECT",
                "fields": [
                    {"name": "match", "type": _scalar_ref("Boolean"), "args": []},
                    {"name": "matches", "type": _list_ref("MatchType"), "args": []},
                    {"name": "match", "type": _scalar_ref("Boolean"), "args": []},
                ]
            }
        }
    )

    assert names == ("match", "matches")


def test_graphql_type_renderer_handles_required_list_of_long() -> None:
    type_ref = parse_graphql_type_ref(_non_null_ref(_list_ref("Long")))

    assert render_graphql_type(type_ref) == "[Long]!"


def test_schema_plan_uses_matches_ids_and_excludes_absent_tournament() -> None:
    plan = build_stratz_query_plan(_schema_fixture())

    assert plan.match_fetch_field == "matches"
    assert plan.match_fetch_argument == "ids"
    assert plan.match_fetch_argument_type == "[Long]!"
    assert plan.match_query is not None
    assert "matches(ids: $ids)" in plan.match_query
    assert "$ids: [Long]!" in plan.match_query
    assert "tournament {" not in plan.match_query
    assert "tournamentId" in plan.match_query
    assert "tournamentRound" in plan.match_query
    assert "tournament" in plan.match_type_fields_absent


def test_schema_plan_records_additional_metadata_queries_when_only_ids_exist() -> None:
    plan = build_stratz_query_plan(_schema_fixture())

    assert "tournament metadata by tournamentId" in plan.additional_query_requirements


def test_schema_inspection_fetches_root_dota_query_and_match_type() -> None:
    client = _SchemaFakeClient()

    schema, request_count = inspect_stratz_schema(client)  # type: ignore[arg-type]

    assert request_count >= 3
    assert schema.query_type_name == "DotaQuery"
    assert "matches" in schema.types["DotaQuery"].fields
    assert "tournamentId" in schema.types["MatchType"].fields


def test_probe_without_match_ids_reports_missing_discovery_path() -> None:
    probe = StratzFeasibilityProbe(_SchemaFakeClient())  # type: ignore[arg-type]

    with pytest.raises(ValueError) as exc_info:
        probe.run(sample_size=1, match_ids=())

    message = str(exc_info.value)
    assert "Automatic STRATZ professional-match discovery is not verified" in message
    assert "matches(ids: [Long]!)" in message


def test_parse_and_select_representative_stratz_candidates() -> None:
    candidates = parse_match_candidates(
        {
            "matches": [
                _candidate("1", "DreamLeague Season 28"),
                _candidate("2", "FISSURE Universe"),
                _candidate("3", "FISSURE Playground 2"),
                _candidate("4", "PGL Open Qualifier"),
                _candidate("5", "The International 2025"),
            ]
        }
    )

    selected = select_representative_candidates(candidates, sample_size=3)

    assert [candidate.match_id for candidate in selected] == ["5", "1", "3"]


def test_complete_payload_reports_draft_and_rich_coverage() -> None:
    analysis = analyze_stratz_match_payload(
        _complete_match_payload(),
        query_plan=build_stratz_query_plan(_schema_fixture()),
    )
    coverage = coverage_by_key(aggregate_field_coverage([analysis]))

    assert analysis.identity.match_id == "9001"
    assert analysis.identity.competition_family.value == "dreamleague"
    assert coverage["complete_5v5_picks"].classification is CoverageClassification.ALWAYS_PRESENT
    assert coverage["ordered_draft_actions"].classification is CoverageClassification.ALWAYS_PRESENT
    assert coverage["first_pick_side"].classification is CoverageClassification.DERIVABLE_FROM_RETURNED_FIELDS
    assert coverage["team_kills_final_score"].classification is CoverageClassification.DERIVABLE_FROM_RETURNED_FIELDS
    assert coverage["timed_item_data"].classification is CoverageClassification.PROVIDER_DERIVED


def test_mixed_payloads_classify_incomplete_draft_as_partial() -> None:
    plan = build_stratz_query_plan(_schema_fixture())
    complete = analyze_stratz_match_payload(_complete_match_payload(), query_plan=plan)
    partial = analyze_stratz_match_payload(_partial_match_payload(), query_plan=plan)

    coverage = coverage_by_key(aggregate_field_coverage([complete, partial]))

    assert coverage["complete_5v5_picks"].present_count == 1
    assert coverage["complete_5v5_picks"].applicable_count == 2
    assert coverage["complete_5v5_picks"].classification is CoverageClassification.PARTIAL
    assert coverage["bans"].classification is CoverageClassification.PARTIAL


def test_real_source_verdict_requires_real_samples_and_required_coverage() -> None:
    analysis = analyze_stratz_match_payload(
        _complete_match_payload(),
        query_plan=build_stratz_query_plan(_schema_fixture()),
    )
    coverage = aggregate_field_coverage([analysis])

    assert (
        determine_source_verdict(
            coverage,
            sample_count=1,
            real_source=True,
            minimum_real_samples=1,
        )
        is SourceVerdict.STRATZ_FREE_SOURCE_FEASIBLE
    )
    assert (
        determine_source_verdict(
            coverage,
            sample_count=1,
            real_source=False,
            minimum_real_samples=1,
        )
        is None
    )


def test_coverage_marks_schema_absence_separately_from_sample_missing() -> None:
    plan = build_stratz_query_plan(_schema_fixture_without_draft())
    analysis = analyze_stratz_match_payload(_partial_match_payload(), query_plan=plan)
    coverage = coverage_by_key(aggregate_field_coverage([analysis]))

    assert coverage["bans"].classification is CoverageClassification.ABSENT
    assert "verified current schema" in coverage["bans"].semantics
    assert coverage["individual_kills"].classification is CoverageClassification.ABSENT
    assert "verified current schema" not in coverage["individual_kills"].semantics


@pytest.mark.parametrize(
    ("count", "expected_sizes"),
    [
        (1, (1,)),
        (10, (10,)),
        (12, (10, 2)),
        (20, (10, 10)),
        (21, (10, 10, 1)),
    ],
)
def test_chunk_match_ids_enforces_observed_stratz_limit(
    count: int,
    expected_sizes: tuple[int, ...],
) -> None:
    match_ids = tuple(str(8885000000 + index) for index in range(count))

    batches = chunk_match_ids(match_ids)

    assert tuple(len(batch) for batch in batches) == expected_sizes
    assert all(len(batch) <= 10 for batch in batches)
    assert sum(batches, ()) == match_ids


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "STRATZ GraphQL errors: Requesting Too Many MatchIds. Max Request Size 10.",
            GraphQLErrorClassification.REQUEST_SIZE_LIMIT,
        ),
        (
            "STRATZ GraphQL errors: User is not an admin.",
            GraphQLErrorClassification.PERMISSION_RESTRICTED,
        ),
    ],
)
def test_classify_graphql_error_handles_live_failures(
    message: str,
    expected: GraphQLErrorClassification,
) -> None:
    assert classify_graphql_error(message) is expected


def test_access_capability_starts_with_minimal_match_id_probe() -> None:
    schema = _schema_fixture()
    query_plan = build_stratz_query_plan(schema)
    client = _AccessFakeClient()

    capability = probe_stratz_access_capability(
        client=client,  # type: ignore[arg-type]
        schema=schema,
        query_plan=query_plan,
        match_id="8886013461",
    )

    assert capability.minimal_match_fetch_status is AccessStatus.ACCESSIBLE
    minimal_query = client.operational_queries[0]
    assert "query StratzMinimalMatchAccessProbe" in minimal_query
    assert "id" in _query_tokens(minimal_query)
    assert "players" not in _query_tokens(minimal_query)
    assert "durationSeconds" not in _query_tokens(minimal_query)


def test_access_capability_narrows_permission_restricted_top_level_field() -> None:
    schema = _schema_fixture()
    query_plan = build_stratz_query_plan(schema)
    client = _AccessFakeClient(restricted_fields={"towerStatusRadiant"})

    capability = probe_stratz_access_capability(
        client=client,  # type: ignore[arg-type]
        schema=schema,
        query_plan=query_plan,
        match_id="8886013461",
    )
    objectives = _field_group(capability, "OBJECTIVES")
    accessible_plan = apply_access_capability_to_query_plan(
        schema=schema,
        query_plan=query_plan,
        capability=capability,
    )

    assert objectives.status is AccessStatus.PERMISSION_RESTRICTED
    assert objectives.restricted_path == "MatchType.towerStatusRadiant"
    assert accessible_plan.match_query is not None
    assert "towerStatusRadiant" not in _query_tokens(accessible_plan.match_query)
    assert "durationSeconds" in _query_tokens(accessible_plan.match_query)


def test_access_capability_narrows_nested_permission_restricted_subtree() -> None:
    schema = _schema_fixture()
    query_plan = build_stratz_query_plan(schema)
    client = _AccessFakeClient(restricted_fields={"kills"})

    capability = probe_stratz_access_capability(
        client=client,  # type: ignore[arg-type]
        schema=schema,
        query_plan=query_plan,
        match_id="8886013461",
    )
    accessible_plan = apply_access_capability_to_query_plan(
        schema=schema,
        query_plan=query_plan,
        capability=capability,
    )

    assert "MatchPlayerType.kills" in capability.restricted_paths
    assert accessible_plan.match_query is not None
    assert "kills" not in _query_tokens(accessible_plan.match_query)
    assert "players" not in _query_tokens(accessible_plan.match_query)


def test_coverage_marks_access_restricted_separately_from_sample_missing() -> None:
    schema = _schema_fixture()
    base_plan = build_stratz_query_plan(schema)
    restricted_capability = StratzAccessCapability(
        observed_max_match_ids_per_request=10,
        minimal_match_fetch_status=AccessStatus.ACCESSIBLE,
        minimal_match_fetch_message=None,
        field_groups=(
            StratzFieldGroupAccess(
                group_name="PLAYER_FINAL_STATS",
                status=AccessStatus.PERMISSION_RESTRICTED,
                selected_paths=("MatchType.players",),
                restricted_path="MatchType.players",
            ),
        ),
        capability_probe_requests=2,
    )
    restricted_plan = apply_access_capability_to_query_plan(
        schema=schema,
        query_plan=base_plan,
        capability=restricted_capability,
    )

    restricted = analyze_stratz_match_payload(
        _partial_match_payload(),
        query_plan=restricted_plan,
    )
    sample_missing = analyze_stratz_match_payload(
        _partial_match_payload(),
        query_plan=base_plan,
    )
    restricted_coverage = coverage_by_key(aggregate_field_coverage([restricted]))
    sample_missing_coverage = coverage_by_key(aggregate_field_coverage([sample_missing]))

    assert restricted.observations["individual_kills"].access_restricted is True
    assert restricted.observations["individual_kills"].schema_absent is False
    assert sample_missing.observations["individual_kills"].access_restricted is False
    assert "permission restricted" in restricted_coverage["individual_kills"].semantics
    assert "permission restricted" not in sample_missing_coverage["individual_kills"].semantics


def test_probe_batches_explicit_ids_and_does_not_repeat_restricted_sample_field() -> None:
    match_ids = tuple(str(8885600000 + index) for index in range(12))
    client = _AccessFakeClient(restricted_fields={"towerStatusRadiant"})
    probe = StratzFeasibilityProbe(
        client,  # type: ignore[arg-type]
        sleep_func=lambda _: None,
    )

    result = probe.run(sample_size=12, match_ids=match_ids, delay_seconds=0)

    assert result.access_capability is not None
    assert result.access_capability.sample_fetch_requests == 2
    assert tuple(
        len(variables["ids"])
        for variables in client.sample_variables
        if isinstance(variables.get("ids"), list)
    ) == (10, 2)
    assert all(
        len(variables["ids"]) <= 10
        for variables in client.sample_variables
        if isinstance(variables.get("ids"), list)
    )
    assert all(
        "towerStatusRadiant" not in _query_tokens(query)
        for query in client.sample_queries
    )
    assert result.request_count == len(client.queries)
    assert result.verdict is None
    assert any("single-family" in warning for warning in result.warnings)


def test_determine_source_verdict_requires_multi_family_when_requested() -> None:
    analysis = analyze_stratz_match_payload(
        _complete_match_payload(),
        query_plan=build_stratz_query_plan(_schema_fixture()),
    )
    coverage = aggregate_field_coverage([analysis] * 12)

    verdict = determine_source_verdict(
        coverage,
        sample_count=12,
        real_source=True,
        competition_families=(HistoricalCompetitionFamily.ESPORTS_WORLD_CUP,) * 12,
        minimum_competition_families=2,
    )

    assert verdict is None


def test_render_probe_result_includes_access_capability_section() -> None:
    schema = _schema_fixture()
    query_plan = build_stratz_query_plan(schema)
    capability = StratzAccessCapability(
        observed_max_match_ids_per_request=10,
        minimal_match_fetch_status=AccessStatus.ACCESSIBLE,
        minimal_match_fetch_message=None,
        field_groups=(
            StratzFieldGroupAccess(
                group_name="OBJECTIVES",
                status=AccessStatus.PERMISSION_RESTRICTED,
                selected_paths=("MatchType.towerStatusRadiant",),
                restricted_path="MatchType.towerStatusRadiant",
            ),
        ),
        capability_probe_requests=3,
        sample_fetch_requests=2,
    )
    result = StratzProbeResult(
        real_source=True,
        probe_started_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        request_count=5,
        sampled_match_ids=("8886013461",),
        sample_selection_method="explicit --match-id values",
        query_field_names=("matches",),
        query_plan=apply_access_capability_to_query_plan(
            schema=schema,
            query_plan=query_plan,
            capability=capability,
        ),
        access_capability=capability,
        analyses=(),
        coverage=(),
        verdict=None,
    )

    output = render_probe_result(result)

    assert "Access capability" in output
    assert "Observed max match IDs per request: 10" in output
    assert "Minimal match ID fetch: ACCESSIBLE" in output
    assert (
        "Field group OBJECTIVES: PERMISSION_RESTRICTED | "
        "Restricted field/subtree: MatchType.towerStatusRadiant"
    ) in output
    assert "Capability-probe requests: 3" in output
    assert "Sample-fetch requests: 2" in output


class _RawFakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "_RawFakeResponse":
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        return False


class _StdlibShapedFakeOpen:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[Request] = []
        self.data_values: list[object | None] = []
        self.timeouts: list[float | None] = []

    def __call__(
        self,
        request: Request,
        data: object | None = None,
        timeout: float | None = None,
    ) -> _RawFakeResponse:
        self.requests.append(request)
        self.data_values.append(data)
        self.timeouts.append(timeout)
        if data is not None:
            raise AssertionError("STRATZ requests must not pass positional data.")
        return _RawFakeResponse(self.body)


class _SchemaFakeClient:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.variables: list[dict[str, object]] = []

    def execute(
        self,
        query: str,
        variables: dict[str, object] | None = None,
    ) -> object:
        self.queries.append(query)
        self.variables.append(dict(variables or {}))
        if "__schema" in query:
            return _FakeGraphQLResponse({"__schema": {"queryType": {"name": "DotaQuery"}}})
        type_name = str((variables or {}).get("name"))
        if type_name == "DotaQuery":
            return _FakeGraphQLResponse({"__type": _dota_query_type_payload()})
        if type_name == "MatchType":
            return _FakeGraphQLResponse({"__type": _match_type_payload()})
        if type_name == "MatchPlayerType":
            return _FakeGraphQLResponse({"__type": _match_player_type_payload()})
        if type_name == "MatchPickBanType":
            return _FakeGraphQLResponse({"__type": _match_pick_ban_type_payload()})
        if type_name == "MatchItemTimingType":
            return _FakeGraphQLResponse({"__type": _match_item_timing_type_payload()})
        return _FakeGraphQLResponse({"__type": None})


class _AccessFakeClient(_SchemaFakeClient):
    def __init__(self, *, restricted_fields: set[str] | None = None) -> None:
        super().__init__()
        self.restricted_fields = restricted_fields or set()
        self.operational_queries: list[str] = []
        self.operational_variables: list[dict[str, object]] = []
        self.sample_queries: list[str] = []
        self.sample_variables: list[dict[str, object]] = []

    def execute(
        self,
        query: str,
        variables: dict[str, object] | None = None,
    ) -> object:
        if "__schema" in query or "__type" in query:
            return super().execute(query, variables)

        normalized_variables = dict(variables or {})
        self.queries.append(query)
        self.variables.append(normalized_variables)
        self.operational_queries.append(query)
        self.operational_variables.append(normalized_variables)
        ids = normalized_variables.get("ids")
        if not isinstance(ids, list):
            ids = []
        if len(ids) > 10:
            raise StratzGraphQLError(
                "STRATZ GraphQL errors: Requesting Too Many MatchIds. "
                "Max Request Size 10."
            )
        tokens = _query_tokens(query)
        if any(field in tokens for field in self.restricted_fields):
            raise StratzGraphQLError("STRATZ GraphQL errors: User is not an admin.")
        if "StratzAccessibleMatchSample" in query:
            self.sample_queries.append(query)
            self.sample_variables.append(normalized_variables)
        return _FakeGraphQLResponse(
            {
                "matches": [
                    _complete_match_payload_for_id(str(match_id)) for match_id in ids
                ]
            }
        )


class _FakeGraphQLResponse:
    def __init__(self, data: dict[str, object]) -> None:
        self.data = data


def _field_group(
    capability: StratzAccessCapability,
    group_name: str,
) -> StratzFieldGroupAccess:
    group = next(
        item for item in capability.field_groups if item.group_name == group_name
    )
    return group


def _query_tokens(query: str) -> set[str]:
    translation = str.maketrans({char: " " for char in "{}():,$!\n\t"})
    return set(query.translate(translation).split())


def _assert_single_stratz_request(
    urlopen: _StdlibShapedFakeOpen,
    *,
    expected_timeout: float,
) -> Request:
    assert len(urlopen.requests) == 1
    assert urlopen.data_values == [None]
    assert urlopen.timeouts == [expected_timeout]
    return urlopen.requests[0]


def _candidate(match_id: str, league_name: str) -> dict[str, object]:
    return {
        "id": match_id,
        "startDateTime": "2025-09-01T12:00:00Z",
        "league": {"id": f"league-{match_id}", "displayName": league_name},
        "radiantTeam": {"id": f"r-{match_id}", "name": "Radiant Team"},
        "direTeam": {"id": f"d-{match_id}", "name": "Dire Team"},
    }


def _complete_match_payload() -> dict[str, object]:
    players: list[dict[str, object]] = []
    for index in range(10):
        radiant = index < 5
        players.append(
            {
                "steamAccountId": 1000 + index,
                "playerSlot": index if radiant else 128 + index,
                "position": index + 1,
                "isRadiant": radiant,
                "heroId": 10 + index,
                "kills": index,
                "deaths": 1,
                "assists": 5,
                "numLastHits": 100 + index,
                "numDenies": index,
                "goldPerMinute": 500,
                "experiencePerMinute": 600,
                "netWorth": 12000,
                "level": 20,
                "heroDamage": 15000,
                "towerDamage": 1000,
                "heroHealing": 200,
                "item0Id": 1,
                "item1Id": 2,
                "items": [{"itemId": 50 + index, "time": 600}],
                "name": f"Player {index}",
                "lane": "safe" if index in (0, 5) else "unknown",
                "role": "core" if index in (0, 1, 5, 6) else "support",
            }
        )
    pick_bans: list[dict[str, object]] = []
    for order, hero_id in enumerate(range(10, 20), start=1):
        pick_bans.append(
            {
                "order": order,
                "isPick": True,
                "heroId": hero_id,
                "isRadiant": order <= 5,
            }
        )
    for order, hero_id in enumerate(range(30, 34), start=11):
        pick_bans.append(
            {
                "order": order,
                "isPick": False,
                "heroId": hero_id,
                "team": 0 if order % 2 else 1,
            }
        )
    return {
        "id": 9001,
        "startDateTime": "2025-09-01T12:00:00Z",
        "durationSeconds": 2400,
        "didRadiantWin": True,
        "gameMode": 22,
        "lobbyType": "PRO",
        "gameVersionId": 176,
        "league": {"id": 77, "displayName": "DreamLeague Season 28"},
        "tournament": {"id": 88, "name": "DreamLeague Season 28 Main Event"},
        "series": {"id": 99, "type": "BO3", "gameNumber": 1},
        "radiantTeam": {"id": 101, "name": "Radiant Pros"},
        "direTeam": {"id": 202, "name": "Dire Pros"},
        "players": players,
        "pickBans": pick_bans,
        "towerStatusRadiant": 1,
        "towerStatusDire": 2,
        "barracksStatusRadiant": 3,
        "barracksStatusDire": 4,
        "radiantNetworthLeads": [0, 100, 300],
        "radiantExperienceLeads": [0, 50, 200],
        "playbackData": {
            "killEvents": [{"time": 120, "attacker": 1000, "target": 1005}],
            "roshanEvents": [{"time": 1800, "team": "radiant"}],
            "buildingEvents": [{"time": 1700, "key": "tower", "team": "dire"}],
        },
    }


def _complete_match_payload_for_id(match_id: str) -> dict[str, object]:
    payload = _complete_match_payload()
    payload["id"] = match_id
    payload["league"] = {
        "id": 77,
        "displayName": "Esports World Cup 2026",
    }
    payload["tournament"] = {
        "id": 88,
        "name": "Esports World Cup 2026 Main Event",
    }
    return payload


def _partial_match_payload() -> dict[str, object]:
    return {
        "id": 9002,
        "startDateTime": datetime(2025, 9, 2, 12, tzinfo=timezone.utc),
        "durationSeconds": 1800,
        "didRadiantWin": False,
        "league": {"id": 77, "displayName": "DreamLeague Season 28"},
        "radiantTeam": {"id": 101, "name": "Radiant Pros"},
        "direTeam": {"id": 202, "name": "Dire Pros"},
        "players": [
            {
                "steamAccountId": 2000 + index,
                "isRadiant": index < 5,
                "heroId": 40 + index if index < 8 else None,
            }
            for index in range(10)
        ],
    }


def _schema_fixture() -> StratzSchemaSnapshot:
    return StratzSchemaSnapshot(
        query_type_name="DotaQuery",
        types={
            "DotaQuery": _type_definition_from_payload(_dota_query_type_payload()),
            "MatchType": _type_definition_from_payload(_match_type_payload()),
            "MatchPlayerType": _type_definition_from_payload(_match_player_type_payload()),
            "MatchPickBanType": _type_definition_from_payload(_match_pick_ban_type_payload()),
            "MatchItemTimingType": _type_definition_from_payload(_match_item_timing_type_payload()),
        },
    )


def _schema_fixture_without_draft() -> StratzSchemaSnapshot:
    schema = _schema_fixture()
    match_type = schema.types["MatchType"]
    filtered_fields = {
        key: value
        for key, value in match_type.fields.items()
        if key != "pickBans"
    }
    types = dict(schema.types)
    types["MatchType"] = type(match_type)(
        name=match_type.name,
        kind=match_type.kind,
        fields=filtered_fields,
    )
    return StratzSchemaSnapshot(query_type_name=schema.query_type_name, types=types)


def _type_definition_from_payload(payload: dict[str, object]) -> GraphQLTypeDefinition:
    parsed = parse_graphql_type_definition(
        {
            "__type": payload,
        }
    )
    assert parsed is not None
    return parsed


def _dota_query_type_payload() -> dict[str, object]:
    return {
        "name": "DotaQuery",
        "kind": "OBJECT",
        "fields": [
            {
                "name": "matches",
                "args": [
                    {
                        "name": "ids",
                        "type": _non_null_ref(_list_ref("Long")),
                    }
                ],
                "type": _list_ref("MatchType"),
            }
        ],
    }


def _match_type_payload() -> dict[str, object]:
    return {
        "name": "MatchType",
        "kind": "OBJECT",
        "fields": [
            _field("id", _scalar_ref("Long")),
            _field("startDateTime", _scalar_ref("DateTime")),
            _field("durationSeconds", _scalar_ref("Int")),
            _field("didRadiantWin", _scalar_ref("Boolean")),
            _field("gameMode", _scalar_ref("Int")),
            _field("lobbyType", _scalar_ref("Int")),
            _field("gameVersionId", _scalar_ref("Int")),
            _field("leagueId", _scalar_ref("Long")),
            _field("tournamentId", _scalar_ref("Long")),
            _field("tournamentRound", _scalar_ref("String")),
            _field("seriesId", _scalar_ref("Long")),
            _field("radiantTeamId", _scalar_ref("Long")),
            _field("direTeamId", _scalar_ref("Long")),
            _field("players", _list_ref("MatchPlayerType")),
            _field("pickBans", _list_ref("MatchPickBanType")),
            _field("radiantKills", _scalar_ref("Int")),
            _field("direKills", _scalar_ref("Int")),
            _field("towerStatusRadiant", _scalar_ref("Int")),
            _field("towerStatusDire", _scalar_ref("Int")),
            _field("barracksStatusRadiant", _scalar_ref("Int")),
            _field("barracksStatusDire", _scalar_ref("Int")),
            _field("radiantNetworthLeads", _list_ref("Int")),
            _field("radiantExperienceLeads", _list_ref("Int")),
        ],
    }


def _match_player_type_payload() -> dict[str, object]:
    return {
        "name": "MatchPlayerType",
        "kind": "OBJECT",
        "fields": [
            _field("steamAccountId", _scalar_ref("Long")),
            _field("playerSlot", _scalar_ref("Int")),
            _field("position", _scalar_ref("Int")),
            _field("isRadiant", _scalar_ref("Boolean")),
            _field("heroId", _scalar_ref("Int")),
            _field("kills", _scalar_ref("Int")),
            _field("deaths", _scalar_ref("Int")),
            _field("assists", _scalar_ref("Int")),
            _field("numLastHits", _scalar_ref("Int")),
            _field("numDenies", _scalar_ref("Int")),
            _field("goldPerMinute", _scalar_ref("Int")),
            _field("experiencePerMinute", _scalar_ref("Int")),
            _field("netWorth", _scalar_ref("Int")),
            _field("level", _scalar_ref("Int")),
            _field("heroDamage", _scalar_ref("Int")),
            _field("towerDamage", _scalar_ref("Int")),
            _field("heroHealing", _scalar_ref("Int")),
            _field("item0Id", _scalar_ref("Int")),
            _field("item1Id", _scalar_ref("Int")),
            _field("items", _list_ref("MatchItemTimingType")),
            _field("name", _scalar_ref("String")),
            _field("lane", _scalar_ref("String")),
            _field("role", _scalar_ref("String")),
        ],
    }


def _match_pick_ban_type_payload() -> dict[str, object]:
    return {
        "name": "MatchPickBanType",
        "kind": "OBJECT",
        "fields": [
            _field("order", _scalar_ref("Int")),
            _field("isPick", _scalar_ref("Boolean")),
            _field("heroId", _scalar_ref("Int")),
            _field("team", _scalar_ref("Int")),
            _field("isRadiant", _scalar_ref("Boolean")),
        ],
    }


def _match_item_timing_type_payload() -> dict[str, object]:
    return {
        "name": "MatchItemTimingType",
        "kind": "OBJECT",
        "fields": [
            _field("itemId", _scalar_ref("Int")),
            _field("time", _scalar_ref("Int")),
        ],
    }


def _field(name: str, type_ref: dict[str, object]) -> dict[str, object]:
    return {"name": name, "type": type_ref, "args": []}


def _scalar_ref(name: str) -> dict[str, object]:
    return {"kind": "SCALAR", "name": name}


def _list_ref(name: str) -> dict[str, object]:
    return {"kind": "LIST", "name": None, "ofType": _scalar_or_object_ref(name)}


def _non_null_ref(of_type: dict[str, object]) -> dict[str, object]:
    return {"kind": "NON_NULL", "name": None, "ofType": of_type}


def _scalar_or_object_ref(name: str) -> dict[str, object]:
    kind = "SCALAR" if name in {"Boolean", "DateTime", "Int", "Long", "String"} else "OBJECT"
    return {"kind": kind, "name": name}
