CREATE TABLE IF NOT EXISTS historical_match_economy_snapshots (
    source TEXT NOT NULL,
    source_match_id TEXT NOT NULL,
    actual_game_time_seconds DOUBLE PRECISION NOT NULL CHECK (actual_game_time_seconds >= 0),
    replay_tick BIGINT NOT NULL CHECK (replay_tick >= 0),
    replay_sha256 TEXT NOT NULL CHECK (length(replay_sha256) = 64),
    radiant_net_worth BIGINT NOT NULL CHECK (radiant_net_worth >= 0),
    radiant_current_gold BIGINT NOT NULL CHECK (radiant_current_gold >= 0),
    radiant_total_xp BIGINT NOT NULL CHECK (radiant_total_xp >= 0),
    dire_net_worth BIGINT NOT NULL CHECK (dire_net_worth >= 0),
    dire_current_gold BIGINT NOT NULL CHECK (dire_current_gold >= 0),
    dire_total_xp BIGINT NOT NULL CHECK (dire_total_xp >= 0),
    PRIMARY KEY (source, source_match_id, replay_tick),
    UNIQUE (source, source_match_id, actual_game_time_seconds),
    FOREIGN KEY (source, source_match_id)
        REFERENCES historical_matches (source, source_match_id)
);
