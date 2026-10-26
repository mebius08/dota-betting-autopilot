-- GLOBAL replay observations only; these positions do not establish Team POV.
CREATE TABLE IF NOT EXISTS historical_match_spatial_frames (
    source TEXT NOT NULL,
    source_match_id TEXT NOT NULL,
    replay_tick BIGINT NOT NULL CHECK (replay_tick >= 0),
    actual_game_time_seconds NUMERIC NOT NULL CHECK (actual_game_time_seconds >= 0),
    replay_sha256 TEXT NOT NULL CHECK (length(replay_sha256) = 64),
    provenance TEXT NOT NULL CHECK (provenance = 'HISTORICAL_ORACLE'),
    clock_normalization_status TEXT NOT NULL CHECK (clock_normalization_status = 'PROVEN'),
    clock_normalization_method TEXT NOT NULL,
    coordinate_system TEXT NOT NULL CHECK (coordinate_system = 'SOURCE2_NATIVE_CELL_GRID'),
    coordinate_units TEXT NOT NULL,
    PRIMARY KEY (source, source_match_id, replay_tick),
    FOREIGN KEY (source, source_match_id)
        REFERENCES historical_matches (source, source_match_id)
);

CREATE TABLE IF NOT EXISTS historical_match_spatial_heroes (
    source TEXT NOT NULL,
    source_match_id TEXT NOT NULL,
    replay_tick BIGINT NOT NULL,
    player_slot INTEGER NOT NULL CHECK (player_slot BETWEEN 0 AND 9),
    team TEXT NOT NULL CHECK (team IN ('RADIANT', 'DIRE')),
    hero_id INTEGER NOT NULL CHECK (hero_id > 0),
    selected_hero_handle BIGINT CHECK (selected_hero_handle >= 0),
    hero_entity_handle BIGINT CHECK (hero_entity_handle >= 0),
    hero_entity_class TEXT,
    life_state INTEGER CHECK (life_state >= 0),
    position_status TEXT NOT NULL CHECK (length(position_status) > 0),
    x NUMERIC,
    y NUMERIC,
    cell_x INTEGER CHECK (cell_x BETWEEN 0 AND 65535),
    cell_y INTEGER CHECK (cell_y BETWEEN 0 AND 65535),
    offset_x NUMERIC CHECK (offset_x >= 0 AND offset_x < 256),
    offset_y NUMERIC CHECK (offset_y >= 0 AND offset_y < 256),
    CHECK ((x IS NULL AND y IS NULL AND position_status <> 'OBSERVED') OR
           (x IS NOT NULL AND y IS NOT NULL AND position_status = 'OBSERVED'
            AND life_state IS NOT NULL AND life_state = 0)),
    CHECK (position_status <> 'HERO_NOT_ALIVE' OR
           (life_state IS NOT NULL AND life_state > 0)),
    PRIMARY KEY (source, source_match_id, replay_tick, player_slot),
    UNIQUE (source, source_match_id, replay_tick, hero_id),
    FOREIGN KEY (source, source_match_id, replay_tick)
        REFERENCES historical_match_spatial_frames (source, source_match_id, replay_tick)
);
