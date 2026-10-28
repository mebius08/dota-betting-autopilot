"""Read-only, match-scoped adapter for the existing schema-v1 context cache.

This is retrospective POST-DRAFT context, not a timed HistoricalObservation.
It does not establish LIVE/pre-draft availability or a permanent team roster.
No result, post-game statistic or arbitrary source field enters the output.
The cache checksum checks integrity, not independent authenticity of API data;
compact/hero reconciliation remains the responsibility of fetch_match_context.
"""

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path


DEFAULT_CACHE_DIR = (Path(__file__).resolve().parents[2]
                     / "tools/replay_probe/output/match-context")
PROVENANCE = "HISTORICAL_POST_DRAFT_CONTEXT"
PLAYER_FIELDS = ("player_slot", "opendota_player_slot", "team", "account_id", "hero_id")
DRAFT_FIELDS = ("is_pick", "team", "hero_id", "order")
SOURCE_FIELDS = (
    "kind", "metadata_source", "metadata_path", "metadata_payload_sha256",
    "compact_path", "compact_sha256", "endpoint", "retrieved_at_utc", "availability",
)
CONTEXT_FIELDS = (
    "match_id", "replay_sha256", "start_time", "league_id", "radiant_team_id",
    "dire_team_id", "players", "picks_bans", "patch",
)


def _fields(row: Mapping, fields: tuple[str, ...]) -> dict:
    if not isinstance(row, Mapping) or any(field not in row for field in fields):
        raise ValueError("Missing context fields; unknown values must be explicit NULL.")
    return {field: row[field] for field in fields}


def _integer(value, name, *, minimum=1, nullable=True):
    if value is None and nullable:
        return None
    if type(value) is not int or value < minimum:
        raise ValueError(f"Invalid {name}: expected integer >= {minimum}.")
    return value


def _match_id(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    return _integer(value, "match_id", nullable=False)


def _sha256(value):
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in value
    ):
        raise ValueError("Invalid context SHA-256.")
    return value


def adapt_match_context(cached: Mapping, *, match_id: int | str) -> dict:
    """Project one validated cache without mutating it or inferring missing data.

    Player list and draft list order are preserved, including NULL draft orders.
    A missing draft remains NULL; final hero identities never synthesize picks.
    Technical identity and source provenance are metadata, not ML predictors.
    """
    mid = _match_id(match_id)
    value = _fields(cached, CONTEXT_FIELDS)
    if (type(cached.get("schema_version")) is not int or cached["schema_version"] != 1
            or _integer(value["match_id"], "cached match_id", nullable=False) != mid):
        raise ValueError("Context schema version or match_id mismatch.")
    checksum = hashlib.sha256(json.dumps(
        {k: v for k, v in cached.items() if k != "content_sha256"},
        sort_keys=True, allow_nan=False,
    ).encode()).hexdigest()
    if _sha256(cached.get("content_sha256")) != checksum:
        raise ValueError("Context checksum mismatch.")
    _sha256(value["replay_sha256"])
    source = _fields(cached.get("provenance"), SOURCE_FIELDS)
    if (source["kind"] != "HISTORICAL_MATCH_CONTEXT"
            or source["metadata_source"] not in ("LOCAL_JSON", "OPENDOTA_GET")):
        raise ValueError("Unexpected context source provenance.")
    for field in ("metadata_payload_sha256", "compact_sha256"):
        _sha256(source[field])
    for field in set(SOURCE_FIELDS) - {"metadata_payload_sha256", "compact_sha256"}:
        if source[field] is not None and not isinstance(source[field], str):
            raise ValueError(f"Invalid source provenance field: {field}.")
    for field in ("start_time", "league_id", "radiant_team_id", "dire_team_id", "patch"):
        _integer(value[field], field, minimum=0 if field == "patch" else 1)

    if not isinstance(value["players"], list) or len(value["players"]) != 10:
        raise ValueError("Context requires ten player identities.")
    players, slots, heroes = [], set(), set()
    for row in value["players"]:
        player = _fields(row, PLAYER_FIELDS)
        slot = _integer(player["player_slot"], "player_slot", minimum=0, nullable=False)
        if slot not in range(10) or slot in slots:
            raise ValueError("Player slots must be unique in 0..9.")
        slots.add(slot)
        expected_team = "RADIANT" if slot < 5 else "DIRE"
        native_slot = slot if slot < 5 else 128 + slot - 5
        raw = _integer(player["opendota_player_slot"], "OpenDota slot", minimum=0)
        if player["team"] != expected_team or (raw is not None and raw != native_slot):
            raise ValueError("Player slot/side mismatch.")
        account = _integer(player["account_id"], "account_id")
        if account == 4294967295:
            raise ValueError("Anonymous account sentinel must be NULL in normalized cache.")
        hero = _integer(player["hero_id"], "hero_id")
        if hero is not None:
            if hero in heroes:
                raise ValueError("Duplicate hero identity.")
            heroes.add(hero)
        players.append(player)

    draft = value["picks_bans"]
    if draft is not None:
        if not isinstance(draft, list):
            raise ValueError("Draft must be a list or NULL.")
        actions, orders, copied = set(), set(), []
        for row in draft:
            action = _fields(row, DRAFT_FIELDS)
            if type(action["is_pick"]) is not bool or action["team"] not in ("RADIANT", "DIRE"):
                raise ValueError("Invalid draft action or side.")
            hero = _integer(action["hero_id"], "draft hero_id", nullable=False)
            order = _integer(action["order"], "draft order", minimum=0)
            key = action["is_pick"], action["team"], hero
            if key in actions or (order is not None and (order in orders or order >= len(draft))):
                raise ValueError("Duplicate draft action or invalid/duplicate order.")
            actions.add(key)
            if order is not None:
                orders.add(order)
            copied.append(action)
        draft = copied
    return {**value, "players": players, "picks_bans": draft,
            "provenance": PROVENANCE, "source_provenance": source}


def load_historical_match_context(match_id: int | str, *,
                                  cache_dir: str | Path = DEFAULT_CACHE_DIR) -> dict:
    """Read exactly one local cache. Missing/corrupt input raises; never fetches.

    No source files, manifests or HistoricalSnapshot channels are modified.
    FileNotFoundError explicitly distinguishes missing context from NULL fields.
    """
    mid = _match_id(match_id)
    path = Path(cache_dir) / f"{mid}.context.json"
    return adapt_match_context(json.loads(path.read_text(encoding="utf-8")), match_id=mid)
