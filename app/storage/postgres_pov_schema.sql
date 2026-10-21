CREATE TABLE IF NOT EXISTS historical_match_pov_snapshots (
    source TEXT NOT NULL,
    source_match_id TEXT NOT NULL,
    replay_sha256 TEXT NOT NULL CHECK (length(replay_sha256) = 64),
    actual_game_time_seconds DOUBLE PRECISION NOT NULL CHECK (actual_game_time_seconds >= 0),
    replay_tick BIGINT NOT NULL CHECK (replay_tick >= 0),
    pov_team TEXT NOT NULL CHECK (pov_team IN ('RADIANT', 'DIRE')),
    visible_enemy_count INTEGER NOT NULL CHECK (visible_enemy_count BETWEEN 0 AND 5),
    visible_enemy_with_position_count INTEGER NOT NULL CHECK (visible_enemy_with_position_count BETWEEN 0 AND visible_enemy_count),
    hidden_enemy_count INTEGER NOT NULL CHECK (hidden_enemy_count BETWEEN 0 AND 5),
    nearest_visible_enemy_distance DOUBLE PRECISION,
    own_team_spread DOUBLE PRECISION,
    own_team_spread_status TEXT NOT NULL,
    nearest_visible_enemy_status TEXT NOT NULL,
    PRIMARY KEY (source, source_match_id, replay_tick, pov_team),
    UNIQUE (source, source_match_id, actual_game_time_seconds, pov_team),
    FOREIGN KEY (source, source_match_id)
        REFERENCES historical_matches (source, source_match_id),
    CHECK (visible_enemy_count + hidden_enemy_count <= 5),
    CHECK (
        (own_team_spread_status = 'OK' AND own_team_spread IS NOT NULL AND own_team_spread >= 0)
        OR (own_team_spread_status = 'INSUFFICIENT_OWN_POSITIONS' AND own_team_spread IS NULL)
    ),
    CHECK (
        (nearest_visible_enemy_status = 'OK' AND nearest_visible_enemy_distance IS NOT NULL AND nearest_visible_enemy_distance >= 0)
        OR (nearest_visible_enemy_status IN ('NO_OWN_POSITION', 'NO_VISIBLE_ENEMY_POSITION') AND nearest_visible_enemy_distance IS NULL)
    )
);
