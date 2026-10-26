-- Discovery metadata only. Presence here proves neither DEM nor game-state availability.
CREATE TABLE IF NOT EXISTS opendota_pro_matches (
    match_id BIGINT PRIMARY KEY CHECK (match_id > 0),
    source TEXT NOT NULL DEFAULT 'opendota/proMatches',
    start_time BIGINT CHECK (start_time > 0),
    duration_seconds INTEGER CHECK (duration_seconds >= 0),
    league_id BIGINT CHECK (league_id >= 0),
    league_name TEXT,
    radiant_team_id BIGINT CHECK (radiant_team_id >= 0),
    radiant_team_name TEXT,
    dire_team_id BIGINT CHECK (dire_team_id >= 0),
    dire_team_name TEXT,
    radiant_win BOOLEAN,
    radiant_score INTEGER CHECK (radiant_score >= 0),
    dire_score INTEGER CHECK (dire_score >= 0),
    series_id BIGINT CHECK (series_id >= 0),
    series_type INTEGER CHECK (series_type >= 0),
    version INTEGER CHECK (version >= 0),
    raw_metadata JSONB NOT NULL CHECK (jsonb_typeof(raw_metadata) = 'object'),
    dem_status TEXT NOT NULL DEFAULT 'NOT_CHECKED',
    game_state_status TEXT NOT NULL DEFAULT 'NOT_CHECKED',
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata_updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS opendota_pro_matches_start_time_idx
    ON opendota_pro_matches (start_time);
