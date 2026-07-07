PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    tournament_keyword TEXT NOT NULL,
    streamer_channel TEXT NOT NULL,
    execution_mode TEXT NOT NULL,
    target_bets_per_match REAL NOT NULL,
    max_bets_per_match INTEGER NOT NULL,
    score_threshold REAL NOT NULL,
    active INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tournament_name TEXT NOT NULL,
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    format TEXT NOT NULL,
    status TEXT NOT NULL,
    start_time TEXT NULL,
    external_id TEXT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id)
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    external_market_id TEXT NULL,
    market TEXT NOT NULL,
    selection TEXT NOT NULL,
    line REAL NULL,
    odds REAL NOT NULL,
    phase TEXT NOT NULL,
    is_live INTEGER NOT NULL,
    is_suspended INTEGER NOT NULL,
    bookmaker TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id),
    FOREIGN KEY (match_id) REFERENCES matches (id)
);

CREATE TABLE IF NOT EXISTS bet_candidates (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    market TEXT NOT NULL,
    selection TEXT NOT NULL,
    line REAL NULL,
    odds REAL NOT NULL,
    phase TEXT NOT NULL,
    market_score REAL NOT NULL,
    phase_score REAL NOT NULL,
    line_score REAL NOT NULL,
    streamer_score REAL NOT NULL,
    risk_score REAL NOT NULL,
    final_score REAL NOT NULL,
    decision TEXT NOT NULL,
    explanation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id),
    FOREIGN KEY (match_id) REFERENCES matches (id)
);

CREATE TABLE IF NOT EXISTS bets (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    market TEXT NOT NULL,
    selection TEXT NOT NULL,
    line REAL NULL,
    odds REAL NOT NULL,
    stake_pct REAL NOT NULL,
    status TEXT NOT NULL,
    result TEXT NOT NULL,
    profit_units REAL NOT NULL,
    created_at TEXT NOT NULL,
    settled_at TEXT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id),
    FOREIGN KEY (match_id) REFERENCES matches (id),
    FOREIGN KEY (candidate_id) REFERENCES bet_candidates (id)
);

CREATE TABLE IF NOT EXISTS streamer_utterances (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    match_id TEXT NULL,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    detected_market TEXT NULL,
    detected_selection TEXT NULL,
    detected_team TEXT NULL,
    signal_type TEXT NULL,
    strength REAL NOT NULL,
    confidence REAL NOT NULL,
    hype_flag INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions (id),
    FOREIGN KEY (match_id) REFERENCES matches (id)
);

CREATE TABLE IF NOT EXISTS historical_matches (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_match_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NULL,
    team_a_name TEXT NOT NULL,
    team_b_name TEXT NOT NULL,
    team_a_source_id TEXT NULL,
    team_b_source_id TEXT NULL,
    winner_name TEXT NULL,
    winner_source_id TEXT NULL,
    winner_side TEXT NULL,
    tournament_name TEXT NULL,
    tournament_source_id TEXT NULL,
    league_name TEXT NULL,
    league_source_id TEXT NULL,
    series_name TEXT NULL,
    series_source_id TEXT NULL,
    raw_stage_label TEXT NULL,
    competitive_stage TEXT NOT NULL,
    normalized_round TEXT NOT NULL,
    best_of INTEGER NULL,
    status TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE (source, source_match_id)
);

CREATE TABLE IF NOT EXISTS players (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_player_id TEXT NOT NULL,
    name TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source, source_player_id)
);

CREATE TABLE IF NOT EXISTS team_organizations (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_team_id TEXT NOT NULL,
    name TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source, source_team_id)
);

CREATE TABLE IF NOT EXISTS roster_snapshots (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_snapshot_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    source_context TEXT NULL,
    tournament_source_id TEXT NULL,
    tournament_name TEXT NULL,
    observed_at TEXT NOT NULL,
    valid_from TEXT NULL,
    valid_until TEXT NULL,
    player_roster_fingerprint TEXT NOT NULL,
    staff_roster_fingerprint TEXT NULL,
    ingested_at TEXT NOT NULL,
    UNIQUE (source, source_snapshot_id),
    FOREIGN KEY (organization_id) REFERENCES team_organizations (id)
);

CREATE TABLE IF NOT EXISTS roster_memberships (
    id TEXT PRIMARY KEY,
    roster_snapshot_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('player', 'coach')),
    player_id TEXT NULL,
    source TEXT NOT NULL,
    source_member_id TEXT NULL,
    member_name TEXT NOT NULL,
    position_index INTEGER NOT NULL,
    FOREIGN KEY (roster_snapshot_id) REFERENCES roster_snapshots (id)
        ON DELETE CASCADE,
    FOREIGN KEY (player_id) REFERENCES players (id),
    CHECK (role != 'player' OR player_id IS NOT NULL),
    CHECK (role != 'coach' OR player_id IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_players_source_identity
ON players (source, source_player_id);

CREATE INDEX IF NOT EXISTS idx_team_organizations_source_identity
ON team_organizations (source, source_team_id);

CREATE INDEX IF NOT EXISTS idx_roster_snapshots_organization
ON roster_snapshots (organization_id, observed_at);

CREATE INDEX IF NOT EXISTS idx_roster_snapshots_available
ON roster_snapshots (observed_at, valid_from, valid_until);

CREATE INDEX IF NOT EXISTS idx_roster_snapshots_player_fingerprint
ON roster_snapshots (player_roster_fingerprint);

CREATE INDEX IF NOT EXISTS idx_roster_memberships_player
ON roster_memberships (player_id, roster_snapshot_id);

CREATE INDEX IF NOT EXISTS idx_roster_memberships_role
ON roster_memberships (role, roster_snapshot_id);
