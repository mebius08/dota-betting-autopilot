from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypeVar
from collections.abc import Callable, Mapping, Sequence


SemanticDotaSide = Literal["radiant", "dire"]
SemanticDraftActionKind = Literal["pick", "ban"]
SemanticAdvantageMetric = Literal["gold", "xp"]
SemanticTimeStatus = Literal["normalized_seconds", "source_index_unstable"]
T = TypeVar("T")


class SemanticEvidenceStatus(str, Enum):
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    USABLE = "usable"


@dataclass(frozen=True)
class PublicSemanticPlayer:
    account_id: str
    team_side: SemanticDotaSide
    hero_id: int
    player_slot: int | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    net_worth: int | None = None
    last_hits: int | None = None
    denies: int | None = None
    gpm: int | None = None
    xpm: int | None = None
    level: int | None = None
    hero_damage: int | None = None
    tower_damage: int | None = None
    hero_healing: int | None = None
    final_item_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class PublicSemanticDraftAction:
    order: int | None
    kind: SemanticDraftActionKind | None
    side: SemanticDotaSide | None
    hero_id: int | None


@dataclass(frozen=True)
class PublicSemanticTimedItem:
    account_id: str
    item_id: int
    source_time_value: str
    normalized_time_seconds: int | None


@dataclass(frozen=True)
class PublicSemanticAdvantagePoint:
    metric: SemanticAdvantageMetric
    source_index: int
    value: float
    source_time_value: str | None
    normalized_time_seconds: int | None
    time_semantics_status: SemanticTimeStatus


@dataclass(frozen=True)
class PublicMatchSemantics:
    match_id: str | None
    started_at_value: object | None
    ended_at_value: object | None
    duration_seconds: int | None
    did_radiant_win: bool | None
    radiant_team_id: str | None
    dire_team_id: str | None
    radiant_team_name: str | None
    dire_team_name: str | None
    patch: str | None
    league: Mapping[str, object] | None
    tournament: Mapping[str, object] | None
    series: Mapping[str, object] | None
    game_number: int | None
    best_of: int | None
    players: tuple[PublicSemanticPlayer, ...]
    draft_actions: tuple[PublicSemanticDraftAction, ...]
    advantage_points: tuple[PublicSemanticAdvantagePoint, ...]
    player_status: SemanticEvidenceStatus
    team_identity_status: SemanticEvidenceStatus
    gold_advantage_status: SemanticEvidenceStatus
    xp_advantage_status: SemanticEvidenceStatus
    warnings: tuple[str, ...] = ()

    @property
    def radiant_hero_ids(self) -> tuple[int, ...]:
        action_heroes = tuple(
            action.hero_id
            for action in self.draft_actions
            if action.kind == "pick"
            and action.side == "radiant"
            and action.hero_id is not None
        )
        if action_heroes:
            return tuple(dict.fromkeys(action_heroes))
        return tuple(
            dict.fromkeys(
                player.hero_id
                for player in self.players
                if player.team_side == "radiant"
            )
        )

    @property
    def dire_hero_ids(self) -> tuple[int, ...]:
        action_heroes = tuple(
            action.hero_id
            for action in self.draft_actions
            if action.kind == "pick"
            and action.side == "dire"
            and action.hero_id is not None
        )
        if action_heroes:
            return tuple(dict.fromkeys(action_heroes))
        return tuple(
            dict.fromkeys(
                player.hero_id for player in self.players if player.team_side == "dire"
            )
        )

    @property
    def has_complete_5v5_composition(self) -> bool:
        return len(self.radiant_hero_ids) == 5 and len(self.dire_hero_ids) == 5


@dataclass(frozen=True)
class PublicMatchSemanticFingerprint:
    match_id: str | None
    patch_id: str | None
    radiant_team_id: str | None
    dire_team_id: str | None
    player_count: int
    radiant_player_count: int
    dire_player_count: int
    player_account_id_count: int
    player_hero_id_count: int
    gold_advantage_point_count: int
    xp_advantage_point_count: int
    draft_action_count: int
    ban_count: int


def extract_public_match_semantics(state: object) -> PublicMatchSemantics:
    players, player_status, player_warnings = _semantic_players(state)
    radiant_team = _mapping(_find_value(state, ("radiantTeam", "homeTeam")))
    dire_team = _mapping(_find_value(state, ("direTeam", "awayTeam")))
    radiant_team_id = _text(_find_value(state, ("radiantTeamId",))) or _mapping_text(
        radiant_team,
        ("id", "teamId"),
    )
    dire_team_id = _text(_find_value(state, ("direTeamId",))) or _mapping_text(
        dire_team,
        ("id", "teamId"),
    )
    team_status = (
        SemanticEvidenceStatus.USABLE
        if radiant_team_id and dire_team_id
        else SemanticEvidenceStatus.NOT_FOUND
    )
    advantage_points = _semantic_advantage_points(state)
    gold_status = _advantage_status(advantage_points, "gold")
    xp_status = _advantage_status(advantage_points, "xp")
    series = _mapping(_find_value(state, ("series",)))
    tournament = _mapping(_find_value(state, ("tournament", "event")))
    league = _mapping(_find_value(state, ("league",)))
    return PublicMatchSemantics(
        match_id=_find_match_id(state),
        started_at_value=_find_value(state, ("startDateTime", "startTime", "startedAt")),
        ended_at_value=_find_value(state, ("endDateTime", "endTime", "endedAt")),
        duration_seconds=_int(_find_value(state, ("durationSeconds", "duration"))),
        did_radiant_win=_bool(_find_value(state, ("didRadiantWin", "radiantWin"))),
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        radiant_team_name=_mapping_name(radiant_team),
        dire_team_name=_mapping_name(dire_team),
        patch=_text(_find_value(state, ("gameVersionId", "patchId", "patch"))),
        league=league,
        tournament=tournament,
        series=series,
        game_number=(
            _mapping_int(series, ("gameNumber", "game", "mapNumber"))
            or _int(_find_value(state, ("gameNumber", "mapNumber")))
        ),
        best_of=_best_of(
            _mapping_text(series, ("type", "seriesType", "bestOf"))
            or _text(_find_value(state, ("seriesType", "bestOf")))
        ),
        players=players,
        draft_actions=_semantic_draft_actions(state),
        advantage_points=advantage_points,
        player_status=player_status,
        team_identity_status=team_status,
        gold_advantage_status=gold_status,
        xp_advantage_status=xp_status,
        warnings=player_warnings,
    )


def extract_public_match_semantics_from_roots(
    states: Sequence[object],
    *,
    requested_match_id: str | None = None,
) -> PublicMatchSemantics:
    return merge_public_match_semantics(
        tuple(extract_public_match_semantics(state) for state in states),
        requested_match_id=requested_match_id,
    )


def merge_public_match_semantics(
    candidates: Sequence[PublicMatchSemantics],
    *,
    requested_match_id: str | None = None,
) -> PublicMatchSemantics:
    semantic_candidates = tuple(candidates)
    requested = _text(requested_match_id)
    if not semantic_candidates:
        return _empty_public_match_semantics(match_id=requested)

    matching_candidates = tuple(
        semantic
        for semantic in semantic_candidates
        if requested is not None and semantic.match_id == requested
    )
    primary_candidates = matching_candidates or semantic_candidates

    def first_value(
        selector: Callable[[PublicMatchSemantics], T | None],
    ) -> T | None:
        value = _first_non_none(primary_candidates, selector)
        if value is not None:
            return value
        if matching_candidates:
            return _first_non_none(semantic_candidates, selector)
        return None

    player_semantics = _best_player_semantics(semantic_candidates, requested)
    draft_semantics = _best_draft_semantics(semantic_candidates, requested)
    gold_points = _best_advantage_points(semantic_candidates, "gold", requested)
    xp_points = _best_advantage_points(semantic_candidates, "xp", requested)
    advantage_points = gold_points + xp_points
    radiant_team_id = first_value(lambda semantic: semantic.radiant_team_id)
    dire_team_id = first_value(lambda semantic: semantic.dire_team_id)
    team_status = (
        SemanticEvidenceStatus.USABLE
        if radiant_team_id is not None and dire_team_id is not None
        else SemanticEvidenceStatus.NOT_FOUND
    )

    return PublicMatchSemantics(
        match_id=requested or first_value(lambda semantic: semantic.match_id),
        started_at_value=first_value(lambda semantic: semantic.started_at_value),
        ended_at_value=first_value(lambda semantic: semantic.ended_at_value),
        duration_seconds=first_value(lambda semantic: semantic.duration_seconds),
        did_radiant_win=first_value(lambda semantic: semantic.did_radiant_win),
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        radiant_team_name=first_value(lambda semantic: semantic.radiant_team_name),
        dire_team_name=first_value(lambda semantic: semantic.dire_team_name),
        patch=first_value(lambda semantic: semantic.patch),
        league=first_value(lambda semantic: semantic.league),
        tournament=first_value(lambda semantic: semantic.tournament),
        series=first_value(lambda semantic: semantic.series),
        game_number=first_value(lambda semantic: semantic.game_number),
        best_of=first_value(lambda semantic: semantic.best_of),
        players=player_semantics.players,
        draft_actions=draft_semantics.draft_actions,
        advantage_points=advantage_points,
        player_status=player_semantics.player_status,
        team_identity_status=team_status,
        gold_advantage_status=_advantage_status(advantage_points, "gold"),
        xp_advantage_status=_advantage_status(advantage_points, "xp"),
        warnings=player_semantics.warnings,
    )


def public_match_semantic_fingerprint(
    semantics: PublicMatchSemantics,
) -> PublicMatchSemanticFingerprint:
    return PublicMatchSemanticFingerprint(
        match_id=semantics.match_id,
        patch_id=semantics.patch,
        radiant_team_id=semantics.radiant_team_id,
        dire_team_id=semantics.dire_team_id,
        player_count=len(semantics.players),
        radiant_player_count=sum(
            1 for player in semantics.players if player.team_side == "radiant"
        ),
        dire_player_count=sum(
            1 for player in semantics.players if player.team_side == "dire"
        ),
        player_account_id_count=len({player.account_id for player in semantics.players}),
        player_hero_id_count=len({player.hero_id for player in semantics.players}),
        gold_advantage_point_count=sum(
            1 for point in semantics.advantage_points if point.metric == "gold"
        ),
        xp_advantage_point_count=sum(
            1 for point in semantics.advantage_points if point.metric == "xp"
        ),
        draft_action_count=len(semantics.draft_actions),
        ban_count=sum(
            1
            for action in semantics.draft_actions
            if action.kind == "ban" and action.hero_id is not None
        ),
    )


def find_public_state_value(value: object, keys: tuple[str, ...]) -> object | None:
    return _find_value(value, keys)


def find_public_mapping_list(
    value: object,
    keys: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
    return _find_mapping_list(value, keys)


def public_player_side(player: Mapping[str, object]) -> SemanticDotaSide | None:
    return _player_side(player)


def public_players_have_items(players: Sequence[Mapping[str, object]]) -> bool:
    return _players_have_items(players)


def public_players_have_timed_items(players: Sequence[Mapping[str, object]]) -> bool:
    return _players_have_timed_items(players)


def public_player_timed_items(
    players: Sequence[Mapping[str, object]],
) -> tuple[PublicSemanticTimedItem, ...]:
    return _timed_items_from_players(players)


def public_has_timed_event_list(value: object, keys: tuple[str, ...]) -> bool:
    return _has_timed_event_list(value, keys)


def public_has_tower_barracks_objectives(state: object) -> bool:
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


def _semantic_players(
    state: object,
) -> tuple[tuple[PublicSemanticPlayer, ...], SemanticEvidenceStatus, tuple[str, ...]]:
    raw_players = _find_mapping_list(state, ("playerMatches", "players", "lineups"))
    if not raw_players:
        return (), SemanticEvidenceStatus.NOT_FOUND, ("player records not found",)
    rows: list[PublicSemanticPlayer] = []
    warnings: list[str] = []
    seen_accounts: set[str] = set()
    for raw_player in raw_players[:10]:
        account_id = _text(_find_value(raw_player, ("steamAccountId", "accountId", "playerId", "id")))
        if account_id is None:
            warnings.append("player row missing account identity")
            continue
        if account_id in seen_accounts:
            warnings.append("duplicate player account identity")
            continue
        seen_accounts.add(account_id)
        side = _player_side(raw_player)
        hero_id = _int(_find_value(raw_player, ("heroId", "hero_id")))
        if side is None:
            warnings.append("ambiguous player side assignment")
            continue
        if hero_id is None:
            warnings.append("player row missing hero identity")
            continue
        rows.append(
            PublicSemanticPlayer(
                account_id=account_id,
                team_side=side,
                hero_id=hero_id,
                player_slot=_int(_find_value(raw_player, ("playerSlot", "slot"))),
                kills=_int(_find_value(raw_player, ("kills",))),
                deaths=_int(_find_value(raw_player, ("deaths",))),
                assists=_int(_find_value(raw_player, ("assists",))),
                net_worth=_int(_find_value(raw_player, ("netWorth", "networth"))),
                last_hits=_int(_find_value(raw_player, ("numLastHits", "lastHits"))),
                denies=_int(_find_value(raw_player, ("numDenies", "denies"))),
                gpm=_int(_find_value(raw_player, ("goldPerMinute", "gpm"))),
                xpm=_int(_find_value(raw_player, ("experiencePerMinute", "xpm"))),
                level=_int(_find_value(raw_player, ("level",))),
                hero_damage=_int(_find_value(raw_player, ("heroDamage",))),
                tower_damage=_int(_find_value(raw_player, ("towerDamage",))),
                hero_healing=_int(_find_value(raw_player, ("heroHealing",))),
                final_item_ids=_final_item_ids(raw_player),
            )
        )
    if len(rows) == 10:
        return tuple(rows), SemanticEvidenceStatus.USABLE, tuple(dict.fromkeys(warnings))
    return tuple(rows), SemanticEvidenceStatus.AMBIGUOUS, tuple(dict.fromkeys(warnings))


def _semantic_draft_actions(state: object) -> tuple[PublicSemanticDraftAction, ...]:
    rows = _find_mapping_list(state, ("pickBans", "draftActions", "picksBans", "bans"))
    actions: list[PublicSemanticDraftAction] = []
    for row in rows:
        order = _int(
            row.get("order")
            or row.get("ord")
            or row.get("sequence")
            or row.get("actionNumber")
            or row.get("actionId")
        )
        actions.append(
            PublicSemanticDraftAction(
                order=order,
                kind=_draft_kind(row),
                side=_side_from_mapping(row),
                hero_id=_int(row.get("heroId") or row.get("hero_id") or row.get("bannedHeroId")),
            )
        )
    return tuple(actions)


def _semantic_advantage_points(
    state: object,
) -> tuple[PublicSemanticAdvantagePoint, ...]:
    points: list[PublicSemanticAdvantagePoint] = []
    metric_keys: tuple[tuple[SemanticAdvantageMetric, tuple[str, ...]], ...] = (
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
            status: SemanticTimeStatus = (
                "normalized_seconds"
                if normalized_seconds is not None
                else "source_index_unstable"
            )
            points.append(
                PublicSemanticAdvantagePoint(
                    metric=metric,
                    source_index=index,
                    value=value,
                    source_time_value=source_time_value,
                    normalized_time_seconds=normalized_seconds,
                    time_semantics_status=status,
                )
            )
    return tuple(points)


def _advantage_item(item: object) -> tuple[float, str | None, int | None] | None:
    if isinstance(item, Mapping):
        value = _float(_find_value(item, ("value", "lead", "amount")))
        if value is None:
            return None
        source_time = _find_value(item, ("time", "gameTime", "timestamp", "second"))
        return value, _text(source_time), _int(source_time)
    value = _float(item)
    if value is None:
        return None
    return value, None, None


def _advantage_status(
    points: Sequence[PublicSemanticAdvantagePoint],
    metric: SemanticAdvantageMetric,
) -> SemanticEvidenceStatus:
    return (
        SemanticEvidenceStatus.USABLE
        if any(point.metric == metric for point in points)
        else SemanticEvidenceStatus.NOT_FOUND
    )


def _empty_public_match_semantics(
    *,
    match_id: str | None = None,
) -> PublicMatchSemantics:
    return PublicMatchSemantics(
        match_id=match_id,
        started_at_value=None,
        ended_at_value=None,
        duration_seconds=None,
        did_radiant_win=None,
        radiant_team_id=None,
        dire_team_id=None,
        radiant_team_name=None,
        dire_team_name=None,
        patch=None,
        league=None,
        tournament=None,
        series=None,
        game_number=None,
        best_of=None,
        players=(),
        draft_actions=(),
        advantage_points=(),
        player_status=SemanticEvidenceStatus.NOT_FOUND,
        team_identity_status=SemanticEvidenceStatus.NOT_FOUND,
        gold_advantage_status=SemanticEvidenceStatus.NOT_FOUND,
        xp_advantage_status=SemanticEvidenceStatus.NOT_FOUND,
    )


def _first_non_none(
    candidates: Sequence[PublicMatchSemantics],
    selector: Callable[[PublicMatchSemantics], T | None],
) -> T | None:
    for candidate in candidates:
        value = selector(candidate)
        if value is not None:
            return value
    return None


def _best_player_semantics(
    candidates: Sequence[PublicMatchSemantics],
    requested_match_id: str | None,
) -> PublicMatchSemantics:
    return max(
        candidates,
        key=lambda semantic: (
            semantic.player_status is SemanticEvidenceStatus.USABLE,
            len(semantic.players),
            len({player.account_id for player in semantic.players}),
            len({player.hero_id for player in semantic.players}),
            requested_match_id is not None and semantic.match_id == requested_match_id,
        ),
    )


def _best_draft_semantics(
    candidates: Sequence[PublicMatchSemantics],
    requested_match_id: str | None,
) -> PublicMatchSemantics:
    return max(
        candidates,
        key=lambda semantic: (
            sum(
                1
                for action in semantic.draft_actions
                if action.kind is not None
                and action.side is not None
                and action.hero_id is not None
            ),
            sum(1 for action in semantic.draft_actions if action.order is not None),
            len(semantic.draft_actions),
            requested_match_id is not None and semantic.match_id == requested_match_id,
        ),
    )


def _best_advantage_points(
    candidates: Sequence[PublicMatchSemantics],
    metric: SemanticAdvantageMetric,
    requested_match_id: str | None,
) -> tuple[PublicSemanticAdvantagePoint, ...]:
    semantic = max(
        candidates,
        key=lambda candidate: (
            sum(1 for point in candidate.advantage_points if point.metric == metric),
            requested_match_id is not None and candidate.match_id == requested_match_id,
        ),
    )
    return tuple(point for point in semantic.advantage_points if point.metric == metric)


def _find_match_id(value: object) -> str | None:
    match_id = _text(_find_value(value, ("matchId",)))
    if match_id is not None:
        return match_id
    return _text(_find_value(value, ("id",)))


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


def _find_mapping_list(
    value: object,
    keys: tuple[str, ...],
) -> tuple[Mapping[str, object], ...]:
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


def _draft_kind(row: Mapping[str, object]) -> SemanticDraftActionKind | None:
    is_pick = _bool(row.get("isPick"))
    if is_pick is not None:
        return "pick" if is_pick else "ban"
    if row.get("bannedHeroId") is not None or row.get("wasBannedSuccessfully") is not None:
        return "ban"
    value = _text(
        row.get("kind")
        or row.get("type")
        or row.get("action")
        or row.get("phase")
        or row.get("draftPhase")
        or row.get("phaseName")
    )
    if value is None:
        return None
    lowered = value.casefold()
    if "pick" in lowered:
        return "pick"
    if "ban" in lowered:
        return "ban"
    return None


def _side_from_mapping(row: Mapping[str, object]) -> SemanticDotaSide | None:
    side = _side(row.get("side") or row.get("teamSide") or row.get("team"))
    if side is not None:
        return side
    is_radiant = _bool(row.get("isRadiant"))
    if is_radiant is not None:
        return "radiant" if is_radiant else "dire"
    return None


def _player_side(player: Mapping[str, object]) -> SemanticDotaSide | None:
    side = _side_from_mapping(player)
    if side is not None:
        return side
    slot = _int(_find_value(player, ("playerSlot", "slot")))
    if slot is None:
        return None
    return "radiant" if slot < 128 else "dire"


def _side(value: object) -> SemanticDotaSide | None:
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


def _players_have_items(players: Sequence[Mapping[str, object]]) -> bool:
    return len(players) >= 10 and all(_final_item_ids(player) for player in players[:10])


def _players_have_timed_items(players: Sequence[Mapping[str, object]]) -> bool:
    if len(players) < 10:
        return False
    timed_items = _timed_items_from_players(players[:10])
    account_ids = {
        _text(_find_value(player, ("steamAccountId", "accountId", "playerId", "id")))
        for player in players[:10]
    }
    account_ids.discard(None)
    timed_item_account_ids = {row.account_id for row in timed_items}
    return len(account_ids) == 10 and account_ids <= timed_item_account_ids


def _timed_items_from_players(
    players: Sequence[Mapping[str, object]],
) -> tuple[PublicSemanticTimedItem, ...]:
    rows: list[PublicSemanticTimedItem] = []
    for player in players:
        account_id = _text(
            _find_value(player, ("steamAccountId", "accountId", "playerId", "id"))
        )
        if account_id is None:
            continue
        for item in _timed_item_mappings(player):
            item_id = _int(
                _find_value(item, ("itemId", "item_id", "item", "id", "key"))
            )
            source_time = _find_value(
                item,
                (
                    "time",
                    "gameTime",
                    "timestamp",
                    "second",
                    "seconds",
                    "matchTime",
                ),
            )
            source_time_value = _text(source_time)
            if item_id is None or source_time_value is None:
                continue
            rows.append(
                PublicSemanticTimedItem(
                    account_id=account_id,
                    item_id=item_id,
                    source_time_value=source_time_value,
                    normalized_time_seconds=_int(source_time),
                )
            )
    return tuple(rows)


def _timed_item_mappings(
    player: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    rows: list[Mapping[str, object]] = []
    for key in (
        "items",
        "itemPurchases",
        "itemPurchaseEvents",
        "itemEvents",
        "inventoryEvents",
        "build",
    ):
        value = player.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, Mapping))
    return tuple(rows)


def _final_item_ids(player: Mapping[str, object]) -> tuple[int, ...]:
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
        item_id = _int(_find_value(player, (key,)))
        if item_id is not None and item_id > 0:
            item_ids.append(item_id)
    for key in ("itemIds", "itemsIds", "inventory"):
        raw_items = _find_value(player, (key,))
        if isinstance(raw_items, list):
            for raw_item in raw_items:
                item_id = _int(raw_item)
                if item_id is not None and item_id > 0:
                    item_ids.append(item_id)
    return tuple(dict.fromkeys(item_ids))


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
    return _text(_find_value(value, keys))


def _mapping_int(
    value: Mapping[str, object] | None,
    keys: tuple[str, ...],
) -> int | None:
    if value is None:
        return None
    return _int(_find_value(value, keys))


def _best_of(value: object) -> int | None:
    integer = _int(value)
    if integer is not None and integer > 0:
        return integer
    text = _text(value)
    if text is None:
        return None
    digits = "".join(character for character in text if character.isdigit())
    parsed = _int(digits)
    return parsed if parsed is not None and parsed > 0 else None


def _bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, "0"):
        return False
    if value in (1, "1"):
        return True
    text = _text(value)
    if text is None:
        return None
    lowered = text.casefold()
    if lowered in {"true", "yes", "radiant"}:
        return True
    if lowered in {"false", "no", "dire"}:
        return False
    return None


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
