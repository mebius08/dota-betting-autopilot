from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import time
from typing import TYPE_CHECKING, cast

from app.draft_history import (
    AdvantageMetric,
    DraftActionKind,
    DraftWinnerSide,
    DotaSide,
    HistoricalDotaAdvantagePoint,
    HistoricalDotaGame,
    HistoricalDotaPlayerFinalStats,
    HistoricalDraftAction,
    TimeSemanticsStatus,
    draft_action_id,
    draft_game_competition_family,
    historical_dota_advantage_point_id,
    historical_dota_game_id,
    historical_dota_player_final_stats_id,
)
from app.public_pages.feasibility import (
    PublicHttpResponse,
    PublicPageHttpClient,
    PublicPageSource,
    build_public_match_url,
    check_public_page_policy,
    extract_public_match_semantics_from_page,
    extract_public_referenced_resource_urls,
)
from app.public_pages.semantics import (
    PublicMatchSemantics,
    PublicSemanticAdvantagePoint,
    PublicSemanticDraftAction,
    PublicSemanticPlayer,
    SemanticEvidenceStatus,
    extract_public_match_semantics,
)

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


STRATZ_PUBLIC_SOURCE = "stratz_public"
STRATZ_PUBLIC_PARSER_VERSION = "stratz-public-page-v1"
STRATZ_PUBLIC_EXTRACTION_REGRESSION_BLOCKER = (
    "production ingestion loses semantics that the shared feasibility path sees "
    "on the same real Next Flight page"
)
STRATZ_PUBLIC_MULTI_FAMILY_CANARY_BLOCKER = (
    "multi-family live canary not yet completed"
)
STRATZ_PUBLIC_MIN_LIVE_CANARY_SUCCESSES = 5
STRATZ_PUBLIC_SYSTEMATIC_PARSE_FAILURE_MIN_COUNT = 2
STRATZ_PUBLIC_SYSTEMATIC_PARSE_FAILURE_RATIO = 0.25


class StratzPublicSyncOutcome(str, Enum):
    INGESTED = "INGESTED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    SKIPPED = "SKIPPED"
    NOT_FOUND = "NOT_FOUND"
    FETCH_FAILED = "FETCH_FAILED"
    PARSE_FAILED = "PARSE_FAILED"
    SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"


class OrderedDraftEvidenceStatus(str, Enum):
    EXPLICIT_ORDERED = "EXPLICIT_ORDERED"
    MISSING_OR_AMBIGUOUS = "MISSING_OR_AMBIGUOUS"


class StratzPublicLiveCanaryEvidenceStatus(str, Enum):
    LIVE_REQUEST_NOT_EXECUTED = "LIVE_REQUEST_NOT_EXECUTED"
    LIVE_SINGLE_OR_HOMOGENEOUS_SAMPLE = "LIVE_SINGLE_OR_HOMOGENEOUS_SAMPLE"
    LIVE_MULTI_FAMILY_CANARY_COMPLETED = "LIVE_MULTI_FAMILY_CANARY_COMPLETED"
    LIVE_CANARY_CRITICAL_FAILURE = "LIVE_CANARY_CRITICAL_FAILURE"


@dataclass(frozen=True)
class NormalizedStratzPublicMatch:
    game: HistoricalDotaGame
    ordered_draft_actions: tuple[HistoricalDraftAction, ...]
    player_final_stats: tuple[HistoricalDotaPlayerFinalStats, ...]
    advantage_points: tuple[HistoricalDotaAdvantagePoint, ...]
    composition_complete: bool
    ordered_draft_evidence_status: OrderedDraftEvidenceStatus
    gold_advantage_points: int
    xp_advantage_points: int
    trajectory_time_semantics_status: str
    parser_version: str = STRATZ_PUBLIC_PARSER_VERSION
    warnings: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class StratzPublicNormalizationResult:
    normalized: NormalizedStratzPublicMatch | None
    parse_findings: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    invariant_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class StratzPublicMatchSyncResult:
    match_id: str
    outcome: StratzPublicSyncOutcome
    http_status: int | None = None
    fetch_error: str | None = None
    storage_result: str | None = None
    competition_family: str = "unknown"
    patch: str | None = None
    composition_complete: bool = False
    ordered_draft_evidence_status: OrderedDraftEvidenceStatus = (
        OrderedDraftEvidenceStatus.MISSING_OR_AMBIGUOUS
    )
    player_count: int = 0
    team_identity_status: str = "unknown"
    gold_advantage_points: int = 0
    xp_advantage_points: int = 0
    trajectory_time_semantics_status: str = "none"
    warnings: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    invariant_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class StratzPublicSyncResult:
    requested_match_ids: tuple[str, ...]
    request_count: int
    robots_disallowed: bool | None
    results: tuple[StratzPublicMatchSyncResult, ...]
    live_canary_executed: bool = False

    @property
    def fetched_pages(self) -> int:
        return sum(1 for result in self.results if result.http_status == 200)

    @property
    def parse_failures(self) -> int:
        return sum(
            1
            for result in self.results
            if result.outcome is StratzPublicSyncOutcome.PARSE_FAILED
        )

    @property
    def invariant_failures(self) -> int:
        return sum(1 for result in self.results if result.invariant_failures)

    @property
    def known_limitation_cases(self) -> int:
        return sum(1 for result in self.results if result.known_limitations)

    @property
    def storage_successes(self) -> int:
        return sum(
            1
            for result in self.results
            if result.outcome
            in {
                StratzPublicSyncOutcome.INGESTED,
                StratzPublicSyncOutcome.UPDATED,
                StratzPublicSyncOutcome.UNCHANGED,
            }
        )

    @property
    def live_request_executed(self) -> bool:
        return any(result.http_status is not None for result in self.results)


@dataclass(frozen=True)
class StratzPublicLiveCanaryEvidence:
    status: StratzPublicLiveCanaryEvidenceStatus
    successful_matches: int
    explicit_patches: tuple[str, ...]
    recognized_families: tuple[str, ...]
    parse_failures: int
    invariant_failures: int
    storage_successes: int
    requested_matches: int


@dataclass(frozen=True)
class _ParsedDraftAction:
    source_order: int
    kind: DraftActionKind
    side: DotaSide
    hero_id: int


def normalize_stratz_public_match_page(
    *,
    requested_match_id: str,
    html: str,
    referenced_states: Sequence[object] = (),
) -> StratzPublicNormalizationResult:
    page_semantics = extract_public_match_semantics_from_page(
        html=html,
        referenced_states=referenced_states,
        requested_match_id=requested_match_id,
    )
    if not page_semantics.decoded_states:
        return StratzPublicNormalizationResult(
            normalized=None,
            parse_findings=page_semantics.parse_findings,
            invariant_failures=("no embedded public state parsed",),
        )

    if page_semantics.semantics is None:
        return StratzPublicNormalizationResult(
            normalized=None,
            parse_findings=page_semantics.parse_findings,
            invariant_failures=("no match-shaped public state found",),
        )

    return normalize_stratz_public_match_semantics(
        requested_match_id=requested_match_id,
        semantics=page_semantics.semantics,
        parse_findings=page_semantics.parse_findings,
    )


def normalize_stratz_public_match_state(
    *,
    requested_match_id: str,
    state: object,
    parse_findings: tuple[str, ...] = (),
) -> StratzPublicNormalizationResult:
    semantics = extract_public_match_semantics(state)
    return normalize_stratz_public_match_semantics(
        requested_match_id=requested_match_id,
        semantics=semantics,
        parse_findings=parse_findings,
    )


def normalize_stratz_public_match_semantics(
    *,
    requested_match_id: str,
    semantics: PublicMatchSemantics,
    parse_findings: tuple[str, ...] = (),
) -> StratzPublicNormalizationResult:
    source_game_id = semantics.match_id or requested_match_id.strip()
    invariant_failures: list[str] = []
    warnings: list[str] = []
    limitations: list[str] = []

    if not source_game_id:
        invariant_failures.append("missing required match identity")

    duration_seconds = semantics.duration_seconds
    started_at = _datetime(semantics.started_at_value)
    ended_at = _datetime(semantics.ended_at_value)
    if started_at is None and ended_at is not None and duration_seconds is not None:
        started_at = ended_at - timedelta(seconds=duration_seconds)
        limitations.append("start timestamp derived from end timestamp and duration")
    if ended_at is None and started_at is not None and duration_seconds is not None:
        ended_at = started_at + timedelta(seconds=duration_seconds)
    if started_at is None:
        invariant_failures.append("missing required started_at timestamp")
    if (
        started_at is not None
        and ended_at is not None
        and ended_at < started_at
    ):
        invariant_failures.append("impossible duration/timestamp relationship")

    radiant_team_id = semantics.radiant_team_id
    dire_team_id = semantics.dire_team_id
    radiant_name = semantics.radiant_team_name or "Radiant"
    dire_name = semantics.dire_team_name or "Dire"
    if radiant_team_id is None or dire_team_id is None:
        limitations.append("partial team identity")
    if radiant_name == "Radiant" or dire_name == "Dire":
        limitations.append("partial team display identity")

    if semantics.player_status is SemanticEvidenceStatus.NOT_FOUND:
        invariant_failures.append("fewer than ten player rows")
    invariant_failures.extend(semantics.warnings)
    player_stats = tuple(
        _player_stats_from_semantic_player(
            row,
            source_game_id=source_game_id,
            radiant_team_id=radiant_team_id,
            dire_team_id=dire_team_id,
        )
        for row in semantics.players
    )
    composition_complete = _composition_complete(player_stats)
    if not composition_complete:
        invariant_failures.append("incomplete 5v5 hero composition")

    game_id = historical_dota_game_id(STRATZ_PUBLIC_SOURCE, source_game_id)
    player_stats = tuple(
        _player_stats_with_game_id(row, game_id) for row in player_stats
    )

    ordered_actions, draft_warnings = _ordered_draft_actions_from_semantic_actions(
        actions=semantics.draft_actions,
        source_game_id=source_game_id,
        game_id=game_id,
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
    )
    warnings.extend(draft_warnings)
    ordered_status = (
        OrderedDraftEvidenceStatus.EXPLICIT_ORDERED
        if ordered_actions
        else OrderedDraftEvidenceStatus.MISSING_OR_AMBIGUOUS
    )
    if not ordered_actions:
        limitations.append("ordered draft sequence missing or ambiguous")

    winner_side = None
    if semantics.did_radiant_win is not None:
        winner_side = "team_a" if semantics.did_radiant_win else "team_b"
    else:
        limitations.append("winner/result missing")

    series = semantics.series
    league = semantics.league
    tournament = semantics.tournament
    game_number = semantics.game_number
    best_of = semantics.best_of
    tournament_name = _mapping_name(tournament) or _mapping_name(league)
    tournament_source_id = _mapping_text(tournament, ("id", "tournamentId"))
    league_name = _mapping_name(league)
    league_source_id = _mapping_text(league, ("id", "leagueId"))
    series_source_id = _mapping_text(series, ("id", "seriesId"))
    patch = semantics.patch

    if tournament_name is None and league_name is None:
        limitations.append("partial competition identity")
    if game_number is None:
        limitations.append("game/map number not normalized")

    advantage_points = _advantage_points_from_semantic_points(
        points=semantics.advantage_points,
        source_game_id=source_game_id,
        game_id=game_id,
    )
    gold_points = sum(1 for point in advantage_points if point.metric == "gold")
    xp_points = sum(1 for point in advantage_points if point.metric == "xp")
    if not gold_points:
        limitations.append("gold advantage trajectory missing")
    if not xp_points:
        limitations.append("XP advantage trajectory missing")
    trajectory_status = _trajectory_status(advantage_points)

    if invariant_failures:
        return StratzPublicNormalizationResult(
            normalized=None,
            parse_findings=parse_findings,
            warnings=tuple(warnings),
            known_limitations=tuple(dict.fromkeys(limitations)),
            invariant_failures=tuple(dict.fromkeys(invariant_failures)),
        )

    game = HistoricalDotaGame(
        id=game_id,
        source=STRATZ_PUBLIC_SOURCE,
        source_game_id=source_game_id,
        parent_series_source_id=series_source_id,
        linked_historical_match_id=None,
        started_at=cast(datetime, started_at),
        ended_at=ended_at,
        team_a_name=radiant_name,
        team_b_name=dire_name,
        team_a_source_id=radiant_team_id,
        team_b_source_id=dire_team_id,
        winner_side=cast(DraftWinnerSide | None, winner_side),
        game_number=game_number,
        best_of=best_of,
        team_a_side="radiant",
        patch=patch,
        draft_complete=_has_complete_ordered_5v5_picks(ordered_actions),
        tournament_name=tournament_name,
        tournament_source_id=tournament_source_id,
        league_name=league_name,
        league_source_id=league_source_id,
        raw_stage_label=_mapping_text(series, ("type", "name")),
    )
    normalized = NormalizedStratzPublicMatch(
        game=game,
        ordered_draft_actions=ordered_actions,
        player_final_stats=player_stats,
        advantage_points=advantage_points,
        composition_complete=composition_complete,
        ordered_draft_evidence_status=ordered_status,
        gold_advantage_points=gold_points,
        xp_advantage_points=xp_points,
        trajectory_time_semantics_status=trajectory_status,
        warnings=tuple(dict.fromkeys(warnings)),
        known_limitations=tuple(dict.fromkeys(limitations)),
    )
    return StratzPublicNormalizationResult(
        normalized=normalized,
        parse_findings=parse_findings,
        warnings=normalized.warnings,
        known_limitations=normalized.known_limitations,
    )


def sync_stratz_public_match_pages(
    *,
    repository: SQLiteRepository,
    match_ids: Sequence[str],
    client: PublicPageHttpClient,
    delay_seconds: float = 1.0,
    max_retries: int = 1,
    retry_backoff_seconds: float = 1.0,
    fetch_referenced_resources: bool = True,
    sleep_func: Callable[[float], None] = time.sleep,
    live_canary_executed: bool = False,
) -> StratzPublicSyncResult:
    normalized_ids = tuple(
        str(match_id).strip() for match_id in match_ids if str(match_id).strip()
    )
    if not normalized_ids:
        raise ValueError("At least one --match-id is required for stratz-public.")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must not be negative")
    if max_retries < 0:
        raise ValueError("max_retries must not be negative")
    if retry_backoff_seconds < 0:
        raise ValueError("retry_backoff_seconds must not be negative")

    request_count = 0
    policy = check_public_page_policy(
        client=client,
        source=PublicPageSource.STRATZ,
        sample_path=f"/match/{normalized_ids[0]}",
    )
    request_count += 1
    if policy.path_disallowed is True:
        return StratzPublicSyncResult(
            requested_match_ids=normalized_ids,
            request_count=request_count,
            robots_disallowed=True,
            results=tuple(
                StratzPublicMatchSyncResult(
                    match_id=match_id,
                    outcome=StratzPublicSyncOutcome.SKIPPED,
                    warnings=("Robots policy disallows STRATZ /match paths.",),
                )
                for match_id in normalized_ids
            ),
            live_canary_executed=live_canary_executed,
        )

    results: list[StratzPublicMatchSyncResult] = []
    for index, match_id in enumerate(normalized_ids):
        url = build_public_match_url(PublicPageSource.STRATZ, match_id)
        response, fetch_requests = _fetch_with_retries(
            client=client,
            url=url,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            sleep_func=sleep_func,
        )
        request_count += fetch_requests
        result, extra_requests = _sync_one_response(
            repository=repository,
            client=client,
            match_id=match_id,
            response=response,
            fetch_referenced_resources=fetch_referenced_resources,
        )
        request_count += extra_requests
        results.append(result)
        if delay_seconds and index < len(normalized_ids) - 1:
            sleep_func(delay_seconds)

    return StratzPublicSyncResult(
        requested_match_ids=normalized_ids,
        request_count=request_count,
        robots_disallowed=policy.path_disallowed,
        results=tuple(results),
        live_canary_executed=live_canary_executed,
    )


def render_stratz_public_sync_result(result: StratzPublicSyncResult) -> str:
    evidence = evaluate_stratz_public_live_canary_evidence(result)
    lines: list[str] = []
    lines.append("STRATZ public-page historical ingestion")
    lines.append(f"Provider: {STRATZ_PUBLIC_SOURCE}")
    lines.append(f"Requested match IDs: {', '.join(result.requested_match_ids)}")
    lines.append(f"Requests: {result.request_count}")
    lines.append(f"Robots disallowed: {_format_optional_bool(result.robots_disallowed)}")
    lines.append("")
    lines.append("Per-match outcomes")
    header = (
        "match_id | outcome | http | storage | family | patch | composition | "
        "ordered_draft | players | team_identity | gold_points | xp_points | "
        "time_semantics"
    )
    lines.append(header)
    for row in result.results:
        lines.append(
            f"{row.match_id} | {row.outcome.value} | "
            f"{_format_optional_int(row.http_status)} | "
            f"{row.storage_result or '-'} | {row.competition_family} | "
            f"{row.patch or '-'} | {_format_bool(row.composition_complete)} | "
            f"{row.ordered_draft_evidence_status.value} | {row.player_count} | "
            f"{row.team_identity_status} | {row.gold_advantage_points} | "
            f"{row.xp_advantage_points} | {row.trajectory_time_semantics_status}"
        )
        for failure in row.invariant_failures:
            lines.append(f"  Invariant failure: {failure}")
        for limitation in row.known_limitations:
            lines.append(f"  Known limitation: {limitation}")
        for warning in row.warnings:
            lines.append(f"  Warning: {warning}")
    lines.append("")
    lines.append("Aggregate canary summary")
    lines.append(f"Match pages requested: {len(result.requested_match_ids)}")
    lines.append(f"Match pages fetched: {result.fetched_pages}")
    lines.append(
        "HTTP/fetch failures: "
        + str(
            sum(
                1
                for row in result.results
                if row.outcome
                in {
                    StratzPublicSyncOutcome.FETCH_FAILED,
                    StratzPublicSyncOutcome.NOT_FOUND,
                }
            )
        )
    )
    lines.append(f"Parse failures: {result.parse_failures}")
    lines.append(f"Ingestion-critical invariant failures: {result.invariant_failures}")
    lines.append(f"Known-limitation cases: {result.known_limitation_cases}")
    lines.append(f"Storage successes: {result.storage_successes}")
    lines.append("")
    lines.append("Live request")
    lines.append(
        "LIVE_REQUEST_EXECUTED"
        if result.live_request_executed
        else "LIVE_REQUEST_NOT_EXECUTED"
    )
    lines.append("Multi-family live canary")
    lines.append(evidence.status.value)
    lines.append("")
    lines.append("Post-canary architecture decision")
    if _stratz_public_ready_for_bounded_backfill(evidence):
        lines.append("STRATZ_PUBLIC_READY_FOR_BOUNDED_BACKFILL")
    else:
        lines.append("STRATZ_PUBLIC_CANARY_BLOCKED")
        lines.append(f"Blocker: {_stratz_public_canary_blocker(result, evidence)}")
    return "\n".join(lines)


def evaluate_stratz_public_live_canary_evidence(
    result: StratzPublicSyncResult,
) -> StratzPublicLiveCanaryEvidence:
    successful_rows = tuple(
        row
        for row in result.results
        if row.http_status == 200
        and row.outcome
        in {
            StratzPublicSyncOutcome.INGESTED,
            StratzPublicSyncOutcome.UPDATED,
            StratzPublicSyncOutcome.UNCHANGED,
        }
        and row.composition_complete
        and row.player_count == 10
    )
    explicit_patches = tuple(
        sorted(
            {
                patch
                for row in successful_rows
                if (patch := _text(row.patch)) is not None
            }
        )
    )
    recognized_families = tuple(
        sorted(
            {
                row.competition_family
                for row in successful_rows
                if row.competition_family != "unknown"
            }
        )
    )
    if not result.live_request_executed:
        status = StratzPublicLiveCanaryEvidenceStatus.LIVE_REQUEST_NOT_EXECUTED
    elif _has_live_canary_critical_failure(result):
        status = StratzPublicLiveCanaryEvidenceStatus.LIVE_CANARY_CRITICAL_FAILURE
    elif (
        len(successful_rows) >= STRATZ_PUBLIC_MIN_LIVE_CANARY_SUCCESSES
        and (len(explicit_patches) > 1 or len(recognized_families) > 1)
    ):
        status = StratzPublicLiveCanaryEvidenceStatus.LIVE_MULTI_FAMILY_CANARY_COMPLETED
    else:
        status = StratzPublicLiveCanaryEvidenceStatus.LIVE_SINGLE_OR_HOMOGENEOUS_SAMPLE
    return StratzPublicLiveCanaryEvidence(
        status=status,
        successful_matches=len(successful_rows),
        explicit_patches=explicit_patches,
        recognized_families=recognized_families,
        parse_failures=result.parse_failures,
        invariant_failures=result.invariant_failures,
        storage_successes=result.storage_successes,
        requested_matches=len(result.requested_match_ids),
    )


def _stratz_public_ready_for_bounded_backfill(
    evidence: StratzPublicLiveCanaryEvidence,
) -> bool:
    return (
        evidence.status
        is StratzPublicLiveCanaryEvidenceStatus.LIVE_MULTI_FAMILY_CANARY_COMPLETED
    )


def _stratz_public_canary_blocker(
    result: StratzPublicSyncResult,
    evidence: StratzPublicLiveCanaryEvidence,
) -> str:
    if _has_production_extraction_regression_evidence(result):
        return STRATZ_PUBLIC_EXTRACTION_REGRESSION_BLOCKER
    if evidence.status is StratzPublicLiveCanaryEvidenceStatus.LIVE_REQUEST_NOT_EXECUTED:
        return "live request not executed"
    if (
        evidence.status
        is StratzPublicLiveCanaryEvidenceStatus.LIVE_SINGLE_OR_HOMOGENEOUS_SAMPLE
    ):
        return STRATZ_PUBLIC_MULTI_FAMILY_CANARY_BLOCKER
    return _multi_family_canary_failure_blocker(result)


def _has_live_canary_critical_failure(result: StratzPublicSyncResult) -> bool:
    if result.invariant_failures:
        return True
    if _has_systematic_parse_failure(result):
        return True
    if _has_production_extraction_regression_evidence(result):
        return True
    return any(
        row.outcome
        in {
            StratzPublicSyncOutcome.FETCH_FAILED,
            StratzPublicSyncOutcome.NOT_FOUND,
            StratzPublicSyncOutcome.SOURCE_INCOMPLETE,
        }
        for row in result.results
    )


def _has_systematic_parse_failure(result: StratzPublicSyncResult) -> bool:
    if not result.results:
        return False
    return (
        result.parse_failures >= STRATZ_PUBLIC_SYSTEMATIC_PARSE_FAILURE_MIN_COUNT
        or result.parse_failures / len(result.results)
        >= STRATZ_PUBLIC_SYSTEMATIC_PARSE_FAILURE_RATIO
    )


def _has_production_extraction_regression_evidence(
    result: StratzPublicSyncResult,
) -> bool:
    regression_invariants = {
        "ambiguous player side assignment",
        "fewer than ten player rows",
        "incomplete 5v5 hero composition",
        "player row missing hero identity",
    }
    for row in result.results:
        if row.http_status != 200:
            continue
        if row.player_count == 0 and row.outcome is StratzPublicSyncOutcome.SOURCE_INCOMPLETE:
            return True
        if any(failure in regression_invariants for failure in row.invariant_failures):
            return True
        if (
            row.outcome is StratzPublicSyncOutcome.SOURCE_INCOMPLETE
            and not row.composition_complete
            and row.player_count < 10
        ):
            return True
    return False


def _multi_family_canary_failure_blocker(result: StratzPublicSyncResult) -> str:
    if result.fetched_pages == 0:
        return "multi-family live canary fetched no match pages"
    if result.parse_failures:
        return "multi-family live canary has parse failures"
    if result.invariant_failures:
        return "multi-family live canary has ingestion-critical invariant failures"
    if result.storage_successes != len(result.results):
        return "multi-family live canary has storage failures or skipped matches"
    return "multi-family live canary failed"


def _sync_one_response(
    *,
    repository: SQLiteRepository,
    client: PublicPageHttpClient,
    match_id: str,
    response: PublicHttpResponse,
    fetch_referenced_resources: bool,
) -> tuple[StratzPublicMatchSyncResult, int]:
    if response.status_code == 404:
        return (
            StratzPublicMatchSyncResult(
                match_id=match_id,
                outcome=StratzPublicSyncOutcome.NOT_FOUND,
                http_status=response.status_code,
                fetch_error=response.error,
            ),
            0,
        )
    if response.status_code != 200:
        return (
            StratzPublicMatchSyncResult(
                match_id=match_id,
                outcome=StratzPublicSyncOutcome.FETCH_FAILED,
                http_status=response.status_code,
                fetch_error=response.error,
                warnings=(f"HTTP status {response.status_code}",),
            ),
            0,
        )

    referenced_states = _referenced_resource_states(
        response=response,
        client=client,
        fetch_referenced_resources=fetch_referenced_resources,
    )
    normalization = normalize_stratz_public_match_page(
        requested_match_id=match_id,
        html=response.text,
        referenced_states=referenced_states[0],
    )
    resource_requests = referenced_states[1]
    if normalization.normalized is None:
        outcome = (
            StratzPublicSyncOutcome.PARSE_FAILED
            if "no embedded public state parsed" in normalization.invariant_failures
            else StratzPublicSyncOutcome.SOURCE_INCOMPLETE
        )
        return (
            StratzPublicMatchSyncResult(
                match_id=match_id,
                outcome=outcome,
                http_status=response.status_code,
                warnings=normalization.warnings,
                known_limitations=normalization.known_limitations,
                invariant_failures=normalization.invariant_failures,
            ),
            resource_requests,
        )

    normalized = normalization.normalized
    try:
        storage_result = _persist_normalized_match(repository, normalized)
    except ValueError as exc:
        return (
            StratzPublicMatchSyncResult(
                match_id=match_id,
                outcome=StratzPublicSyncOutcome.SOURCE_INCOMPLETE,
                http_status=response.status_code,
                warnings=normalization.warnings + (str(exc),),
                known_limitations=normalization.known_limitations,
                invariant_failures=("storage conflict",),
            ),
            resource_requests,
        )
    outcome = {
        "inserted": StratzPublicSyncOutcome.INGESTED,
        "updated": StratzPublicSyncOutcome.UPDATED,
        "unchanged": StratzPublicSyncOutcome.UNCHANGED,
    }[storage_result]
    return (
        StratzPublicMatchSyncResult(
            match_id=match_id,
            outcome=outcome,
            http_status=response.status_code,
            storage_result=storage_result,
            competition_family=draft_game_competition_family(
                normalized.game
            ).value,
            patch=normalized.game.patch,
            composition_complete=normalized.composition_complete,
            ordered_draft_evidence_status=normalized.ordered_draft_evidence_status,
            player_count=len(normalized.player_final_stats),
            team_identity_status=_team_identity_status(normalized.game),
            gold_advantage_points=normalized.gold_advantage_points,
            xp_advantage_points=normalized.xp_advantage_points,
            trajectory_time_semantics_status=(
                normalized.trajectory_time_semantics_status
            ),
            warnings=normalization.warnings,
            known_limitations=normalization.known_limitations,
        ),
        resource_requests,
    )


def _persist_normalized_match(
    repository: SQLiteRepository,
    normalized: NormalizedStratzPublicMatch,
) -> str:
    existing = repository.get_historical_dota_game_by_source(
        normalized.game.source,
        normalized.game.source_game_id,
    )
    game = (
        _merge_existing_game(existing, normalized.game)
        if existing is not None
        else normalized.game
    )
    actions = normalized.ordered_draft_actions
    if existing is not None and not actions:
        existing_actions = tuple(repository.list_historical_draft_actions(existing.id))
        if existing_actions:
            actions = existing_actions
    storage_result = repository.upsert_historical_dota_game(game, actions)
    if normalized.player_final_stats:
        player_rows = tuple(
            _player_stats_with_game_id(row, game.id)
            for row in normalized.player_final_stats
        )
        if existing is not None:
            existing_stats = {
                row.account_id: row
                for row in repository.list_historical_dota_player_final_stats(
                    existing.id
                )
            }
            player_rows = tuple(
                _merge_existing_player_final_stats(
                    existing_stats.get(row.account_id),
                    row,
                )
                for row in player_rows
            )
        repository.replace_historical_dota_player_final_stats(
            game.id,
            player_rows,
        )
    if normalized.advantage_points:
        repository.replace_historical_dota_advantage_points(
            game.id,
            tuple(
                _advantage_point_with_game_id(row, game.id)
                for row in normalized.advantage_points
            ),
        )
    return storage_result


def _merge_existing_game(
    existing: HistoricalDotaGame,
    incoming: HistoricalDotaGame,
) -> HistoricalDotaGame:
    return replace(
        incoming,
        team_a_name=_prefer_name(existing.team_a_name, incoming.team_a_name, "Radiant"),
        team_b_name=_prefer_name(existing.team_b_name, incoming.team_b_name, "Dire"),
        team_a_source_id=incoming.team_a_source_id or existing.team_a_source_id,
        team_b_source_id=incoming.team_b_source_id or existing.team_b_source_id,
        parent_series_source_id=(
            incoming.parent_series_source_id or existing.parent_series_source_id
        ),
        linked_historical_match_id=(
            incoming.linked_historical_match_id or existing.linked_historical_match_id
        ),
        ended_at=incoming.ended_at or existing.ended_at,
        winner_side=incoming.winner_side or existing.winner_side,
        game_number=incoming.game_number or existing.game_number,
        best_of=incoming.best_of or existing.best_of,
        patch=incoming.patch or existing.patch,
        draft_complete=incoming.draft_complete or existing.draft_complete,
        tournament_name=incoming.tournament_name or existing.tournament_name,
        tournament_source_id=(
            incoming.tournament_source_id or existing.tournament_source_id
        ),
        league_name=incoming.league_name or existing.league_name,
        league_source_id=incoming.league_source_id or existing.league_source_id,
        raw_stage_label=incoming.raw_stage_label or existing.raw_stage_label,
    )


def _merge_existing_player_final_stats(
    existing: HistoricalDotaPlayerFinalStats | None,
    incoming: HistoricalDotaPlayerFinalStats,
) -> HistoricalDotaPlayerFinalStats:
    if existing is None:
        return incoming
    return replace(
        incoming,
        player_slot=(
            incoming.player_slot
            if incoming.player_slot is not None
            else existing.player_slot
        ),
        team_source_id=incoming.team_source_id or existing.team_source_id,
        kills=incoming.kills if incoming.kills is not None else existing.kills,
        deaths=incoming.deaths if incoming.deaths is not None else existing.deaths,
        assists=(
            incoming.assists if incoming.assists is not None else existing.assists
        ),
        net_worth=(
            incoming.net_worth
            if incoming.net_worth is not None
            else existing.net_worth
        ),
        last_hits=(
            incoming.last_hits
            if incoming.last_hits is not None
            else existing.last_hits
        ),
        denies=incoming.denies if incoming.denies is not None else existing.denies,
        gpm=incoming.gpm if incoming.gpm is not None else existing.gpm,
        xpm=incoming.xpm if incoming.xpm is not None else existing.xpm,
        level=incoming.level if incoming.level is not None else existing.level,
        hero_damage=(
            incoming.hero_damage
            if incoming.hero_damage is not None
            else existing.hero_damage
        ),
        tower_damage=(
            incoming.tower_damage
            if incoming.tower_damage is not None
            else existing.tower_damage
        ),
        hero_healing=(
            incoming.hero_healing
            if incoming.hero_healing is not None
            else existing.hero_healing
        ),
        final_item_ids=incoming.final_item_ids or existing.final_item_ids,
    )


def _prefer_name(existing: str, incoming: str, generic: str) -> str:
    if incoming == generic and existing != generic:
        return existing
    return incoming


def _fetch_with_retries(
    *,
    client: PublicPageHttpClient,
    url: str,
    max_retries: int,
    retry_backoff_seconds: float,
    sleep_func: Callable[[float], None],
) -> tuple[PublicHttpResponse, int]:
    attempts = 0
    while True:
        attempts += 1
        response = client.fetch(url)
        if not _is_retryable_response(response) or attempts > max_retries + 1:
            return response, attempts
        if retry_backoff_seconds:
            sleep_func(retry_backoff_seconds)


def _is_retryable_response(response: PublicHttpResponse) -> bool:
    return (
        response.status_code is None
        or response.status_code == 429
        or response.status_code >= 500
    )


def _referenced_resource_states(
    *,
    response: PublicHttpResponse,
    client: PublicPageHttpClient,
    fetch_referenced_resources: bool,
) -> tuple[tuple[object, ...], int]:
    if not fetch_referenced_resources:
        return (), 0
    states: list[object] = []
    request_count = 0
    for url in extract_public_referenced_resource_urls(response.text, response.url)[:3]:
        resource_response = client.fetch(url)
        request_count += 1
        if resource_response.status_code != 200:
            continue
        try:
            states.append(json.loads(resource_response.text))
        except json.JSONDecodeError:
            continue
    return tuple(states), request_count


def _find_match_state(
    states: Sequence[object],
    requested_match_id: str,
) -> Mapping[str, object] | None:
    candidates: list[Mapping[str, object]] = []
    for state in states:
        for mapping in _walk_mappings(state):
            if _is_match_mapping(mapping):
                candidates.append(mapping)
    if not candidates:
        return None
    requested = requested_match_id.strip()
    for candidate in candidates:
        candidate_id = _text_at(candidate, ("matchId", "id"))
        if candidate_id == requested:
            return candidate
    return candidates[0]


def _is_match_mapping(value: Mapping[str, object]) -> bool:
    return (
        bool(_list_at(value, ("players", "playerMatches")))
        and (
            _text_at(value, ("matchId", "id")) is not None
            or _find_value(value, ("radiantTeam", "direTeam")) is not None
        )
    )


def _walk_mappings(value: object) -> tuple[Mapping[str, object], ...]:
    found: list[Mapping[str, object]] = []
    if isinstance(value, Mapping):
        found.append(value)
        for nested in value.values():
            found.extend(_walk_mappings(nested))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_mappings(item))
    return tuple(found)


def _players_from_state(
    *,
    state: Mapping[str, object],
    source_game_id: str,
    radiant_team_id: str | None,
    dire_team_id: str | None,
) -> tuple[tuple[HistoricalDotaPlayerFinalStats, ...], tuple[str, ...]]:
    raw_players = _list_at(state, ("players", "playerMatches"))
    rows: list[HistoricalDotaPlayerFinalStats] = []
    failures: list[str] = []
    seen_accounts: set[str] = set()
    if len(raw_players) < 10:
        failures.append("fewer than ten player rows")
    for index, raw_player in enumerate(raw_players[:10]):
        if not isinstance(raw_player, Mapping):
            failures.append("malformed player row")
            continue
        account_id = _text_at(
            raw_player,
            ("steamAccountId", "accountId", "playerId", "id"),
        )
        if account_id is None:
            failures.append("player row missing account identity")
            continue
        if account_id in seen_accounts:
            failures.append("duplicate player account identity")
            continue
        seen_accounts.add(account_id)
        side = _player_side(raw_player)
        hero_id = _int_at(raw_player, ("heroId", "hero_id"))
        if side not in ("radiant", "dire"):
            failures.append("ambiguous player side assignment")
            continue
        if hero_id is None:
            failures.append("player row missing hero identity")
            continue
        rows.append(
            HistoricalDotaPlayerFinalStats(
                id=historical_dota_player_final_stats_id("pending", account_id),
                game_id="pending",
                source=STRATZ_PUBLIC_SOURCE,
                source_game_id=source_game_id,
                account_id=account_id,
                player_slot=_int_at(raw_player, ("playerSlot", "slot")),
                team_side=side,
                team_source_id=(
                    radiant_team_id if side == "radiant" else dire_team_id
                ),
                hero_id=hero_id,
                kills=_int_at(raw_player, ("kills",)),
                deaths=_int_at(raw_player, ("deaths",)),
                assists=_int_at(raw_player, ("assists",)),
                net_worth=_int_at(raw_player, ("netWorth", "networth")),
                last_hits=_int_at(raw_player, ("numLastHits", "lastHits")),
                denies=_int_at(raw_player, ("numDenies", "denies")),
                gpm=_int_at(raw_player, ("goldPerMinute", "gpm")),
                xpm=_int_at(raw_player, ("experiencePerMinute", "xpm")),
                level=_int_at(raw_player, ("level",)),
                hero_damage=_int_at(raw_player, ("heroDamage",)),
                tower_damage=_int_at(raw_player, ("towerDamage",)),
                hero_healing=_int_at(raw_player, ("heroHealing",)),
                final_item_ids=_final_item_ids(raw_player),
            )
        )
    return tuple(rows), tuple(dict.fromkeys(failures))


def _player_stats_from_semantic_player(
    player: PublicSemanticPlayer,
    *,
    source_game_id: str,
    radiant_team_id: str | None,
    dire_team_id: str | None,
) -> HistoricalDotaPlayerFinalStats:
    return HistoricalDotaPlayerFinalStats(
        id=historical_dota_player_final_stats_id("pending", player.account_id),
        game_id="pending",
        source=STRATZ_PUBLIC_SOURCE,
        source_game_id=source_game_id,
        account_id=player.account_id,
        player_slot=player.player_slot,
        team_side=player.team_side,
        team_source_id=(
            radiant_team_id if player.team_side == "radiant" else dire_team_id
        ),
        hero_id=player.hero_id,
        kills=player.kills,
        deaths=player.deaths,
        assists=player.assists,
        net_worth=player.net_worth,
        last_hits=player.last_hits,
        denies=player.denies,
        gpm=player.gpm,
        xpm=player.xpm,
        level=player.level,
        hero_damage=player.hero_damage,
        tower_damage=player.tower_damage,
        hero_healing=player.hero_healing,
        final_item_ids=player.final_item_ids,
    )


def _player_stats_with_game_id(
    row: HistoricalDotaPlayerFinalStats,
    game_id: str,
) -> HistoricalDotaPlayerFinalStats:
    return replace(
        row,
        id=historical_dota_player_final_stats_id(game_id, row.account_id),
        game_id=game_id,
    )


def _advantage_point_with_game_id(
    row: HistoricalDotaAdvantagePoint,
    game_id: str,
) -> HistoricalDotaAdvantagePoint:
    return replace(
        row,
        id=historical_dota_advantage_point_id(game_id, row.metric, row.source_index),
        game_id=game_id,
    )


def _composition_complete(players: Sequence[HistoricalDotaPlayerFinalStats]) -> bool:
    radiant = {row.hero_id for row in players if row.team_side == "radiant"}
    dire = {row.hero_id for row in players if row.team_side == "dire"}
    return len(radiant) == 5 and len(dire) == 5


def _ordered_draft_actions_from_state(
    *,
    state: Mapping[str, object],
    source_game_id: str,
    game_id: str,
    radiant_team_id: str | None,
    dire_team_id: str | None,
) -> tuple[tuple[HistoricalDraftAction, ...], tuple[str, ...]]:
    raw_actions = _list_at(state, ("pickBans", "draftActions", "picksBans"))
    if not raw_actions:
        return (), ()
    parsed: list[_ParsedDraftAction] = []
    warnings: list[str] = []
    for raw_action in raw_actions:
        if not isinstance(raw_action, Mapping):
            warnings.append("Skipped malformed draft action row.")
            continue
        order = _first_int(raw_action, ("order", "ord", "sequence"))
        kind = _draft_kind(raw_action)
        side = _side_from_mapping(raw_action)
        hero_id = _first_int(raw_action, ("heroId", "hero_id", "bannedHeroId"))
        if order is None or kind is None or side not in ("radiant", "dire") or hero_id is None:
            return (), ("Ordered draft action evidence is incomplete.",)
        parsed.append(
            _ParsedDraftAction(
                source_order=order,
                kind=kind,
                side=side,
                hero_id=hero_id,
            )
        )
    if not parsed:
        return (), tuple(warnings)
    source_orders = [action.source_order for action in parsed]
    if len(source_orders) != len(set(source_orders)):
        return (), ("Duplicate draft action order encountered.",)
    actions: list[HistoricalDraftAction] = []
    for normalized_order, action in enumerate(
        sorted(parsed, key=lambda item: item.source_order),
        start=1,
    ):
        team_source_id = (
            radiant_team_id if action.side == "radiant" else dire_team_id
        )
        actions.append(
            HistoricalDraftAction(
                id=draft_action_id(game_id, normalized_order),
                game_id=game_id,
                source=STRATZ_PUBLIC_SOURCE,
                source_game_id=source_game_id,
                action_order=normalized_order,
                action_kind=action.kind,
                team_side=action.side,
                team_source_id=team_source_id,
                hero_id=action.hero_id,
            )
        )
    return tuple(actions), tuple(warnings)


def _ordered_draft_actions_from_semantic_actions(
    *,
    actions: Sequence[PublicSemanticDraftAction],
    source_game_id: str,
    game_id: str,
    radiant_team_id: str | None,
    dire_team_id: str | None,
) -> tuple[tuple[HistoricalDraftAction, ...], tuple[str, ...]]:
    if not actions:
        return (), ()
    parsed: list[_ParsedDraftAction] = []
    for semantic_action in actions:
        if (
            semantic_action.order is None
            or semantic_action.kind is None
            or semantic_action.side not in ("radiant", "dire")
            or semantic_action.hero_id is None
        ):
            return (), ("Ordered draft action evidence is incomplete.",)
        parsed.append(
            _ParsedDraftAction(
                source_order=semantic_action.order,
                kind=semantic_action.kind,
                side=semantic_action.side,
                hero_id=semantic_action.hero_id,
            )
        )
    source_orders = [action.source_order for action in parsed]
    if len(source_orders) != len(set(source_orders)):
        return (), ("Duplicate draft action order encountered.",)
    rows: list[HistoricalDraftAction] = []
    for normalized_order, parsed_action in enumerate(
        sorted(parsed, key=lambda item: item.source_order),
        start=1,
    ):
        team_source_id = (
            radiant_team_id if parsed_action.side == "radiant" else dire_team_id
        )
        rows.append(
            HistoricalDraftAction(
                id=draft_action_id(game_id, normalized_order),
                game_id=game_id,
                source=STRATZ_PUBLIC_SOURCE,
                source_game_id=source_game_id,
                action_order=normalized_order,
                action_kind=parsed_action.kind,
                team_side=parsed_action.side,
                team_source_id=team_source_id,
                hero_id=parsed_action.hero_id,
            )
        )
    return tuple(rows), ()


def _has_complete_ordered_5v5_picks(
    actions: Sequence[HistoricalDraftAction],
) -> bool:
    radiant = {
        action.hero_id
        for action in actions
        if action.action_kind == "pick" and action.team_side == "radiant"
    }
    dire = {
        action.hero_id
        for action in actions
        if action.action_kind == "pick" and action.team_side == "dire"
    }
    return len(radiant) == 5 and len(dire) == 5


def _advantage_points_from_state(
    *,
    state: Mapping[str, object],
    source_game_id: str,
    game_id: str,
) -> tuple[HistoricalDotaAdvantagePoint, ...]:
    points: list[HistoricalDotaAdvantagePoint] = []
    metric_keys: tuple[tuple[AdvantageMetric, tuple[str, ...]], ...] = (
        ("gold", ("radiantNetworthLeads", "goldAdvantage")),
        ("xp", ("radiantExperienceLeads", "xpAdvantage")),
    )
    for metric, keys in metric_keys:
        raw = _find_value(state, keys)
        if not isinstance(raw, list):
            continue
        for index, item in enumerate(raw):
            parsed = _advantage_item(item)
            if parsed is None:
                continue
            value, source_time_value, normalized_seconds = parsed
            status: TimeSemanticsStatus = (
                "normalized_seconds"
                if normalized_seconds is not None
                else "source_index_unstable"
            )
            points.append(
                HistoricalDotaAdvantagePoint(
                    id=historical_dota_advantage_point_id(
                        game_id,
                        metric,
                        index,
                    ),
                    game_id=game_id,
                    source=STRATZ_PUBLIC_SOURCE,
                    source_game_id=source_game_id,
                    metric=metric,
                    source_index=index,
                    source_time_value=source_time_value,
                    normalized_time_seconds=normalized_seconds,
                    time_semantics_status=status,
                    value=value,
                )
            )
    return tuple(points)


def _advantage_points_from_semantic_points(
    *,
    points: Sequence[PublicSemanticAdvantagePoint],
    source_game_id: str,
    game_id: str,
) -> tuple[HistoricalDotaAdvantagePoint, ...]:
    return tuple(
        HistoricalDotaAdvantagePoint(
            id=historical_dota_advantage_point_id(
                game_id,
                point.metric,
                point.source_index,
            ),
            game_id=game_id,
            source=STRATZ_PUBLIC_SOURCE,
            source_game_id=source_game_id,
            metric=point.metric,
            source_index=point.source_index,
            source_time_value=point.source_time_value,
            normalized_time_seconds=point.normalized_time_seconds,
            time_semantics_status=point.time_semantics_status,
            value=point.value,
        )
        for point in points
    )


def _advantage_item(item: object) -> tuple[float, str | None, int | None] | None:
    if isinstance(item, Mapping):
        value = _float_at(item, ("value", "lead", "amount"))
        if value is None:
            return None
        source_time = _find_value(item, ("time", "gameTime", "timestamp", "second"))
        normalized_seconds = _int(source_time)
        return value, _text(source_time), normalized_seconds
    value = _float(item)
    if value is None:
        return None
    return value, None, None


def _trajectory_status(points: Sequence[HistoricalDotaAdvantagePoint]) -> str:
    if not points:
        return "none"
    statuses = {point.time_semantics_status for point in points}
    if statuses == {"normalized_seconds"}:
        return "normalized_seconds"
    if "normalized_seconds" in statuses:
        return "mixed"
    return "source_index_unstable"


def _find_value(value: object, keys: tuple[str, ...]) -> object | None:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value and value[key] is not None:
                return value[key]
        for nested in value.values():
            found = _find_value(nested, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_value(item, keys)
            if found is not None:
                return found
    return None


def _list_at(value: Mapping[str, object], keys: tuple[str, ...]) -> list[object]:
    for key in keys:
        item = value.get(key)
        if isinstance(item, list):
            return item
    return []


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _mapping_name(value: Mapping[str, object] | None) -> str | None:
    return _mapping_text(value, ("displayName", "name", "shortName", "fullName"))


def _mapping_text(
    value: Mapping[str, object] | None,
    keys: tuple[str, ...],
) -> str | None:
    if value is None:
        return None
    return _text_at(value, keys)


def _mapping_int(
    value: Mapping[str, object] | None,
    keys: tuple[str, ...],
) -> int | None:
    if value is None:
        return None
    return _int_at(value, keys)


def _text_at(value: Mapping[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        text = _text(value.get(key))
        if text is not None:
            return text
    return None


def _int_at(value: Mapping[str, object], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        integer = _int(value.get(key))
        if integer is not None:
            return integer
    return None


def _float_at(value: Mapping[str, object], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        number = _float(value.get(key))
        if number is not None:
            return number
    return None


def _bool_at(value: Mapping[str, object], keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, bool):
            return item
    return None


def _datetime_at(
    value: Mapping[str, object],
    keys: tuple[str, ...],
) -> datetime | None:
    for key in keys:
        parsed = _datetime(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = _text(value)
        if text is None:
            return None
        if text.isdigit():
            result = datetime.fromtimestamp(int(text), tz=timezone.utc)
        else:
            try:
                result = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _first_int(value: Mapping[str, object], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key not in value:
            continue
        parsed = _int(value.get(key))
        if parsed is not None:
            return parsed
    return None


def _draft_kind(value: Mapping[str, object]) -> DraftActionKind | None:
    is_pick = value.get("isPick")
    if isinstance(is_pick, bool):
        return "pick" if is_pick else "ban"
    if value.get("bannedHeroId") is not None or value.get("wasBannedSuccessfully") is not None:
        return "ban"
    text = _text(value.get("kind") or value.get("type") or value.get("action"))
    if text is None:
        return None
    lowered = text.casefold()
    if "pick" in lowered:
        return "pick"
    if "ban" in lowered:
        return "ban"
    return None


def _side_from_mapping(value: Mapping[str, object]) -> DotaSide:
    side = _side(value.get("side") or value.get("teamSide") or value.get("team"))
    if side != "unknown":
        return side
    is_radiant = value.get("isRadiant")
    if isinstance(is_radiant, bool):
        return "radiant" if is_radiant else "dire"
    return "unknown"


def _player_side(value: Mapping[str, object]) -> DotaSide:
    side = _side_from_mapping(value)
    if side != "unknown":
        return side
    slot = _int(value.get("playerSlot"))
    if slot is None:
        return "unknown"
    return "radiant" if slot < 128 else "dire"


def _side(value: object) -> DotaSide:
    if value in (0, "0"):
        return "radiant"
    if value in (1, "1"):
        return "dire"
    text = _text(value)
    if text is None:
        return "unknown"
    lowered = text.casefold()
    if lowered in {"radiant", "home"}:
        return "radiant"
    if lowered in {"dire", "away"}:
        return "dire"
    return "unknown"


def _final_item_ids(value: Mapping[str, object]) -> tuple[int, ...]:
    item_ids: list[int] = []
    for key in (
        "item0Id",
        "item1Id",
        "item2Id",
        "item3Id",
        "item4Id",
        "item5Id",
        "backpack0Id",
        "backpack1Id",
        "backpack2Id",
        "neutral0Id",
    ):
        item_id = _int(value.get(key))
        if item_id is not None and item_id > 0:
            item_ids.append(item_id)
    return tuple(item_ids)


def _best_of(value: object) -> int | None:
    if value is None:
        return None
    integer = _int(value)
    if integer is not None and integer > 0:
        return integer
    text = _text(value)
    if text is None:
        return None
    digits = "".join(character for character in text if character.isdigit())
    integer = _int(digits)
    return integer if integer is not None and integer > 0 else None


def _team_identity_status(game: HistoricalDotaGame) -> str:
    if game.team_a_source_id and game.team_b_source_id:
        return "complete"
    if game.team_a_source_id or game.team_b_source_id:
        return "partial"
    return "missing"


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def _format_optional_bool(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _format_bool(value: bool) -> str:
    return "yes" if value else "no"


def _format_optional_int(value: int | None) -> str:
    return "unknown" if value is None else str(value)
