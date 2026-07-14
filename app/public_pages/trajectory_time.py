from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from html.parser import HTMLParser
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from app.draft_history import HistoricalDotaAdvantagePoint
from app.public_pages.feasibility import (
    PublicHttpResponse,
    PublicPageHttpClient,
    PublicPageSource,
    build_public_match_url,
    check_public_page_policy,
    extract_public_match_semantics_from_page,
)
from app.public_pages.semantics import (
    PublicMatchSemantics,
    PublicSemanticTimedItem,
    find_public_mapping_list,
    public_player_timed_items,
)

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


STRATZ_TRAJECTORY_TIME_DIAGNOSTIC_MATCH_IDS: tuple[str, ...] = (
    "8011794134",
    "8346430978",
    "8886013461",
)


class TrajectoryTimeEvidenceProvenance(str, Enum):
    SOURCE_POINT_TIME = "source_point_time"
    PUBLIC_CLIENT_INDEX_MAPPING = "public_client_index_mapping"


@dataclass(frozen=True)
class PublicClientTrajectoryTimeMapping:
    provenance: TrajectoryTimeEvidenceProvenance
    cadence_seconds: int
    origin_offset_seconds: int
    covered_metrics: tuple[str, ...]
    evidence_summary: str

    def normalized_seconds_for_index(self, source_index: int) -> int:
        return self.origin_offset_seconds + source_index * self.cadence_seconds


@dataclass(frozen=True)
class TrajectorySourceShapeEvidence:
    metric: str
    source_key: str
    source_path: str
    parent_path: str
    raw_shape: str
    point_count: int
    first_source_indices: tuple[int, ...]
    last_source_indices: tuple[int, ...]
    source_time_values: int
    normalized_time_seconds: int
    adjacent_parent_keys: tuple[str, ...]
    candidate_coordinate_fields: tuple[str, ...]


@dataclass(frozen=True)
class PublicRouteTrajectoryEvidence:
    route_kind: str
    url: str
    robots_disallowed: bool | None
    http_status: int | None
    byte_size: int
    decoded_state_count: int
    duration_seconds: int | None
    patch: str | None
    gold_point_count: int
    xp_point_count: int
    source_shapes: tuple[TrajectorySourceShapeEvidence, ...]
    candidate_global_coordinate_fields: tuple[str, ...]
    timed_item_player_count: int
    timed_item_row_count: int
    ordered_draft_action_count: int
    complete_ordered_draft_sequence: bool
    parse_findings: tuple[str, ...]


@dataclass(frozen=True)
class PublicClientAssetEvidence:
    url: str
    http_status: int | None
    byte_size: int
    matched_terms: tuple[str, ...]
    snippets: tuple[str, ...]


@dataclass(frozen=True)
class StratzTrajectoryTimeMatchDiagnostic:
    match_id: str
    overview: PublicRouteTrajectoryEvidence
    graph: PublicRouteTrajectoryEvidence
    client_asset_urls: tuple[str, ...]
    client_assets: tuple[PublicClientAssetEvidence, ...]


@dataclass(frozen=True)
class PointCountDurationRow:
    match_id: str
    duration_seconds: int
    duration_minutes: float
    gold_point_count: int
    xp_point_count: int
    gold_minus_floor_minutes: int
    gold_minus_ceil_minutes: int
    seconds_per_gold_point: float
    seconds_per_gold_interval: float


@dataclass(frozen=True)
class PointCountDurationAnalysis:
    rows: tuple[PointCountDurationRow, ...]
    gold_minus_floor_distribution: tuple[tuple[int, int], ...]
    gold_minus_ceil_distribution: tuple[tuple[int, int], ...]
    exact_floor_plus_constant: int | None
    exact_ceil_plus_constant: int | None
    seconds_per_point_min: float | None
    seconds_per_point_median: float | None
    seconds_per_point_max: float | None
    seconds_per_interval_min: float | None
    seconds_per_interval_median: float | None
    seconds_per_interval_max: float | None


@dataclass(frozen=True)
class StratzTrajectoryTimeDiagnosticResult:
    match_diagnostics: tuple[StratzTrajectoryTimeMatchDiagnostic, ...]
    point_count_duration_analysis: PointCountDurationAnalysis | None
    public_client_mapping: PublicClientTrajectoryTimeMapping | None
    client_asset_inspection_requested: bool


def build_stratz_trajectory_time_diagnostic(
    *,
    repository: "SQLiteRepository | None",
    client: PublicPageHttpClient,
    match_ids: Sequence[str] = STRATZ_TRAJECTORY_TIME_DIAGNOSTIC_MATCH_IDS,
    delay_seconds: float = 1.0,
    inspect_client_assets: bool = False,
    max_client_assets: int = 4,
    sleep_func: Callable[[float], None] = time.sleep,
) -> StratzTrajectoryTimeDiagnosticResult:
    normalized_ids = tuple(str(match_id).strip() for match_id in match_ids if str(match_id).strip())
    if not normalized_ids:
        raise ValueError("At least one --match-id is required.")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must not be negative")
    if max_client_assets < 0:
        raise ValueError("max_client_assets must not be negative")

    diagnostics: list[StratzTrajectoryTimeMatchDiagnostic] = []
    for index, match_id in enumerate(normalized_ids):
        overview_url = build_public_match_url(PublicPageSource.STRATZ, match_id)
        graph_url = _stratz_networth_graph_url(match_id)
        overview_response = _fetch_if_allowed(
            client=client,
            url=overview_url,
            route_kind="overview",
        )
        graph_response = _fetch_if_allowed(
            client=client,
            url=graph_url,
            route_kind="networth_graph",
        )
        overview = _route_evidence(
            match_id=match_id,
            route_kind="overview",
            url=overview_url,
            response=overview_response,
        )
        graph = _route_evidence(
            match_id=match_id,
            route_kind="networth_graph",
            url=graph_url,
            response=graph_response,
        )
        # The graph route can be robots-disallowed. Asset inspection is rooted
        # only in the already-allowed Overview HTML and never depends on it.
        client_asset_urls = (
            _script_urls(overview_response.response.text, overview_url)
            if inspect_client_assets and overview_response.response is not None
            else ()
        )
        client_assets = _client_asset_evidence(
            client=client,
            script_urls=client_asset_urls,
            max_assets=max_client_assets,
        )
        diagnostics.append(
            StratzTrajectoryTimeMatchDiagnostic(
                match_id=match_id,
                overview=overview,
                graph=graph,
                client_asset_urls=client_asset_urls,
                client_assets=client_assets,
            )
        )
        if delay_seconds and index < len(normalized_ids) - 1:
            sleep_func(delay_seconds)

    analysis = (
        build_point_count_duration_analysis(repository)
        if repository is not None
        else None
    )
    return StratzTrajectoryTimeDiagnosticResult(
        match_diagnostics=tuple(diagnostics),
        point_count_duration_analysis=analysis,
        public_client_mapping=None,
        client_asset_inspection_requested=inspect_client_assets,
    )


def build_point_count_duration_analysis(
    repository: "SQLiteRepository",
) -> PointCountDurationAnalysis:
    games = tuple(
        game
        for game in repository.list_historical_dota_games()
        if game.source == "stratz_public" and game.ended_at is not None
    )
    rows: list[PointCountDurationRow] = []
    for game in games:
        points = tuple(repository.list_historical_dota_advantage_points(game.id))
        gold_count = sum(1 for point in points if point.metric == "gold")
        xp_count = sum(1 for point in points if point.metric == "xp")
        if gold_count <= 0:
            continue
        if game.ended_at is None:
            continue
        duration_seconds = int((game.ended_at - game.started_at).total_seconds())
        duration_minutes = duration_seconds / 60.0
        floor_minutes = duration_seconds // 60
        ceil_minutes = (duration_seconds + 59) // 60
        rows.append(
            PointCountDurationRow(
                match_id=game.source_game_id,
                duration_seconds=duration_seconds,
                duration_minutes=duration_minutes,
                gold_point_count=gold_count,
                xp_point_count=xp_count,
                gold_minus_floor_minutes=gold_count - floor_minutes,
                gold_minus_ceil_minutes=gold_count - ceil_minutes,
                seconds_per_gold_point=duration_seconds / gold_count,
                seconds_per_gold_interval=duration_seconds / max(gold_count - 1, 1),
            )
        )
    rows.sort(key=lambda row: int(row.match_id))
    floor_distribution = tuple(sorted(Counter(row.gold_minus_floor_minutes for row in rows).items()))
    ceil_distribution = tuple(sorted(Counter(row.gold_minus_ceil_minutes for row in rows).items()))
    seconds_per_point = [row.seconds_per_gold_point for row in rows]
    seconds_per_interval = [row.seconds_per_gold_interval for row in rows]
    return PointCountDurationAnalysis(
        rows=tuple(rows),
        gold_minus_floor_distribution=floor_distribution,
        gold_minus_ceil_distribution=ceil_distribution,
        exact_floor_plus_constant=_exact_single_constant(floor_distribution),
        exact_ceil_plus_constant=_exact_single_constant(ceil_distribution),
        seconds_per_point_min=_min(seconds_per_point),
        seconds_per_point_median=_median(seconds_per_point),
        seconds_per_point_max=_max(seconds_per_point),
        seconds_per_interval_min=_min(seconds_per_interval),
        seconds_per_interval_median=_median(seconds_per_interval),
        seconds_per_interval_max=_max(seconds_per_interval),
    )


def classify_trajectory_time_semantics_with_evidence(
    points: Sequence[HistoricalDotaAdvantagePoint],
    *,
    public_client_mapping: PublicClientTrajectoryTimeMapping | None = None,
) -> str:
    if not points:
        return "TRAJECTORY_TIME_SEMANTICS_UNRESOLVED"
    unresolved_points = [
        point
        for point in points
        if point.time_semantics_status != "normalized_seconds"
        or point.normalized_time_seconds is None
    ]
    normalized_points = [
        point
        for point in points
        if point.time_semantics_status == "normalized_seconds"
        and point.normalized_time_seconds is not None
    ]
    if normalized_points and not unresolved_points:
        return "TRAJECTORY_TIME_SEMANTICS_CONFIRMED"
    if public_client_mapping is not None:
        covered_metrics = set(public_client_mapping.covered_metrics)
        point_metrics = {point.metric for point in points}
        if point_metrics <= covered_metrics:
            return "TRAJECTORY_TIME_SEMANTICS_CONFIRMED"
        if covered_metrics & point_metrics:
            return "TRAJECTORY_TIME_SEMANTICS_PARTIALLY_CONFIRMED"
    if normalized_points:
        return "TRAJECTORY_TIME_SEMANTICS_PARTIALLY_CONFIRMED"
    return "TRAJECTORY_TIME_SEMANTICS_UNRESOLVED"


def normalize_points_with_public_client_mapping(
    points: Sequence[HistoricalDotaAdvantagePoint],
    mapping: PublicClientTrajectoryTimeMapping,
) -> tuple[HistoricalDotaAdvantagePoint, ...]:
    covered_metrics = set(mapping.covered_metrics)
    normalized: list[HistoricalDotaAdvantagePoint] = []
    for point in points:
        if point.metric not in covered_metrics:
            normalized.append(point)
            continue
        normalized.append(
            replace(
                point,
                normalized_time_seconds=mapping.normalized_seconds_for_index(
                    point.source_index
                ),
                time_semantics_status="normalized_seconds",
            )
        )
    return tuple(normalized)


def render_stratz_trajectory_time_diagnostic(
    result: StratzTrajectoryTimeDiagnosticResult,
) -> str:
    lines: list[str] = ["STRATZ public trajectory-time diagnostic"]
    for diagnostic in result.match_diagnostics:
        lines.append("")
        lines.append(f"Match {diagnostic.match_id}")
        lines.extend(_render_route_evidence(diagnostic.overview))
        lines.extend(_render_route_evidence(diagnostic.graph))
        if result.client_asset_inspection_requested:
            lines.append(
                "Public client asset evidence: "
                f"discovered={len(diagnostic.client_asset_urls)} "
                f"inspected={len(diagnostic.client_assets)}"
            )
            for asset in diagnostic.client_assets:
                lines.append(
                    f"- {asset.url}: http={_format_optional_int(asset.http_status)}, "
                    f"bytes={asset.byte_size}, terms={','.join(asset.matched_terms) or '-'}"
                )
                for snippet in asset.snippets[:3]:
                    lines.append(f"  snippet: {snippet}")
            trajectory_identifiers = {
                "radiantNetworthLeads",
                "radiantExperienceLeads",
            }
            identifier_asset_count = sum(
                bool(trajectory_identifiers & set(asset.matched_terms))
                for asset in diagnostic.client_assets
            )
            lines.append(
                "  exact_trajectory_identifier_assets="
                f"{identifier_asset_count}"
            )
    if result.point_count_duration_analysis is not None:
        lines.append("")
        lines.append("Point-count versus duration analysis")
        lines.extend(_render_duration_analysis(result.point_count_duration_analysis))
    lines.append("")
    lines.append("Temporal semantics decision")
    lines.append("TRAJECTORY_TIME_SEMANTICS_UNRESOLVED")
    lines.append(
        "Blocker: no source or directly referenced public-client "
        "index-to-time mapping was proven"
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class _FetchedRoute:
    robots_disallowed: bool | None
    response: PublicHttpResponse | None
    parse_findings: tuple[str, ...] = ()


def _fetch_if_allowed(
    *,
    client: PublicPageHttpClient,
    url: str,
    route_kind: str,
) -> _FetchedRoute:
    policy = check_public_page_policy(
        client=client,
        source=PublicPageSource.STRATZ,
        sample_path=urlparse(url).path or "/",
    )
    if policy.path_disallowed is True:
        return _FetchedRoute(
            robots_disallowed=True,
            response=None,
            parse_findings=(f"{route_kind}: robots policy disallowed route",),
        )
    response = client.fetch(url)
    return _FetchedRoute(robots_disallowed=policy.path_disallowed, response=response)


def _route_evidence(
    *,
    match_id: str,
    route_kind: str,
    url: str,
    response: _FetchedRoute,
) -> PublicRouteTrajectoryEvidence:
    if response.response is None:
        return PublicRouteTrajectoryEvidence(
            route_kind=route_kind,
            url=url,
            robots_disallowed=response.robots_disallowed,
            http_status=None,
            byte_size=0,
            decoded_state_count=0,
            duration_seconds=None,
            patch=None,
            gold_point_count=0,
            xp_point_count=0,
            source_shapes=(),
            candidate_global_coordinate_fields=(),
            timed_item_player_count=0,
            timed_item_row_count=0,
            ordered_draft_action_count=0,
            complete_ordered_draft_sequence=False,
            parse_findings=response.parse_findings,
        )
    page = response.response
    extraction = extract_public_match_semantics_from_page(
        html=page.text,
        requested_match_id=match_id,
    )
    semantics = extraction.semantics
    all_states = extraction.decoded_states
    timed_items = _timed_items_from_states(all_states)
    source_shapes = _trajectory_source_shapes(all_states, semantics)
    return PublicRouteTrajectoryEvidence(
        route_kind=route_kind,
        url=url,
        robots_disallowed=response.robots_disallowed,
        http_status=page.status_code,
        byte_size=len(page.body),
        decoded_state_count=len(all_states),
        duration_seconds=semantics.duration_seconds if semantics else None,
        patch=semantics.patch if semantics else None,
        gold_point_count=_metric_count(semantics, "gold"),
        xp_point_count=_metric_count(semantics, "xp"),
        source_shapes=source_shapes,
        candidate_global_coordinate_fields=_global_coordinate_candidates(all_states),
        timed_item_player_count=len({row.account_id for row in timed_items}),
        timed_item_row_count=len(timed_items),
        ordered_draft_action_count=len(semantics.draft_actions) if semantics else 0,
        complete_ordered_draft_sequence=_has_complete_ordered_draft_sequence(
            semantics
        ),
        parse_findings=extraction.parse_findings,
    )


def _trajectory_source_shapes(
    states: Sequence[object],
    semantics: PublicMatchSemantics | None,
) -> tuple[TrajectorySourceShapeEvidence, ...]:
    semantic_points = semantics.advantage_points if semantics is not None else ()
    semantic_counts = {
        metric: sum(1 for point in semantic_points if point.metric == metric)
        for metric in ("gold", "xp")
    }
    semantic_source_times = {
        metric: sum(
            1
            for point in semantic_points
            if point.metric == metric and point.source_time_value is not None
        )
        for metric in ("gold", "xp")
    }
    semantic_normalized = {
        metric: sum(
            1
            for point in semantic_points
            if point.metric == metric and point.normalized_time_seconds is not None
        )
        for metric in ("gold", "xp")
    }
    entries: list[TrajectorySourceShapeEvidence] = []
    for state in states:
        for path, key, value, parent in _walk_mappings(state):
            metric = _trajectory_metric_for_key(key)
            if metric is None or not isinstance(value, list):
                continue
            point_count = semantic_counts.get(metric) or len(value)
            entries.append(
                TrajectorySourceShapeEvidence(
                    metric=metric,
                    source_key=key,
                    source_path=_format_path(path),
                    parent_path=_format_path(path[:-1]),
                    raw_shape=_list_shape(value),
                    point_count=point_count,
                    first_source_indices=tuple(range(min(point_count, 5))),
                    last_source_indices=tuple(
                        range(max(0, point_count - 5), point_count)
                    ),
                    source_time_values=semantic_source_times.get(metric, 0),
                    normalized_time_seconds=semantic_normalized.get(metric, 0),
                    adjacent_parent_keys=_adjacent_keys(parent),
                    candidate_coordinate_fields=_coordinate_candidates(parent, point_count),
                )
            )
    return tuple(_dedupe_source_shapes(entries))


def _client_asset_evidence(
    *,
    client: PublicPageHttpClient,
    script_urls: Sequence[str],
    max_assets: int,
) -> tuple[PublicClientAssetEvidence, ...]:
    if max_assets == 0:
        return ()
    assets: list[PublicClientAssetEvidence] = []
    for url in script_urls[:max_assets]:
        response = client.fetch(url)
        terms = _matched_client_terms(response.text)
        snippets = _client_snippets(response.text, terms)
        assets.append(
            PublicClientAssetEvidence(
                url=url,
                http_status=response.status_code,
                byte_size=len(response.body),
                matched_terms=terms,
                snippets=snippets,
            )
        )
    return tuple(assets)


def _stratz_networth_graph_url(match_id: str) -> str:
    return f"https://stratz.com/matches/{match_id}/graphs/networth"


def _timed_items_from_states(
    states: Sequence[object],
) -> tuple[PublicSemanticTimedItem, ...]:
    rows: list[PublicSemanticTimedItem] = []
    for state in states:
        players = find_public_mapping_list(state, ("playerMatches", "players", "lineups"))
        rows.extend(public_player_timed_items(players))
    seen: set[tuple[str, int, str]] = set()
    deduped: list[PublicSemanticTimedItem] = []
    for row in rows:
        key = (row.account_id, row.item_id, row.source_time_value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return tuple(deduped)


def _has_complete_ordered_draft_sequence(
    semantics: PublicMatchSemantics | None,
) -> bool:
    if semantics is None or not semantics.draft_actions:
        return False
    ordered = [action for action in semantics.draft_actions if action.order is not None]
    return (
        len(ordered) == len(semantics.draft_actions)
        and len({action.order for action in ordered}) == len(ordered)
        and all(action.kind in {"pick", "ban"} for action in semantics.draft_actions)
        and all(action.side in {"radiant", "dire"} for action in semantics.draft_actions)
        and all(action.hero_id is not None for action in semantics.draft_actions)
        and any(action.kind == "pick" for action in semantics.draft_actions)
        and any(action.kind == "ban" for action in semantics.draft_actions)
    )


def _metric_count(semantics: PublicMatchSemantics | None, metric: str) -> int:
    if semantics is None:
        return 0
    return sum(1 for point in semantics.advantage_points if point.metric == metric)


def _walk_mappings(
    value: object,
    path: tuple[str, ...] = (),
    parent: Mapping[str, object] | None = None,
) -> tuple[tuple[tuple[str, ...], str, object, Mapping[str, object]], ...]:
    rows: list[tuple[tuple[str, ...], str, object, Mapping[str, object]]] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            next_path = path + (str(key),)
            rows.append((next_path, str(key), nested, value))
            rows.extend(_walk_mappings(nested, next_path, value))
    elif isinstance(value, list):
        for index, nested in enumerate(value[:50]):
            rows.extend(_walk_mappings(nested, path + (f"[{index}]",), parent))
    return tuple(rows)


def _trajectory_metric_for_key(key: str) -> str | None:
    lowered = key.casefold()
    if lowered in {"radiantnetworthleads", "goldadvantage"}:
        return "gold"
    if lowered in {"radiantexperienceleads", "xpadvantage"}:
        return "xp"
    return None


def _list_shape(value: Sequence[object]) -> str:
    if not value:
        return "empty_array"
    mapping_count = sum(1 for item in value if isinstance(item, Mapping))
    number_count = sum(1 for item in value if isinstance(item, (int, float)) and not isinstance(item, bool))
    if number_count == len(value):
        return "number_only_array"
    if mapping_count == len(value):
        return "point_object_array"
    return "mixed_array"


def _coordinate_candidates(
    parent: Mapping[str, object],
    expected_count: int,
) -> tuple[str, ...]:
    rows: list[str] = []
    for key, value in parent.items():
        if _trajectory_metric_for_key(str(key)) is not None:
            continue
        lowered = str(key).casefold()
        if not _coordinateish_key(lowered):
            continue
        if isinstance(value, list):
            rows.append(f"{key}: list[{len(value)}], matches_points={len(value) == expected_count}")
        else:
            rows.append(f"{key}: {_scalar_preview(value)}")
    return tuple(rows[:8])


def _global_coordinate_candidates(states: Sequence[object]) -> tuple[str, ...]:
    rows: list[str] = []
    for state in states:
        for path, key, value, _parent in _walk_mappings(state):
            lowered = key.casefold()
            if not _coordinateish_key(lowered):
                continue
            if isinstance(value, list):
                rows.append(f"{_format_path(path)}: list[{len(value)}]")
            else:
                rows.append(f"{_format_path(path)}: {_scalar_preview(value)}")
            if len(rows) >= 12:
                return tuple(rows)
    return tuple(rows)


def _coordinateish_key(lowered: str) -> bool:
    return any(
        fragment in lowered
        for fragment in (
            "time",
            "second",
            "minute",
            "timestamp",
            "duration",
            "interval",
            "cadence",
            "resolution",
            "origin",
            "axis",
            "tick",
            "label",
            "categor",
            "graph",
            "chart",
            "series",
        )
    )


def _adjacent_keys(parent: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(str(key) for key in list(parent.keys())[:24])


def _dedupe_source_shapes(
    rows: Sequence[TrajectorySourceShapeEvidence],
) -> tuple[TrajectorySourceShapeEvidence, ...]:
    seen: set[tuple[str, str, str]] = set()
    result: list[TrajectorySourceShapeEvidence] = []
    for row in rows:
        key = (row.metric, row.source_key, row.source_path)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return tuple(result)


def _script_urls(html: str, page_url: str) -> tuple[str, ...]:
    parser = _OverviewStaticScriptParser()
    parser.feed(html)
    page = urlparse(page_url)
    urls: list[str] = []
    for match in parser.urls:
        url = urljoin(page_url, match)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc != page.netloc:
            continue
        if not parsed.path.startswith("/_next/static/"):
            continue
        if not parsed.path.endswith(".js"):
            continue
        urls.append(url)
    return tuple(dict.fromkeys(urls))


class _OverviewStaticScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {key.casefold(): value for key, value in attrs}
        if tag.casefold() == "script":
            url = attributes.get("src")
        elif tag.casefold() == "link" and _is_script_preload(attributes):
            url = attributes.get("href")
        else:
            return
        if url:
            self.urls.append(url)


def _is_script_preload(attributes: Mapping[str, str | None]) -> bool:
    rel = (attributes.get("rel") or "").casefold().split()
    resource_kind = (attributes.get("as") or "").casefold()
    return "modulepreload" in rel or (
        "preload" in rel and resource_kind == "script"
    )


def _matched_client_terms(text: str) -> tuple[str, ...]:
    terms = (
        "radiantNetworthLeads",
        "radiantExperienceLeads",
        "networth",
        "experience",
        "xAxis",
        "tick",
        "minute",
        "duration",
        "60",
    )
    lowered = text.casefold()
    return tuple(term for term in terms if term.casefold() in lowered)


def _client_snippets(text: str, terms: Sequence[str]) -> tuple[str, ...]:
    snippets: list[str] = []
    for term in terms:
        index = text.casefold().find(term.casefold())
        if index < 0:
            continue
        start = max(0, index - 80)
        end = min(len(text), index + 120)
        snippets.append(_compact_text(text[start:end]))
        if len(snippets) >= 6:
            break
    return tuple(snippets)


def _render_route_evidence(route: PublicRouteTrajectoryEvidence) -> list[str]:
    lines = [
        f"{route.route_kind}: {route.url}",
        f"  robots_disallowed={_format_optional_bool(route.robots_disallowed)} "
        f"http={_format_optional_int(route.http_status)} bytes={route.byte_size} "
        f"decoded_states={route.decoded_state_count}",
        f"  duration_seconds={_format_optional_int(route.duration_seconds)} "
        f"patch={route.patch or '-'} gold_points={route.gold_point_count} "
        f"xp_points={route.xp_point_count}",
        f"  timed_item_players={route.timed_item_player_count} "
        f"timed_item_rows={route.timed_item_row_count}",
        f"  ordered_draft_actions={route.ordered_draft_action_count} "
        f"complete_ordered_draft_sequence={_format_bool(route.complete_ordered_draft_sequence)}",
    ]
    for shape in route.source_shapes:
        lines.append(
            f"  {shape.metric}: key={shape.source_key} path={shape.source_path} "
            f"shape={shape.raw_shape} points={shape.point_count} "
            f"indices={_format_indices(shape.first_source_indices)}/"
            f"{_format_indices(shape.last_source_indices)} "
            f"source_time_values={shape.source_time_values} "
            f"normalized_seconds={shape.normalized_time_seconds}"
        )
        lines.append(
            f"    parent={shape.parent_path} adjacent_keys="
            f"{', '.join(shape.adjacent_parent_keys) or '-'}"
        )
        lines.append(
            "    coordinate_candidates="
            f"{'; '.join(shape.candidate_coordinate_fields) or '-'}"
        )
    if route.candidate_global_coordinate_fields:
        lines.append(
            "  global_coordinate_candidates="
            + "; ".join(route.candidate_global_coordinate_fields)
        )
    for finding in route.parse_findings[:4]:
        lines.append(f"  finding: {finding}")
    return lines


def _render_duration_analysis(analysis: PointCountDurationAnalysis) -> list[str]:
    lines = [
        f"Rows: {len(analysis.rows)}",
        "gold_count - floor(duration_minutes): "
        + _format_distribution(analysis.gold_minus_floor_distribution),
        "gold_count - ceil(duration_minutes): "
        + _format_distribution(analysis.gold_minus_ceil_distribution),
        "exact floor + C: "
        + ("-" if analysis.exact_floor_plus_constant is None else str(analysis.exact_floor_plus_constant)),
        "exact ceil + C: "
        + ("-" if analysis.exact_ceil_plus_constant is None else str(analysis.exact_ceil_plus_constant)),
        "seconds/gold point min/median/max: "
        f"{_format_float(analysis.seconds_per_point_min)}/"
        f"{_format_float(analysis.seconds_per_point_median)}/"
        f"{_format_float(analysis.seconds_per_point_max)}",
        "seconds/gold interval min/median/max: "
        f"{_format_float(analysis.seconds_per_interval_min)}/"
        f"{_format_float(analysis.seconds_per_interval_median)}/"
        f"{_format_float(analysis.seconds_per_interval_max)}",
    ]
    for row in analysis.rows:
        lines.append(
            f"{row.match_id}: duration={row.duration_seconds}s "
            f"minutes={row.duration_minutes:.2f} gold={row.gold_point_count} "
            f"xp={row.xp_point_count} gold-floor={row.gold_minus_floor_minutes} "
            f"gold-ceil={row.gold_minus_ceil_minutes} "
            f"sec/point={row.seconds_per_gold_point:.2f} "
            f"sec/interval={row.seconds_per_gold_interval:.2f}"
        )
    return lines


def _format_path(path: Sequence[str]) -> str:
    return ".".join(path) or "<root>"


def _format_indices(values: Sequence[int]) -> str:
    return ",".join(str(value) for value in values) or "-"


def _format_optional_bool(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return _format_bool(value)


def _format_bool(value: bool) -> str:
    return "yes" if value else "no"


def _format_optional_int(value: int | None) -> str:
    return str(value) if value is not None else "-"


def _format_distribution(values: Sequence[tuple[int, int]]) -> str:
    return ", ".join(f"{key}={count}" for key, count in values) or "-"


def _format_float(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def _scalar_preview(value: object) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _compact_text(str(value))
    return type(value).__name__


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:240]


def _exact_single_constant(values: Sequence[tuple[int, int]]) -> int | None:
    return values[0][0] if len(values) == 1 else None


def _median(values: Sequence[float]) -> float | None:
    values_tuple = tuple(values)
    if not values_tuple:
        return None
    ordered = sorted(values_tuple)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _min(values: Sequence[float]) -> float | None:
    values_tuple = tuple(values)
    return min(values_tuple) if values_tuple else None


def _max(values: Sequence[float]) -> float | None:
    values_tuple = tuple(values)
    return max(values_tuple) if values_tuple else None
