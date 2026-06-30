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
