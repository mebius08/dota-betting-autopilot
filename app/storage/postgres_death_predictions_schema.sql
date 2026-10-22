CREATE TABLE IF NOT EXISTS historical_match_death_predictions (
    source TEXT NOT NULL CHECK (source = 'replay_clarity'),
    source_match_id TEXT NOT NULL,
    replay_tick BIGINT NOT NULL CHECK (replay_tick >= 0),
    game_time_seconds NUMERIC NOT NULL CHECK (game_time_seconds >= 0),
    pov_team TEXT NOT NULL CHECK (pov_team IN ('RADIANT', 'DIRE')),
    horizon_seconds INTEGER NOT NULL CHECK (horizon_seconds = 30),
    constant_probability DOUBLE PRECISION NOT NULL CHECK (constant_probability BETWEEN 0 AND 1),
    logistic_probability DOUBLE PRECISION NOT NULL CHECK (logistic_probability BETWEEN 0 AND 1),
    target INTEGER NOT NULL CHECK (target IN (0, 1)),
    prediction_origin TEXT NOT NULL DEFAULT 'HISTORICAL_OUT_OF_FOLD'
        CHECK (prediction_origin = 'HISTORICAL_OUT_OF_FOLD'),
    PRIMARY KEY (source, source_match_id, replay_tick, pov_team, horizon_seconds),
    FOREIGN KEY (source, source_match_id)
        REFERENCES historical_matches (source, source_match_id)
);
