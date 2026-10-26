-- Historical observer-specific data; never fill from GLOBAL coordinates.
CREATE TABLE IF NOT EXISTS historical_match_enemy_observations (
    source TEXT NOT NULL,
    source_match_id TEXT NOT NULL,
    replay_tick BIGINT NOT NULL CHECK (replay_tick >= 0),
    pov_team TEXT NOT NULL CHECK (pov_team IN ('RADIANT', 'DIRE')),
    enemy_player_slot INTEGER NOT NULL CHECK (enemy_player_slot BETWEEN 0 AND 9),
    replay_sha256 TEXT NOT NULL CHECK (length(replay_sha256) = 64),
    actual_game_time_seconds NUMERIC NOT NULL CHECK (actual_game_time_seconds >= 0),
    provenance TEXT NOT NULL CHECK (provenance = 'HISTORICAL_TEAM_POV'),
    enemy_hero_id INTEGER NOT NULL CHECK (enemy_hero_id > 0),
    enemy_team TEXT NOT NULL CHECK (enemy_team IN ('RADIANT', 'DIRE') AND enemy_team <> pov_team),
    visibility_state TEXT NOT NULL CHECK (visibility_state IN ('VISIBLE', 'HIDDEN', 'UNKNOWN')),
    current_position_status TEXT NOT NULL,
    current_x NUMERIC,
    current_y NUMERIC,
    last_seen_status TEXT NOT NULL CHECK (last_seen_status IN ('NEVER_SEEN', 'LAST_SEEN', 'OBSERVED_NOW')),
    last_seen_game_time_seconds NUMERIC,
    last_seen_age_seconds NUMERIC,
    last_seen_x NUMERIC,
    last_seen_y NUMERIC,
    PRIMARY KEY (source, source_match_id, replay_tick, pov_team, enemy_player_slot),
    UNIQUE (source, source_match_id, replay_tick, pov_team, enemy_hero_id),
    FOREIGN KEY (source, source_match_id, replay_tick, pov_team)
        REFERENCES historical_match_pov_snapshots (source, source_match_id, replay_tick, pov_team),
    CHECK (
        (visibility_state = 'VISIBLE' AND current_position_status = 'OBSERVED'
            AND current_x IS NOT NULL AND current_y IS NOT NULL)
        OR (visibility_state = 'VISIBLE' AND current_position_status = 'VISIBLE_POSITION_UNAVAILABLE'
            AND current_x IS NULL AND current_y IS NULL)
        OR (visibility_state IN ('HIDDEN', 'UNKNOWN') AND current_position_status = visibility_state
            AND current_x IS NULL AND current_y IS NULL)
    ),
    CHECK (
        (last_seen_status = 'NEVER_SEEN' AND current_x IS NULL
            AND last_seen_game_time_seconds IS NULL AND last_seen_age_seconds IS NULL
            AND last_seen_x IS NULL AND last_seen_y IS NULL)
        OR (last_seen_status IN ('LAST_SEEN', 'OBSERVED_NOW')
            AND last_seen_game_time_seconds IS NOT NULL AND last_seen_game_time_seconds >= 0
            AND last_seen_game_time_seconds <= actual_game_time_seconds
            AND last_seen_age_seconds IS NOT NULL AND last_seen_age_seconds >= 0
            AND last_seen_x IS NOT NULL AND last_seen_y IS NOT NULL
            AND ((last_seen_status = 'LAST_SEEN' AND current_x IS NULL)
                OR (last_seen_status = 'OBSERVED_NOW' AND current_x IS NOT NULL
                    AND last_seen_game_time_seconds = actual_game_time_seconds
                    AND last_seen_age_seconds = 0 AND last_seen_x = current_x AND last_seen_y = current_y)))
    )
);
