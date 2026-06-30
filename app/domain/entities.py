from dataclasses import dataclass
from datetime import datetime
from typing import Literal


ExecutionMode = Literal["paper", "confirm", "auto", "signal"]
MatchStatus = Literal["upcoming", "live", "finished", "cancelled"]
OddsPhase = Literal["pre_match", "after_draft", "live", "finished", "unknown"]
BetStatus = Literal["created", "confirmed", "placed", "skipped", "settled"]
BetResult = Literal["win", "loss", "push", "void", "unknown"]
Decision = Literal["bet", "skip", "watch"]

EXECUTION_MODES = ("paper", "confirm", "auto", "signal")
MATCH_STATUSES = ("upcoming", "live", "finished", "cancelled")
ODDS_PHASES = ("pre_match", "after_draft", "live", "finished", "unknown")
BET_STATUSES = ("created", "confirmed", "placed", "skipped", "settled")
BET_RESULTS = ("win", "loss", "push", "void", "unknown")
DECISIONS = ("bet", "skip", "watch")


def _validate_allowed(
    field_name: str,
    value: str,
    allowed_values: tuple[str, ...],
) -> None:
    if value not in allowed_values:
        allowed = ", ".join(allowed_values)
        raise ValueError(f"{field_name} must be one of: {allowed}")


@dataclass
class Session:
    id: str
    name: str
    tournament_keyword: str
    streamer_channel: str
    execution_mode: ExecutionMode
    target_bets_per_match: float
    max_bets_per_match: int
    score_threshold: float
    active: bool
    created_at: datetime
    ended_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_allowed("execution_mode", self.execution_mode, EXECUTION_MODES)


@dataclass
class Match:
    id: str
    session_id: str
    tournament_name: str
    team_a: str
    team_b: str
    format: str
    status: MatchStatus
    start_time: datetime | None = None
    external_id: str | None = None

    def __post_init__(self) -> None:
        _validate_allowed("status", self.status, MATCH_STATUSES)


@dataclass
class OddsSnapshot:
    id: str
    session_id: str
    match_id: str
    external_market_id: str | None
    market: str
    selection: str
    line: float | None
    odds: float
    phase: OddsPhase
    is_live: bool
    is_suspended: bool
    bookmaker: str
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_allowed("phase", self.phase, ODDS_PHASES)


@dataclass
class BetCandidate:
    id: str
    session_id: str
    match_id: str
    market: str
    selection: str
    line: float | None
    odds: float
    phase: OddsPhase
    market_score: float
    phase_score: float
    line_score: float
    streamer_score: float
    risk_score: float
    final_score: float
    decision: Decision
    explanation: str
    created_at: datetime

    def __post_init__(self) -> None:
        _validate_allowed("phase", self.phase, ODDS_PHASES)
        _validate_allowed("decision", self.decision, DECISIONS)


@dataclass
class Bet:
    id: str
    session_id: str
    match_id: str
    candidate_id: str
    mode: ExecutionMode
    market: str
    selection: str
    line: float | None
    odds: float
    stake_pct: float
    status: BetStatus
    result: BetResult
    profit_units: float
    created_at: datetime
    settled_at: datetime | None = None

    def __post_init__(self) -> None:
        _validate_allowed("mode", self.mode, EXECUTION_MODES)
        _validate_allowed("status", self.status, BET_STATUSES)
        _validate_allowed("result", self.result, BET_RESULTS)


@dataclass
class StreamerUtterance:
    id: str
    session_id: str
    match_id: str | None
    source: str
    text: str
    detected_market: str | None
    detected_selection: str | None
    detected_team: str | None
    signal_type: str | None
    strength: float
    confidence: float
    hype_flag: bool
    created_at: datetime
