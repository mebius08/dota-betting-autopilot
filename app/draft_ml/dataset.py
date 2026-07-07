from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from app.draft_history import (
    HistoricalDotaGame,
    HistoricalDraftAction,
    has_complete_5v5_picks,
    is_draft_game_scope_eligible,
)

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


DRAFT_FEATURE_SCHEMA_VERSION = 1
DRAFT_PREDICTION_MODE = "POST_DRAFT_MAP"
DRAFT_MISSING_HERO = "missing_hero"
DRAFT_MISSING_PATCH = "missing_patch"
DRAFT_UNKNOWN_SIDE = "unknown"
DRAFT_PICK_SLOTS = 5
DRAFT_BAN_SLOTS = 7

DRAFT_PICK_FEATURE_NAMES: tuple[str, ...] = tuple(
    f"{team}_pick_{index}"
    for team in ("team_a", "team_b")
    for index in range(1, DRAFT_PICK_SLOTS + 1)
)
DRAFT_BAN_FEATURE_NAMES: tuple[str, ...] = tuple(
    f"{team}_ban_{index}"
    for team in ("team_a", "team_b")
    for index in range(1, DRAFT_BAN_SLOTS + 1)
)
DRAFT_CATEGORICAL_FEATURE_NAMES: tuple[str, ...] = (
    *DRAFT_PICK_FEATURE_NAMES,
    *DRAFT_BAN_FEATURE_NAMES,
    "team_a_side",
    "patch",
)
DRAFT_NUMERIC_FEATURE_NAMES: tuple[str, ...] = (
    "game_number",
    "best_of",
    "team_a_series_wins_before",
    "team_b_series_wins_before",
    "team_a_hero_prior_win_rate_mean",
    "team_b_hero_prior_win_rate_mean",
    "team_a_unseen_hero_count",
    "team_b_unseen_hero_count",
    "hero_history_sample_total",
    "team_a_synergy_mean_win_rate",
    "team_b_synergy_mean_win_rate",
    "team_a_unseen_synergy_pair_count",
    "team_b_unseen_synergy_pair_count",
    "synergy_sample_total",
    "cross_team_matchup_mean_win_rate",
    "cross_team_matchup_min_win_rate",
    "cross_team_matchup_max_win_rate",
    "cross_team_matchup_sample_total",
    "unseen_cross_team_matchup_count",
    "team_a_hero_familiarity_mean_matches",
    "team_b_hero_familiarity_mean_matches",
    "team_a_hero_familiarity_unavailable",
    "team_b_hero_familiarity_unavailable",
)
DRAFT_FEATURE_NAMES: tuple[str, ...] = (
    *DRAFT_CATEGORICAL_FEATURE_NAMES,
    *DRAFT_NUMERIC_FEATURE_NAMES,
)


@dataclass(frozen=True)
class DraftMapRowMetadata:
    source: str
    source_game_id: str
    prediction_timestamp: datetime
    team_a_source_id: str | None
    team_b_source_id: str | None


@dataclass(frozen=True)
class DraftMapFeatureRow:
    metadata: DraftMapRowMetadata
    target: int
    features: Mapping[str, object]


@dataclass(frozen=True)
class DraftMapDataset:
    x: pd.DataFrame
    y: np.ndarray
    metadata: tuple[DraftMapRowMetadata, ...]
    categorical_feature_names: tuple[str, ...] = DRAFT_CATEGORICAL_FEATURE_NAMES
    numeric_feature_names: tuple[str, ...] = DRAFT_NUMERIC_FEATURE_NAMES
    feature_schema_version: int = DRAFT_FEATURE_SCHEMA_VERSION

    def __len__(self) -> int:
        return len(self.y)


def build_draft_map_dataset(repository: "SQLiteRepository") -> DraftMapDataset:
    games = tuple(repository.list_historical_dota_games())
    actions_by_game = {
        game.id: tuple(repository.list_historical_draft_actions(game.id))
        for game in games
    }
    rows = tuple(
        sorted(
            (
                row
                for game in games
                if (
                    row := build_draft_map_feature_row(
                        game,
                        actions_by_game[game.id],
                        historical_games=games,
                        actions_by_game=actions_by_game,
                    )
                )
                is not None
            ),
            key=lambda row: (
                row.metadata.prediction_timestamp,
                row.metadata.source,
                row.metadata.source_game_id,
            ),
        )
    )
    frame = pd.DataFrame(
        [row.features for row in rows],
        columns=list(DRAFT_FEATURE_NAMES),
    )
    for column in DRAFT_CATEGORICAL_FEATURE_NAMES:
        if column in frame:
            frame[column] = frame[column].astype("object")
    for column in DRAFT_NUMERIC_FEATURE_NAMES:
        if column in frame:
            frame[column] = frame[column].astype("float64")
    return DraftMapDataset(
        x=frame,
        y=np.asarray([row.target for row in rows], dtype=np.int_),
        metadata=tuple(row.metadata for row in rows),
    )


def build_draft_map_feature_row(
    game: HistoricalDotaGame,
    actions: Sequence[HistoricalDraftAction],
    *,
    historical_games: Sequence[HistoricalDotaGame],
    actions_by_game: Mapping[str, Sequence[HistoricalDraftAction]],
) -> DraftMapFeatureRow | None:
    if (
        not game.usable_for_draft_training
        or not is_draft_game_scope_eligible(game)
        or not has_complete_5v5_picks(game, actions)
    ):
        return None
    side_picks = _side_pick_heroes(game, actions)
    team_a_picks = side_picks["team_a"]
    team_b_picks = side_picks["team_b"]
    if len(team_a_picks) != 5 or len(team_b_picks) != 5:
        return None

    prior_games = tuple(
        prior
        for prior in historical_games
        if prior.id != game.id
        and prior.completed_before(game.started_at)
        and prior.usable_for_draft_training
        and is_draft_game_scope_eligible(prior)
        and has_complete_5v5_picks(prior, actions_by_game.get(prior.id, ()))
    )
    prior_actions_by_game = {
        prior.id: tuple(actions_by_game.get(prior.id, ()))
        for prior in prior_games
    }

    hero_stats = _hero_stats(prior_games, prior_actions_by_game)
    synergy_stats = _synergy_stats(prior_games, prior_actions_by_game)
    matchup_stats = _matchup_stats(prior_games, prior_actions_by_game)
    familiarity_stats = _team_hero_familiarity(prior_games, prior_actions_by_game)

    features: dict[str, object] = {}
    features.update(_target_draft_categorical_features(game, actions))
    features.update(
        {
            "game_number": float(game.game_number or 0),
            "best_of": float(game.best_of or 0),
            "team_a_series_wins_before": float(
                game.team_a_series_wins_before or 0
            ),
            "team_b_series_wins_before": float(
                game.team_b_series_wins_before or 0
            ),
        }
    )
    features.update(
        _hero_history_features(
            team_a_picks=team_a_picks,
            team_b_picks=team_b_picks,
            hero_stats=hero_stats,
        )
    )
    features.update(
        _synergy_features(
            team_a_picks=team_a_picks,
            team_b_picks=team_b_picks,
            synergy_stats=synergy_stats,
        )
    )
    features.update(
        _matchup_features(
            team_a_picks=team_a_picks,
            team_b_picks=team_b_picks,
            matchup_stats=matchup_stats,
        )
    )
    features.update(
        _familiarity_features(
            game=game,
            team_a_picks=team_a_picks,
            team_b_picks=team_b_picks,
            familiarity_stats=familiarity_stats,
        )
    )
    return DraftMapFeatureRow(
        metadata=DraftMapRowMetadata(
            source=game.source,
            source_game_id=game.source_game_id,
            prediction_timestamp=game.started_at,
            team_a_source_id=game.team_a_source_id,
            team_b_source_id=game.team_b_source_id,
        ),
        target=1 if game.winner_side == "team_a" else 0,
        features={name: features[name] for name in DRAFT_FEATURE_NAMES},
    )


def draft_categorical_feature_indices() -> tuple[int, ...]:
    return tuple(
        index
        for index, name in enumerate(DRAFT_FEATURE_NAMES)
        if name in DRAFT_CATEGORICAL_FEATURE_NAMES
    )


def _target_draft_categorical_features(
    game: HistoricalDotaGame,
    actions: Sequence[HistoricalDraftAction],
) -> dict[str, object]:
    side_picks = _side_pick_heroes(game, actions)
    side_bans = _side_ban_heroes(game, actions)
    features: dict[str, object] = {}
    for team, heroes in side_picks.items():
        for index, hero_identity in enumerate(_hero_slots(heroes, DRAFT_PICK_SLOTS), start=1):
            features[f"{team}_pick_{index}"] = hero_identity
    for team, heroes in side_bans.items():
        for index, hero_identity in enumerate(_hero_slots(heroes, DRAFT_BAN_SLOTS), start=1):
            features[f"{team}_ban_{index}"] = hero_identity
    features["team_a_side"] = game.team_a_side or DRAFT_UNKNOWN_SIDE
    features["patch"] = f"{game.source}:patch:{game.patch}" if game.patch else DRAFT_MISSING_PATCH
    return features


def _side_pick_heroes(
    game: HistoricalDotaGame,
    actions: Sequence[HistoricalDraftAction],
) -> dict[str, tuple[str, ...]]:
    return _side_heroes(game, actions, action_kind="pick")


def _side_ban_heroes(
    game: HistoricalDotaGame,
    actions: Sequence[HistoricalDraftAction],
) -> dict[str, tuple[str, ...]]:
    return _side_heroes(game, actions, action_kind="ban")


def _side_heroes(
    game: HistoricalDotaGame,
    actions: Sequence[HistoricalDraftAction],
    *,
    action_kind: str,
) -> dict[str, tuple[str, ...]]:
    team_a_side = game.team_a_side
    team_b_side = "dire" if team_a_side == "radiant" else "radiant"
    team_a: list[str] = []
    team_b: list[str] = []
    for action in sorted(actions, key=lambda item: item.action_order):
        if action.action_kind != action_kind:
            continue
        hero_identity = _hero_identity(action.source, action.hero_id)
        if action.team_side == team_a_side:
            team_a.append(hero_identity)
        elif action.team_side == team_b_side:
            team_b.append(hero_identity)
    return {"team_a": tuple(team_a), "team_b": tuple(team_b)}


def _hero_slots(heroes: Sequence[str], slot_count: int) -> tuple[str, ...]:
    return tuple(heroes[:slot_count]) + (DRAFT_MISSING_HERO,) * max(
        0,
        slot_count - len(heroes),
    )


def _hero_stats(
    games: Sequence[HistoricalDotaGame],
    actions_by_game: Mapping[str, Sequence[HistoricalDraftAction]],
) -> dict[str, tuple[int, int]]:
    stats: dict[str, tuple[int, int]] = {}
    for game in games:
        side_picks = _side_pick_heroes(game, actions_by_game[game.id])
        for team, heroes in side_picks.items():
            won = (game.winner_side == team)
            for hero in heroes:
                wins, count = stats.get(hero, (0, 0))
                stats[hero] = (wins + int(won), count + 1)
    return stats


def _synergy_stats(
    games: Sequence[HistoricalDotaGame],
    actions_by_game: Mapping[str, Sequence[HistoricalDraftAction]],
) -> dict[tuple[str, str], tuple[int, int]]:
    stats: dict[tuple[str, str], tuple[int, int]] = {}
    for game in games:
        side_picks = _side_pick_heroes(game, actions_by_game[game.id])
        for team, heroes in side_picks.items():
            won = game.winner_side == team
            for pair in _hero_pairs(heroes):
                wins, count = stats.get(pair, (0, 0))
                stats[pair] = (wins + int(won), count + 1)
    return stats


def _matchup_stats(
    games: Sequence[HistoricalDotaGame],
    actions_by_game: Mapping[str, Sequence[HistoricalDraftAction]],
) -> dict[tuple[str, str], tuple[int, int]]:
    stats: dict[tuple[str, str], tuple[int, int]] = {}
    for game in games:
        side_picks = _side_pick_heroes(game, actions_by_game[game.id])
        team_a_won = game.winner_side == "team_a"
        for hero_a in side_picks["team_a"]:
            for hero_b in side_picks["team_b"]:
                _add_matchup_result(stats, hero_a, hero_b, team_a_won)
                _add_matchup_result(stats, hero_b, hero_a, not team_a_won)
    return stats


def _team_hero_familiarity(
    games: Sequence[HistoricalDotaGame],
    actions_by_game: Mapping[str, Sequence[HistoricalDraftAction]],
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for game in games:
        side_picks = _side_pick_heroes(game, actions_by_game[game.id])
        for team, source_id in (
            ("team_a", game.team_a_source_id),
            ("team_b", game.team_b_source_id),
        ):
            if source_id is None:
                continue
            team_key = f"{game.source}:team:{source_id}"
            for hero in side_picks[team]:
                key = (team_key, hero)
                counts[key] = counts.get(key, 0) + 1
    return counts


def _hero_history_features(
    *,
    team_a_picks: Sequence[str],
    team_b_picks: Sequence[str],
    hero_stats: Mapping[str, tuple[int, int]],
) -> dict[str, float]:
    team_a = _rate_summary(team_a_picks, hero_stats)
    team_b = _rate_summary(team_b_picks, hero_stats)
    return {
        "team_a_hero_prior_win_rate_mean": team_a.mean_rate,
        "team_b_hero_prior_win_rate_mean": team_b.mean_rate,
        "team_a_unseen_hero_count": float(team_a.unseen_count),
        "team_b_unseen_hero_count": float(team_b.unseen_count),
        "hero_history_sample_total": float(team_a.sample_total + team_b.sample_total),
    }


def _synergy_features(
    *,
    team_a_picks: Sequence[str],
    team_b_picks: Sequence[str],
    synergy_stats: Mapping[tuple[str, str], tuple[int, int]],
) -> dict[str, float]:
    team_a = _pair_rate_summary(_hero_pairs(team_a_picks), synergy_stats)
    team_b = _pair_rate_summary(_hero_pairs(team_b_picks), synergy_stats)
    return {
        "team_a_synergy_mean_win_rate": team_a.mean_rate,
        "team_b_synergy_mean_win_rate": team_b.mean_rate,
        "team_a_unseen_synergy_pair_count": float(team_a.unseen_count),
        "team_b_unseen_synergy_pair_count": float(team_b.unseen_count),
        "synergy_sample_total": float(team_a.sample_total + team_b.sample_total),
    }


def _matchup_features(
    *,
    team_a_picks: Sequence[str],
    team_b_picks: Sequence[str],
    matchup_stats: Mapping[tuple[str, str], tuple[int, int]],
) -> dict[str, float]:
    pairs = tuple((hero_a, hero_b) for hero_a in team_a_picks for hero_b in team_b_picks)
    summary = _pair_rate_summary(pairs, matchup_stats)
    rates = [
        _rate(matchup_stats[pair])
        for pair in pairs
        if pair in matchup_stats and matchup_stats[pair][1] > 0
    ]
    return {
        "cross_team_matchup_mean_win_rate": summary.mean_rate,
        "cross_team_matchup_min_win_rate": min(rates) if rates else 0.5,
        "cross_team_matchup_max_win_rate": max(rates) if rates else 0.5,
        "cross_team_matchup_sample_total": float(summary.sample_total),
        "unseen_cross_team_matchup_count": float(summary.unseen_count),
    }


def _familiarity_features(
    *,
    game: HistoricalDotaGame,
    team_a_picks: Sequence[str],
    team_b_picks: Sequence[str],
    familiarity_stats: Mapping[tuple[str, str], int],
) -> dict[str, float]:
    team_a = _team_familiarity(
        game.source,
        game.team_a_source_id,
        team_a_picks,
        familiarity_stats,
    )
    team_b = _team_familiarity(
        game.source,
        game.team_b_source_id,
        team_b_picks,
        familiarity_stats,
    )
    return {
        "team_a_hero_familiarity_mean_matches": team_a[0],
        "team_b_hero_familiarity_mean_matches": team_b[0],
        "team_a_hero_familiarity_unavailable": team_a[1],
        "team_b_hero_familiarity_unavailable": team_b[1],
    }


@dataclass(frozen=True)
class _Summary:
    mean_rate: float
    sample_total: int
    unseen_count: int


def _rate_summary(
    heroes: Sequence[str],
    stats: Mapping[str, tuple[int, int]],
) -> _Summary:
    rates: list[float] = []
    sample_total = 0
    unseen = 0
    for hero in heroes:
        value = stats.get(hero)
        if value is None or value[1] == 0:
            rates.append(0.5)
            unseen += 1
        else:
            rates.append(_rate(value))
            sample_total += value[1]
    return _Summary(
        mean_rate=sum(rates) / len(rates) if rates else 0.5,
        sample_total=sample_total,
        unseen_count=unseen,
    )


def _pair_rate_summary(
    pairs: Sequence[tuple[str, str]],
    stats: Mapping[tuple[str, str], tuple[int, int]],
) -> _Summary:
    rates: list[float] = []
    sample_total = 0
    unseen = 0
    for pair in pairs:
        value = stats.get(pair)
        if value is None or value[1] == 0:
            rates.append(0.5)
            unseen += 1
        else:
            rates.append(_rate(value))
            sample_total += value[1]
    return _Summary(
        mean_rate=sum(rates) / len(rates) if rates else 0.5,
        sample_total=sample_total,
        unseen_count=unseen,
    )


def _team_familiarity(
    source: str,
    source_team_id: str | None,
    heroes: Sequence[str],
    stats: Mapping[tuple[str, str], int],
) -> tuple[float, float]:
    if source_team_id is None:
        return 0.0, 1.0
    team_key = f"{source}:team:{source_team_id}"
    counts = [stats.get((team_key, hero), 0) for hero in heroes]
    return (sum(counts) / len(counts) if counts else 0.0), 0.0


def _hero_pairs(heroes: Sequence[str]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (hero_a, hero_b) if hero_a <= hero_b else (hero_b, hero_a)
        for hero_a, hero_b in combinations(heroes, 2)
    )


def _add_matchup_result(
    stats: dict[tuple[str, str], tuple[int, int]],
    hero: str,
    opponent_hero: str,
    won: bool,
) -> None:
    key = (hero, opponent_hero)
    wins, count = stats.get(key, (0, 0))
    stats[key] = (wins + int(won), count + 1)


def _rate(value: tuple[int, int]) -> float:
    wins, count = value
    return wins / count if count else 0.5


def _hero_identity(source: str, hero_id: int) -> str:
    return f"{source}:hero:{hero_id}"
