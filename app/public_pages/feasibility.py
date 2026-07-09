from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from html import unescape
from html.parser import HTMLParser
import json
import re
import time
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from app.public_pages.semantics import (
    PublicMatchSemanticFingerprint,
    PublicMatchSemantics,
    SemanticEvidenceStatus,
    extract_public_match_semantics,
    extract_public_match_semantics_from_roots,
    find_public_mapping_list,
    find_public_state_value,
    public_match_semantic_fingerprint,
    public_has_timed_event_list,
    public_has_tower_barracks_objectives,
    public_players_have_timed_items,
)


PUBLIC_PAGE_USER_AGENT = "dota-betting-autopilot/1.0"
STRATZ_PUBLIC_BASE_URL = "https://stratz.com"
SOFASCORE_PUBLIC_BASE_URL = "https://www.sofascore.com"


class PublicPageSource(str, Enum):
    STRATZ = "stratz"
    SOFASCORE = "sofascore"


class PublicPageAccessStatus(str, Enum):
    HTTP_FORBIDDEN = "HTTP_FORBIDDEN"
    HTTP_RATE_LIMITED = "HTTP_RATE_LIMITED"
    ROBOTS_PATH_DISALLOWED = "ROBOTS_PATH_DISALLOWED"
    PUBLIC_PAGE_NOT_FOUND = "PUBLIC_PAGE_NOT_FOUND"
    PUBLIC_PAGE_AVAILABLE = "PUBLIC_PAGE_AVAILABLE"
    PAGE_AVAILABLE_DATA_NOT_STATIC = "PAGE_AVAILABLE_DATA_NOT_STATIC"
    PUBLIC_REFERENCED_RESOURCE_AVAILABLE = "PUBLIC_REFERENCED_RESOURCE_AVAILABLE"
    ACCESS_CONTROL_REQUIRED = "ACCESS_CONTROL_REQUIRED"
    HTTP_ERROR = "HTTP_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"


class PublicFieldProvenance(str, Enum):
    VISIBLE_HTML = "VISIBLE_HTML"
    EMBEDDED_PUBLIC_PAGE_STATE = "EMBEDDED_PUBLIC_PAGE_STATE"
    PUBLIC_PAGE_REFERENCED_RESOURCE = "PUBLIC_PAGE_REFERENCED_RESOURCE"
    DERIVED_FROM_PUBLIC_FIELDS = "DERIVED_FROM_PUBLIC_FIELDS"
    NOT_FOUND = "NOT_FOUND"
    UNCLEAR = "UNCLEAR"


class PublicDataUsage(str, Enum):
    TARGET_GAME_PRE_OUTCOME_INPUT = "TARGET_GAME_PRE_OUTCOME_INPUT"
    POST_GAME_TARGET_OR_LABEL = "POST_GAME_TARGET_OR_LABEL"
    PRIOR_GAME_HISTORICAL_CONTEXT_ONLY = "PRIOR_GAME_HISTORICAL_CONTEXT_ONLY"
    IDENTITY_OR_CONTEXT = "IDENTITY_OR_CONTEXT"
    UNSUITABLE_OR_UNCLEAR = "UNSUITABLE_OR_UNCLEAR"


class PublicSourceRecommendation(str, Enum):
    PRIMARY_HISTORICAL_GAME_SOURCE_CANDIDATE = (
        "PRIMARY HISTORICAL GAME SOURCE CANDIDATE"
    )
    DRAFT_SOURCE_CANDIDATE = "DRAFT SOURCE CANDIDATE"
    PARTIAL_ENRICHMENT_SOURCE_CANDIDATE = "PARTIAL / ENRICHMENT SOURCE CANDIDATE"
    INSUFFICIENT_SOURCE = "INSUFFICIENT SOURCE"


class PublicSourceCoverageClassification(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    DERIVABLE = "DERIVABLE"
    MISSING = "MISSING"
    UNSTABLE = "UNSTABLE"


class PublicWorkloadSuitability(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    SUFFICIENT_WITH_LIMITATIONS = "SUFFICIENT_WITH_LIMITATIONS"
    INSUFFICIENT = "INSUFFICIENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PublicArchitectureDecision(str, Enum):
    STRATZ_PUBLIC_SUFFICIENT = "STRATZ_PUBLIC_SUFFICIENT"
    STRATZ_PUBLIC_CRITICAL_GAPS = "STRATZ_PUBLIC_CRITICAL_GAPS"


@dataclass(frozen=True)
class PublicFieldDefinition:
    key: str
    label: str
    usage: tuple[PublicDataUsage, ...]
    semantics: str


@dataclass(frozen=True)
class PublicFieldObservation:
    present: bool
    provenance: PublicFieldProvenance = PublicFieldProvenance.NOT_FOUND
    note: str = ""


@dataclass(frozen=True)
class PublicFieldCoverage:
    key: str
    label: str
    present_count: int
    applicable_count: int
    coverage_pct: float
    provenance: tuple[PublicFieldProvenance, ...]
    usage: tuple[PublicDataUsage, ...]
    semantics: str


@dataclass(frozen=True)
class PublicSourceContractField:
    key: str
    label: str
    group: str
    source_keys: tuple[str, ...]
    parser_status: str
    caveat: str
    derivable_from: tuple[str, ...] = ()
    partial_keys: tuple[str, ...] = ()
    derived_semantic: bool = False
    unstable_when_present: bool = False


@dataclass(frozen=True)
class PublicSourceContractCoverage:
    key: str
    label: str
    group: str
    classification: PublicSourceCoverageClassification
    source_evidence: str
    parser_status: str
    caveat: str


@dataclass(frozen=True)
class PublicWorkloadAssessment:
    workload: str
    suitability: PublicWorkloadSuitability
    reasoning: str


@dataclass(frozen=True)
class PublicSourceContract:
    source: PublicPageSource
    sample_count: int
    coverage: tuple[PublicSourceContractCoverage, ...]
    workloads: tuple[PublicWorkloadAssessment, ...]
    architecture_decision: PublicArchitectureDecision
    critical_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublicPolicyCheck:
    source: PublicPageSource
    robots_url: str
    http_status: int | None
    content_type: str | None
    byte_size: int
    checked_path: str
    path_disallowed: bool | None
    relevant_rules: tuple[str, ...]
    content_signals: tuple[str, ...]
    terms_url: str | None = None
    terms_http_status: int | None = None
    terms_note: str = ""


@dataclass(frozen=True)
class PublicHttpResponse:
    url: str
    status_code: int | None
    content_type: str | None
    body: bytes
    error: str | None = None

    @property
    def text(self) -> str:
        return _decode_body(self.body)


@dataclass(frozen=True)
class PublicPageAnalysis:
    source: PublicPageSource
    match_id: str | None
    url: str
    http_status: int | None
    content_type: str | None
    byte_size: int
    access_status: PublicPageAccessStatus
    static_html_findings: tuple[str, ...]
    embedded_state_findings: tuple[str, ...]
    referenced_resource_findings: tuple[str, ...]
    observations: Mapping[str, PublicFieldObservation]


@dataclass(frozen=True)
class PublicPageProbeResult:
    source: PublicPageSource
    probe_started_at: datetime
    request_count: int
    policy: PublicPolicyCheck
    analyses: tuple[PublicPageAnalysis, ...]
    coverage: tuple[PublicFieldCoverage, ...]
    recommendation: PublicSourceRecommendation
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublicPageSemanticExtraction:
    embedded_states: tuple[object, ...]
    referenced_states: tuple[object, ...]
    parse_findings: tuple[str, ...]
    semantics: PublicMatchSemantics | None
    fingerprint: PublicMatchSemanticFingerprint | None
    provenance: PublicFieldProvenance

    @property
    def decoded_states(self) -> tuple[object, ...]:
        return self.embedded_states + self.referenced_states


class _Response(Protocol):
    def read(self) -> bytes:
        ...

    def __enter__(self) -> "_Response":
        ...

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> object:
        ...


class _UrlOpen(Protocol):
    def __call__(
        self,
        request: Request,
        data: object | None = None,
        timeout: float = 10.0,
    ) -> _Response:
        ...


DEFAULT_URL_OPEN: _UrlOpen = cast(_UrlOpen, urlopen)


PUBLIC_FIELD_DEFINITIONS: tuple[PublicFieldDefinition, ...] = (
    PublicFieldDefinition(
        "stable_match_id",
        "stable match ID",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "Valve/source match ID from visible page data or public page state.",
    ),
    PublicFieldDefinition(
        "start_timestamp",
        "start timestamp",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "Explicit source start timestamp.",
    ),
    PublicFieldDefinition(
        "end_timestamp",
        "end timestamp",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "Explicit source end timestamp; no patch/date inference.",
    ),
    PublicFieldDefinition(
        "duration",
        "duration",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game map duration, not a target-map POST_DRAFT input.",
    ),
    PublicFieldDefinition(
        "winner_side",
        "winner / winning side",
        (PublicDataUsage.POST_GAME_TARGET_OR_LABEL,),
        "Post-game winner, not a target-map POST_DRAFT input.",
    ),
    PublicFieldDefinition(
        "radiant_dire_orientation",
        "Radiant/Dire orientation",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "Radiant and Dire team/player orientation.",
    ),
    PublicFieldDefinition(
        "league_event",
        "league/event",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "League, tournament, or event name/ID.",
    ),
    PublicFieldDefinition(
        "series_context",
        "series context",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "Series ID/name/type, map number, or best-of context.",
    ),
    PublicFieldDefinition(
        "patch_id",
        "patch/game version ID",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "Explicit source patch or game version only; no date inference.",
    ),
    PublicFieldDefinition(
        "team_ids",
        "team IDs",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "Source-stable Radiant and Dire team IDs.",
    ),
    PublicFieldDefinition(
        "team_display_names",
        "team display names",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "Visible or public-state team names.",
    ),
    PublicFieldDefinition(
        "player_account_ids",
        "player account IDs",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "Stable account/player IDs for all ten players.",
    ),
    PublicFieldDefinition(
        "player_sides",
        "player sides",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "Radiant/Dire side for all ten players.",
    ),
    PublicFieldDefinition(
        "player_hero_ids",
        "player hero IDs",
        (PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Hero IDs for all ten players.",
    ),
    PublicFieldDefinition(
        "player_display_names",
        "player display names",
        (PublicDataUsage.IDENTITY_OR_CONTEXT,),
        "Player display names, informational only.",
    ),
    PublicFieldDefinition(
        "radiant_picks",
        "5 Radiant picks",
        (PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Five Radiant hero picks.",
    ),
    PublicFieldDefinition(
        "dire_picks",
        "5 Dire picks",
        (PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Five Dire hero picks.",
    ),
    PublicFieldDefinition(
        "complete_5v5_picks",
        "complete 5v5 picks",
        (PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Exactly five unique picks per side.",
    ),
    PublicFieldDefinition(
        "bans",
        "bans",
        (PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Ban actions with hero IDs.",
    ),
    PublicFieldDefinition(
        "ordered_draft_actions",
        "ordered draft actions",
        (PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Explicit source action order only; visual display order is insufficient.",
    ),
    PublicFieldDefinition(
        "draft_action_order",
        "draft action order",
        (PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Explicit order field on draft actions.",
    ),
    PublicFieldDefinition(
        "draft_action_kind",
        "draft action kind",
        (PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Explicit pick/ban semantics.",
    ),
    PublicFieldDefinition(
        "draft_action_side",
        "draft action side",
        (PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Radiant/Dire action side.",
    ),
    PublicFieldDefinition(
        "draft_action_hero_id",
        "draft action hero ID",
        (PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Hero ID on draft actions.",
    ),
    PublicFieldDefinition(
        "first_pick_side",
        "first-pick side",
        (PublicDataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Derived only from explicit ordered pick actions.",
    ),
    PublicFieldDefinition(
        "team_kills_final_score",
        "team kills / final score",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game team kills/final score.",
    ),
    PublicFieldDefinition(
        "individual_kills",
        "individual kills",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game player kills.",
    ),
    PublicFieldDefinition(
        "deaths",
        "deaths",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game player deaths.",
    ),
    PublicFieldDefinition(
        "assists",
        "assists",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game player assists.",
    ),
    PublicFieldDefinition(
        "final_net_worth",
        "final net worth",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game final net worth.",
    ),
    PublicFieldDefinition(
        "last_hits",
        "last hits",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game last hits.",
    ),
    PublicFieldDefinition(
        "denies",
        "denies",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game denies.",
    ),
    PublicFieldDefinition(
        "gpm",
        "GPM",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game gold per minute.",
    ),
    PublicFieldDefinition(
        "xpm",
        "XPM",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game experience per minute.",
    ),
    PublicFieldDefinition(
        "level",
        "level",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game player level.",
    ),
    PublicFieldDefinition(
        "damage",
        "damage/building/heal",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Hero damage, building damage, and healing.",
    ),
    PublicFieldDefinition(
        "final_items",
        "final inventory/items",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Final item inventory/backpack/neutral items.",
    ),
    PublicFieldDefinition(
        "timed_item_data",
        "timed item data",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Item acquisition timing or build order, distinct from final inventory.",
    ),
    PublicFieldDefinition(
        "kill_events",
        "kill events",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Combat timeline events with timestamps.",
    ),
    PublicFieldDefinition(
        "minute_farm_timeline",
        "minute farm/economy timeline",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Minute-by-minute farm or economy data.",
    ),
    PublicFieldDefinition(
        "gold_advantage_timeline",
        "gold advantage timeline",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Radiant/Dire gold or net-worth advantage points.",
    ),
    PublicFieldDefinition(
        "xp_advantage_timeline",
        "XP advantage timeline",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Radiant/Dire XP advantage points.",
    ),
    PublicFieldDefinition(
        "advantage_timeline",
        "advantage timeline",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Net-worth, XP, or gold advantage by time.",
    ),
    PublicFieldDefinition(
        "tower_barracks_objectives",
        "tower/barracks objectives",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Tower and barracks state or destruction events.",
    ),
    PublicFieldDefinition(
        "roshan_objectives",
        "Roshan objectives",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Roshan events and timestamps.",
    ),
    PublicFieldDefinition(
        "tormentor_objectives",
        "Tormentor objectives",
        (
            PublicDataUsage.POST_GAME_TARGET_OR_LABEL,
            PublicDataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Tormentor events and timestamps.",
    ),
)


PUBLIC_SOURCE_CONTRACT_FIELDS: tuple[PublicSourceContractField, ...] = (
    PublicSourceContractField(
        key="valve_match_id",
        label="Valve/source match ID",
        group="Match identity and result",
        source_keys=("stable_match_id",),
        parser_status="normalized as field coverage",
        caveat="Provider identity is treated as source-local until ingestion links it.",
    ),
    PublicSourceContractField(
        key="start_time",
        label="start time / timestamp",
        group="Match identity and result",
        source_keys=("start_timestamp",),
        derivable_from=("end_timestamp", "duration"),
        parser_status="direct timestamp checked; derivation not normalized yet",
        caveat="When direct start time is absent, start is derivable from end minus duration.",
    ),
    PublicSourceContractField(
        key="duration",
        label="duration",
        group="Match identity and result",
        source_keys=("duration",),
        parser_status="normalized as field coverage",
        caveat="Post-game duration is label/context only, not a target POST_DRAFT input.",
    ),
    PublicSourceContractField(
        key="radiant_dire_identity",
        label="Radiant/Dire identity",
        group="Match identity and result",
        source_keys=("radiant_dire_orientation",),
        parser_status="normalized as side/orientation coverage",
        caveat="Side orientation is source-local and must be preserved through ingestion.",
    ),
    PublicSourceContractField(
        key="team_identity",
        label="team identity",
        group="Match identity and result",
        source_keys=("team_ids", "team_display_names"),
        parser_status="normalized as IDs and display-name coverage",
        caveat="Team IDs are STRATZ-local; no cross-provider equivalence is assumed.",
    ),
    PublicSourceContractField(
        key="winner_result",
        label="winner/result",
        group="Match identity and result",
        source_keys=("winner_side",),
        parser_status="normalized as field coverage",
        caveat="Winner is a post-game label, never a target-map feature.",
    ),
    PublicSourceContractField(
        key="league_competition_identity",
        label="league or competition identity",
        group="Match identity and result",
        source_keys=("league_event",),
        partial_keys=("series_context",),
        parser_status="checked separately from generic series context",
        caveat="The EWC sample proved series context, not direct league/event coverage.",
    ),
    PublicSourceContractField(
        key="series_identity",
        label="series identity",
        group="Match identity and result",
        source_keys=("series_context",),
        parser_status="presence-only coverage; not split into normalized series fields",
        caveat="A production mapper must extract explicit series ID/type fields.",
    ),
    PublicSourceContractField(
        key="game_map_number",
        label="game/map number",
        group="Match identity and result",
        source_keys=("series_context",),
        parser_status="presence-only coverage under series context",
        caveat="Map number is not yet normalized as a standalone parser field.",
    ),
    PublicSourceContractField(
        key="hero_picks",
        label="hero picks",
        group="Draft",
        source_keys=("complete_5v5_picks",),
        parser_status="derived from per-player hero and side fields",
        caveat="Current parser derives complete side picks, not visual draft order.",
        derived_semantic=True,
    ),
    PublicSourceContractField(
        key="pick_order",
        label="pick order",
        group="Draft",
        source_keys=("ordered_draft_actions",),
        partial_keys=(
            "draft_action_order",
            "draft_action_side",
            "draft_action_hero_id",
            "complete_5v5_picks",
        ),
        parser_status="requires explicit action order and pick/ban kind",
        caveat="The parser does not promote display order to semantic draft order.",
    ),
    PublicSourceContractField(
        key="bans",
        label="bans",
        group="Draft",
        source_keys=("bans",),
        parser_status="normalized as field coverage",
        caveat="Ban presence is separate from complete ordered draft sequence support.",
    ),
    PublicSourceContractField(
        key="ban_order",
        label="ban order",
        group="Draft",
        source_keys=("ordered_draft_actions",),
        partial_keys=("bans", "draft_action_order", "draft_action_side"),
        parser_status="requires complete ordered draft action semantics",
        caveat="Order alone is insufficient without stable pick/ban kind semantics.",
    ),
    PublicSourceContractField(
        key="pick_side_ownership",
        label="team/side ownership of picks",
        group="Draft",
        source_keys=("radiant_picks", "dire_picks", "player_sides"),
        parser_status="normalized as side pick coverage",
        caveat="Ownership is Radiant/Dire; organization/team mapping remains separate.",
    ),
    PublicSourceContractField(
        key="ban_side_ownership",
        label="team/side ownership of bans",
        group="Draft",
        source_keys=("draft_action_side", "bans"),
        parser_status="checked from draft action side and ban evidence",
        caveat="Full support depends on reliable pick/ban action semantics.",
    ),
    PublicSourceContractField(
        key="complete_draft_sequence",
        label="complete draft sequence",
        group="Draft",
        source_keys=("ordered_draft_actions",),
        partial_keys=("draft_action_order", "draft_action_side", "draft_action_hero_id"),
        parser_status="not normalized unless all action semantics are explicit",
        caveat="The EWC sample did not prove complete ordered pick/ban semantics.",
    ),
    PublicSourceContractField(
        key="captain_drafter_identity",
        label="captain/drafter identity",
        group="Draft",
        source_keys=(),
        parser_status="not implemented by current parser",
        caveat="No captain or drafter identity evidence was proven.",
    ),
    PublicSourceContractField(
        key="player_account_identity",
        label="player account/Steam identity",
        group="Player and roster state",
        source_keys=("player_account_ids",),
        parser_status="normalized as all-ten-player ID coverage",
        caveat="Display names are informational; stable account IDs are the anchor.",
    ),
    PublicSourceContractField(
        key="player_display_identity",
        label="player display identity",
        group="Player and roster state",
        source_keys=("player_display_names",),
        parser_status="checked separately from account IDs",
        caveat="The EWC sample did not expose display names in parsed page state.",
    ),
    PublicSourceContractField(
        key="player_team_association",
        label="player-to-team association",
        group="Player and roster state",
        source_keys=("player_sides", "team_ids"),
        parser_status="derived through side plus team identity coverage",
        caveat="Current contract maps players to sides, then sides to source-local teams.",
        derived_semantic=True,
    ),
    PublicSourceContractField(
        key="player_side_association",
        label="player-to-side association",
        group="Player and roster state",
        source_keys=("player_sides",),
        parser_status="normalized as all-ten-player side coverage",
        caveat="Side association is stable enough for Radiant/Dire draft mapping.",
    ),
    PublicSourceContractField(
        key="hero_per_player",
        label="hero per player",
        group="Player and roster state",
        source_keys=("player_hero_ids",),
        parser_status="normalized as all-ten-player hero coverage",
        caveat="Hero IDs remain in the STRATZ/Valve namespace.",
    ),
    PublicSourceContractField(
        key="final_kda",
        label="final K/D/A",
        group="Player and roster state",
        source_keys=("individual_kills", "deaths", "assists"),
        parser_status="normalized as final player stat coverage",
        caveat="KDA is a post-game value.",
    ),
    PublicSourceContractField(
        key="player_slot_position",
        label="player slot/position semantics",
        group="Player and roster state",
        source_keys=(),
        partial_keys=(),
        parser_status="not normalized as lane/role/position coverage",
        caveat="Radiant/Dire side support does not prove player role or lane semantics.",
    ),
    PublicSourceContractField(
        key="stable_roster_identity",
        label="stable roster identity",
        group="Player and roster state",
        source_keys=(),
        derivable_from=("player_account_ids", "player_sides", "team_ids"),
        parser_status="not a source roster ID; derivable as a lineup fingerprint",
        caveat="A lineup fingerprint is not the same as provider roster versioning.",
    ),
    PublicSourceContractField(
        key="substitutes_standins",
        label="substitutes/stand-ins",
        group="Player and roster state",
        source_keys=(),
        parser_status="not implemented by current parser",
        caveat="No explicit substitute or stand-in evidence was proven.",
    ),
    PublicSourceContractField(
        key="final_net_worth",
        label="final net worth",
        group="Economy and item state",
        source_keys=("final_net_worth",),
        parser_status="normalized as final player stat coverage",
        caveat="Final economy is post-game context only.",
    ),
    PublicSourceContractField(
        key="gold",
        label="gold",
        group="Economy and item state",
        source_keys=(),
        partial_keys=("gpm", "final_net_worth"),
        parser_status="not normalized as raw player gold",
        caveat="GPM and net worth are related but do not equal raw gold state.",
    ),
    PublicSourceContractField(
        key="xp",
        label="XP",
        group="Economy and item state",
        source_keys=(),
        partial_keys=("xpm", "level"),
        parser_status="not normalized as raw player XP",
        caveat="XPM and level are related but do not equal raw XP state.",
    ),
    PublicSourceContractField(
        key="gpm_xpm",
        label="GPM/XPM",
        group="Economy and item state",
        source_keys=("gpm", "xpm"),
        parser_status="normalized as final player stat coverage",
        caveat="These are post-game summary rates.",
    ),
    PublicSourceContractField(
        key="item_inventory",
        label="item inventory",
        group="Economy and item state",
        source_keys=("final_items",),
        parser_status="normalized as final inventory coverage",
        caveat="Final inventory is not item timing.",
    ),
    PublicSourceContractField(
        key="neutral_items",
        label="neutral items",
        group="Economy and item state",
        source_keys=("final_items",),
        parser_status="covered through final inventory item slots",
        caveat="Neutral item timing was not proven.",
    ),
    PublicSourceContractField(
        key="item_purchase_timing",
        label="item purchase/timing history",
        group="Economy and item state",
        source_keys=("timed_item_data",),
        parser_status="checked separately from final inventory",
        caveat="The EWC sample did not prove timed item acquisition rows.",
    ),
    PublicSourceContractField(
        key="item_state_over_time",
        label="item state over time",
        group="Economy and item state",
        source_keys=("timed_item_data",),
        parser_status="not normalized beyond timed item evidence",
        caveat="No full inventory-over-time state was proven.",
    ),
    PublicSourceContractField(
        key="buybacks",
        label="buybacks",
        group="Economy and item state",
        source_keys=(),
        parser_status="not implemented by current parser",
        caveat="No buyback event/count evidence was proven.",
    ),
    PublicSourceContractField(
        key="gold_advantage_progression",
        label="Radiant/Dire gold advantage progression",
        group="Match trajectory",
        source_keys=("gold_advantage_timeline",),
        parser_status="gold/net-worth advantage array coverage",
        caveat="Production ingestion must still preserve point-level time semantics.",
    ),
    PublicSourceContractField(
        key="xp_advantage_progression",
        label="XP advantage progression",
        group="Match trajectory",
        source_keys=("xp_advantage_timeline",),
        parser_status="XP advantage array coverage",
        caveat="Production ingestion must still preserve point-level time semantics.",
    ),
    PublicSourceContractField(
        key="time_series_resolution",
        label="time-series timestamps/resolution",
        group="Match trajectory",
        source_keys=("advantage_timeline",),
        parser_status="presence-only; no normalized timestamp or interval metadata",
        caveat="Resolution must be validated before stable ingestion.",
        unstable_when_present=True,
    ),
    PublicSourceContractField(
        key="kill_progression",
        label="kill progression",
        group="Match trajectory",
        source_keys=("kill_events",),
        parser_status="requires timed kill event rows",
        caveat="Final kills are supported, but kill timeline was not proven.",
    ),
    PublicSourceContractField(
        key="player_death_events",
        label="player death events",
        group="Match trajectory",
        source_keys=("kill_events",),
        parser_status="requires timed combat event rows",
        caveat="Final deaths are supported, but death events were not proven.",
    ),
    PublicSourceContractField(
        key="objective_progression",
        label="objective progression",
        group="Match trajectory",
        source_keys=("tower_barracks_objectives", "roshan_objectives"),
        partial_keys=("tower_barracks_objectives", "roshan_objectives"),
        parser_status="requires objective state or timed objective rows",
        caveat="The EWC sample did not prove objective progression.",
    ),
    PublicSourceContractField(
        key="tower_building_state",
        label="tower/building state",
        group="Match trajectory",
        source_keys=("tower_barracks_objectives",),
        parser_status="checked as tower/barracks objective coverage",
        caveat="Final or timed building state was not proven in the EWC sample.",
    ),
    PublicSourceContractField(
        key="tower_destruction_timing",
        label="tower destruction timing",
        group="Match trajectory",
        source_keys=("tower_barracks_objectives",),
        parser_status="requires timed building events",
        caveat="No tower destruction timestamps were proven.",
    ),
    PublicSourceContractField(
        key="barracks_state_timing",
        label="barracks state/timing",
        group="Match trajectory",
        source_keys=("tower_barracks_objectives",),
        parser_status="requires barracks state or timed building events",
        caveat="No barracks state/timing evidence was proven.",
    ),
    PublicSourceContractField(
        key="roshan_events",
        label="Roshan events/timing",
        group="Match trajectory",
        source_keys=("roshan_objectives",),
        parser_status="requires timed Roshan event rows",
        caveat="No Roshan event timing was proven.",
    ),
    PublicSourceContractField(
        key="rune_events",
        label="rune events",
        group="Match trajectory",
        source_keys=(),
        parser_status="not implemented by current parser",
        caveat="No rune event evidence was proven.",
    ),
    PublicSourceContractField(
        key="teamfight_timeline",
        label="teamfight/event timeline",
        group="Match trajectory",
        source_keys=("kill_events",),
        parser_status="requires timed combat event rows",
        caveat="No dedicated teamfight timeline was proven.",
    ),
    PublicSourceContractField(
        key="patch_version",
        label="patch/game version",
        group="Additional rich statistics",
        source_keys=("patch_id",),
        parser_status="normalized as explicit patch/version coverage",
        caveat="Patch is accepted only when source-provided, never inferred by date.",
    ),
    PublicSourceContractField(
        key="final_damage_stats",
        label="final damage/healing/building stats",
        group="Additional rich statistics",
        source_keys=("damage",),
        parser_status="normalized as final player stat coverage",
        caveat="These are post-game summary values.",
    ),
    PublicSourceContractField(
        key="final_farm_stats",
        label="last hits and denies",
        group="Additional rich statistics",
        source_keys=("last_hits", "denies"),
        parser_status="normalized as final player stat coverage",
        caveat="These are post-game summary values.",
    ),
)


class PublicPageHttpClient:
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        user_agent: str = PUBLIC_PAGE_USER_AGENT,
        urlopen_func: _UrlOpen = DEFAULT_URL_OPEN,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        self.timeout = timeout
        self.user_agent = user_agent
        self.urlopen_func = urlopen_func

    def fetch(self, url: str) -> PublicHttpResponse:
        request = Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "User-Agent": self.user_agent,
            },
            method="GET",
        )
        try:
            with self.urlopen_func(request, timeout=self.timeout) as response:
                body = response.read()
                status_code = getattr(response, "status", None)
                headers = getattr(response, "headers", {})
                content_type = _header_value(headers, "Content-Type")
                return PublicHttpResponse(
                    url=url,
                    status_code=int(status_code) if status_code is not None else 200,
                    content_type=content_type,
                    body=body,
                )
        except HTTPError as exc:
            body = exc.read()
            return PublicHttpResponse(
                url=url,
                status_code=exc.code,
                content_type=exc.headers.get("Content-Type"),
                body=body,
                error=str(exc),
            )
        except (TimeoutError, URLError, OSError) as exc:
            return PublicHttpResponse(
                url=url,
                status_code=None,
                content_type=None,
                body=b"",
                error=str(exc),
            )


class PublicMatchPageProbe:
    def __init__(
        self,
        client: PublicPageHttpClient,
        *,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.sleep_func = sleep_func

    def run(
        self,
        *,
        source: PublicPageSource = PublicPageSource.STRATZ,
        match_ids: Sequence[str],
        page_urls: Sequence[str] = (),
        delay_seconds: float = 1.0,
        fetch_referenced_resources: bool = True,
    ) -> PublicPageProbeResult:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        targets = _probe_targets(source=source, match_ids=match_ids, page_urls=page_urls)
        if not targets:
            raise ValueError("At least one --match-id or --page-url is required.")

        probe_started_at = datetime.now(timezone.utc)
        request_count = 0
        first_path = urlparse(targets[0][1]).path or "/"
        policy = check_public_page_policy(
            client=self.client,
            source=source,
            sample_path=first_path,
        )
        request_count += 1

        analyses: list[PublicPageAnalysis] = []
        warnings: list[str] = []
        if policy.path_disallowed is True:
            for match_id, url in targets:
                analyses.append(
                    _empty_analysis(
                        source=source,
                        match_id=match_id,
                        url=url,
                        access_status=PublicPageAccessStatus.ROBOTS_PATH_DISALLOWED,
                        finding="Robots policy disallows this public page path.",
                    )
                )
        else:
            for index, (match_id, url) in enumerate(targets):
                response = self.client.fetch(url)
                request_count += 1
                analysis, resource_requests = analyze_public_match_page_response(
                    source=source,
                    match_id=match_id,
                    url=url,
                    response=response,
                    client=self.client,
                    fetch_referenced_resources=fetch_referenced_resources,
                )
                request_count += resource_requests
                analyses.append(analysis)
                if delay_seconds and index < len(targets) - 1:
                    self.sleep_func(delay_seconds)

        if policy.path_disallowed is None:
            warnings.append(
                "Robots policy could not be fully evaluated from the HTTP response."
            )

        coverage = aggregate_public_field_coverage(analyses)
        recommendation = determine_public_source_recommendation(analyses, coverage)
        return PublicPageProbeResult(
            source=source,
            probe_started_at=probe_started_at,
            request_count=request_count,
            policy=policy,
            analyses=tuple(analyses),
            coverage=coverage,
            recommendation=recommendation,
            warnings=tuple(warnings),
        )


def build_public_match_url(source: PublicPageSource | str, match_id: str) -> str:
    parsed_source = PublicPageSource(source)
    safe_match_id = str(match_id).strip().strip("/")
    if not safe_match_id:
        raise ValueError("match_id must not be empty")
    if parsed_source is PublicPageSource.STRATZ:
        return f"{STRATZ_PUBLIC_BASE_URL}/match/{safe_match_id}"
    raise ValueError("Sofascore page URLs are not derivable from Valve match IDs.")


def check_public_page_policy(
    *,
    client: PublicPageHttpClient,
    source: PublicPageSource,
    sample_path: str,
) -> PublicPolicyCheck:
    robots_url = _robots_url(source)
    response = client.fetch(robots_url)
    text = response.text
    content_signals = tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip().casefold().startswith("content-signal:")
    )
    path_disallowed: bool | None = None
    relevant_rules: tuple[str, ...] = ()
    if response.status_code == 200:
        path_disallowed, relevant_rules = evaluate_robots_path(
            text,
            user_agent="*",
            path=sample_path,
        )
    return PublicPolicyCheck(
        source=source,
        robots_url=robots_url,
        http_status=response.status_code,
        content_type=response.content_type,
        byte_size=len(response.body),
        checked_path=sample_path,
        path_disallowed=path_disallowed,
        relevant_rules=relevant_rules,
        content_signals=content_signals,
        terms_note="No clearly linked terms page was fetched by the probe.",
    )


def evaluate_robots_path(
    robots_text: str,
    *,
    user_agent: str,
    path: str,
) -> tuple[bool, tuple[str, ...]]:
    groups = _parse_robots_groups(robots_text)
    applicable: list[tuple[str, str]] = []
    normalized_agent = user_agent.casefold()
    for agents, rules in groups:
        if not any(agent == "*" or agent in normalized_agent for agent in agents):
            continue
        applicable.extend(rules)
    matches: list[tuple[int, str, str]] = []
    for directive, pattern in applicable:
        if directive == "disallow" and pattern == "":
            continue
        if _robots_rule_matches(pattern, path):
            matches.append((len(pattern.replace("*", "")), directive, pattern))
    if not matches:
        return False, ()
    matches.sort(key=lambda item: (item[0], item[1] == "allow"), reverse=True)
    best_length = matches[0][0]
    best = [item for item in matches if item[0] == best_length]
    allowed = any(directive == "allow" for _, directive, _ in best)
    relevant = tuple(f"{directive.title()}: {pattern}" for _, directive, pattern in best)
    return not allowed, relevant


def analyze_public_match_page_response(
    *,
    source: PublicPageSource,
    match_id: str | None,
    url: str,
    response: PublicHttpResponse,
    client: PublicPageHttpClient | None = None,
    fetch_referenced_resources: bool = False,
) -> tuple[PublicPageAnalysis, int]:
    status = _access_status_from_response(response)
    if status is not PublicPageAccessStatus.PUBLIC_PAGE_AVAILABLE:
        return (
            _empty_analysis(
                source=source,
                match_id=match_id,
                url=url,
                access_status=status,
                http_status=response.status_code,
                content_type=response.content_type,
                byte_size=len(response.body),
                finding=_access_finding(status, response),
            ),
            0,
        )

    html = response.text
    observations: dict[str, PublicFieldObservation] = {}
    static_findings: list[str] = []
    embedded_findings: list[str] = []
    referenced_findings: list[str] = []
    static_values = extract_visible_html_values(html)
    if static_values:
        static_findings.append(f"visible data fields: {len(static_values)}")
        _merge_observations(
            observations,
            observations_from_flat_values(
                static_values,
                PublicFieldProvenance.VISIBLE_HTML,
            ),
        )
    else:
        static_findings.append("no visible data-field values found")

    referenced_states, referenced_findings, resource_requests = (
        _fetch_public_referenced_resource_states(
            html=html,
            page_url=url,
            client=client,
            fetch_referenced_resources=fetch_referenced_resources,
        )
    )
    page_semantics = extract_public_match_semantics_from_page(
        html=html,
        referenced_states=referenced_states,
        requested_match_id=match_id,
    )
    embedded_findings = list(page_semantics.parse_findings)
    if page_semantics.semantics is not None:
        _merge_observations(
            observations,
            observations_from_public_match_semantics(
                page_semantics.semantics,
                list(page_semantics.decoded_states),
                page_semantics.provenance,
            ),
        )

    if not referenced_findings:
        referenced_findings.append("no public JSON/page-data resources fetched")

    if not any(observation.present for observation in observations.values()):
        status = PublicPageAccessStatus.PAGE_AVAILABLE_DATA_NOT_STATIC

    return (
        PublicPageAnalysis(
            source=source,
            match_id=match_id,
            url=url,
            http_status=response.status_code,
            content_type=response.content_type,
            byte_size=len(response.body),
            access_status=status,
            static_html_findings=tuple(static_findings),
            embedded_state_findings=tuple(embedded_findings),
            referenced_resource_findings=tuple(referenced_findings),
            observations=observations,
        ),
        resource_requests,
    )


def extract_visible_html_values(html: str) -> Mapping[str, tuple[str, ...]]:
    parser = _VisibleDataParser()
    parser.feed(html)
    return {key: tuple(values) for key, values in parser.values.items()}


def extract_embedded_public_states(
    html: str,
) -> tuple[tuple[object, ...], list[str]]:
    parser = _ScriptParser()
    parser.feed(html)
    states: list[object] = []
    findings: list[str] = []
    next_flight_chunks: list[str] = []
    for script in parser.scripts:
        content = script.content.strip()
        if not content:
            continue
        parsed: object | None = None
        if script.script_id == "__NEXT_DATA__" or "json" in script.script_type:
            try:
                parsed = json.loads(unescape(content))
            except json.JSONDecodeError:
                findings.append(
                    f"malformed embedded JSON in script {script.script_id or '<inline>'}"
                )
        elif "self.__next_f.push" in content:
            next_flight_chunks.extend(_next_flight_chunk_strings(content))
            continue
        else:
            parsed = _parse_assignment_json(content)
            if parsed is None and _looks_like_page_state_script(content):
                findings.append("script contains page-state markers but no JSON parsed")
        if parsed is not None:
            states.append(parsed)
            label = script.script_id or "inline script"
            findings.append(f"embedded public state parsed from {label}")
    if next_flight_chunks:
        next_states = _parse_next_flight_states(next_flight_chunks)
        if next_states:
            states.extend(next_states)
            findings.append("embedded public state parsed from Next flight stream")
        else:
            findings.append("Next flight stream found but no JSON state parsed")
    if not findings:
        findings.append("no embedded public state found")
    return tuple(states), findings


def extract_public_match_semantics_from_page(
    *,
    html: str,
    referenced_states: Sequence[object] = (),
    requested_match_id: str | None = None,
) -> PublicPageSemanticExtraction:
    embedded_states, findings = extract_embedded_public_states(html)
    referenced = tuple(referenced_states)
    decoded_states = embedded_states + referenced
    if not decoded_states:
        return PublicPageSemanticExtraction(
            embedded_states=embedded_states,
            referenced_states=referenced,
            parse_findings=tuple(findings),
            semantics=None,
            fingerprint=None,
            provenance=PublicFieldProvenance.NOT_FOUND,
        )
    semantics = extract_public_match_semantics_from_roots(
        decoded_states,
        requested_match_id=requested_match_id,
    )
    provenance = (
        PublicFieldProvenance.EMBEDDED_PUBLIC_PAGE_STATE
        if embedded_states
        else PublicFieldProvenance.PUBLIC_PAGE_REFERENCED_RESOURCE
    )
    return PublicPageSemanticExtraction(
        embedded_states=embedded_states,
        referenced_states=referenced,
        parse_findings=tuple(findings),
        semantics=semantics,
        fingerprint=public_match_semantic_fingerprint(semantics),
        provenance=provenance,
    )


def extract_public_referenced_resource_urls(html: str, page_url: str) -> tuple[str, ...]:
    parser = _ResourceLinkParser()
    parser.feed(html)
    base = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    urls: list[str] = []
    for value in parser.urls:
        absolute = urljoin(page_url, value)
        parsed = urlparse(absolute)
        if f"{parsed.scheme}://{parsed.netloc}" != base:
            continue
        lowered_path = parsed.path.casefold()
        if lowered_path.endswith(".json") or "/_next/data/" in lowered_path:
            urls.append(absolute)
    return tuple(dict.fromkeys(urls))


def _fetch_public_referenced_resource_states(
    *,
    html: str,
    page_url: str,
    client: PublicPageHttpClient | None,
    fetch_referenced_resources: bool,
) -> tuple[tuple[object, ...], list[str], int]:
    referenced_findings: list[str] = []
    if not fetch_referenced_resources or client is None:
        return (), referenced_findings, 0

    resource_requests = 0
    states: list[object] = []
    for resource_url in extract_public_referenced_resource_urls(html, page_url)[:3]:
        resource_response = client.fetch(resource_url)
        resource_requests += 1
        if resource_response.status_code != 200:
            referenced_findings.append(
                f"{resource_url}: HTTP {resource_response.status_code}"
            )
            continue
        resource_state = _json_from_response(resource_response)
        if resource_state is None:
            referenced_findings.append(f"{resource_url}: no JSON payload parsed")
            continue
        referenced_findings.append(f"{resource_url}: public referenced JSON parsed")
        states.append(resource_state)
    return tuple(states), referenced_findings, resource_requests


def observations_from_flat_values(
    values: Mapping[str, Sequence[str]],
    provenance: PublicFieldProvenance,
) -> Mapping[str, PublicFieldObservation]:
    normalized = {
        _normalize_field_name(key): tuple(item for item in raw_values if item)
        for key, raw_values in values.items()
    }
    observations: dict[str, PublicFieldObservation] = {}

    def has_any(*keys: str) -> bool:
        return any(normalized.get(key) for key in keys)

    _set_if(observations, "stable_match_id", has_any("match_id"), provenance)
    _set_if(observations, "start_timestamp", has_any("start_timestamp"), provenance)
    _set_if(observations, "end_timestamp", has_any("end_timestamp"), provenance)
    _set_if(observations, "duration", has_any("duration"), provenance)
    _set_if(observations, "winner_side", has_any("winner_side"), provenance)
    _set_if(observations, "league_event", has_any("league_event"), provenance)
    _set_if(observations, "series_context", has_any("series_context"), provenance)
    _set_if(observations, "patch_id", has_any("patch_id"), provenance)
    _set_if(
        observations,
        "team_ids",
        has_any("radiant_team_id") and has_any("dire_team_id"),
        provenance,
    )
    _set_if(
        observations,
        "team_display_names",
        has_any("radiant_team_name") and has_any("dire_team_name"),
        provenance,
    )
    radiant_picks = tuple(_unique_ints(normalized.get("radiant_pick", ())))
    dire_picks = tuple(_unique_ints(normalized.get("dire_pick", ())))
    bans = tuple(_unique_ints(normalized.get("ban", ())))
    _set_if(observations, "radiant_picks", len(radiant_picks) == 5, provenance)
    _set_if(observations, "dire_picks", len(dire_picks) == 5, provenance)
    _set_if(
        observations,
        "complete_5v5_picks",
        len(radiant_picks) == 5 and len(dire_picks) == 5,
        PublicFieldProvenance.DERIVED_FROM_PUBLIC_FIELDS,
    )
    _set_if(observations, "bans", bool(bans), provenance)
    return observations


def observations_from_public_state(
    state: object,
    provenance: PublicFieldProvenance,
) -> Mapping[str, PublicFieldObservation]:
    semantics = extract_public_match_semantics(state)
    return observations_from_public_match_semantics(semantics, state, provenance)


def observations_from_public_match_semantics(
    semantics: PublicMatchSemantics,
    state: object,
    provenance: PublicFieldProvenance,
) -> Mapping[str, PublicFieldObservation]:
    raw_players = find_public_mapping_list(
        state,
        ("playerMatches", "players", "lineups"),
    )
    observations: dict[str, PublicFieldObservation] = {}
    _set_if(observations, "stable_match_id", semantics.match_id is not None, provenance)
    _set_if(observations, "start_timestamp", semantics.started_at_value is not None, provenance)
    _set_if(observations, "end_timestamp", semantics.ended_at_value is not None, provenance)
    _set_if(observations, "duration", semantics.duration_seconds is not None, provenance)
    _set_if(observations, "winner_side", semantics.did_radiant_win is not None, provenance)
    _set_if(observations, "radiant_dire_orientation", semantics.player_status is SemanticEvidenceStatus.USABLE or semantics.team_identity_status is SemanticEvidenceStatus.USABLE, provenance)
    _set_if(observations, "league_event", semantics.league is not None or semantics.tournament is not None, provenance)
    _set_if(observations, "series_context", semantics.series is not None or semantics.game_number is not None or semantics.best_of is not None, provenance)
    _set_if(observations, "patch_id", semantics.patch is not None, provenance)
    _set_if(observations, "team_ids", semantics.team_identity_status is SemanticEvidenceStatus.USABLE, provenance)
    _set_if(observations, "team_display_names", semantics.radiant_team_name is not None and semantics.dire_team_name is not None, provenance)

    _set_if(observations, "player_account_ids", len({player.account_id for player in semantics.players}) == 10, provenance)
    _set_if(observations, "player_sides", semantics.player_status is SemanticEvidenceStatus.USABLE, provenance)
    _set_if(observations, "player_hero_ids", len({player.hero_id for player in semantics.players}) == 10, provenance)
    _set_if(observations, "player_display_names", _all_players_have(raw_players, ("name", "displayName", "nickName")), provenance)
    _set_if(observations, "radiant_picks", len(semantics.radiant_hero_ids) == 5, provenance)
    _set_if(observations, "dire_picks", len(semantics.dire_hero_ids) == 5, provenance)
    _set_if(
        observations,
        "complete_5v5_picks",
        semantics.has_complete_5v5_composition,
        PublicFieldProvenance.DERIVED_FROM_PUBLIC_FIELDS,
    )

    bans = [action for action in semantics.draft_actions if action.kind == "ban" and action.hero_id is not None]
    ordered = [action for action in semantics.draft_actions if action.order is not None]
    has_pick_and_ban = (
        any(action.kind == "pick" for action in semantics.draft_actions)
        and any(action.kind == "ban" for action in semantics.draft_actions)
    )
    _set_if(observations, "bans", bool(bans), provenance)
    _set_if(
        observations,
        "ordered_draft_actions",
        bool(semantics.draft_actions)
        and has_pick_and_ban
        and len(ordered) == len(semantics.draft_actions)
        and all(action.kind is not None for action in semantics.draft_actions)
        and len({action.order for action in ordered}) == len(ordered),
        provenance,
    )
    _set_if(observations, "draft_action_order", bool(ordered), provenance)
    _set_if(observations, "draft_action_kind", bool(semantics.draft_actions) and all(action.kind is not None for action in semantics.draft_actions), provenance)
    _set_if(observations, "draft_action_side", bool(semantics.draft_actions) and all(action.side is not None for action in semantics.draft_actions), provenance)
    _set_if(observations, "draft_action_hero_id", bool(semantics.draft_actions) and all(action.hero_id is not None for action in semantics.draft_actions), provenance)
    first_pick_side = next(
        (
            action.side
            for action in sorted(
                (
                    item
                    for item in semantics.draft_actions
                    if item.order is not None
                ),
                key=lambda item: item.order or 0,
            )
            if action.kind == "pick" and action.side in {"radiant", "dire"}
        ),
        None,
    )
    _set_if(observations, "first_pick_side", first_pick_side is not None, PublicFieldProvenance.DERIVED_FROM_PUBLIC_FIELDS)

    _set_if(observations, "team_kills_final_score", find_public_state_value(state, ("radiantKills", "direKills", "teamKills")) is not None, provenance)
    _set_if(observations, "individual_kills", len(semantics.players) >= 10 and all(player.kills is not None for player in semantics.players[:10]), provenance)
    _set_if(observations, "deaths", len(semantics.players) >= 10 and all(player.deaths is not None for player in semantics.players[:10]), provenance)
    _set_if(observations, "assists", len(semantics.players) >= 10 and all(player.assists is not None for player in semantics.players[:10]), provenance)
    _set_if(observations, "final_net_worth", len(semantics.players) >= 10 and all(player.net_worth is not None for player in semantics.players[:10]), provenance)
    _set_if(observations, "last_hits", len(semantics.players) >= 10 and all(player.last_hits is not None for player in semantics.players[:10]), provenance)
    _set_if(observations, "denies", len(semantics.players) >= 10 and all(player.denies is not None for player in semantics.players[:10]), provenance)
    _set_if(observations, "gpm", len(semantics.players) >= 10 and all(player.gpm is not None for player in semantics.players[:10]), provenance)
    _set_if(observations, "xpm", len(semantics.players) >= 10 and all(player.xpm is not None for player in semantics.players[:10]), provenance)
    _set_if(observations, "level", len(semantics.players) >= 10 and all(player.level is not None for player in semantics.players[:10]), provenance)
    _set_if(observations, "damage", len(semantics.players) >= 10 and all(player.hero_damage is not None and player.tower_damage is not None and player.hero_healing is not None for player in semantics.players[:10]), provenance)
    _set_if(
        observations,
        "final_items",
        len(semantics.players) >= 10
        and all(player.final_item_ids for player in semantics.players[:10]),
        provenance,
    )
    _set_if(observations, "timed_item_data", public_players_have_timed_items(raw_players), provenance)
    _set_if(observations, "kill_events", public_has_timed_event_list(state, ("killEvents", "kills")), provenance)
    _set_if(observations, "minute_farm_timeline", bool(find_public_state_value(state, ("minuteStats", "goldTimeline", "xpTimeline", "lastHitTimeline"))), provenance)
    _set_if(observations, "gold_advantage_timeline", semantics.gold_advantage_status is SemanticEvidenceStatus.USABLE, provenance)
    _set_if(observations, "xp_advantage_timeline", semantics.xp_advantage_status is SemanticEvidenceStatus.USABLE, provenance)
    _set_if(observations, "advantage_timeline", bool(semantics.advantage_points), provenance)
    _set_if(observations, "tower_barracks_objectives", public_has_tower_barracks_objectives(state), provenance)
    _set_if(observations, "roshan_objectives", public_has_timed_event_list(state, ("roshanEvents",)), provenance)
    _set_if(observations, "tormentor_objectives", public_has_timed_event_list(state, ("tormentorEvents",)), provenance)
    return observations


def aggregate_public_field_coverage(
    analyses: Sequence[PublicPageAnalysis],
) -> tuple[PublicFieldCoverage, ...]:
    rows: list[PublicFieldCoverage] = []
    for definition in PUBLIC_FIELD_DEFINITIONS:
        observations = [
            analysis.observations.get(definition.key, PublicFieldObservation(False))
            for analysis in analyses
        ]
        present = [observation for observation in observations if observation.present]
        provenances = tuple(
            sorted(
                {
                    observation.provenance
                    for observation in present
                    if observation.provenance is not PublicFieldProvenance.NOT_FOUND
                },
                key=lambda item: item.value,
            )
        )
        applicable_count = len(observations)
        present_count = len(present)
        coverage_pct = (
            present_count / applicable_count * 100.0
            if applicable_count
            else 0.0
        )
        rows.append(
            PublicFieldCoverage(
                key=definition.key,
                label=definition.label,
                present_count=present_count,
                applicable_count=applicable_count,
                coverage_pct=coverage_pct,
                provenance=provenances or (PublicFieldProvenance.NOT_FOUND,),
                usage=definition.usage,
                semantics=definition.semantics,
            )
        )
    return tuple(rows)


def determine_public_source_recommendation(
    analyses: Sequence[PublicPageAnalysis],
    coverage: Sequence[PublicFieldCoverage],
) -> PublicSourceRecommendation:
    if not analyses or not any(
        analysis.access_status
        in (
            PublicPageAccessStatus.PUBLIC_PAGE_AVAILABLE,
            PublicPageAccessStatus.PAGE_AVAILABLE_DATA_NOT_STATIC,
            PublicPageAccessStatus.PUBLIC_REFERENCED_RESOURCE_AVAILABLE,
        )
        for analysis in analyses
    ):
        return PublicSourceRecommendation.INSUFFICIENT_SOURCE
    by_key = {row.key: row for row in coverage}

    def pct(key: str) -> float:
        row = by_key.get(key)
        return row.coverage_pct if row is not None else 0.0

    draft_ready = all(
        pct(key) >= 80.0
        for key in (
            "stable_match_id",
            "team_display_names",
            "player_account_ids",
            "player_hero_ids",
            "complete_5v5_picks",
        )
    )
    rich_ready = all(
        pct(key) >= 80.0
        for key in (
            "duration",
            "winner_side",
            "team_kills_final_score",
            "individual_kills",
            "final_items",
        )
    )
    timeline_ready = pct("advantage_timeline") >= 80.0 or pct("kill_events") >= 80.0
    if draft_ready and rich_ready and timeline_ready:
        return PublicSourceRecommendation.PRIMARY_HISTORICAL_GAME_SOURCE_CANDIDATE
    if draft_ready:
        return PublicSourceRecommendation.DRAFT_SOURCE_CANDIDATE
    if any(row.present_count for row in coverage):
        return PublicSourceRecommendation.PARTIAL_ENRICHMENT_SOURCE_CANDIDATE
    return PublicSourceRecommendation.INSUFFICIENT_SOURCE


def classify_public_source_contract_coverage(
    coverage: Sequence[PublicFieldCoverage],
) -> tuple[PublicSourceContractCoverage, ...]:
    by_key = {row.key: row for row in coverage}
    return tuple(
        _contract_coverage_row(field, by_key)
        for field in PUBLIC_SOURCE_CONTRACT_FIELDS
    )


def assess_public_page_workloads(
    coverage: Sequence[PublicSourceContractCoverage],
) -> tuple[PublicWorkloadAssessment, ...]:
    by_key = {row.key: row for row in coverage}

    def usable(key: str) -> bool:
        row = by_key.get(key)
        return row is not None and row.classification in {
            PublicSourceCoverageClassification.SUPPORTED,
            PublicSourceCoverageClassification.DERIVABLE,
        }

    def limited(key: str) -> bool:
        row = by_key.get(key)
        return row is not None and row.classification in {
            PublicSourceCoverageClassification.SUPPORTED,
            PublicSourceCoverageClassification.DERIVABLE,
            PublicSourceCoverageClassification.PARTIAL,
            PublicSourceCoverageClassification.UNSTABLE,
        }

    post_draft_ready = (
        usable("hero_picks")
        and usable("winner_result")
        and limited("bans")
        and limited("team_identity")
    )
    if post_draft_ready:
        post_draft = PublicWorkloadAssessment(
            workload="POST_DRAFT win probability",
            suitability=PublicWorkloadSuitability.SUFFICIENT_WITH_LIMITATIONS,
            reasoning=(
                "Completed 5v5 picks, side ownership, bans, teams, players, "
                "and result labels are supported or derivable; complete ordered "
                "pick/ban sequencing and first-pick side remain partial/missing."
            ),
        )
    else:
        post_draft = PublicWorkloadAssessment(
            workload="POST_DRAFT win probability",
            suitability=PublicWorkloadSuitability.INSUFFICIENT,
            reasoning=(
                "Completed drafts, labels, or team/player identity are not "
                "supported well enough for post-draft training."
            ),
        )

    pre_map_ready = (
        limited("start_time")
        and limited("team_identity")
        and limited("player_account_identity")
        and limited("stable_roster_identity")
        and usable("winner_result")
    )
    if pre_map_ready:
        pre_map = PublicWorkloadAssessment(
            workload="PRE_MAP / historical features",
            suitability=PublicWorkloadSuitability.SUFFICIENT_WITH_LIMITATIONS,
            reasoning=(
                "Match labels, teams, player account IDs, sides, roster "
                "fingerprints, and timestamps are available or derivable, but "
                "league identity and display names are incomplete."
            ),
        )
    else:
        pre_map = PublicWorkloadAssessment(
            workload="PRE_MAP / historical features",
            suitability=PublicWorkloadSuitability.INSUFFICIENT,
            reasoning=(
                "Point-in-time timestamps, labels, or team/player identity are "
                "not supported well enough for historical feature construction."
            ),
        )

    live_state = PublicWorkloadAssessment(
        workload="live state estimation",
        suitability=PublicWorkloadSuitability.INSUFFICIENT,
        reasoning=(
            "This is historical public-page data, not a verified real-time feed. "
            "Advantage curves are present when supported, but kill, objective, "
            "item-timing, and event timelines are not sufficient for live-state "
            "model validation."
        ),
    )

    if usable("gold_advantage_progression"):
        cash_out = PublicWorkloadAssessment(
            workload="cash-out policy research",
            suitability=PublicWorkloadSuitability.SUFFICIENT_WITH_LIMITATIONS,
            reasoning=(
                "Historical advantage curves, drafts, final stats, and results "
                "can support coarse counterfactual state-trajectory research. "
                "Historical bookmaker price/cash-out data remains outside this "
                "source, and objective/item event timing is missing."
            ),
        )
        multi_step = PublicWorkloadAssessment(
            workload="planned multi-step betting sequence research",
            suitability=PublicWorkloadSuitability.SUFFICIENT_WITH_LIMITATIONS,
            reasoning=(
                "Draft context plus advantage curves can support early/late "
                "transition studies, but objective timing, item timing, kill "
                "events, and market-price history are not covered."
            ),
        )
    else:
        cash_out = PublicWorkloadAssessment(
            workload="cash-out policy research",
            suitability=PublicWorkloadSuitability.INSUFFICIENT,
            reasoning=(
                "The source does not expose enough historical trajectory data "
                "for cash-out policy research, and market price history is out "
                "of scope."
            ),
        )
        multi_step = PublicWorkloadAssessment(
            workload="planned multi-step betting sequence research",
            suitability=PublicWorkloadSuitability.INSUFFICIENT,
            reasoning=(
                "The source does not expose enough temporal trajectory evidence "
                "to study planned early/late state transitions."
            ),
        )

    return (post_draft, pre_map, live_state, cash_out, multi_step)


def decide_public_page_architecture(
    workloads: Sequence[PublicWorkloadAssessment],
    coverage: Sequence[PublicSourceContractCoverage],
) -> tuple[PublicArchitectureDecision, tuple[str, ...]]:
    by_workload = {row.workload: row for row in workloads}
    by_key = {row.key: row for row in coverage}
    critical_gaps: list[str] = []

    def is_missing_or_insufficient(key: str) -> bool:
        row = by_key.get(key)
        return row is None or row.classification is PublicSourceCoverageClassification.MISSING

    if (
        by_workload["POST_DRAFT win probability"].suitability
        is PublicWorkloadSuitability.INSUFFICIENT
    ):
        critical_gaps.append("completed draft plus winner-label support")
    if (
        by_workload["PRE_MAP / historical features"].suitability
        is PublicWorkloadSuitability.INSUFFICIENT
    ):
        critical_gaps.append("point-in-time team/player identity support")
    if is_missing_or_insufficient("gold_advantage_progression"):
        critical_gaps.append("historical advantage-curve trajectory support")

    if critical_gaps:
        return (
            PublicArchitectureDecision.STRATZ_PUBLIC_CRITICAL_GAPS,
            tuple(critical_gaps),
        )
    return (PublicArchitectureDecision.STRATZ_PUBLIC_SUFFICIENT, ())


def build_public_source_contract(
    result: PublicPageProbeResult,
) -> PublicSourceContract:
    coverage = classify_public_source_contract_coverage(result.coverage)
    workloads = assess_public_page_workloads(coverage)
    decision, critical_gaps = decide_public_page_architecture(workloads, coverage)
    return PublicSourceContract(
        source=result.source,
        sample_count=len(result.analyses),
        coverage=coverage,
        workloads=workloads,
        architecture_decision=decision,
        critical_gaps=critical_gaps,
    )


def render_public_page_probe_result(result: PublicPageProbeResult) -> str:
    lines: list[str] = []
    lines.append("Public professional Dota match page feasibility probe")
    lines.append(f"Source: {result.source.value}")
    lines.append(f"Probe started: {result.probe_started_at.isoformat()}")
    lines.append(f"Requests: {result.request_count}")
    lines.append("")
    lines.append("Public access policy")
    lines.append(f"Robots URL: {result.policy.robots_url}")
    lines.append(f"Robots HTTP status: {_format_optional_int(result.policy.http_status)}")
    lines.append(f"Checked path: {result.policy.checked_path}")
    lines.append(
        "Path disallowed for generic crawler: "
        + _format_optional_bool(result.policy.path_disallowed)
    )
    if result.policy.relevant_rules:
        lines.append("Relevant robots rules: " + "; ".join(result.policy.relevant_rules))
    if result.policy.content_signals:
        lines.append("Content signals: " + "; ".join(result.policy.content_signals))
    if result.policy.terms_note:
        lines.append(f"Terms note: {result.policy.terms_note}")
    lines.append("")
    lines.append("Public page results")
    for analysis in result.analyses:
        lines.append(
            f"{analysis.match_id or '-'} | {analysis.url} | "
            f"HTTP={_format_optional_int(analysis.http_status)} | "
            f"{analysis.access_status.value} | bytes={analysis.byte_size}"
        )
        lines.append("  Static HTML: " + "; ".join(analysis.static_html_findings))
        lines.append("  Embedded state: " + "; ".join(analysis.embedded_state_findings))
        lines.append(
            "  Referenced resources: "
            + "; ".join(analysis.referenced_resource_findings)
        )
    if result.warnings:
        lines.append("")
        lines.append("Warnings")
        for warning in result.warnings:
            lines.append(f"- {warning}")
    lines.append("")
    lines.append("Field coverage matrix")
    lines.append("field | present/applicable | coverage | provenance | usage | semantics")
    for row in result.coverage:
        provenances = ",".join(item.value for item in row.provenance)
        usages = ",".join(item.value for item in row.usage)
        lines.append(
            f"{row.label} | {row.present_count}/{row.applicable_count} | "
            f"{row.coverage_pct:.1f}% | {provenances} | {usages} | "
            f"{row.semantics}"
        )
    lines.append("")
    lines.append("Source recommendation")
    lines.append(result.recommendation.value)
    contract = build_public_source_contract(result)
    lines.append("")
    lines.append("Formal source contract")
    lines.append(
        "field | group | classification | source evidence | parser status | caveat"
    )
    for contract_row in contract.coverage:
        lines.append(
            f"{contract_row.label} | {contract_row.group} | "
            f"{contract_row.classification.value} | "
            f"{contract_row.source_evidence} | {contract_row.parser_status} | "
            f"{contract_row.caveat}"
        )
    lines.append("")
    lines.append("Workload suitability")
    for workload in contract.workloads:
        lines.append(
            f"{workload.workload}: {workload.suitability.value} | "
            f"{workload.reasoning}"
        )
    lines.append("")
    lines.append("Architecture decision")
    lines.append(contract.architecture_decision.value)
    if contract.critical_gaps:
        lines.append("Critical gaps")
        for gap in contract.critical_gaps:
            lines.append(f"- {gap}")
    return "\n".join(lines)


def _contract_coverage_row(
    field: PublicSourceContractField,
    by_key: Mapping[str, PublicFieldCoverage],
) -> PublicSourceContractCoverage:
    classification = _contract_field_classification(field, by_key)
    return PublicSourceContractCoverage(
        key=field.key,
        label=field.label,
        group=field.group,
        classification=classification,
        source_evidence=_contract_source_evidence(field, by_key),
        parser_status=field.parser_status,
        caveat=field.caveat,
    )


def _contract_field_classification(
    field: PublicSourceContractField,
    by_key: Mapping[str, PublicFieldCoverage],
) -> PublicSourceCoverageClassification:
    direct_supported = bool(field.source_keys) and all(
        _coverage_supported(by_key, key) for key in field.source_keys
    )
    if direct_supported:
        if field.unstable_when_present:
            return PublicSourceCoverageClassification.UNSTABLE
        if field.derived_semantic or any(
            _coverage_is_derived(by_key, key) for key in field.source_keys
        ):
            return PublicSourceCoverageClassification.DERIVABLE
        return PublicSourceCoverageClassification.SUPPORTED

    derivable_supported = bool(field.derivable_from) and all(
        _coverage_supported(by_key, key) for key in field.derivable_from
    )
    if derivable_supported:
        return PublicSourceCoverageClassification.DERIVABLE

    evidence_keys = (
        field.source_keys + field.derivable_from + field.partial_keys
    )
    if any(_coverage_present(by_key, key) for key in evidence_keys):
        return PublicSourceCoverageClassification.PARTIAL
    return PublicSourceCoverageClassification.MISSING


def _coverage_supported(
    by_key: Mapping[str, PublicFieldCoverage],
    key: str,
) -> bool:
    row = by_key.get(key)
    return row is not None and row.present_count > 0 and row.coverage_pct >= 80.0


def _coverage_present(
    by_key: Mapping[str, PublicFieldCoverage],
    key: str,
) -> bool:
    row = by_key.get(key)
    return row is not None and row.present_count > 0


def _coverage_is_derived(
    by_key: Mapping[str, PublicFieldCoverage],
    key: str,
) -> bool:
    row = by_key.get(key)
    return (
        row is not None
        and PublicFieldProvenance.DERIVED_FROM_PUBLIC_FIELDS in row.provenance
    )


def _contract_source_evidence(
    field: PublicSourceContractField,
    by_key: Mapping[str, PublicFieldCoverage],
) -> str:
    evidence_keys = tuple(
        dict.fromkeys(
            field.source_keys + field.derivable_from + field.partial_keys
        )
    )
    if not evidence_keys:
        return "no parsed evidence key is defined for this semantic field"

    parts: list[str] = []
    for key in evidence_keys:
        row = by_key.get(key)
        if row is None:
            parts.append(f"{key}=not in parser coverage")
            continue
        provenance = ",".join(item.value for item in row.provenance)
        parts.append(
            f"{key}={row.present_count}/{row.applicable_count} via {provenance}"
        )
    return "; ".join(parts)


@dataclass(frozen=True)
class _RobotGroup:
    agents: tuple[str, ...]
    rules: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _DraftAction:
    order: int | None
    kind: str | None
    side: str | None
    hero_id: int | None


@dataclass(frozen=True)
class _ScriptPayload:
    script_id: str | None
    script_type: str
    content: str


def _probe_targets(
    *,
    source: PublicPageSource,
    match_ids: Sequence[str],
    page_urls: Sequence[str],
) -> tuple[tuple[str | None, str], ...]:
    targets: list[tuple[str | None, str]] = []
    for match_id in match_ids:
        parsed = str(match_id).strip()
        if parsed:
            targets.append((parsed, build_public_match_url(source, parsed)))
    for page_url in page_urls:
        parsed_url = str(page_url).strip()
        if parsed_url:
            targets.append((None, parsed_url))
    return tuple(targets)


def _robots_url(source: PublicPageSource) -> str:
    if source is PublicPageSource.STRATZ:
        return f"{STRATZ_PUBLIC_BASE_URL}/robots.txt"
    return f"{SOFASCORE_PUBLIC_BASE_URL}/robots.txt"


def _parse_robots_groups(
    robots_text: str,
) -> tuple[tuple[tuple[str, ...], tuple[tuple[str, str], ...]], ...]:
    groups: list[tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", maxsplit=1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", maxsplit=1)
        key = key.strip().casefold()
        value = value.strip()
        if key == "user-agent":
            if agents and rules:
                groups.append((tuple(agents), tuple(rules)))
                agents = []
                rules = []
            agents.append(value.casefold())
            continue
        if key in {"allow", "disallow"} and agents:
            rules.append((key, value))
    if agents:
        groups.append((tuple(agents), tuple(rules)))
    return tuple(groups)


def _robots_rule_matches(pattern: str, path: str) -> bool:
    if not pattern:
        return False
    escaped = re.escape(pattern).replace("\\*", ".*")
    return re.match(f"^{escaped}", path) is not None


def _access_status_from_response(
    response: PublicHttpResponse,
) -> PublicPageAccessStatus:
    if response.status_code is None:
        return PublicPageAccessStatus.NETWORK_ERROR
    if response.status_code in (401, 403):
        return PublicPageAccessStatus.HTTP_FORBIDDEN
    if response.status_code == 429:
        return PublicPageAccessStatus.HTTP_RATE_LIMITED
    if response.status_code == 404:
        return PublicPageAccessStatus.PUBLIC_PAGE_NOT_FOUND
    if response.status_code != 200:
        return PublicPageAccessStatus.HTTP_ERROR
    return PublicPageAccessStatus.PUBLIC_PAGE_AVAILABLE


def _access_finding(
    status: PublicPageAccessStatus,
    response: PublicHttpResponse,
) -> str:
    if status is PublicPageAccessStatus.HTTP_FORBIDDEN:
        text = response.text.casefold()
        if "cloudflare" in text or "enable javascript and cookies" in text:
            return "HTTP 403; Cloudflare JavaScript/cookie challenge detected."
        return "HTTP 403 forbidden."
    if status is PublicPageAccessStatus.HTTP_RATE_LIMITED:
        return "HTTP 429 rate limited."
    if status is PublicPageAccessStatus.PUBLIC_PAGE_NOT_FOUND:
        return "HTTP 404 page not found."
    if status is PublicPageAccessStatus.NETWORK_ERROR:
        return f"Network error: {response.error or 'unknown'}"
    return f"HTTP status {response.status_code}."


def _empty_analysis(
    *,
    source: PublicPageSource,
    match_id: str | None,
    url: str,
    access_status: PublicPageAccessStatus,
    finding: str,
    http_status: int | None = None,
    content_type: str | None = None,
    byte_size: int = 0,
) -> PublicPageAnalysis:
    return PublicPageAnalysis(
        source=source,
        match_id=match_id,
        url=url,
        http_status=http_status,
        content_type=content_type,
        byte_size=byte_size,
        access_status=access_status,
        static_html_findings=(finding,),
        embedded_state_findings=("not inspected",),
        referenced_resource_findings=("not inspected",),
        observations={},
    )


class _VisibleDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, list[str]] = defaultdict(list)
        self._field_stack: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attr_map = {key.casefold(): value or "" for key, value in attrs}
        field = attr_map.get("data-field")
        if field:
            value = attr_map.get("data-value")
            normalized = _normalize_field_name(field)
            if value:
                self.values[normalized].append(unescape(value).strip())
            self._field_stack.append(normalized)
        for attr_name, key in (
            ("data-radiant-pick", "radiant_pick"),
            ("data-dire-pick", "dire_pick"),
            ("data-ban", "ban"),
        ):
            value = attr_map.get(attr_name)
            if value:
                self.values[key].append(unescape(value).strip())

    def handle_endtag(self, tag: str) -> None:
        if self._field_stack:
            self._field_stack.pop()

    def handle_data(self, data: str) -> None:
        if not self._field_stack:
            return
        value = unescape(data).strip()
        if value:
            self.values[self._field_stack[-1]].append(value)


class _ScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[_ScriptPayload] = []
        self._inside_script = False
        self._current_id: str | None = None
        self._current_type = ""
        self._content: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "script":
            return
        attr_map = {key.casefold(): value or "" for key, value in attrs}
        if "src" in attr_map:
            return
        self._inside_script = True
        self._current_id = attr_map.get("id") or None
        self._current_type = attr_map.get("type", "")
        self._content = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "script" or not self._inside_script:
            return
        self.scripts.append(
            _ScriptPayload(
                script_id=self._current_id,
                script_type=self._current_type.casefold(),
                content="".join(self._content),
            )
        )
        self._inside_script = False
        self._current_id = None
        self._current_type = ""
        self._content = []

    def handle_data(self, data: str) -> None:
        if self._inside_script:
            self._content.append(data)


class _ResourceLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attr_map = {key.casefold(): value or "" for key, value in attrs}
        for key in ("href", "src"):
            value = attr_map.get(key)
            if value:
                self.urls.append(value)


def _merge_observations(
    target: dict[str, PublicFieldObservation],
    source: Mapping[str, PublicFieldObservation],
) -> None:
    for key, observation in source.items():
        if not observation.present:
            continue
        existing = target.get(key)
        if existing is None or existing.provenance is PublicFieldProvenance.NOT_FOUND:
            target[key] = observation


def _set_if(
    observations: dict[str, PublicFieldObservation],
    key: str,
    condition: bool,
    provenance: PublicFieldProvenance,
    note: str = "",
) -> None:
    if condition:
        observations[key] = PublicFieldObservation(True, provenance, note)


def _normalize_field_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip()).strip("_").casefold()
    aliases = {
        "match_id": "match_id",
        "stable_match_id": "match_id",
        "start": "start_timestamp",
        "start_time": "start_timestamp",
        "started_at": "start_timestamp",
        "end": "end_timestamp",
        "end_time": "end_timestamp",
        "ended_at": "end_timestamp",
        "league": "league_event",
        "event": "league_event",
        "patch": "patch_id",
        "radiant_team": "radiant_team_name",
        "dire_team": "dire_team_name",
    }
    return aliases.get(normalized, normalized)


def _parse_assignment_json(script: str) -> object | None:
    for pattern in (
        r"window\.__MATCH_DATA__\s*=\s*(\{.*\})\s*;",
        r"window\.__PUBLIC_MATCH_STATE__\s*=\s*(\{.*\})\s*;",
    ):
        match = re.search(pattern, script, flags=re.DOTALL)
        if match is None:
            continue
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
    return None


def _next_flight_chunk_strings(script: str) -> tuple[str, ...]:
    chunks: list[str] = []
    for match in re.finditer(
        r"self\.__next_f\.push\((\[.*?\])\)",
        script,
        flags=re.DOTALL,
    ):
        try:
            pushed = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(pushed, list):
            continue
        for item in pushed:
            if isinstance(item, str):
                chunks.append(item)
    return tuple(chunks)


def _parse_next_flight_states(chunks: Sequence[str]) -> tuple[object, ...]:
    states: list[object] = []
    stream = "".join(chunks)
    for line in stream.splitlines():
        parsed = _parse_next_flight_payload_string(line)
        if parsed is not None:
            states.append(parsed)
    return tuple(states)


def _parse_next_flight_payload_string(value: str) -> object | None:
    stripped = value.strip()
    if ":" in stripped:
        stripped = stripped.split(":", maxsplit=1)[1].strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _looks_like_page_state_script(script: str) -> bool:
    lowered = script.casefold()
    return (
        "__next_data__" in lowered
        or "__match_data__" in lowered
        or "__next_f.push" in lowered
        or "matchid" in lowered
    )


def _json_from_response(response: PublicHttpResponse) -> object | None:
    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        return None


def _find_value(value: object, keys: tuple[str, ...]) -> object | None:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value and value[key] is not None:
                return value[key]
        for nested in value.values():
            found = _find_value(nested, keys)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_value(item, keys)
            if found is not None:
                return found
    return None


def _find_mapping_list(value: object, keys: tuple[str, ...]) -> tuple[Mapping[str, object], ...]:
    found = _find_value(value, keys)
    if isinstance(found, list):
        return tuple(item for item in found if isinstance(item, Mapping))
    return ()


def _has_timed_event_list(value: object, keys: tuple[str, ...]) -> bool:
    for row in _find_mapping_list(value, keys):
        if _find_value(row, ("time", "gameTime", "timestamp", "second")) is None:
            continue
        if any(
            _find_value(row, (key,)) is not None
            for key in (
                "team",
                "side",
                "killer",
                "attacker",
                "victim",
                "target",
                "playerId",
                "unit",
                "type",
            )
        ):
            return True
    return False


def _has_tower_barracks_objectives(state: object) -> bool:
    if _find_value(
        state,
        (
            "towerStatusRadiant",
            "towerStatusDire",
            "barracksStatusRadiant",
            "barracksStatusDire",
        ),
    ) is not None:
        return True
    return _has_timed_event_list(state, ("buildingEvents",))


def _draft_actions_from_state(state: object) -> tuple[_DraftAction, ...]:
    rows = _find_mapping_list(state, ("pickBans", "draftActions", "picksBans", "bans"))
    actions: list[_DraftAction] = []
    for index, row in enumerate(rows, start=1):
        order = _int(row.get("order") or row.get("ord") or row.get("sequence"))
        kind = _draft_kind(row)
        side = _side_from_mapping(row)
        hero_id = _int(row.get("heroId") or row.get("hero_id") or row.get("bannedHeroId"))
        if order is None and (
            kind is not None
            or side is not None
            or hero_id is not None
        ):
            display_order = row.get("displayOrder")
            if display_order is not None:
                order = None
        actions.append(_DraftAction(order=order, kind=kind, side=side, hero_id=hero_id))
    return tuple(actions)


def _draft_kind(row: Mapping[str, object]) -> str | None:
    is_pick = row.get("isPick")
    if isinstance(is_pick, bool):
        return "pick" if is_pick else "ban"
    if row.get("bannedHeroId") is not None or row.get("wasBannedSuccessfully") is not None:
        return "ban"
    value = _text(row.get("kind") or row.get("type") or row.get("action"))
    if value is None:
        return None
    lowered = value.casefold()
    if "pick" in lowered:
        return "pick"
    if "ban" in lowered:
        return "ban"
    return None


def _side_from_mapping(row: Mapping[str, object]) -> str | None:
    side = _side(row.get("side") or row.get("teamSide") or row.get("team"))
    if side is not None:
        return side
    is_radiant = row.get("isRadiant")
    if isinstance(is_radiant, bool):
        return "radiant" if is_radiant else "dire"
    return None


def _side(value: object) -> str | None:
    if value in (0, "0"):
        return "radiant"
    if value in (1, "1"):
        return "dire"
    text = _text(value)
    if text is None:
        return None
    lowered = text.casefold()
    if lowered in {"radiant", "home"}:
        return "radiant"
    if lowered in {"dire", "away"}:
        return "dire"
    return None


def _player_side(player: Mapping[str, object]) -> str | None:
    side = _side_from_mapping(player)
    if side is not None:
        return side
    slot = _int(player.get("playerSlot"))
    if slot is None:
        return None
    return "radiant" if slot < 128 else "dire"


def _side_pick_hero_ids(
    players: Sequence[Mapping[str, object]],
    actions: Sequence[_DraftAction],
    side: str,
) -> tuple[int, ...]:
    action_heroes = tuple(
        action.hero_id
        for action in actions
        if action.kind == "pick" and action.side == side and action.hero_id is not None
    )
    if action_heroes:
        return tuple(dict.fromkeys(action_heroes))
    return tuple(
        dict.fromkeys(
            hero_id
            for player in players
            if _player_side(player) == side
            and (hero_id := _int(player.get("heroId") or player.get("hero_id")))
            is not None
        )
    )


def _first_pick_side(actions: Sequence[_DraftAction]) -> str | None:
    ordered = sorted(
        (action for action in actions if action.order is not None),
        key=lambda action: action.order or 0,
    )
    for action in ordered:
        if action.kind == "pick" and action.side in {"radiant", "dire"}:
            return action.side
    return None


def _has_radiant_dire_orientation(state: object) -> bool:
    if _find_value(state, ("radiantTeam", "direTeam")) is not None:
        return True
    if _find_value(state, ("radiantTeamId",)) is not None and _find_value(state, ("direTeamId",)) is not None:
        return True
    players = _find_mapping_list(state, ("players", "playerMatches", "lineups"))
    return len(players) >= 10 and all(_player_side(player) is not None for player in players[:10])


def _has_team_ids(state: object) -> bool:
    return _find_value(state, ("radiantTeamId",)) is not None and _find_value(state, ("direTeamId",)) is not None


def _has_team_names(state: object) -> bool:
    radiant = _find_value(state, ("radiantTeam", "homeTeam"))
    dire = _find_value(state, ("direTeam", "awayTeam"))
    return _mapping_has_name(radiant) and _mapping_has_name(dire)


def _mapping_has_name(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return any(_text(value.get(key)) is not None for key in ("name", "displayName", "shortName"))


def _all_players_have(
    players: Sequence[Mapping[str, object]],
    keys: tuple[str, ...],
) -> bool:
    return len(players) >= 10 and all(
        any(_has_value(player.get(key)) for key in keys) for player in players[:10]
    )


def _players_have_items(players: Sequence[Mapping[str, object]]) -> bool:
    item_keys = (
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
    )
    return _all_players_have(players, item_keys)


def _players_have_timed_items(players: Sequence[Mapping[str, object]]) -> bool:
    for player in players:
        items = player.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if _int(item.get("itemId")) is not None and _find_value(item, ("time", "gameTime")) is not None:
                return True
    return False


def _unique_ints(values: Sequence[str]) -> tuple[int, ...]:
    parsed = [_int(value) for value in values]
    return tuple(dict.fromkeys(value for value in parsed if value is not None))


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


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


def _decode_body(body: bytes) -> str:
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


def _header_value(headers: object, key: str) -> str | None:
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(key)
        return str(value) if value is not None else None
    if isinstance(headers, Mapping):
        value = headers.get(key)
        return str(value) if value is not None else None
    return None


def _format_optional_int(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _format_optional_bool(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"
