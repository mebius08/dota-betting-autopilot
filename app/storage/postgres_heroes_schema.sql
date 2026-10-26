-- Historical GLOBAL observations only; no Team POV or LIVE availability claim.
CREATE TABLE IF NOT EXISTS historical_match_hero_snapshots (
    source TEXT NOT NULL,
    source_match_id TEXT NOT NULL,
    replay_tick BIGINT NOT NULL CHECK (replay_tick >= 0),
    player_slot INTEGER NOT NULL CHECK (player_slot BETWEEN 0 AND 9),
    actual_game_time_seconds NUMERIC NOT NULL CHECK (actual_game_time_seconds >= 0),
    replay_sha256 TEXT NOT NULL CHECK (length(replay_sha256) = 64),
    provenance TEXT NOT NULL CHECK (provenance = 'HISTORICAL_ORACLE'),
    team TEXT NOT NULL CHECK (team IN ('RADIANT', 'DIRE')),
    team_source_id TEXT,
    hero_id INTEGER NOT NULL CHECK (hero_id > 0),
    hero_name TEXT NOT NULL CHECK (length(hero_name) > 0),
    game_state INTEGER NOT NULL CHECK (game_state >= 0),
    clock_normalization_status TEXT NOT NULL CHECK (clock_normalization_status = 'PROVEN'),
    clock_normalization_method TEXT NOT NULL,
    level BIGINT NOT NULL CHECK (level >= 0),
    kills BIGINT NOT NULL CHECK (kills >= 0),
    deaths BIGINT NOT NULL CHECK (deaths >= 0),
    assists BIGINT NOT NULL CHECK (assists >= 0),
    last_hits BIGINT NOT NULL CHECK (last_hits >= 0),
    denies BIGINT NOT NULL CHECK (denies >= 0),
    net_worth BIGINT NOT NULL CHECK (net_worth >= 0),
    current_gold BIGINT NOT NULL CHECK (current_gold >= 0),
    total_xp BIGINT NOT NULL CHECK (total_xp >= 0),
    -- Ingestion qualifications, not per-frame statuses exported by the parser.
    current_gold_status TEXT NOT NULL CHECK (current_gold_status = 'COMPONENT_COMPLETENESS_UNVERIFIED'),
    inventory_status TEXT NOT NULL CHECK (inventory_status = 'EXPORTED_ENTRIES_ONLY'),
    PRIMARY KEY (source, source_match_id, replay_tick, player_slot),
    UNIQUE (source, source_match_id, replay_tick, hero_id),
    FOREIGN KEY (source, source_match_id)
        REFERENCES historical_matches (source, source_match_id)
);

CREATE TABLE IF NOT EXISTS historical_match_hero_items (
    source TEXT NOT NULL,
    source_match_id TEXT NOT NULL,
    replay_tick BIGINT NOT NULL,
    player_slot INTEGER NOT NULL,
    inventory_slot INTEGER NOT NULL CHECK (inventory_slot BETWEEN 0 AND 23),
    -- Exact compact value: Clarity entity DT-class, not numeric item definition ID.
    item_name TEXT NOT NULL CHECK (length(item_name) > 0),
    PRIMARY KEY (source, source_match_id, replay_tick, player_slot, inventory_slot),
    FOREIGN KEY (source, source_match_id, replay_tick, player_slot)
        REFERENCES historical_match_hero_snapshots
            (source, source_match_id, replay_tick, player_slot)
);
