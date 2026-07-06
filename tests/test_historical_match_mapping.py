from app.history import (
    map_pandascore_historical_match,
    map_pandascore_historical_matches,
)
from app.tournaments import CompetitiveStage, TournamentRound


def test_map_valid_finished_match_with_team_a_winner() -> None:
    match = map_pandascore_historical_match(_payload())

    assert match is not None
    assert match.id == "pandascore-123"
    assert match.source == "pandascore"
    assert match.source_match_id == "123"
    assert match.team_a_name == "Team Spirit"
    assert match.team_b_name == "PARIVISION"
    assert match.team_a_source_id == "10"
    assert match.team_b_source_id == "20"
    assert match.winner_side == "team_a"
    assert match.usable_for_match_winner_training
    assert match.started_at.isoformat() == "2026-01-01T10:00:00+00:00"
    assert match.ended_at is not None
    assert match.ended_at.isoformat() == "2026-01-01T12:00:00+00:00"
    assert match.tournament_name == "Upper Bracket Final"
    assert match.league_name == "DreamLeague"
    assert match.series_name == "Season 25"
    assert match.best_of == 3


def test_winner_can_map_to_team_b() -> None:
    match = map_pandascore_historical_match(
        _payload(winner_id=20, winner_name="PARIVISION")
    )

    assert match is not None
    assert match.winner_side == "team_b"


def test_missing_winner_is_persistable_but_not_training_usable() -> None:
    match = map_pandascore_historical_match(
        _payload(winner_id=None, winner_name=None, winner=None)
    )

    assert match is not None
    assert match.winner_side is None
    assert not match.usable_for_match_winner_training


def test_winner_id_not_matching_opponents_is_never_guessed() -> None:
    match = map_pandascore_historical_match(
        _payload(winner_id=999, winner_name="Mystery Team")
    )

    assert match is not None
    assert match.winner_side is None
    assert not match.usable_for_match_winner_training


def test_skips_rows_without_two_distinct_teams_or_start_time() -> None:
    assert map_pandascore_historical_match(_payload(opponents=[])) is None
    assert map_pandascore_historical_match(
        _payload(opponents=[_opponent("10", "Team Spirit")])
    ) is None
    assert map_pandascore_historical_match(
        _payload(
            opponents=[
                _opponent("10", " Team  Spirit "),
                _opponent("20", "Team\tSpirit"),
            ]
        )
    ) is None
    assert map_pandascore_historical_match(_payload(begin_at=None)) is None
    assert map_pandascore_historical_match(_payload(begin_at="bad-date")) is None


def test_missing_completion_time_is_not_point_in_time_usable() -> None:
    match = map_pandascore_historical_match(_payload(end_at=None))

    assert match is not None
    assert match.ended_at is None
    assert not match.usable_for_match_winner_training


def test_generic_stage_parser_is_reused_for_bracket_labels() -> None:
    upper = map_pandascore_historical_match(
        _payload(tournament_name="Upper Bracket Final")
    )
    lower = map_pandascore_historical_match(
        _payload(tournament_name="Lower Bracket Final")
    )

    assert upper is not None
    assert lower is not None
    assert upper.competitive_stage is CompetitiveStage.UPPER_BRACKET
    assert upper.normalized_round is TournamentRound.UPPER_BRACKET_FINAL
    assert lower.competitive_stage is CompetitiveStage.LOWER_BRACKET
    assert lower.normalized_round is TournamentRound.LOWER_BRACKET_FINAL


def test_ewc_stage_parser_is_used_only_for_ewc_identity() -> None:
    match = map_pandascore_historical_match(
        _payload(
            league_name="Esports World Cup 2026",
            tournament_name="Survival Grand Final",
        )
    )

    assert match is not None
    assert match.competitive_stage is CompetitiveStage.CROSSOVER
    assert match.normalized_round is TournamentRound.SURVIVAL_FINAL


def test_unknown_stage_remains_unknown() -> None:
    match = map_pandascore_historical_match(
        _payload(tournament_name="Mysterious Invitational")
    )

    assert match is not None
    assert match.competitive_stage is CompetitiveStage.UNKNOWN


def test_mixed_payload_batch_preserves_valid_history() -> None:
    result = map_pandascore_historical_matches(
        [
            _payload(match_id=1),
            {"id": 2},
            "not-a-dict",
            _payload(match_id=3, winner_id=20, winner_name="PARIVISION"),
        ]
    )

    assert [match.source_match_id for match in result.matches] == ["1", "3"]
    assert result.skipped_rows == 2
    assert len(result.warnings) == 2


def _payload(
    *,
    match_id: int | None = 123,
    status: str = "finished",
    begin_at: object = "2026-01-01T10:00:00Z",
    end_at: object = "2026-01-01T12:00:00Z",
    winner_id: int | None = 10,
    winner_name: str | None = "Team Spirit",
    winner: dict[str, object] | None = None,
    opponents: list[dict[str, object]] | None = None,
    league_name: str = "DreamLeague",
    tournament_name: str = "Upper Bracket Final",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "begin_at": begin_at,
        "end_at": end_at,
        "number_of_games": 3,
        "league": {"id": 100, "name": league_name},
        "serie": {"id": 200, "full_name": "Season 25"},
        "tournament": {"id": 300, "name": tournament_name},
        "opponents": opponents
        if opponents is not None
        else [_opponent("10", "Team Spirit"), _opponent("20", "PARIVISION")],
    }
    if match_id is not None:
        payload["id"] = match_id
    if winner is not None:
        payload["winner"] = winner
    elif winner_id is not None or winner_name is not None:
        payload["winner"] = {"id": winner_id, "name": winner_name}
    if winner_id is not None:
        payload["winner_id"] = winner_id
    return payload


def _opponent(source_id: str, name: str) -> dict[str, object]:
    return {"opponent": {"id": source_id, "name": name}}
