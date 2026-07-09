from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import time

from app.history import (
    DEFAULT_HISTORICAL_COMPETITION_SCOPE,
    HistoricalCompetitionFamily,
    HistoricalMatch,
    classify_historical_competition_family,
    is_historical_competition_qualifier,
    is_historical_match_scope_eligible,
)
from app.stratz.graphql import (
    STRATZ_SCHEMA_OVERVIEW_QUERY,
    STRATZ_TYPE_INTROSPECTION_QUERY,
    StratzGraphQLClient,
    StratzGraphQLError,
)
from app.tournaments import CompetitiveStage, TournamentRound


class CoverageClassification(str, Enum):
    ALWAYS_PRESENT = "ALWAYS_PRESENT"
    PARTIAL = "PARTIAL"
    ABSENT = "ABSENT"
    DERIVABLE_FROM_RETURNED_FIELDS = "DERIVABLE_FROM_RETURNED_FIELDS"
    REQUIRES_ADDITIONAL_PUBLIC_API_QUERY = (
        "REQUIRES_ADDITIONAL_PUBLIC_API_QUERY"
    )
    PROVIDER_DERIVED = "PROVIDER_DERIVED"
    UNKNOWN_SEMANTICS = "UNKNOWN_SEMANTICS"


class DataUsage(str, Enum):
    TARGET_GAME_PRE_OUTCOME_INPUT = "TARGET_GAME_PRE_OUTCOME_INPUT"
    POST_GAME_TARGET_OR_LABEL = "POST_GAME_TARGET_OR_LABEL"
    PRIOR_GAME_HISTORICAL_CONTEXT_ONLY = "PRIOR_GAME_HISTORICAL_CONTEXT_ONLY"
    IDENTITY_OR_CONTEXT = "IDENTITY_OR_CONTEXT"
    UNSUITABLE_OR_UNCLEAR = "UNSUITABLE_OR_UNCLEAR"


class SourceVerdict(str, Enum):
    STRATZ_FREE_SOURCE_FEASIBLE = "STRATZ_FREE_SOURCE_FEASIBLE"
    STRATZ_FREE_SOURCE_INSUFFICIENT = "STRATZ_FREE_SOURCE_INSUFFICIENT"


class GraphQLErrorClassification(str, Enum):
    PERMISSION_RESTRICTED = "PERMISSION_RESTRICTED"
    REQUEST_SIZE_LIMIT = "REQUEST_SIZE_LIMIT"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    CLIENT_QUERY_ERROR = "CLIENT_QUERY_ERROR"
    GRAPHQL_ERROR = "GRAPHQL_ERROR"


class AccessStatus(str, Enum):
    ACCESSIBLE = "ACCESSIBLE"
    PERMISSION_RESTRICTED = "PERMISSION_RESTRICTED"
    GRAPHQL_ERROR = "GRAPHQL_ERROR"
    REQUEST_SIZE_LIMIT = "REQUEST_SIZE_LIMIT"
    SAMPLE_MISSING = "SAMPLE_MISSING"
    ACCESS_UNVERIFIED = "ACCESS_UNVERIFIED"


@dataclass(frozen=True)
class GraphQLTypeRef:
    kind: str
    name: str | None = None
    of_type: "GraphQLTypeRef | None" = None


@dataclass(frozen=True)
class GraphQLArgument:
    name: str
    type_ref: GraphQLTypeRef

    @property
    def rendered_type(self) -> str:
        return render_graphql_type(self.type_ref)


@dataclass(frozen=True)
class GraphQLField:
    name: str
    type_ref: GraphQLTypeRef
    args: Mapping[str, GraphQLArgument]

    @property
    def rendered_type(self) -> str:
        return render_graphql_type(self.type_ref)

    @property
    def named_type(self) -> str | None:
        return named_graphql_type(self.type_ref)


@dataclass(frozen=True)
class GraphQLTypeDefinition:
    name: str
    kind: str
    fields: Mapping[str, GraphQLField]


@dataclass(frozen=True)
class StratzSchemaSnapshot:
    query_type_name: str
    types: Mapping[str, GraphQLTypeDefinition]

    def type_definition(self, name: str) -> GraphQLTypeDefinition | None:
        return self.types.get(name)


@dataclass(frozen=True)
class StratzQueryPlan:
    query_type_name: str
    match_fetch_field: str | None
    match_fetch_argument: str | None
    match_fetch_argument_type: str | None
    match_query: str | None
    match_type_fields_used: tuple[str, ...]
    match_type_fields_absent: tuple[str, ...]
    match_type_fields_restricted: tuple[str, ...]
    restricted_field_paths: tuple[str, ...]
    nested_fields_used: Mapping[str, tuple[str, ...]]
    inspected_type_names: tuple[str, ...]
    professional_discovery_path: str | None
    additional_query_requirements: tuple[str, ...] = ()

    @property
    def can_fetch_matches_by_ids(self) -> bool:
        return self.match_query is not None and self.match_fetch_argument is not None


@dataclass(frozen=True)
class FieldDefinition:
    key: str
    label: str
    family: str
    usage: tuple[DataUsage, ...]
    semantics: str
    requires_additional_query_when_absent: bool = False


@dataclass(frozen=True)
class FieldObservation:
    present: bool
    classification: CoverageClassification | None = None
    note: str = ""
    schema_absent: bool = False
    access_restricted: bool = False


@dataclass(frozen=True)
class FieldCoverage:
    key: str
    label: str
    family: str
    present_count: int
    applicable_count: int
    coverage_pct: float
    classification: CoverageClassification
    usage: tuple[DataUsage, ...]
    semantics: str


@dataclass(frozen=True)
class StratzSampleIdentity:
    match_id: str
    started_at: datetime | None
    league_name: str | None
    tournament_name: str | None
    series_id: str | None
    radiant_team_name: str | None
    dire_team_name: str | None
    competition_family: HistoricalCompetitionFamily


@dataclass(frozen=True)
class StratzMatchAnalysis:
    identity: StratzSampleIdentity
    observations: Mapping[str, FieldObservation]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class StratzMatchCandidate:
    match_id: str
    started_at: datetime | None
    league_name: str | None
    tournament_name: str | None
    series_name: str | None
    radiant_team_name: str | None = None
    dire_team_name: str | None = None

    @property
    def competition_family(self) -> HistoricalCompetitionFamily:
        return _competition_family_from_metadata(
            match_id=self.match_id,
            started_at=self.started_at,
            league_name=self.league_name,
            tournament_name=self.tournament_name,
            series_name=self.series_name,
            radiant_team_name=self.radiant_team_name,
            dire_team_name=self.dire_team_name,
        )


@dataclass(frozen=True)
class StratzProbeResult:
    real_source: bool
    probe_started_at: datetime
    request_count: int
    sampled_match_ids: tuple[str, ...]
    sample_selection_method: str
    query_field_names: tuple[str, ...]
    query_plan: StratzQueryPlan | None
    access_capability: "StratzAccessCapability | None"
    analyses: tuple[StratzMatchAnalysis, ...]
    coverage: tuple[FieldCoverage, ...]
    verdict: SourceVerdict | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _DraftAction:
    order: int | None
    kind: str | None
    side: str | None
    hero_id: int | None


@dataclass(frozen=True)
class StratzFieldGroupAccess:
    group_name: str
    status: AccessStatus
    selected_paths: tuple[str, ...]
    restricted_path: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class StratzAccessCapability:
    observed_max_match_ids_per_request: int
    minimal_match_fetch_status: AccessStatus
    minimal_match_fetch_message: str | None
    field_groups: tuple[StratzFieldGroupAccess, ...]
    capability_probe_requests: int
    sample_fetch_requests: int = 0

    @property
    def restricted_paths(self) -> tuple[str, ...]:
        return tuple(
            result.restricted_path
            for result in self.field_groups
            if result.restricted_path is not None
        )


FIELD_DEFINITIONS: tuple[FieldDefinition, ...] = (
    FieldDefinition(
        "stable_match_id",
        "stable match ID",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Provider match ID returned on the match object.",
    ),
    FieldDefinition(
        "source_reference",
        "source URL/reference",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Deterministic STRATZ public match URL derived from the match ID.",
    ),
    FieldDefinition(
        "start_timestamp",
        "start timestamp",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Explicit provider start timestamp.",
    ),
    FieldDefinition(
        "end_timestamp",
        "end timestamp",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Explicit end timestamp, or derivable from start timestamp plus duration.",
    ),
    FieldDefinition(
        "duration",
        "duration",
        "MATCH / MAP IDENTITY",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game map duration; never a target-map post-draft feature.",
    ),
    FieldDefinition(
        "winner_side",
        "winner / winning side",
        "MATCH / MAP IDENTITY",
        (DataUsage.POST_GAME_TARGET_OR_LABEL,),
        "Winning side or radiant win flag.",
    ),
    FieldDefinition(
        "radiant_dire_orientation",
        "Radiant/Dire orientation",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Radiant and Dire team/player orientation from provider fields.",
    ),
    FieldDefinition(
        "game_mode",
        "game mode",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Provider game mode field.",
    ),
    FieldDefinition(
        "lobby_professional_identity",
        "lobby/professional identity",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Lobby/pro/professional marker from lobby type or league context.",
    ),
    FieldDefinition(
        "league_id",
        "league ID",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Provider league ID.",
    ),
    FieldDefinition(
        "league_name",
        "league/tournament name",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Provider league display name.",
    ),
    FieldDefinition(
        "tournament_context",
        "tournament/event context",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Tournament, league, or series context returned with the match.",
    ),
    FieldDefinition(
        "series_id",
        "series ID",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Provider series ID.",
    ),
    FieldDefinition(
        "game_number",
        "game/map number within series",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Explicit game number within the series.",
    ),
    FieldDefinition(
        "series_type_best_of",
        "series type / best-of",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Provider series type or best-of metadata.",
    ),
    FieldDefinition(
        "pre_current_map_series_score",
        "pre-current-map series score",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Explicit score before current map; not reconstructed from future games.",
    ),
    FieldDefinition(
        "patch_id",
        "patch/game version ID",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Explicit provider game version or patch ID. No date inference.",
    ),
    FieldDefinition(
        "patch_name",
        "human-readable patch/version name",
        "MATCH / MAP IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Provider-linked patch name when returned by match or version metadata.",
        requires_additional_query_when_absent=True,
    ),
    FieldDefinition(
        "radiant_team_id",
        "Radiant team ID",
        "TEAM IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Source-local Radiant team ID.",
    ),
    FieldDefinition(
        "dire_team_id",
        "Dire team ID",
        "TEAM IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Source-local Dire team ID.",
    ),
    FieldDefinition(
        "team_display_names",
        "team display names",
        "TEAM IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Provider team names/tags for both sides.",
    ),
    FieldDefinition(
        "stable_provider_team_ids",
        "stable provider team IDs",
        "TEAM IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Team IDs appear stable within STRATZ only; no cross-provider equivalence.",
    ),
    FieldDefinition(
        "organization_vs_roster_identity",
        "organization vs roster identity",
        "TEAM IDENTITY",
        (DataUsage.UNSUITABLE_OR_UNCLEAR,),
        "Whether team IDs represent organization identity or roster identity.",
    ),
    FieldDefinition(
        "player_account_ids",
        "all 10 player account IDs",
        "PLAYER IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Stable provider player/account IDs for all ten players.",
    ),
    FieldDefinition(
        "player_sides",
        "player team/side",
        "PLAYER IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Radiant/Dire side for all ten players.",
    ),
    FieldDefinition(
        "player_slots",
        "player slot/position orientation",
        "PLAYER IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Provider slot/position orientation for all ten players.",
    ),
    FieldDefinition(
        "player_hero_ids",
        "player hero IDs",
        "PLAYER IDENTITY",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Hero IDs for all ten players.",
    ),
    FieldDefinition(
        "player_display_names",
        "player display names",
        "PLAYER IDENTITY",
        (DataUsage.IDENTITY_OR_CONTEXT,),
        "Display names for all ten players, informational only.",
    ),
    FieldDefinition(
        "player_role_lane",
        "role/lane",
        "PLAYER IDENTITY",
        (DataUsage.UNSUITABLE_OR_UNCLEAR,),
        "Role/lane fields only if returned with clear provider semantics.",
    ),
    FieldDefinition(
        "radiant_picks",
        "5 Radiant picks",
        "HERO / DRAFT DATA",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Five Radiant hero picks from players or draft actions.",
    ),
    FieldDefinition(
        "dire_picks",
        "5 Dire picks",
        "HERO / DRAFT DATA",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Five Dire hero picks from players or draft actions.",
    ),
    FieldDefinition(
        "complete_5v5_picks",
        "exact complete 5v5 pick coverage",
        "HERO / DRAFT DATA",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Exactly five unique hero picks per side.",
    ),
    FieldDefinition(
        "bans",
        "bans",
        "HERO / DRAFT DATA",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Ban actions with hero IDs.",
    ),
    FieldDefinition(
        "ordered_draft_actions",
        "ordered draft action sequence",
        "HERO / DRAFT DATA",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Pick/ban actions with explicit provider order.",
    ),
    FieldDefinition(
        "draft_action_order",
        "action/order number",
        "HERO / DRAFT DATA",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Explicit order on every returned draft action.",
    ),
    FieldDefinition(
        "draft_action_kind",
        "pick vs ban",
        "HERO / DRAFT DATA",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Explicit pick/ban kind on every returned draft action.",
    ),
    FieldDefinition(
        "draft_action_side",
        "action team/side",
        "HERO / DRAFT DATA",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Radiant/Dire side on every returned draft action.",
    ),
    FieldDefinition(
        "draft_action_hero_id",
        "draft action hero ID",
        "HERO / DRAFT DATA",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Hero ID on every returned draft action.",
    ),
    FieldDefinition(
        "first_pick_side",
        "first-pick side",
        "HERO / DRAFT DATA",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Derived only from explicit ordered draft actions.",
    ),
    FieldDefinition(
        "draft_completion_status",
        "draft completion status",
        "HERO / DRAFT DATA",
        (DataUsage.TARGET_GAME_PRE_OUTCOME_INPUT,),
        "Explicit provider completion flag or derived complete 5v5 picks.",
    ),
    FieldDefinition(
        "duplicate_malformed_draft_behavior",
        "duplicate/malformed draft behavior",
        "HERO / DRAFT DATA",
        (DataUsage.UNSUITABLE_OR_UNCLEAR,),
        "Whether duplicate or malformed draft actions can be detected.",
    ),
    FieldDefinition(
        "team_kills_final_score",
        "team kills / final score",
        "BASIC MAP OUTCOME DATA",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game team kills or derivable from player kills.",
    ),
    FieldDefinition(
        "individual_kills",
        "individual kills",
        "BASIC MAP OUTCOME DATA",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game player kills for all ten players.",
    ),
    FieldDefinition(
        "deaths",
        "deaths",
        "BASIC MAP OUTCOME DATA",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game player deaths for all ten players.",
    ),
    FieldDefinition(
        "assists",
        "assists",
        "BASIC MAP OUTCOME DATA",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game player assists for all ten players.",
    ),
    FieldDefinition(
        "final_net_worth",
        "final net worth",
        "PLAYER ECONOMY / FARM",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game final net worth, not a target-map draft feature.",
    ),
    FieldDefinition(
        "last_hits",
        "last hits",
        "PLAYER ECONOMY / FARM",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game last hits for all ten players.",
    ),
    FieldDefinition(
        "denies",
        "denies",
        "PLAYER ECONOMY / FARM",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game denies for all ten players.",
    ),
    FieldDefinition(
        "gpm",
        "GPM",
        "PLAYER ECONOMY / FARM",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game gold per minute.",
    ),
    FieldDefinition(
        "xpm",
        "XPM",
        "PLAYER ECONOMY / FARM",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game experience per minute.",
    ),
    FieldDefinition(
        "level",
        "level",
        "PLAYER ECONOMY / FARM",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Post-game player level.",
    ),
    FieldDefinition(
        "damage",
        "damage/building/heal",
        "COMBAT / OBJECTIVES",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Hero damage, tower damage, and healing for all ten players.",
    ),
    FieldDefinition(
        "final_items",
        "final inventory/items",
        "ITEMS",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Final inventory/backpack/neutral item IDs.",
    ),
    FieldDefinition(
        "timed_item_data",
        "item timing coverage",
        "ITEMS",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Provider parsed item acquisition timings.",
    ),
    FieldDefinition(
        "kill_events",
        "kill/combat event timeline",
        "TIMELINE",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Provider parsed combat timeline events.",
    ),
    FieldDefinition(
        "minute_farm_timeline",
        "minute farm timeline",
        "TIMELINE",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Minute-by-minute farm/economy arrays.",
    ),
    FieldDefinition(
        "advantage_timeline",
        "advantage/minute timeline",
        "TIMELINE",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Provider parsed net worth / XP / gold advantage arrays.",
    ),
    FieldDefinition(
        "tower_barracks_objectives",
        "tower/barracks/objectives",
        "OBJECTIVES",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Tower/barracks status or explicit building objective events.",
    ),
    FieldDefinition(
        "roshan_objectives",
        "Roshan/objective events",
        "OBJECTIVES",
        (
            DataUsage.POST_GAME_TARGET_OR_LABEL,
            DataUsage.PRIOR_GAME_HISTORICAL_CONTEXT_ONLY,
        ),
        "Roshan or neutral objective events.",
    ),
)

_FIELD_DEFINITION_BY_KEY = {field.key: field for field in FIELD_DEFINITIONS}

_SCALAR_GRAPHQL_KINDS = {"SCALAR", "ENUM"}
_SCALAR_GRAPHQL_TYPE_NAMES = {
    "Boolean",
    "DateTime",
    "Float",
    "ID",
    "Int",
    "Long",
    "Short",
    "String",
}
_ROOT_QUERY_CANDIDATE_TYPES = ("DotaQuery",)
_MATCH_TYPE_NAME = "MatchType"
_MATCH_ID_FETCH_FIELD = "matches"
_MATCH_ID_FETCH_ARGUMENT = "ids"
_OBSERVED_MAX_MATCH_IDS_PER_REQUEST = 10

_DESIRED_MATCH_FIELDS: tuple[str, ...] = (
    "id",
    "matchId",
    "startDateTime",
    "startTime",
    "endDateTime",
    "endTime",
    "durationSeconds",
    "duration",
    "didRadiantWin",
    "radiantWin",
    "winnerSide",
    "winner",
    "gameMode",
    "gameModeId",
    "lobbyType",
    "lobbyTypeId",
    "gameVersionId",
    "patchId",
    "patch",
    "parsedDateTime",
    "league",
    "leagueId",
    "tournament",
    "tournamentId",
    "tournamentRound",
    "series",
    "seriesId",
    "gameNumber",
    "bestOf",
    "seriesType",
    "radiantTeam",
    "direTeam",
    "radiantTeamId",
    "direTeamId",
    "players",
    "playerMatches",
    "pickBans",
    "picksBans",
    "draftActions",
    "radiantKills",
    "direKills",
    "towerStatusRadiant",
    "towerStatusDire",
    "barracksStatusRadiant",
    "barracksStatusDire",
    "radiantNetworthLeads",
    "radiantExperienceLeads",
    "goldAdvantage",
    "xpAdvantage",
    "minuteStats",
    "playbackData",
)

_DESIRED_FIELD_NAMES_BY_SEMANTIC: Mapping[str, tuple[str, ...]] = {
    "stable_match_id": ("id", "matchId"),
    "source_reference": ("id", "matchId"),
    "start_timestamp": ("startDateTime", "startTime"),
    "end_timestamp": ("endDateTime", "endTime", "startDateTime", "durationSeconds"),
    "duration": ("durationSeconds", "duration"),
    "winner_side": ("didRadiantWin", "radiantWin", "winnerSide", "winner"),
    "radiant_dire_orientation": (
        "radiantTeam",
        "direTeam",
        "radiantTeamId",
        "direTeamId",
        "players",
        "playerMatches",
    ),
    "game_mode": ("gameMode", "gameModeId"),
    "lobby_professional_identity": ("lobbyType", "lobbyTypeId", "league", "leagueId"),
    "league_id": ("league", "leagueId"),
    "league_name": ("league",),
    "tournament_context": (
        "tournament",
        "tournamentId",
        "tournamentRound",
        "league",
        "leagueId",
        "series",
        "seriesId",
    ),
    "series_id": ("series", "seriesId"),
    "game_number": ("gameNumber", "series"),
    "series_type_best_of": ("bestOf", "seriesType", "series"),
    "pre_current_map_series_score": (
        "radiantSeriesWinsBefore",
        "direSeriesWinsBefore",
        "teamOneSeriesWinsBefore",
        "teamTwoSeriesWinsBefore",
    ),
    "patch_id": ("gameVersionId", "patchId", "patch"),
    "patch_name": ("gameVersion", "patch"),
    "radiant_team_id": ("radiantTeam", "radiantTeamId"),
    "dire_team_id": ("direTeam", "direTeamId"),
    "team_display_names": ("radiantTeam", "direTeam"),
    "stable_provider_team_ids": (
        "radiantTeam",
        "direTeam",
        "radiantTeamId",
        "direTeamId",
    ),
    "organization_vs_roster_identity": (
        "radiantTeam",
        "direTeam",
        "radiantTeamId",
        "direTeamId",
    ),
    "player_account_ids": ("players", "playerMatches"),
    "player_sides": ("players", "playerMatches"),
    "player_slots": ("players", "playerMatches"),
    "player_hero_ids": ("players", "playerMatches"),
    "player_display_names": ("players", "playerMatches"),
    "player_role_lane": ("players", "playerMatches"),
    "radiant_picks": ("players", "playerMatches", "pickBans", "picksBans", "draftActions"),
    "dire_picks": ("players", "playerMatches", "pickBans", "picksBans", "draftActions"),
    "complete_5v5_picks": (
        "players",
        "playerMatches",
        "pickBans",
        "picksBans",
        "draftActions",
    ),
    "bans": ("pickBans", "picksBans", "draftActions"),
    "ordered_draft_actions": ("pickBans", "picksBans", "draftActions"),
    "draft_action_order": ("pickBans", "picksBans", "draftActions"),
    "draft_action_kind": ("pickBans", "picksBans", "draftActions"),
    "draft_action_side": ("pickBans", "picksBans", "draftActions"),
    "draft_action_hero_id": ("pickBans", "picksBans", "draftActions"),
    "first_pick_side": ("pickBans", "picksBans", "draftActions"),
    "draft_completion_status": (
        "draftComplete",
        "isDraftComplete",
        "players",
        "playerMatches",
        "pickBans",
        "picksBans",
        "draftActions",
    ),
    "duplicate_malformed_draft_behavior": ("pickBans", "picksBans", "draftActions"),
    "team_kills_final_score": ("radiantKills", "direKills", "players", "playerMatches"),
    "individual_kills": ("players", "playerMatches"),
    "deaths": ("players", "playerMatches"),
    "assists": ("players", "playerMatches"),
    "final_net_worth": ("players", "playerMatches"),
    "last_hits": ("players", "playerMatches"),
    "denies": ("players", "playerMatches"),
    "gpm": ("players", "playerMatches"),
    "xpm": ("players", "playerMatches"),
    "level": ("players", "playerMatches"),
    "damage": ("players", "playerMatches"),
    "final_items": ("players", "playerMatches"),
    "timed_item_data": ("players", "playerMatches"),
    "kill_events": ("playbackData",),
    "minute_farm_timeline": ("minuteStats", "players", "playerMatches"),
    "advantage_timeline": (
        "radiantNetworthLeads",
        "radiantExperienceLeads",
        "goldAdvantage",
        "xpAdvantage",
    ),
    "tower_barracks_objectives": (
        "towerStatusRadiant",
        "towerStatusDire",
        "barracksStatusRadiant",
        "barracksStatusDire",
        "playbackData",
    ),
    "roshan_objectives": ("playbackData",),
}

_COMMON_OBJECT_FIELD_DESIRES: Mapping[str, tuple[str, ...]] = {
    "team": ("id", "teamId", "name", "displayName", "tag"),
    "league": ("id", "leagueId", "name", "displayName"),
    "series": ("id", "seriesId", "type", "bestOf", "gameNumber"),
    "player": (
        "steamAccountId",
        "accountId",
        "playerId",
        "playerSlot",
        "position",
        "slot",
        "lane",
        "role",
        "isRadiant",
        "side",
        "teamSide",
        "heroId",
        "hero_id",
        "kills",
        "deaths",
        "assists",
        "numLastHits",
        "lastHits",
        "numDenies",
        "denies",
        "goldPerMinute",
        "gpm",
        "experiencePerMinute",
        "xpm",
        "networth",
        "netWorth",
        "level",
        "heroDamage",
        "towerDamage",
        "heroHealing",
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
        "items",
        "goldTimeline",
        "xpTimeline",
        "lastHitTimeline",
        "name",
        "displayName",
        "steamAccountName",
    ),
    "draft": (
        "order",
        "ord",
        "sequence",
        "isPick",
        "type",
        "action",
        "kind",
        "heroId",
        "hero_id",
        "team",
        "side",
        "teamSide",
        "isRadiant",
    ),
    "playback": ("killEvents", "kills", "roshanEvents", "buildingEvents"),
    "event": ("time", "gameTime", "attacker", "target", "team", "key", "type"),
    "item": ("itemId", "time", "gameTime"),
}

_FIELD_GROUP_DESIRES: Mapping[str, tuple[str, ...]] = {
    "IDENTITY_CONTEXT": (
        "id",
        "matchId",
        "startDateTime",
        "startTime",
        "durationSeconds",
        "duration",
        "didRadiantWin",
        "radiantWin",
        "leagueId",
        "tournamentId",
        "tournamentRound",
        "seriesId",
        "gameNumber",
        "bestOf",
        "seriesType",
    ),
    "TEAM_CONTEXT": (
        "radiantTeam",
        "direTeam",
        "radiantTeamId",
        "direTeamId",
    ),
    "PLAYERS_BASIC": ("players", "playerMatches"),
    "DRAFT": ("pickBans", "picksBans", "draftActions"),
    "BASIC_OUTCOME": (
        "didRadiantWin",
        "radiantWin",
        "durationSeconds",
        "duration",
        "radiantKills",
        "direKills",
    ),
    "PLAYER_FINAL_STATS": ("players", "playerMatches"),
    "ITEMS": ("players", "playerMatches"),
    "TIMELINES": (
        "radiantNetworthLeads",
        "radiantExperienceLeads",
        "goldAdvantage",
        "xpAdvantage",
        "minuteStats",
    ),
    "COMBAT_EVENTS": ("playbackData",),
    "OBJECTIVES": (
        "towerStatusRadiant",
        "towerStatusDire",
        "barracksStatusRadiant",
        "barracksStatusDire",
        "playbackData",
    ),
    "PATCH_VERSION": ("gameVersionId", "patchId", "patch", "gameVersion"),
}


class StratzFeasibilityProbe:
    def __init__(
        self,
        client: StratzGraphQLClient,
        *,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.sleep_func = sleep_func

    def run(
        self,
        *,
        sample_size: int = 12,
        match_ids: Sequence[str] = (),
        delay_seconds: float = 1.0,
        real_source: bool = True,
    ) -> StratzProbeResult:
        if sample_size < 1:
            raise ValueError("sample_size must be at least 1")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")

        request_count = 0
        warnings: list[str] = []
        probe_started_at = datetime.now(timezone.utc)

        schema, schema_requests = inspect_stratz_schema(self.client)
        request_count += schema_requests
        query_plan = build_stratz_query_plan(schema)
        query_type = schema.type_definition(query_plan.query_type_name)
        query_field_names = tuple(
            sorted(query_type.fields if query_type is not None else ())
        )

        selected_ids = tuple(str(match_id).strip() for match_id in match_ids if str(match_id).strip())
        selection_method = "explicit --match-id values"
        if not selected_ids:
            raise ValueError(_missing_discovery_message(query_plan))

        if not query_plan.can_fetch_matches_by_ids or query_plan.match_query is None:
            raise ValueError(
                "STRATZ schema introspection did not verify a usable "
                "matches(ids: ...) fetch field."
            )

        access_capability = probe_stratz_access_capability(
            client=self.client,
            schema=schema,
            query_plan=query_plan,
            match_id=selected_ids[0],
        )
        request_count += access_capability.capability_probe_requests
        query_plan = apply_access_capability_to_query_plan(
            schema=schema,
            query_plan=query_plan,
            capability=access_capability,
        )

        analyses: list[StratzMatchAnalysis] = []
        sample_fetch_requests = 0
        batches = chunk_match_ids(
            selected_ids[:sample_size],
            batch_size=access_capability.observed_max_match_ids_per_request,
        )
        if (
            access_capability.minimal_match_fetch_status is AccessStatus.ACCESSIBLE
            and query_plan.match_query is not None
        ):
            for batch_index, batch_ids in enumerate(batches, start=1):
                response = self.client.execute(
                    query_plan.match_query,
                    {"ids": [_int_match_id(match_id) for match_id in batch_ids]},
                )
                request_count += 1
                sample_fetch_requests += 1
                payloads = response.data.get(_MATCH_ID_FETCH_FIELD)
                if not isinstance(payloads, list):
                    warnings.append("STRATZ matches(ids: ...) returned no match list.")
                    payloads = []
                for index, payload in enumerate(payloads):
                    if not isinstance(payload, Mapping):
                        requested_id = (
                            batch_ids[index] if index < len(batch_ids) else "unknown"
                        )
                        warnings.append(
                            f"Skipped STRATZ match without data: {requested_id}"
                        )
                        continue
                    analyses.append(
                        analyze_stratz_match_payload(payload, query_plan=query_plan)
                    )
                if delay_seconds and batch_index < len(batches):
                    self.sleep_func(delay_seconds)
        else:
            warnings.append(
                "STRATZ sample fetch skipped because minimal match fetch is "
                f"{access_capability.minimal_match_fetch_status.value}."
            )
        access_capability = replace(
            access_capability,
            sample_fetch_requests=sample_fetch_requests,
        )

        coverage = aggregate_field_coverage(analyses)
        competition_families = tuple(
            analysis.identity.competition_family for analysis in analyses
        )
        verdict = determine_source_verdict(
            coverage,
            sample_count=len(analyses),
            real_source=real_source,
            competition_families=competition_families,
            minimum_competition_families=2,
        )
        if (
            real_source
            and len(analyses) >= 10
            and _known_competition_family_count(competition_families) < 2
        ):
            warnings.append(
                "STRATZ sample is single-family only; treating it as an access "
                "smoke sample, not final multi-family source evidence."
            )
        return StratzProbeResult(
            real_source=real_source,
            probe_started_at=probe_started_at,
            request_count=request_count,
            sampled_match_ids=tuple(analysis.identity.match_id for analysis in analyses),
            sample_selection_method=selection_method,
            query_field_names=query_field_names,
            query_plan=query_plan,
            access_capability=access_capability,
            analyses=tuple(analyses),
            coverage=coverage,
            verdict=verdict,
            warnings=tuple(warnings),
        )


def inspect_stratz_schema(
    client: StratzGraphQLClient,
) -> tuple[StratzSchemaSnapshot, int]:
    overview = client.execute(STRATZ_SCHEMA_OVERVIEW_QUERY)
    query_type_name = _query_type_name(overview.data)
    request_count = 1
    types: dict[str, GraphQLTypeDefinition] = {}
    pending = [query_type_name, *_ROOT_QUERY_CANDIDATE_TYPES, _MATCH_TYPE_NAME]
    seen: set[str] = set()

    while pending:
        type_name = pending.pop(0)
        if type_name in seen:
            continue
        seen.add(type_name)
        response = client.execute(STRATZ_TYPE_INTROSPECTION_QUERY, {"name": type_name})
        request_count += 1
        type_definition = parse_graphql_type_definition(response.data)
        if type_definition is None:
            continue
        types[type_definition.name] = type_definition

        if type_definition.name == _MATCH_TYPE_NAME:
            for nested_type in _nested_type_names_for_fields(
                type_definition,
                _DESIRED_MATCH_FIELDS,
            ):
                if nested_type not in seen and nested_type not in pending:
                    pending.append(nested_type)
        elif type_definition.name != query_type_name:
            desired = _desired_fields_for_type_name(type_definition.name)
            for nested_type in _nested_type_names_for_fields(type_definition, desired):
                if nested_type not in seen and nested_type not in pending:
                    pending.append(nested_type)

    return (
        StratzSchemaSnapshot(
            query_type_name=query_type_name,
            types=types,
        ),
        request_count,
    )


def build_stratz_query_plan(schema: StratzSchemaSnapshot) -> StratzQueryPlan:
    root_type = (
        schema.type_definition(schema.query_type_name)
        or schema.type_definition("DotaQuery")
    )
    match_type = schema.type_definition(_MATCH_TYPE_NAME)
    if root_type is None:
        raise ValueError("STRATZ schema introspection did not expose DotaQuery.")
    if match_type is None:
        raise ValueError("STRATZ schema introspection did not expose MatchType.")

    matches_field = root_type.fields.get(_MATCH_ID_FETCH_FIELD)
    ids_argument = (
        matches_field.args.get(_MATCH_ID_FETCH_ARGUMENT)
        if matches_field is not None
        else None
    )
    ids_argument_type = ids_argument.rendered_type if ids_argument is not None else None
    selected_fields, nested_fields_used = _selection_lines_for_match_fields(
        schema,
        _DESIRED_MATCH_FIELDS,
    )
    used_match_fields = tuple(_selection_field_name(field) for field in selected_fields)
    absent_match_fields = tuple(
        field for field in _DESIRED_MATCH_FIELDS if field not in match_type.fields
    )
    match_query: str | None = None
    if matches_field is not None and ids_argument is not None and selected_fields:
        match_query = _build_matches_query(
            operation_name="StratzHistoricalMatchFeasibility",
            argument_type=ids_argument.rendered_type,
            selection_lines=selected_fields,
        )

    return StratzQueryPlan(
        query_type_name=root_type.name,
        match_fetch_field=_MATCH_ID_FETCH_FIELD if matches_field is not None else None,
        match_fetch_argument=(
            _MATCH_ID_FETCH_ARGUMENT if ids_argument is not None else None
        ),
        match_fetch_argument_type=ids_argument_type,
        match_query=match_query,
        match_type_fields_used=used_match_fields,
        match_type_fields_absent=absent_match_fields,
        match_type_fields_restricted=(),
        restricted_field_paths=(),
        nested_fields_used=nested_fields_used,
        inspected_type_names=tuple(sorted(schema.types)),
        professional_discovery_path=None,
        additional_query_requirements=_additional_query_requirements(match_type),
    )


def parse_graphql_type_definition(
    payload: Mapping[str, object],
) -> GraphQLTypeDefinition | None:
    type_payload = payload.get("__type")
    if not isinstance(type_payload, Mapping):
        return None
    name = _text(type_payload.get("name"))
    kind = _text(type_payload.get("kind")) or "UNKNOWN"
    if name is None:
        return None
    fields_payload = type_payload.get("fields")
    fields: dict[str, GraphQLField] = {}
    if isinstance(fields_payload, list):
        for item in fields_payload:
            if not isinstance(item, Mapping):
                continue
            field_name = _text(item.get("name"))
            type_ref_payload = item.get("type")
            if field_name is None or not isinstance(type_ref_payload, Mapping):
                continue
            args_payload = item.get("args")
            args: dict[str, GraphQLArgument] = {}
            if isinstance(args_payload, list):
                for arg in args_payload:
                    if not isinstance(arg, Mapping):
                        continue
                    arg_name = _text(arg.get("name"))
                    arg_type_payload = arg.get("type")
                    if arg_name is None or not isinstance(arg_type_payload, Mapping):
                        continue
                    args[arg_name] = GraphQLArgument(
                        name=arg_name,
                        type_ref=parse_graphql_type_ref(arg_type_payload),
                    )
            fields[field_name] = GraphQLField(
                name=field_name,
                type_ref=parse_graphql_type_ref(type_ref_payload),
                args=args,
            )
    return GraphQLTypeDefinition(name=name, kind=kind, fields=fields)


def parse_graphql_type_ref(payload: Mapping[str, object]) -> GraphQLTypeRef:
    of_type = payload.get("ofType")
    return GraphQLTypeRef(
        kind=_text(payload.get("kind")) or "UNKNOWN",
        name=_text(payload.get("name")),
        of_type=parse_graphql_type_ref(of_type) if isinstance(of_type, Mapping) else None,
    )


def render_graphql_type(type_ref: GraphQLTypeRef) -> str:
    if type_ref.kind == "NON_NULL" and type_ref.of_type is not None:
        return f"{render_graphql_type(type_ref.of_type)}!"
    if type_ref.kind == "LIST" and type_ref.of_type is not None:
        return f"[{render_graphql_type(type_ref.of_type)}]"
    return type_ref.name or type_ref.kind


def named_graphql_type(type_ref: GraphQLTypeRef) -> str | None:
    if type_ref.kind in ("NON_NULL", "LIST") and type_ref.of_type is not None:
        return named_graphql_type(type_ref.of_type)
    return type_ref.name


def extract_query_field_names(payload: Mapping[str, object]) -> tuple[str, ...]:
    type_definition = parse_graphql_type_definition(payload)
    if type_definition is None:
        return ()
    return tuple(sorted(type_definition.fields))


def chunk_match_ids(
    match_ids: Sequence[str],
    *,
    batch_size: int = _OBSERVED_MAX_MATCH_IDS_PER_REQUEST,
) -> tuple[tuple[str, ...], ...]:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    normalized = tuple(str(match_id).strip() for match_id in match_ids if str(match_id).strip())
    return tuple(
        normalized[index : index + batch_size]
        for index in range(0, len(normalized), batch_size)
    )


def classify_graphql_error(value: str) -> GraphQLErrorClassification:
    normalized = value.casefold()
    if "user is not an admin" in normalized:
        return GraphQLErrorClassification.PERMISSION_RESTRICTED
    if "requesting too many matchids" in normalized or "max request size" in normalized:
        return GraphQLErrorClassification.REQUEST_SIZE_LIMIT
    if "cannot query field" in normalized or "unknown argument" in normalized:
        return GraphQLErrorClassification.SCHEMA_ERROR
    if (
        "was not provided" in normalized
        or "required type" in normalized
        or "variable" in normalized
    ):
        return GraphQLErrorClassification.CLIENT_QUERY_ERROR
    return GraphQLErrorClassification.GRAPHQL_ERROR


def probe_stratz_access_capability(
    *,
    client: StratzGraphQLClient,
    schema: StratzSchemaSnapshot,
    query_plan: StratzQueryPlan,
    match_id: str,
) -> StratzAccessCapability:
    if query_plan.match_fetch_argument_type is None:
        return StratzAccessCapability(
            observed_max_match_ids_per_request=_OBSERVED_MAX_MATCH_IDS_PER_REQUEST,
            minimal_match_fetch_status=AccessStatus.GRAPHQL_ERROR,
            minimal_match_fetch_message="matches(ids: ...) not verified by schema.",
            field_groups=(),
            capability_probe_requests=0,
        )

    requests = 0
    minimal_query = _query_for_desired_fields(
        schema=schema,
        query_plan=query_plan,
        desired_fields=("id", "matchId"),
        operation_name="StratzMinimalMatchAccessProbe",
    )
    if minimal_query is None:
        return StratzAccessCapability(
            observed_max_match_ids_per_request=_OBSERVED_MAX_MATCH_IDS_PER_REQUEST,
            minimal_match_fetch_status=AccessStatus.GRAPHQL_ERROR,
            minimal_match_fetch_message="No schema-present match ID field found.",
            field_groups=(),
            capability_probe_requests=0,
        )

    minimal_result = _execute_access_probe(
        client=client,
        query=minimal_query,
        match_id=match_id,
    )
    requests += 1
    if minimal_result.status is not AccessStatus.ACCESSIBLE:
        return StratzAccessCapability(
            observed_max_match_ids_per_request=_OBSERVED_MAX_MATCH_IDS_PER_REQUEST,
            minimal_match_fetch_status=minimal_result.status,
            minimal_match_fetch_message=minimal_result.message,
            field_groups=(),
            capability_probe_requests=requests,
        )

    group_results: list[StratzFieldGroupAccess] = []
    for group_name, desired_fields in _FIELD_GROUP_DESIRES.items():
        group_selection = tuple(dict.fromkeys(("id", *desired_fields)))
        selected_paths = _selected_paths_for_group(schema, group_selection)
        if not selected_paths:
            group_results.append(
                StratzFieldGroupAccess(
                    group_name=group_name,
                    status=AccessStatus.ACCESS_UNVERIFIED,
                    selected_paths=(),
                    message="No selected fields from current schema.",
                )
            )
            continue
        query = _query_for_desired_fields(
            schema=schema,
            query_plan=query_plan,
            desired_fields=group_selection,
            operation_name=f"Stratz{group_name.title().replace('_', '')}AccessProbe",
        )
        if query is None:
            continue
        result = _execute_access_probe(
            client=client,
            query=query,
            match_id=match_id,
        )
        requests += 1
        if result.status is AccessStatus.PERMISSION_RESTRICTED:
            narrowed = _narrow_restricted_group(
                client=client,
                schema=schema,
                query_plan=query_plan,
                match_id=match_id,
                desired_fields=desired_fields,
            )
            requests += narrowed.capability_probe_requests
            group_results.append(
                StratzFieldGroupAccess(
                    group_name=group_name,
                    status=AccessStatus.PERMISSION_RESTRICTED,
                    selected_paths=selected_paths,
                    restricted_path=narrowed.restricted_paths[0]
                    if narrowed.restricted_paths
                    else None,
                    message=result.message,
                )
            )
        else:
            group_results.append(
                StratzFieldGroupAccess(
                    group_name=group_name,
                    status=result.status,
                    selected_paths=selected_paths,
                    message=result.message,
                )
            )

    return StratzAccessCapability(
        observed_max_match_ids_per_request=_OBSERVED_MAX_MATCH_IDS_PER_REQUEST,
        minimal_match_fetch_status=AccessStatus.ACCESSIBLE,
        minimal_match_fetch_message=None,
        field_groups=tuple(group_results),
        capability_probe_requests=requests,
    )


def apply_access_capability_to_query_plan(
    *,
    schema: StratzSchemaSnapshot,
    query_plan: StratzQueryPlan,
    capability: StratzAccessCapability,
) -> StratzQueryPlan:
    if capability.minimal_match_fetch_status is not AccessStatus.ACCESSIBLE:
        restricted_top_level = _restricted_top_level_fields(
            capability.restricted_paths,
            schema,
        )
        return replace(
            query_plan,
            match_query=None,
            match_type_fields_restricted=restricted_top_level,
            restricted_field_paths=capability.restricted_paths,
        )

    restricted_top_level_set = set(
        _restricted_top_level_fields(capability.restricted_paths, schema)
    )
    accessible_group_fields: list[str] = ["id", "matchId"]
    for group_name, desired_fields in _FIELD_GROUP_DESIRES.items():
        group = next(
            (item for item in capability.field_groups if item.group_name == group_name),
            None,
        )
        if group is None or group.status is not AccessStatus.ACCESSIBLE:
            continue
        accessible_group_fields.extend(desired_fields)

    desired = tuple(
        field
        for field in dict.fromkeys(accessible_group_fields)
        if field not in restricted_top_level_set
    )
    selection_lines, nested_fields_used = _selection_lines_for_match_fields(
        schema,
        desired,
    )
    selection_lines = tuple(
        line
        for line in selection_lines
        if _selection_field_name(line) not in restricted_top_level_set
    )
    match_query = (
        _build_matches_query(
            operation_name="StratzAccessibleMatchSample",
            argument_type=query_plan.match_fetch_argument_type,
            selection_lines=selection_lines,
        )
        if query_plan.match_fetch_argument_type is not None and selection_lines
        else None
    )
    return replace(
        query_plan,
        match_query=match_query,
        match_type_fields_used=tuple(_selection_field_name(line) for line in selection_lines),
        match_type_fields_restricted=tuple(sorted(restricted_top_level_set)),
        restricted_field_paths=capability.restricted_paths,
        nested_fields_used=nested_fields_used,
    )


@dataclass(frozen=True)
class _AccessProbeResult:
    status: AccessStatus
    message: str | None = None


def _execute_access_probe(
    *,
    client: StratzGraphQLClient,
    query: str,
    match_id: str,
) -> _AccessProbeResult:
    try:
        response = client.execute(query, {"ids": [_int_match_id(match_id)]})
    except StratzGraphQLError as exc:
        message = str(exc)
        classification = classify_graphql_error(message)
        if classification is GraphQLErrorClassification.PERMISSION_RESTRICTED:
            return _AccessProbeResult(AccessStatus.PERMISSION_RESTRICTED, message)
        if classification is GraphQLErrorClassification.REQUEST_SIZE_LIMIT:
            return _AccessProbeResult(AccessStatus.REQUEST_SIZE_LIMIT, message)
        return _AccessProbeResult(AccessStatus.GRAPHQL_ERROR, message)
    payload = response.data.get(_MATCH_ID_FETCH_FIELD)
    if not isinstance(payload, list) or not payload:
        return _AccessProbeResult(AccessStatus.SAMPLE_MISSING, "No match rows returned.")
    first = payload[0]
    if not isinstance(first, Mapping):
        return _AccessProbeResult(AccessStatus.SAMPLE_MISSING, "Malformed match row.")
    return _AccessProbeResult(AccessStatus.ACCESSIBLE)


def _narrow_restricted_group(
    *,
    client: StratzGraphQLClient,
    schema: StratzSchemaSnapshot,
    query_plan: StratzQueryPlan,
    match_id: str,
    desired_fields: Sequence[str],
) -> StratzAccessCapability:
    requests = 0
    restricted_paths: list[str] = []
    match_type = schema.type_definition(_MATCH_TYPE_NAME)
    if match_type is None:
        return StratzAccessCapability(
            observed_max_match_ids_per_request=_OBSERVED_MAX_MATCH_IDS_PER_REQUEST,
            minimal_match_fetch_status=AccessStatus.ACCESSIBLE,
            minimal_match_fetch_message=None,
            field_groups=(),
            capability_probe_requests=requests,
        )
    for field_name in desired_fields:
        if field_name not in match_type.fields:
            continue
        query = _query_for_desired_fields(
            schema=schema,
            query_plan=query_plan,
            desired_fields=("id", field_name),
            operation_name="StratzRestrictedFieldNarrowing",
        )
        if query is None:
            continue
        result = _execute_access_probe(client=client, query=query, match_id=match_id)
        requests += 1
        if result.status is not AccessStatus.PERMISSION_RESTRICTED:
            continue
        nested_path = _narrow_nested_field(
            client=client,
            schema=schema,
            query_plan=query_plan,
            match_id=match_id,
            field_name=field_name,
        )
        requests += nested_path.capability_probe_requests
        if nested_path.restricted_paths:
            restricted_paths.extend(nested_path.restricted_paths)
        else:
            restricted_paths.append(f"{_MATCH_TYPE_NAME}.{field_name}")
        break
    return StratzAccessCapability(
        observed_max_match_ids_per_request=_OBSERVED_MAX_MATCH_IDS_PER_REQUEST,
        minimal_match_fetch_status=AccessStatus.ACCESSIBLE,
        minimal_match_fetch_message=None,
        field_groups=(),
        capability_probe_requests=requests,
        sample_fetch_requests=0,
    ) if not restricted_paths else StratzAccessCapability(
        observed_max_match_ids_per_request=_OBSERVED_MAX_MATCH_IDS_PER_REQUEST,
        minimal_match_fetch_status=AccessStatus.ACCESSIBLE,
        minimal_match_fetch_message=None,
        field_groups=(
            StratzFieldGroupAccess(
                group_name="NARROWED",
                status=AccessStatus.PERMISSION_RESTRICTED,
                selected_paths=tuple(restricted_paths),
                restricted_path=restricted_paths[0],
            ),
        ),
        capability_probe_requests=requests,
    )


def _narrow_nested_field(
    *,
    client: StratzGraphQLClient,
    schema: StratzSchemaSnapshot,
    query_plan: StratzQueryPlan,
    match_id: str,
    field_name: str,
) -> StratzAccessCapability:
    match_type = schema.type_definition(_MATCH_TYPE_NAME)
    if match_type is None:
        return _empty_narrowing_result()
    field = match_type.fields.get(field_name)
    if field is None or field.named_type is None:
        return _empty_narrowing_result()
    nested_type = schema.type_definition(field.named_type)
    if nested_type is None:
        return _empty_narrowing_result()
    requests = 0
    for nested_field in _desired_fields_for_type_name(nested_type.name):
        if nested_field not in nested_type.fields:
            continue
        line = _selection_line_for_parent_child(
            schema=schema,
            parent_field=field_name,
            child_field=nested_field,
        )
        if line is None or query_plan.match_fetch_argument_type is None:
            continue
        query = _build_matches_query(
            operation_name="StratzRestrictedNestedFieldNarrowing",
            argument_type=query_plan.match_fetch_argument_type,
            selection_lines=("id", line),
        )
        result = _execute_access_probe(client=client, query=query, match_id=match_id)
        requests += 1
        if result.status is AccessStatus.PERMISSION_RESTRICTED:
            return StratzAccessCapability(
                observed_max_match_ids_per_request=_OBSERVED_MAX_MATCH_IDS_PER_REQUEST,
                minimal_match_fetch_status=AccessStatus.ACCESSIBLE,
                minimal_match_fetch_message=None,
                field_groups=(
                    StratzFieldGroupAccess(
                        group_name="NARROWED",
                        status=AccessStatus.PERMISSION_RESTRICTED,
                        selected_paths=(f"{nested_type.name}.{nested_field}",),
                        restricted_path=f"{nested_type.name}.{nested_field}",
                    ),
                ),
                capability_probe_requests=requests,
            )
    return replace(_empty_narrowing_result(), capability_probe_requests=requests)


def _empty_narrowing_result() -> StratzAccessCapability:
    return StratzAccessCapability(
        observed_max_match_ids_per_request=_OBSERVED_MAX_MATCH_IDS_PER_REQUEST,
        minimal_match_fetch_status=AccessStatus.ACCESSIBLE,
        minimal_match_fetch_message=None,
        field_groups=(),
        capability_probe_requests=0,
    )


def _selection_lines_for_match_fields(
    schema: StratzSchemaSnapshot,
    desired_fields: Sequence[str],
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    nested_fields_used: dict[str, tuple[str, ...]] = {}
    return (
        _selection_for_type(
            schema=schema,
            type_name=_MATCH_TYPE_NAME,
            desired_fields=desired_fields,
            nested_fields_used=nested_fields_used,
            depth=0,
        ),
        nested_fields_used,
    )


def _query_for_desired_fields(
    *,
    schema: StratzSchemaSnapshot,
    query_plan: StratzQueryPlan,
    desired_fields: Sequence[str],
    operation_name: str,
) -> str | None:
    if query_plan.match_fetch_argument_type is None:
        return None
    selection_lines, _ = _selection_lines_for_match_fields(schema, desired_fields)
    if not selection_lines:
        return None
    return _build_matches_query(
        operation_name=operation_name,
        argument_type=query_plan.match_fetch_argument_type,
        selection_lines=selection_lines,
    )


def _build_matches_query(
    *,
    operation_name: str,
    argument_type: str,
    selection_lines: Sequence[str],
) -> str:
    selection = "\n".join(f"    {line}" for line in selection_lines)
    return (
        f"query {operation_name}(${_MATCH_ID_FETCH_ARGUMENT}: {argument_type}) {{\n"
        f"  {_MATCH_ID_FETCH_FIELD}({_MATCH_ID_FETCH_ARGUMENT}: "
        f"${_MATCH_ID_FETCH_ARGUMENT}) {{\n"
        f"{selection}\n"
        "  }\n"
        "}"
    )


def _selected_paths_for_group(
    schema: StratzSchemaSnapshot,
    desired_fields: Sequence[str],
) -> tuple[str, ...]:
    match_type = schema.type_definition(_MATCH_TYPE_NAME)
    if match_type is None:
        return ()
    paths: list[str] = []
    for field_name in desired_fields:
        field = match_type.fields.get(field_name)
        if field is None:
            continue
        if _is_scalar_or_enum(schema, field):
            paths.append(f"{_MATCH_TYPE_NAME}.{field_name}")
            continue
        if field.named_type is None:
            paths.append(f"{_MATCH_TYPE_NAME}.{field_name}")
            continue
        nested_type = schema.type_definition(field.named_type)
        if nested_type is None:
            paths.append(f"{_MATCH_TYPE_NAME}.{field_name}")
            continue
        nested_fields = [
            child
            for child in _desired_fields_for_type_name(nested_type.name)
            if child in nested_type.fields
        ]
        if not nested_fields:
            paths.append(f"{_MATCH_TYPE_NAME}.{field_name}")
        else:
            paths.extend(f"{nested_type.name}.{child}" for child in nested_fields)
    return tuple(dict.fromkeys(paths))


def _selection_line_for_parent_child(
    *,
    schema: StratzSchemaSnapshot,
    parent_field: str,
    child_field: str,
) -> str | None:
    match_type = schema.type_definition(_MATCH_TYPE_NAME)
    if match_type is None:
        return None
    parent = match_type.fields.get(parent_field)
    if parent is None or parent.named_type is None:
        return None
    nested_type = schema.type_definition(parent.named_type)
    if nested_type is None:
        return None
    child = nested_type.fields.get(child_field)
    if child is None:
        return None
    if _is_scalar_or_enum(schema, child):
        child_selection = child_field
    elif child.named_type is not None:
        grandchild_desired = _desired_fields_for_type_name(child.named_type)
        grandchild_lines, _ = _selection_lines_for_type(
            schema=schema,
            type_name=child.named_type,
            desired_fields=grandchild_desired,
        )
        if not grandchild_lines:
            return None
        body = "\n".join(f"        {line}" for line in grandchild_lines)
        child_selection = f"{child_field} {{\n{body}\n      }}"
    else:
        return None
    return f"{parent_field} {{\n      {child_selection}\n    }}"


def _selection_lines_for_type(
    *,
    schema: StratzSchemaSnapshot,
    type_name: str,
    desired_fields: Sequence[str],
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    nested_fields_used: dict[str, tuple[str, ...]] = {}
    return (
        _selection_for_type(
            schema=schema,
            type_name=type_name,
            desired_fields=desired_fields,
            nested_fields_used=nested_fields_used,
            depth=0,
        ),
        nested_fields_used,
    )


def _restricted_top_level_fields(
    paths: Sequence[str],
    schema: StratzSchemaSnapshot,
) -> tuple[str, ...]:
    match_type = schema.type_definition(_MATCH_TYPE_NAME)
    if match_type is None:
        return ()
    fields: list[str] = []
    for path in paths:
        if path.startswith(f"{_MATCH_TYPE_NAME}."):
            fields.append(path.split(".", maxsplit=1)[1])
            continue
        type_name = path.split(".", maxsplit=1)[0]
        for field_name, field in match_type.fields.items():
            if field.named_type == type_name:
                fields.append(field_name)
                break
    return tuple(sorted(set(fields)))


def _selection_for_type(
    *,
    schema: StratzSchemaSnapshot,
    type_name: str,
    desired_fields: Sequence[str],
    nested_fields_used: dict[str, tuple[str, ...]],
    depth: int,
) -> tuple[str, ...]:
    type_definition = schema.type_definition(type_name)
    if type_definition is None:
        return ()
    selected: list[str] = []
    selected_names: list[str] = []
    for field_name in desired_fields:
        field = type_definition.fields.get(field_name)
        if field is None:
            continue
        named_type = field.named_type
        if _is_scalar_or_enum(schema, field):
            selected.append(field_name)
            selected_names.append(field_name)
            continue
        if named_type is None or depth >= 3:
            continue
        nested_desired = _desired_fields_for_type_name(named_type)
        nested_selection = _selection_for_type(
            schema=schema,
            type_name=named_type,
            desired_fields=nested_desired,
            nested_fields_used=nested_fields_used,
            depth=depth + 1,
        )
        if not nested_selection:
            continue
        body = "\n".join(f"      {line}" for line in nested_selection)
        selected.append(f"{field_name} {{\n{body}\n    }}")
        selected_names.append(field_name)
    if selected_names:
        nested_fields_used[type_name] = tuple(selected_names)
    return tuple(selected)


def _is_scalar_or_enum(
    schema: StratzSchemaSnapshot,
    field: GraphQLField,
) -> bool:
    if field.type_ref.kind in _SCALAR_GRAPHQL_KINDS:
        return True
    named_type = field.named_type
    if named_type is None:
        return False
    if named_type in _SCALAR_GRAPHQL_TYPE_NAMES:
        return True
    type_definition = schema.type_definition(named_type)
    return type_definition is not None and type_definition.kind in _SCALAR_GRAPHQL_KINDS


def _desired_fields_for_type_name(type_name: str) -> tuple[str, ...]:
    lowered = type_name.casefold()
    if "player" in lowered:
        return _COMMON_OBJECT_FIELD_DESIRES["player"]
    if "pick" in lowered or "ban" in lowered or "draft" in lowered:
        return _COMMON_OBJECT_FIELD_DESIRES["draft"]
    if "team" in lowered:
        return _COMMON_OBJECT_FIELD_DESIRES["team"]
    if "league" in lowered:
        return _COMMON_OBJECT_FIELD_DESIRES["league"]
    if "series" in lowered:
        return _COMMON_OBJECT_FIELD_DESIRES["series"]
    if "playback" in lowered or "timeline" in lowered:
        return _COMMON_OBJECT_FIELD_DESIRES["playback"]
    if "event" in lowered:
        return _COMMON_OBJECT_FIELD_DESIRES["event"]
    if "item" in lowered:
        return _COMMON_OBJECT_FIELD_DESIRES["item"]
    return (
        "id",
        "name",
        "displayName",
        "type",
        "time",
        "gameTime",
        "value",
    )


def _nested_type_names_for_fields(
    type_definition: GraphQLTypeDefinition,
    desired_fields: Sequence[str],
) -> tuple[str, ...]:
    names: list[str] = []
    for field_name in desired_fields:
        field = type_definition.fields.get(field_name)
        if field is None:
            continue
        named_type = field.named_type
        if (
            named_type is not None
            and named_type not in _SCALAR_GRAPHQL_TYPE_NAMES
            and named_type not in names
            and field.type_ref.kind not in _SCALAR_GRAPHQL_KINDS
        ):
            names.append(named_type)
    return tuple(names)


def _selection_field_name(selection: str) -> str:
    return selection.split(maxsplit=1)[0]


def _query_type_name(payload: Mapping[str, object]) -> str:
    schema = payload.get("__schema")
    if not isinstance(schema, Mapping):
        raise ValueError("STRATZ schema overview did not include __schema.")
    query_type = schema.get("queryType")
    if not isinstance(query_type, Mapping):
        raise ValueError("STRATZ schema overview did not include queryType.")
    name = _text(query_type.get("name"))
    if name is None:
        raise ValueError("STRATZ schema overview did not include query type name.")
    return name


def _additional_query_requirements(
    match_type: GraphQLTypeDefinition,
) -> tuple[str, ...]:
    requirements: list[str] = []
    if "league" not in match_type.fields and "leagueId" in match_type.fields:
        requirements.append("league metadata by leagueId")
    if "tournament" not in match_type.fields and "tournamentId" in match_type.fields:
        requirements.append("tournament metadata by tournamentId")
    if "gameVersion" not in match_type.fields and "gameVersionId" in match_type.fields:
        requirements.append("game version metadata by gameVersionId")
    return tuple(requirements)


def _missing_discovery_message(query_plan: StratzQueryPlan) -> str:
    verified_fetch = "unverified"
    if (
        query_plan.match_fetch_field is not None
        and query_plan.match_fetch_argument is not None
        and query_plan.match_fetch_argument_type is not None
    ):
        verified_fetch = (
            f"{query_plan.match_fetch_field}"
            f"({query_plan.match_fetch_argument}: "
            f"{query_plan.match_fetch_argument_type})"
        )
    return (
        "Automatic STRATZ professional-match discovery is not verified by the "
        "current introspected schema. Use repeated --match-id values for this "
        "probe. "
        f"Verified match fetch field: {verified_fetch}. "
        "Professional discovery path not verified."
    )


def _int_match_id(value: str) -> int:
    integer = _int(value)
    if integer is None:
        raise ValueError("STRATZ --match-id values must be integer IDs.")
    return integer


def parse_match_candidates(
    payload: Mapping[str, object],
) -> tuple[StratzMatchCandidate, ...]:
    rows = _find_first_list(payload, ("matches", "proMatches", "leagueMatches"))
    candidates: list[StratzMatchCandidate] = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        match_id = _text_at(item, ("id", "matchId"))
        if match_id is None:
            continue
        league = _mapping(item.get("league"))
        tournament = _mapping(item.get("tournament"))
        series = _mapping(item.get("series"))
        radiant_team = _mapping(item.get("radiantTeam"))
        dire_team = _mapping(item.get("direTeam"))
        candidates.append(
            StratzMatchCandidate(
                match_id=match_id,
                started_at=_datetime_at(item, ("startDateTime", "startTime")),
                league_name=_name_from_mapping(league),
                tournament_name=_name_from_mapping(tournament),
                series_name=_name_from_mapping(series),
                radiant_team_name=_name_from_mapping(radiant_team),
                dire_team_name=_name_from_mapping(dire_team),
            )
        )
    return tuple(candidates)


def select_representative_candidates(
    candidates: Iterable[StratzMatchCandidate],
    *,
    sample_size: int,
) -> tuple[StratzMatchCandidate, ...]:
    if sample_size < 1:
        raise ValueError("sample_size must be at least 1")

    scope = DEFAULT_HISTORICAL_COMPETITION_SCOPE
    eligible = [
        candidate
        for candidate in candidates
        if candidate.started_at is not None
        and candidate.started_at >= scope.target_start_at
        and _candidate_scope_eligible(candidate)
    ]
    grouped: dict[HistoricalCompetitionFamily, list[StratzMatchCandidate]] = defaultdict(list)
    for candidate in sorted(
        eligible,
        key=lambda item: (item.started_at or datetime.min.replace(tzinfo=timezone.utc), item.match_id),
    ):
        grouped[candidate.competition_family].append(candidate)

    selected: list[StratzMatchCandidate] = []
    family_order = tuple(
        family
        for family in (
            HistoricalCompetitionFamily.THE_INTERNATIONAL,
            HistoricalCompetitionFamily.ESPORTS_WORLD_CUP,
            HistoricalCompetitionFamily.DREAMLEAGUE,
            HistoricalCompetitionFamily.FISSURE_PLAYGROUND,
            HistoricalCompetitionFamily.BETBOOM_DACHA,
            HistoricalCompetitionFamily.BLAST,
            HistoricalCompetitionFamily.PGL,
            HistoricalCompetitionFamily.ESL,
        )
        if family in grouped
    )
    while len(selected) < sample_size:
        added = False
        for family in family_order:
            rows = grouped[family]
            if not rows:
                continue
            if len(selected) % 2 == 0:
                candidate = rows.pop(0)
            else:
                candidate = rows.pop()
            selected.append(candidate)
            added = True
            if len(selected) == sample_size:
                break
        if not added:
            break
    return tuple(selected)


def analyze_stratz_match_payload(
    payload: Mapping[str, object],
    *,
    query_plan: StratzQueryPlan | None = None,
) -> StratzMatchAnalysis:
    players = _players(payload)
    actions = _draft_actions(payload)
    identity = _sample_identity(payload)
    observations = {
        definition.key: _observe_field(
            definition.key,
            payload,
            players,
            actions,
            query_plan,
        )
        for definition in FIELD_DEFINITIONS
    }
    warnings = _draft_warnings(actions)
    return StratzMatchAnalysis(
        identity=identity,
        observations=observations,
        warnings=warnings,
    )


def aggregate_field_coverage(
    analyses: Sequence[StratzMatchAnalysis],
) -> tuple[FieldCoverage, ...]:
    rows: list[FieldCoverage] = []
    for definition in FIELD_DEFINITIONS:
        observations = [
            analysis.observations.get(definition.key, FieldObservation(False))
            for analysis in analyses
        ]
        applicable_count = len(observations)
        present_count = sum(1 for observation in observations if observation.present)
        coverage_pct = (
            present_count / applicable_count * 100.0
            if applicable_count
            else 0.0
        )
        rows.append(
            FieldCoverage(
                key=definition.key,
                label=definition.label,
                family=definition.family,
                present_count=present_count,
                applicable_count=applicable_count,
                coverage_pct=coverage_pct,
                classification=_coverage_classification(definition, observations),
                usage=definition.usage,
                semantics=_coverage_semantics(definition, observations),
            )
        )
    return tuple(rows)


def determine_source_verdict(
    coverage: Sequence[FieldCoverage],
    *,
    sample_count: int,
    real_source: bool,
    minimum_real_samples: int = 10,
    competition_families: Sequence[HistoricalCompetitionFamily] = (),
    minimum_competition_families: int = 1,
) -> SourceVerdict | None:
    if not real_source:
        return None
    if sample_count < minimum_real_samples:
        return None
    if (
        minimum_competition_families > 1
        and _known_competition_family_count(competition_families)
        < minimum_competition_families
    ):
        return None

    by_key = {row.key: row for row in coverage}
    required_keys = (
        "stable_match_id",
        "start_timestamp",
        "winner_side",
        "duration",
        "radiant_dire_orientation",
        "radiant_team_id",
        "dire_team_id",
        "player_account_ids",
        "player_hero_ids",
        "complete_5v5_picks",
        "ordered_draft_actions",
        "team_kills_final_score",
    )
    required_coverage = [
        by_key[key].coverage_pct
        for key in required_keys
        if key in by_key
    ]
    if len(required_coverage) != len(required_keys):
        return SourceVerdict.STRATZ_FREE_SOURCE_INSUFFICIENT
    if all(value >= 80.0 for value in required_coverage):
        return SourceVerdict.STRATZ_FREE_SOURCE_FEASIBLE
    return SourceVerdict.STRATZ_FREE_SOURCE_INSUFFICIENT


def _known_competition_family_count(
    families: Sequence[HistoricalCompetitionFamily],
) -> int:
    return len(
        {
            family
            for family in families
            if family is not HistoricalCompetitionFamily.UNKNOWN
        }
    )


def render_probe_result(result: StratzProbeResult) -> str:
    lines: list[str] = []
    lines.append("STRATZ free historical game data feasibility probe")
    lines.append("")
    lines.append("Source: STRATZ free GraphQL API / Default Token")
    lines.append(f"Probe started: {_format_datetime(result.probe_started_at)}")
    lines.append(f"Real source: {'yes' if result.real_source else 'no'}")
    lines.append(f"Requests: {result.request_count}")
    lines.append(f"Sample selection: {result.sample_selection_method}")
    lines.append(f"Samples: {len(result.analyses)}")
    if result.sampled_match_ids:
        lines.append(f"Sampled match IDs: {', '.join(result.sampled_match_ids)}")
    if result.query_field_names:
        preview = ", ".join(result.query_field_names[:20])
        suffix = " ..." if len(result.query_field_names) > 20 else ""
        lines.append(f"Query fields observed: {preview}{suffix}")
    if result.query_plan is not None:
        lines.append("")
        lines.append("Schema / query plan")
        plan = result.query_plan
        if (
            plan.match_fetch_field is not None
            and plan.match_fetch_argument is not None
            and plan.match_fetch_argument_type is not None
        ):
            lines.append(
                "Verified match fetch field: "
                f"{plan.match_fetch_field}"
                f"({plan.match_fetch_argument}: "
                f"{plan.match_fetch_argument_type})"
            )
        else:
            lines.append("Verified match fetch field: unavailable")
        lines.append(
            "Verified MatchType fields used: "
            + (", ".join(plan.match_type_fields_used) or "none")
        )
        absent_preview = ", ".join(plan.match_type_fields_absent[:20])
        absent_suffix = " ..." if len(plan.match_type_fields_absent) > 20 else ""
        lines.append(
            "Desired MatchType fields absent: "
            + (f"{absent_preview}{absent_suffix}" if absent_preview else "none")
        )
        if plan.professional_discovery_path is None:
            lines.append("Professional discovery path not verified")
        else:
            lines.append(
                "Verified professional discovery path: "
                f"{plan.professional_discovery_path}"
            )
        if plan.additional_query_requirements:
            lines.append(
                "Additional API query required for enrichment: "
                + ", ".join(plan.additional_query_requirements)
            )
    if result.access_capability is not None:
        capability = result.access_capability
        lines.append("")
        lines.append("Access capability")
        lines.append(
            "Observed max match IDs per request: "
            f"{capability.observed_max_match_ids_per_request}"
        )
        lines.append(
            "Minimal match ID fetch: "
            f"{capability.minimal_match_fetch_status.value}"
        )
        if capability.minimal_match_fetch_message:
            lines.append(f"Minimal fetch message: {capability.minimal_match_fetch_message}")
        for group in capability.field_groups:
            line = f"Field group {group.group_name}: {group.status.value}"
            if group.restricted_path is not None:
                line += f" | Restricted field/subtree: {group.restricted_path}"
            lines.append(line)
        lines.append(
            "Accessible rich query fields: "
            + (
                ", ".join(result.query_plan.match_type_fields_used)
                if result.query_plan is not None
                else "none"
            )
        )
        if capability.restricted_paths:
            lines.append(
                "Restricted field/subtree findings: "
                + ", ".join(capability.restricted_paths)
            )
        lines.append(
            f"Capability-probe requests: {capability.capability_probe_requests}"
        )
        lines.append(f"Sample-fetch requests: {capability.sample_fetch_requests}")
    if result.warnings:
        lines.append("")
        lines.append(f"Warnings: {len(result.warnings)}")
        for warning in result.warnings[:10]:
            lines.append(f"Warning: {warning}")

    lines.append("")
    lines.append("Sample identity")
    for index, analysis in enumerate(result.analyses, start=1):
        identity = analysis.identity
        teams = _teams_text(identity)
        lines.append(
            f"{index}. {identity.match_id} | "
            f"{_format_datetime(identity.started_at)} | "
            f"{identity.league_name or identity.tournament_name or 'unknown'} | "
            f"{identity.competition_family.name} | {teams}"
        )

    lines.append("")
    lines.append("Field coverage matrix")
    lines.append(
        "field | present/applicable | coverage | classification | usage | semantics"
    )
    for row in result.coverage:
        usage = ",".join(item.value for item in row.usage)
        lines.append(
            f"{row.label} | {row.present_count}/{row.applicable_count} | "
            f"{row.coverage_pct:.1f}% | {row.classification.value} | "
            f"{usage} | {row.semantics}"
        )

    lines.append("")
    lines.append("Feasibility verdict")
    if result.verdict is None:
        lines.append("UNVERIFIED_PENDING_REAL_PROBE")
        lines.append("Source role recommendation: pending successful real probe.")
    else:
        lines.append(result.verdict.value)
        role = (
            "PRIMARY HISTORICAL GAME SOURCE CANDIDATE"
            if result.verdict is SourceVerdict.STRATZ_FREE_SOURCE_FEASIBLE
            else "INSUFFICIENT SOURCE"
        )
        lines.append(f"Source role recommendation: {role}")
    return "\n".join(lines)


def coverage_by_key(
    coverage: Sequence[FieldCoverage],
) -> dict[str, FieldCoverage]:
    return {row.key: row for row in coverage}


def _observe_field(
    key: str,
    payload: Mapping[str, object],
    players: Sequence[Mapping[str, object]],
    actions: Sequence[_DraftAction],
    query_plan: StratzQueryPlan | None = None,
) -> FieldObservation:
    schema_absent = _coverage_key_schema_absent(key, query_plan)
    if schema_absent:
        return FieldObservation(
            present=False,
            note=_schema_absent_note(key, query_plan),
            schema_absent=True,
        )
    access_restricted = _coverage_key_access_restricted(key, query_plan)
    if access_restricted:
        return FieldObservation(
            present=False,
            note=_access_restricted_note(key, query_plan),
            access_restricted=True,
        )
    if key == "stable_match_id":
        return _present(_text_at(payload, ("id", "matchId")) is not None)
    if key == "source_reference":
        return _present(
            _text_at(payload, ("id", "matchId")) is not None,
            CoverageClassification.DERIVABLE_FROM_RETURNED_FIELDS,
        )
    if key == "start_timestamp":
        return _present(_datetime_at(payload, ("startDateTime", "startTime")) is not None)
    if key == "end_timestamp":
        explicit = _datetime_at(payload, ("endDateTime", "endTime"))
        derivable = explicit is None and _has_start_and_duration(payload)
        return _present(
            explicit is not None or derivable,
            CoverageClassification.DERIVABLE_FROM_RETURNED_FIELDS if derivable else None,
        )
    if key == "duration":
        return _present(_number_at(payload, ("durationSeconds", "duration")) is not None)
    if key == "winner_side":
        return _present(
            _bool_at(payload, ("didRadiantWin", "radiantWin")) is not None
            or _text_at(payload, ("winnerSide", "winner")) is not None
        )
    if key == "radiant_dire_orientation":
        return _present(_has_radiant_dire_orientation(payload, players))
    if key == "game_mode":
        return _present(_text_at(payload, ("gameMode", "gameModeId")) is not None)
    if key == "lobby_professional_identity":
        return _present(
            _text_at(payload, ("lobbyType", "lobbyTypeId")) is not None
            or _mapping(payload.get("league")) is not None
            or _text_at(payload, ("leagueId",)) is not None
        )
    if key == "league_id":
        return _present(
            _nested_text(payload, "league", ("id", "leagueId")) is not None
            or _text_at(payload, ("leagueId",)) is not None
        )
    if key == "league_name":
        return _present(_name_from_mapping(_mapping(payload.get("league"))) is not None)
    if key == "tournament_context":
        return _present(
            _name_from_mapping(_mapping(payload.get("tournament"))) is not None
            or _text_at(payload, ("tournamentId", "tournamentRound")) is not None
            or _name_from_mapping(_mapping(payload.get("league"))) is not None
            or _text_at(payload, ("leagueId",)) is not None
            or _name_from_mapping(_mapping(payload.get("series"))) is not None
            or _text_at(payload, ("seriesId",)) is not None
        )
    if key == "series_id":
        return _present(
            _nested_text(payload, "series", ("id", "seriesId")) is not None
            or _text_at(payload, ("seriesId",)) is not None
        )
    if key == "game_number":
        return _present(
            _number_at(payload, ("gameNumber",))
            is not None
            or _nested_number(payload, "series", ("gameNumber", "game"))
            is not None
        )
    if key == "series_type_best_of":
        return _present(
            _text_at(payload, ("bestOf", "seriesType")) is not None
            or _nested_text(payload, "series", ("type", "bestOf")) is not None
        )
    if key == "pre_current_map_series_score":
        return _present(
            _number_at(
                payload,
                (
                    "radiantSeriesWinsBefore",
                    "direSeriesWinsBefore",
                    "teamOneSeriesWinsBefore",
                    "teamTwoSeriesWinsBefore",
                ),
            )
            is not None
        )
    if key == "patch_id":
        return _present(_text_at(payload, ("gameVersionId", "patchId", "patch")) is not None)
    if key == "patch_name":
        return _present(
            _nested_text(payload, "gameVersion", ("name", "displayName"))
            is not None
            or _nested_text(payload, "patch", ("name", "displayName"))
            is not None
        )
    if key == "radiant_team_id":
        return _present(
            _nested_text(payload, "radiantTeam", ("id", "teamId")) is not None
            or _text_at(payload, ("radiantTeamId",)) is not None
        )
    if key == "dire_team_id":
        return _present(
            _nested_text(payload, "direTeam", ("id", "teamId")) is not None
            or _text_at(payload, ("direTeamId",)) is not None
        )
    if key == "team_display_names":
        return _present(
            _name_from_mapping(_mapping(payload.get("radiantTeam"))) is not None
            and _name_from_mapping(_mapping(payload.get("direTeam"))) is not None
        )
    if key == "stable_provider_team_ids":
        return _present(
            (
                _nested_text(payload, "radiantTeam", ("id", "teamId"))
                is not None
                or _text_at(payload, ("radiantTeamId",)) is not None
            )
            and (
                _nested_text(payload, "direTeam", ("id", "teamId")) is not None
                or _text_at(payload, ("direTeamId",)) is not None
            )
        )
    if key == "organization_vs_roster_identity":
        return _present(
            _nested_text(payload, "radiantTeam", ("id", "teamId")) is not None
            or _nested_text(payload, "direTeam", ("id", "teamId")) is not None
            or _text_at(payload, ("radiantTeamId", "direTeamId")) is not None,
            CoverageClassification.UNKNOWN_SEMANTICS,
        )
    if key == "player_account_ids":
        return _present(_all_players_have(players, ("steamAccountId", "accountId", "playerId")))
    if key == "player_sides":
        return _present(_all_players_have_side(players))
    if key == "player_slots":
        return _present(_all_players_have(players, ("playerSlot", "position", "slot")))
    if key == "player_hero_ids":
        return _present(_all_players_have(players, ("heroId", "hero_id")))
    if key == "player_display_names":
        return _present(_all_players_have(players, ("name", "displayName", "steamAccountName")))
    if key == "player_role_lane":
        return _present(
            _all_players_have(players, ("lane", "role")),
            CoverageClassification.UNKNOWN_SEMANTICS,
        )
    if key == "radiant_picks":
        return _present(_side_pick_count(players, actions, "radiant") == 5)
    if key == "dire_picks":
        return _present(_side_pick_count(players, actions, "dire") == 5)
    if key == "complete_5v5_picks":
        return _present(
            _side_pick_count(players, actions, "radiant") == 5
            and _side_pick_count(players, actions, "dire") == 5
        )
    if key == "bans":
        return _present(any(action.kind == "ban" and action.hero_id is not None for action in actions))
    if key == "ordered_draft_actions":
        return _present(_has_ordered_actions(actions))
    if key == "draft_action_order":
        return _present(bool(actions) and all(action.order is not None for action in actions))
    if key == "draft_action_kind":
        return _present(bool(actions) and all(action.kind in ("pick", "ban") for action in actions))
    if key == "draft_action_side":
        return _present(bool(actions) and all(action.side in ("radiant", "dire") for action in actions))
    if key == "draft_action_hero_id":
        return _present(bool(actions) and all(action.hero_id is not None for action in actions))
    if key == "first_pick_side":
        return _present(
            _first_pick_side(actions) is not None,
            CoverageClassification.DERIVABLE_FROM_RETURNED_FIELDS,
        )
    if key == "draft_completion_status":
        explicit_draft_complete = _bool_at(
            payload,
            ("draftComplete", "isDraftComplete"),
        )
        derived_draft_complete = (
            _side_pick_count(players, actions, "radiant") == 5
            and _side_pick_count(players, actions, "dire") == 5
        )
        return _present(
            explicit_draft_complete is not None or derived_draft_complete,
            (
                CoverageClassification.DERIVABLE_FROM_RETURNED_FIELDS
                if explicit_draft_complete is None and derived_draft_complete
                else None
            ),
        )
    if key == "duplicate_malformed_draft_behavior":
        return _present(bool(actions), CoverageClassification.DERIVABLE_FROM_RETURNED_FIELDS)
    if key == "team_kills_final_score":
        explicit_team_kills = (
            _number_at(payload, ("radiantKills", "direKills")) is not None
        )
        derived_team_kills = _all_players_have(
            players,
            ("kills",),
        ) and _all_players_have_side(players)
        return _present(
            explicit_team_kills or derived_team_kills,
            (
                CoverageClassification.DERIVABLE_FROM_RETURNED_FIELDS
                if derived_team_kills and not explicit_team_kills
                else None
            ),
        )
    if key == "individual_kills":
        return _present(_all_players_have(players, ("kills",)))
    if key == "deaths":
        return _present(_all_players_have(players, ("deaths",)))
    if key == "assists":
        return _present(_all_players_have(players, ("assists",)))
    if key == "final_net_worth":
        return _present(_all_players_have(players, ("networth", "netWorth")))
    if key == "last_hits":
        return _present(_all_players_have(players, ("numLastHits", "lastHits")))
    if key == "denies":
        return _present(_all_players_have(players, ("numDenies", "denies")))
    if key == "gpm":
        return _present(_all_players_have(players, ("goldPerMinute", "gpm")))
    if key == "xpm":
        return _present(_all_players_have(players, ("experiencePerMinute", "xpm")))
    if key == "level":
        return _present(_all_players_have(players, ("level",)))
    if key == "damage":
        return _present(_all_players_have(players, ("heroDamage", "towerDamage", "heroHealing")))
    if key == "final_items":
        return _present(_players_have_final_items(players))
    if key == "timed_item_data":
        return _present(_players_have_timed_items(players), CoverageClassification.PROVIDER_DERIVED)
    if key == "kill_events":
        return _present(
            bool(_nested_list(payload, "playbackData", ("killEvents", "kills"))),
            CoverageClassification.PROVIDER_DERIVED,
        )
    if key == "minute_farm_timeline":
        return _present(_has_minute_farm_timeline(payload, players), CoverageClassification.PROVIDER_DERIVED)
    if key == "advantage_timeline":
        return _present(
            bool(
                _list_at(
                    payload,
                    (
                        "radiantNetworthLeads",
                        "radiantExperienceLeads",
                        "goldAdvantage",
                        "xpAdvantage",
                    ),
                )
            ),
            CoverageClassification.PROVIDER_DERIVED,
        )
    if key == "tower_barracks_objectives":
        return _present(
            _number_at(
                payload,
                (
                    "towerStatusRadiant",
                    "towerStatusDire",
                    "barracksStatusRadiant",
                    "barracksStatusDire",
                ),
            )
            is not None
            or bool(_nested_list(payload, "playbackData", ("buildingEvents",))),
            CoverageClassification.PROVIDER_DERIVED,
        )
    if key == "roshan_objectives":
        return _present(
            bool(_nested_list(payload, "playbackData", ("roshanEvents",))),
            CoverageClassification.PROVIDER_DERIVED,
        )
    raise KeyError(f"Unhandled STRATZ coverage field: {key}")


def _coverage_classification(
    definition: FieldDefinition,
    observations: Sequence[FieldObservation],
) -> CoverageClassification:
    if not observations:
        return CoverageClassification.ABSENT
    present = [observation for observation in observations if observation.present]
    if not present:
        if definition.requires_additional_query_when_absent:
            return CoverageClassification.REQUIRES_ADDITIONAL_PUBLIC_API_QUERY
        return CoverageClassification.ABSENT
    if len(present) != len(observations):
        return CoverageClassification.PARTIAL
    explicit_classifications = {
        observation.classification
        for observation in present
        if observation.classification is not None
    }
    if len(explicit_classifications) == 1:
        return explicit_classifications.pop()
    if len(explicit_classifications) > 1:
        return CoverageClassification.PARTIAL
    return CoverageClassification.ALWAYS_PRESENT


def _coverage_semantics(
    definition: FieldDefinition,
    observations: Sequence[FieldObservation],
) -> str:
    notes = sorted(
        {
            observation.note
            for observation in observations
            if observation.note.strip()
        }
    )
    if notes:
        return f"{definition.semantics} {' '.join(notes)}"
    return definition.semantics


def _coverage_key_schema_absent(
    key: str,
    query_plan: StratzQueryPlan | None,
) -> bool:
    if query_plan is None:
        return False
    desired = _DESIRED_FIELD_NAMES_BY_SEMANTIC.get(key, ())
    if not desired:
        return False
    used_or_available = set(query_plan.match_type_fields_used)
    absent = set(query_plan.match_type_fields_absent)
    if any(field in used_or_available for field in desired):
        return False
    return all(field in absent for field in desired)


def _schema_absent_note(
    key: str,
    query_plan: StratzQueryPlan | None,
) -> str:
    desired = _DESIRED_FIELD_NAMES_BY_SEMANTIC.get(key, ())
    absent = []
    if query_plan is not None:
        absent = [field for field in desired if field in query_plan.match_type_fields_absent]
    if absent:
        return "ABSENT: field not present in verified current schema: " + ", ".join(absent) + "."
    return "ABSENT: supporting field not present in verified current schema."


def _coverage_key_access_restricted(
    key: str,
    query_plan: StratzQueryPlan | None,
) -> bool:
    if query_plan is None or not query_plan.match_type_fields_restricted:
        return False
    desired = _DESIRED_FIELD_NAMES_BY_SEMANTIC.get(key, ())
    restricted = set(query_plan.match_type_fields_restricted)
    if not desired:
        return False
    if any(field in query_plan.match_type_fields_used for field in desired):
        return False
    return any(field in restricted for field in desired)


def _access_restricted_note(
    key: str,
    query_plan: StratzQueryPlan | None,
) -> str:
    if query_plan is None:
        return "schema present but permission restricted for current STRATZ Default Token."
    desired = set(_DESIRED_FIELD_NAMES_BY_SEMANTIC.get(key, ()))
    restricted = sorted(desired.intersection(query_plan.match_type_fields_restricted))
    if restricted:
        return (
            "schema present but permission restricted for current STRATZ "
            f"Default Token: {', '.join(restricted)}."
        )
    return "schema present but permission restricted for current STRATZ Default Token."


def _present(
    value: bool,
    classification: CoverageClassification | None = None,
) -> FieldObservation:
    return FieldObservation(present=value, classification=classification if value else None)


def _sample_identity(payload: Mapping[str, object]) -> StratzSampleIdentity:
    match_id = _text_at(payload, ("id", "matchId")) or "unknown"
    league = _mapping(payload.get("league"))
    tournament = _mapping(payload.get("tournament"))
    series = _mapping(payload.get("series"))
    radiant_team = _mapping(payload.get("radiantTeam"))
    dire_team = _mapping(payload.get("direTeam"))
    started_at = _datetime_at(payload, ("startDateTime", "startTime"))
    league_name = _name_from_mapping(league)
    tournament_name = _name_from_mapping(tournament)
    series_name = _name_from_mapping(series)
    radiant_team_name = _name_from_mapping(radiant_team)
    dire_team_name = _name_from_mapping(dire_team)
    return StratzSampleIdentity(
        match_id=match_id,
        started_at=started_at,
        league_name=league_name,
        tournament_name=tournament_name,
        series_id=_nested_text(payload, "series", ("id", "seriesId")),
        radiant_team_name=radiant_team_name,
        dire_team_name=dire_team_name,
        competition_family=_competition_family_from_metadata(
            match_id=match_id,
            started_at=started_at,
            league_name=league_name,
            tournament_name=tournament_name,
            series_name=series_name,
            radiant_team_name=radiant_team_name,
            dire_team_name=dire_team_name,
        ),
    )


def _candidate_scope_eligible(candidate: StratzMatchCandidate) -> bool:
    match = _historical_match_from_metadata(
        match_id=candidate.match_id,
        started_at=candidate.started_at,
        league_name=candidate.league_name,
        tournament_name=candidate.tournament_name,
        series_name=candidate.series_name,
        radiant_team_name=candidate.radiant_team_name,
        dire_team_name=candidate.dire_team_name,
    )
    return (
        is_historical_match_scope_eligible(match)
        and not is_historical_competition_qualifier(match)
    )


def _competition_family_from_metadata(
    *,
    match_id: str,
    started_at: datetime | None,
    league_name: str | None,
    tournament_name: str | None,
    series_name: str | None,
    radiant_team_name: str | None,
    dire_team_name: str | None,
) -> HistoricalCompetitionFamily:
    match = _historical_match_from_metadata(
        match_id=match_id,
        started_at=started_at,
        league_name=league_name,
        tournament_name=tournament_name,
        series_name=series_name,
        radiant_team_name=radiant_team_name,
        dire_team_name=dire_team_name,
    )
    return classify_historical_competition_family(match)


def _historical_match_from_metadata(
    *,
    match_id: str,
    started_at: datetime | None,
    league_name: str | None,
    tournament_name: str | None,
    series_name: str | None,
    radiant_team_name: str | None,
    dire_team_name: str | None,
) -> HistoricalMatch:
    safe_started_at = started_at or datetime(1970, 1, 1, tzinfo=timezone.utc)
    return HistoricalMatch(
        id=f"stratz-{match_id}",
        source="stratz",
        source_match_id=match_id,
        started_at=safe_started_at,
        ended_at=None,
        team_a_name=radiant_team_name or "Radiant",
        team_b_name=dire_team_name or "Dire",
        tournament_name=tournament_name,
        league_name=league_name,
        series_name=series_name,
        competitive_stage=CompetitiveStage.UNKNOWN,
        normalized_round=TournamentRound.UNKNOWN,
        status="finished",
    )


def _players(payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    players = _list_at(payload, ("players", "playerMatches"))
    return tuple(item for item in players if isinstance(item, Mapping))


def _draft_actions(payload: Mapping[str, object]) -> tuple[_DraftAction, ...]:
    rows = _list_at(payload, ("pickBans", "draftActions", "picksBans"))
    actions: list[_DraftAction] = []
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, Mapping):
            continue
        order = _int(item.get("order") or item.get("ord") or item.get("sequence"))
        kind = _draft_kind(item)
        side = _side_from_mapping(item)
        hero_id = _int(item.get("heroId") or item.get("hero_id"))
        if order is None and (kind is not None or side is not None or hero_id is not None):
            order = index
        actions.append(_DraftAction(order=order, kind=kind, side=side, hero_id=hero_id))
    return tuple(actions)


def _draft_kind(item: Mapping[str, object]) -> str | None:
    is_pick = item.get("isPick")
    if isinstance(is_pick, bool):
        return "pick" if is_pick else "ban"
    value = _text(item.get("type") or item.get("action") or item.get("kind"))
    if value is None:
        return None
    normalized = value.casefold()
    if "pick" in normalized:
        return "pick"
    if "ban" in normalized:
        return "ban"
    return None


def _side_from_mapping(item: Mapping[str, object]) -> str | None:
    side_value = item.get("side") or item.get("teamSide") or item.get("team")
    side = _side(side_value)
    if side is not None:
        return side
    is_radiant = item.get("isRadiant")
    if isinstance(is_radiant, bool):
        return "radiant" if is_radiant else "dire"
    return None


def _side(value: object) -> str | None:
    if value in (0, "0", "radiant", "Radiant", "RADIANT"):
        return "radiant"
    if value in (1, "1", "dire", "Dire", "DIRE"):
        return "dire"
    text = _text(value)
    if text is None:
        return None
    lowered = text.casefold()
    if lowered == "radiant":
        return "radiant"
    if lowered == "dire":
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


def _side_pick_count(
    players: Sequence[Mapping[str, object]],
    actions: Sequence[_DraftAction],
    side: str,
) -> int:
    action_heroes = {
        action.hero_id
        for action in actions
        if action.kind == "pick" and action.side == side and action.hero_id is not None
    }
    if action_heroes:
        return len(action_heroes)
    player_heroes = {
        hero_id
        for player in players
        if _player_side(player) == side
        and (hero_id := _int(player.get("heroId") or player.get("hero_id"))) is not None
    }
    return len(player_heroes)


def _has_ordered_actions(actions: Sequence[_DraftAction]) -> bool:
    orders = [action.order for action in actions]
    return bool(orders) and all(order is not None for order in orders) and len(set(orders)) == len(orders)


def _first_pick_side(actions: Sequence[_DraftAction]) -> str | None:
    ordered = sorted(
        (action for action in actions if action.order is not None),
        key=lambda action: action.order or 0,
    )
    for action in ordered:
        if action.kind == "pick" and action.side in ("radiant", "dire"):
            return action.side
    return None


def _draft_warnings(actions: Sequence[_DraftAction]) -> tuple[str, ...]:
    warnings: list[str] = []
    orders = [action.order for action in actions if action.order is not None]
    if len(orders) != len(set(orders)):
        warnings.append("Duplicate draft action order encountered.")
    if any(
        action.kind is None or action.side is None or action.hero_id is None
        for action in actions
    ):
        warnings.append("Malformed or incomplete draft action encountered.")
    return tuple(warnings)


def _all_players_have(
    players: Sequence[Mapping[str, object]],
    keys: tuple[str, ...],
) -> bool:
    return len(players) >= 10 and all(_any_key_has_value(player, keys) for player in players[:10])


def _all_players_have_side(players: Sequence[Mapping[str, object]]) -> bool:
    return len(players) >= 10 and all(_player_side(player) is not None for player in players[:10])


def _players_have_final_items(players: Sequence[Mapping[str, object]]) -> bool:
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
    return len(players) >= 10 and all(
        any(_int(player.get(key)) is not None for key in item_keys)
        for player in players[:10]
    )


def _players_have_timed_items(players: Sequence[Mapping[str, object]]) -> bool:
    for player in players:
        items = player.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, Mapping) and _int(item.get("itemId")) is not None and _number_at(item, ("time", "gameTime")) is not None:
                return True
    return False


def _has_minute_farm_timeline(
    payload: Mapping[str, object],
    players: Sequence[Mapping[str, object]],
) -> bool:
    if _list_at(payload, ("minuteStats", "goldTimeline", "xpTimeline", "lastHitTimeline")):
        return True
    return any(
        bool(_list_at(player, ("goldTimeline", "xpTimeline", "lastHitTimeline")))
        for player in players
    )


def _has_radiant_dire_orientation(
    payload: Mapping[str, object],
    players: Sequence[Mapping[str, object]],
) -> bool:
    if _mapping(payload.get("radiantTeam")) is not None and _mapping(payload.get("direTeam")) is not None:
        return True
    if _text_at(payload, ("radiantTeamId",)) is not None and _text_at(payload, ("direTeamId",)) is not None:
        return True
    return _all_players_have_side(players)


def _has_start_and_duration(payload: Mapping[str, object]) -> bool:
    return (
        _datetime_at(payload, ("startDateTime", "startTime")) is not None
        and _number_at(payload, ("durationSeconds", "duration")) is not None
    )


def _candidate_datetime(value: object) -> datetime:
    parsed = _datetime(value)
    if parsed is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _name_from_mapping(value: Mapping[str, object] | None) -> str | None:
    if value is None:
        return None
    return _text_at(value, ("displayName", "name", "fullName", "tag"))


def _nested_text(
    payload: Mapping[str, object],
    object_key: str,
    keys: tuple[str, ...],
) -> str | None:
    return _text_at(_mapping(payload.get(object_key)) or {}, keys)


def _nested_number(
    payload: Mapping[str, object],
    object_key: str,
    keys: tuple[str, ...],
) -> float | None:
    return _number_at(_mapping(payload.get(object_key)) or {}, keys)


def _nested_list(
    payload: Mapping[str, object],
    object_key: str,
    keys: tuple[str, ...],
) -> list[object]:
    return _list_at(_mapping(payload.get(object_key)) or {}, keys)


def _text_at(payload: Mapping[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _text(payload.get(key))
        if value is not None:
            return value
    return None


def _datetime_at(
    payload: Mapping[str, object],
    keys: tuple[str, ...],
) -> datetime | None:
    for key in keys:
        value = _datetime(payload.get(key))
        if value is not None:
            return value
    return None


def _number_at(
    payload: Mapping[str, object],
    keys: tuple[str, ...],
) -> float | None:
    for key in keys:
        value = _number(payload.get(key))
        if value is not None:
            return value
    return None


def _bool_at(payload: Mapping[str, object], keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    return None


def _list_at(payload: Mapping[str, object], keys: tuple[str, ...]) -> list[object]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _find_first_list(
    payload: Mapping[str, object],
    keys: tuple[str, ...],
) -> list[object]:
    direct = _list_at(payload, keys)
    if direct:
        return direct
    for value in payload.values():
        if isinstance(value, Mapping):
            nested = _find_first_list(value, keys)
            if nested:
                return nested
    return []


def _any_key_has_value(payload: Mapping[str, object], keys: tuple[str, ...]) -> bool:
    return any(_has_value(payload.get(key)) for key in keys)


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


def _int_or_text(value: str) -> int | str:
    integer = _int(value)
    return integer if integer is not None else value


def _int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def _datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        if not text:
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


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _teams_text(identity: StratzSampleIdentity) -> str:
    radiant = identity.radiant_team_name or "Radiant"
    dire = identity.dire_team_name or "Dire"
    return f"{radiant} vs {dire}"
