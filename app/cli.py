from argparse import ArgumentParser, ArgumentTypeError, Namespace
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time as datetime_time, timezone
import math
from pathlib import Path
import sys
import time
from typing import Any, cast

from app.collectors import TranscriptFileStreamerSpeechCollector
from app.domain import Bet, BetCandidate, BetResult, Match, OddsSnapshot, Session
from app.domain import StreamerUtterance
from app.edge import CandidateEdgeAnalysis, build_edge_analyses
from app.reports import build_report, build_report_from_repository
from app.scoring.hybrid_scorer import BetScorePredictor
from app.services import SessionManager, create_autopilot_service
from app.storage import SQLiteRepository, init_db


EXECUTION_MODE_CHOICES = ("paper", "signal", "confirm", "auto")


def build_config_from_args(args: Namespace) -> dict[str, Any]:
    return {
        "mode": {"execution": args.mode},
        "session": {
            "game": "Dota 2",
            "tournament_keyword": args.tournament,
            "blocked_keywords": ["Qualifier", "Academy", "Showmatch"],
        },
        "streamer": {
            "platform": "transcript",
            "channel": "manual_transcript",
            "use_chat": False,
            "use_speech_to_text": False,
            "transcript_path": str(args.transcript),
        },
        "betting": {
            "target_bets_per_match": args.target_bets_per_match,
            "max_bets_per_match": args.max_bets_per_match,
            "score_threshold": args.score_threshold,
            "max_exposure_per_match_pct": 1.5,
            "max_exposure_per_day_pct": 5.0,
            "default_stake_pct": 0.35,
        },
        "markets": {
            "allow": [
                "total_kills",
                "map_duration",
                "team_total_kills",
                "total_maps",
                "map_handicap",
                "map_winner",
                "live_total_kills",
                "live_duration",
            ],
            "block": ["next_kill", "next_tower", "first_blood"],
        },
        "odds": {
            "min_odds": 1.30,
            "max_odds": 3.50,
            "max_odds_drop_pct_before_execution": 8,
        },
    }


def ensure_transcript_file(path: str | Path) -> None:
    transcript_path = Path(path)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    if not transcript_path.exists():
        transcript_path.write_text("", encoding="utf-8")


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="python -m app.cli",
        description="Run the Dota paper betting autopilot from a transcript file.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_once_parser = subparsers.add_parser(
        "run-once",
        help="Run one paper autopilot pass.",
    )
    _add_common_run_options(run_once_parser)

    loop_parser = subparsers.add_parser(
        "loop",
        help="Run multiple paper autopilot passes.",
    )
    _add_common_run_options(loop_parser)
    loop_parser.add_argument(
        "--interval-seconds",
        type=_positive_int,
        default=30,
        help="Delay between iterations. Must be at least 1 second.",
    )
    loop_parser.add_argument(
        "--iterations",
        type=_positive_int,
        default=1,
        help="Number of passes to run. Defaults to 1; no infinite loop.",
    )

    report_parser = subparsers.add_parser(
        "report",
        help="Show paper trading history from SQLite.",
    )
    report_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    report_parser.add_argument("--session-id")
    report_parser.add_argument("--last-bets", type=_positive_int, default=10)
    report_parser.add_argument("--show-utterances", action="store_true")
    report_parser.add_argument(
        "--last-utterances",
        type=_positive_int,
        default=10,
    )

    fetch_matches_parser = subparsers.add_parser(
        "fetch-matches",
        help="Fetch read-only real match metadata from a provider.",
    )
    fetch_matches_parser.add_argument(
        "--provider",
        choices=("pandascore",),
        required=True,
    )
    fetch_matches_parser.add_argument("--limit", type=_positive_int, default=20)
    fetch_matches_parser.add_argument(
        "--status",
        choices=("upcoming", "live", "all"),
        default="all",
    )
    fetch_matches_parser.add_argument(
        "--scope",
        choices=("ewc-2026",),
        help="Optional read-only scope filter for fetched matches.",
    )
    fetch_matches_parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        help="HTTP timeout in seconds.",
    )

    sync_history_parser = subparsers.add_parser(
        "sync-history",
        help="Sync bounded historical Dota match data from a provider.",
    )
    sync_history_parser.add_argument(
        "--provider",
        choices=("pandascore",),
        required=True,
    )
    sync_history_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    sync_history_parser.add_argument(
        "--since",
        type=_utc_start_date,
        required=True,
        help="UTC start date for provider history, YYYY-MM-DD.",
    )
    sync_history_parser.add_argument(
        "--until",
        type=_utc_end_date,
        required=True,
        help="UTC end date for provider history, YYYY-MM-DD.",
    )
    sync_history_parser.add_argument(
        "--page-size",
        type=_pandascore_page_size,
        default=50,
        help="Provider page size, 1-100.",
    )
    sync_history_parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=None,
        help=(
            "Maximum provider pages to read. Omit to continue until provider "
            "completion inside the explicit date window."
        ),
    )
    sync_history_parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        help="HTTP timeout in seconds.",
    )

    sync_drafts_parser = subparsers.add_parser(
        "sync-drafts",
        help="Sync bounded historical Dota game draft data from a provider.",
    )
    sync_drafts_parser.add_argument(
        "--provider",
        choices=("opendota", "stratz-public"),
        required=True,
    )
    sync_drafts_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    sync_drafts_parser.add_argument(
        "--since",
        type=_utc_start_date,
        help="UTC start date for provider game history, YYYY-MM-DD.",
    )
    sync_drafts_parser.add_argument(
        "--until",
        type=_utc_end_date,
        help="UTC end date for provider game history, YYYY-MM-DD.",
    )
    sync_drafts_parser.add_argument(
        "--match-id",
        action="append",
        default=[],
        help=(
            "Explicit Valve match ID for the stratz-public provider. Repeat for "
            "a bounded canary set."
        ),
    )
    sync_drafts_parser.add_argument(
        "--manifest",
        help=(
            "Named bounded STRATZ public trajectory backfill manifest. "
            "Use instead of repeated --match-id."
        ),
    )
    sync_drafts_parser.add_argument(
        "--page-size",
        type=_pandascore_page_size,
        default=100,
        help="Provider page size, 1-100.",
    )
    sync_drafts_parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=None,
        help="Maximum provider pages to read.",
    )
    sync_drafts_parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        help="HTTP timeout in seconds.",
    )
    sync_drafts_parser.add_argument(
        "--delay-seconds",
        type=_non_negative_float,
        default=1.0,
        help="Sequential delay between stratz-public page requests.",
    )
    sync_drafts_parser.add_argument(
        "--max-retries",
        type=_non_negative_int,
        default=1,
        help="Bounded retries for retryable stratz-public transport failures.",
    )
    sync_drafts_parser.add_argument(
        "--retry-backoff-seconds",
        type=_non_negative_float,
        default=1.0,
        help="Backoff between retryable stratz-public attempts.",
    )
    sync_drafts_parser.add_argument(
        "--skip-referenced-resources",
        action="store_true",
        help="Do not fetch JSON/page-data resources directly referenced by pages.",
    )

    stratz_probe_parser = subparsers.add_parser(
        "probe-stratz-history",
        help="Run a read-only STRATZ historical game data feasibility probe.",
    )
    stratz_probe_parser.add_argument(
        "--sample-size",
        type=_positive_int,
        default=12,
        help="Representative sample size to probe, default 12.",
    )
    stratz_probe_parser.add_argument(
        "--match-id",
        action="append",
        default=[],
        help=(
            "Explicit STRATZ/Valve match ID to probe. Repeat for multiple "
            "matches. If omitted, the command tries STRATZ match discovery."
        ),
    )
    stratz_probe_parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        help="HTTP timeout in seconds.",
    )
    stratz_probe_parser.add_argument(
        "--delay-seconds",
        type=_non_negative_float,
        default=1.0,
        help="Sequential delay between match requests.",
    )

    public_pages_probe_parser = subparsers.add_parser(
        "probe-public-match-pages",
        help="Run a read-only public match page data feasibility probe.",
    )
    public_pages_probe_parser.add_argument(
        "--source",
        choices=("stratz", "sofascore"),
        default="stratz",
        help="Public page source to probe.",
    )
    public_pages_probe_parser.add_argument(
        "--match-id",
        action="append",
        default=[],
        help=(
            "Valve/source match ID to probe. Repeat for multiple matches. "
            "STRATZ URLs are built as /match/<id>."
        ),
    )
    public_pages_probe_parser.add_argument(
        "--page-url",
        action="append",
        default=[],
        help=(
            "Explicit public match page URL to probe. Useful for sources whose "
            "page slug is not derivable from a Valve match ID."
        ),
    )
    public_pages_probe_parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        help="HTTP timeout in seconds.",
    )
    public_pages_probe_parser.add_argument(
        "--delay-seconds",
        type=_non_negative_float,
        default=1.0,
        help="Sequential delay between public page requests.",
    )
    public_pages_probe_parser.add_argument(
        "--skip-referenced-resources",
        action="store_true",
        help="Do not fetch JSON/page-data resources directly referenced by pages.",
    )

    stratz_trajectory_audit_parser = subparsers.add_parser(
        "stratz-trajectory-audit",
        help="Audit persisted STRATZ public gold/XP trajectory corpus.",
    )
    stratz_trajectory_audit_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )

    sync_rosters_parser = subparsers.add_parser(
        "sync-rosters",
        help="Sync bounded historical Dota roster data from a provider.",
    )
    sync_rosters_parser.add_argument(
        "--provider",
        choices=("pandascore",),
        required=True,
    )
    sync_rosters_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    sync_rosters_parser.add_argument(
        "--max-tournaments",
        type=_positive_int,
        default=25,
        help="Maximum persisted historical tournaments to request.",
    )
    sync_rosters_parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        help="HTTP timeout in seconds.",
    )

    history_status_parser = subparsers.add_parser(
        "history-status",
        help="Show offline historical Dota dataset status.",
    )
    history_status_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )

    draft_history_status_parser = subparsers.add_parser(
        "draft-history-status",
        help="Show offline historical Dota game/draft dataset status.",
    )
    draft_history_status_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    draft_history_status_parser.add_argument(
        "--provider",
        choices=("opendota", "stratz-public"),
        default="opendota",
        help="Draft data provider namespace to inspect.",
    )

    roster_status_parser = subparsers.add_parser(
        "roster-status",
        help="Show offline historical Dota roster dataset status.",
    )
    roster_status_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )

    lineage_status_parser = subparsers.add_parser(
        "lineage-status",
        help="Show offline derived roster lineage status.",
    )
    lineage_status_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    lineage_status_parser.add_argument(
        "--as-of",
        type=_utc_datetime,
        help=(
            "ISO-8601 UTC-aware cutoff timestamp, for example "
            "2026-07-07T12:00:00Z."
        ),
    )

    feature_status_parser = subparsers.add_parser(
        "feature-status",
        help="Show offline point-in-time historical feature readiness.",
    )
    feature_status_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    feature_status_parser.add_argument(
        "--as-of",
        type=_utc_datetime,
        help=(
            "ISO-8601 UTC-aware cutoff timestamp, for example "
            "2026-07-07T12:00:00Z."
        ),
    )
    feature_status_parser.add_argument(
        "--decay-days",
        type=_positive_float,
        default=90.0,
        help="Recency exponential decay baseline in days.",
    )

    ewc_status_parser = subparsers.add_parser(
        "ewc-status",
        help="Show persisted EWC 2026 Dota match scope status.",
    )
    ewc_status_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )

    fetch_odds_parser = subparsers.add_parser(
        "fetch-odds",
        help="Fetch read-only real Dota 2 odds from a provider.",
    )
    fetch_odds_parser.add_argument(
        "--provider",
        choices=("oddspapi",),
        required=True,
    )
    fetch_odds_parser.add_argument("--limit", type=_positive_int, default=20)
    fetch_odds_parser.add_argument(
        "--bookmakers",
        help="Optional comma-separated bookmaker filter.",
    )
    fetch_odds_parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        help="HTTP timeout in seconds.",
    )

    export_bets_parser = subparsers.add_parser(
        "export-bets",
        help="Export persisted paper bets to CSV.",
    )
    export_bets_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    export_bets_parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output CSV path.",
    )

    export_candidates_parser = subparsers.add_parser(
        "export-candidates",
        help="Export persisted bet candidates to CSV.",
    )
    export_candidates_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    export_candidates_parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output CSV path.",
    )

    export_utterances_parser = subparsers.add_parser(
        "export-utterances",
        help="Export persisted streamer utterances to CSV.",
    )
    export_utterances_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    export_utterances_parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output CSV path.",
    )

    export_history_parser = subparsers.add_parser(
        "export-history",
        help="Export persisted historical Dota matches to CSV.",
    )
    export_history_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    export_history_parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output CSV path.",
    )

    import_settlements_parser = subparsers.add_parser(
        "import-settlements",
        help="Import paper bet settlements from CSV.",
    )
    import_settlements_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    import_settlements_parser.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="Settlement CSV path.",
    )

    inspect_dataset_parser = subparsers.add_parser(
        "inspect-dataset",
        help="Show offline data readiness for ML training and evaluation.",
    )
    inspect_dataset_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )

    open_bets_parser = subparsers.add_parser(
        "open-bets",
        help="List unsettled paper bets.",
    )
    open_bets_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    open_bets_parser.add_argument("--session-id")
    open_bets_parser.add_argument("--limit", type=_positive_int, default=20)

    settle_bet_parser = subparsers.add_parser(
        "settle-bet",
        help="Manually settle a paper bet.",
    )
    settle_bet_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    settle_bet_parser.add_argument("--bet-id", required=True)
    settle_bet_parser.add_argument(
        "--result",
        choices=("win", "loss", "push", "void"),
        required=True,
    )

    train_ml_parser = subparsers.add_parser(
        "train-ml",
        help="Train the optional ML scoring model from settled paper bets.",
    )
    train_ml_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    train_ml_parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("data") / "models" / "bet_model.joblib",
        help="Path where the trained model is saved.",
    )
    train_ml_parser.add_argument("--min-rows", type=_positive_int, default=30)

    ml_status_parser = subparsers.add_parser(
        "ml-status",
        help="Show ML training data readiness.",
    )
    ml_status_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    ml_status_parser.add_argument("--min-rows", type=_positive_int, default=30)

    train_historical_ml_parser = subparsers.add_parser(
        "train-historical-ml",
        help="Train Historical ML v2 match winner model from historical matches.",
    )
    train_historical_ml_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    train_historical_ml_parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("data") / "models" / "historical_match_win.joblib",
        help="Path where the Historical ML v2 artifact is saved.",
    )
    train_historical_ml_parser.add_argument(
        "--decay-days",
        type=_positive_float,
        default=90.0,
        help="Recency exponential decay baseline in days.",
    )

    historical_ml_status_parser = subparsers.add_parser(
        "historical-ml-status",
        help="Show Historical ML v2 data and artifact readiness.",
    )
    historical_ml_status_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    historical_ml_status_parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("data") / "models" / "historical_match_win.joblib",
        help="Historical ML v2 model artifact path.",
    )
    historical_ml_status_parser.add_argument(
        "--decay-days",
        type=_positive_float,
        default=90.0,
        help="Recency exponential decay baseline in days.",
    )

    evaluate_historical_ml_parser = subparsers.add_parser(
        "evaluate-historical-ml",
        help="Evaluate an existing Historical ML v2 model on current data.",
    )
    evaluate_historical_ml_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    evaluate_historical_ml_parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("data") / "models" / "historical_match_win.joblib",
        help="Historical ML v2 model artifact path.",
    )
    evaluate_historical_ml_parser.add_argument(
        "--decay-days",
        type=_positive_float,
        default=90.0,
        help="Recency exponential decay baseline in days.",
    )

    diagnose_historical_ml_parser = subparsers.add_parser(
        "diagnose-historical-ml",
        help="Run read-only Historical ML baseline and CatBoost diagnostics.",
    )
    diagnose_historical_ml_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    diagnose_historical_ml_parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("data") / "models" / "historical_match_win.joblib",
        help="Existing logistic artifact path. It is not overwritten.",
    )

    draft_ml_status_parser = subparsers.add_parser(
        "draft-ml-status",
        help="Show POST_DRAFT map ML readiness and artifact status.",
    )
    draft_ml_status_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    draft_ml_status_parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("data") / "models" / "historical_draft_map_win_catboost.joblib",
        help="POST_DRAFT map model artifact path.",
    )

    train_draft_ml_parser = subparsers.add_parser(
        "train-draft-ml",
        help="Train only the POST_DRAFT map CatBoost model.",
    )
    train_draft_ml_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    train_draft_ml_parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("data") / "models" / "historical_draft_map_win_catboost.joblib",
        help="Path where the POST_DRAFT map model artifact is saved.",
    )

    evaluate_draft_ml_parser = subparsers.add_parser(
        "evaluate-draft-ml",
        help="Evaluate an existing POST_DRAFT map model without retraining.",
    )
    evaluate_draft_ml_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    evaluate_draft_ml_parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("data") / "models" / "historical_draft_map_win_catboost.joblib",
        help="POST_DRAFT map model artifact path.",
    )

    evaluate_ml_parser = subparsers.add_parser(
        "evaluate-ml",
        help="Backtest ML scoring against rule-based scoring on settled paper bets.",
    )
    evaluate_ml_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    evaluate_ml_parser.add_argument(
        "--test-size",
        type=_test_size,
        default=0.3,
        help="Share of usable records reserved for evaluation.",
    )
    evaluate_ml_parser.add_argument(
        "--min-records",
        type=_positive_int,
        default=10,
        help="Minimum usable settled win/loss records required.",
    )
    evaluate_ml_parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic train/test split seed.",
    )

    analyze_edge_parser = subparsers.add_parser(
        "analyze-edge",
        help="Inspect market probability and estimated edge from stored data.",
    )
    analyze_edge_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    analyze_edge_parser.add_argument(
        "--model-path",
        "--model",
        dest="model_path",
        type=Path,
        default=Path("data") / "models" / "bet_model.joblib",
        help="Optional ML model path.",
    )
    analyze_edge_parser.add_argument("--match-id")
    analyze_edge_parser.add_argument("--bookmaker")
    analyze_edge_parser.add_argument(
        "--min-edge",
        type=_float_value,
        help="Minimum decimal probability edge to display, for example 0.05.",
    )
    analyze_edge_parser.add_argument(
        "--limit",
        type=_positive_int,
        default=20,
        help="Maximum analyses to print.",
    )

    show_transcript_parser = subparsers.add_parser(
        "show-transcript",
        help="Show recent transcriptions from transcript file.",
    )
    show_transcript_parser.add_argument(
        "--transcript",
        type=Path,
        default=Path("data") / "streamer_transcript.txt",
        help="Path to a manual streamer transcript file.",
    )
    show_transcript_parser.add_argument(
        "--last",
        type=_positive_int,
        default=10,
        help="Number of recent utterances to show.",
    )

    add_utterance_parser = subparsers.add_parser(
        "add-utterance",
        help="Add one utterance to a transcript file.",
    )
    add_utterance_parser.add_argument(
        "--transcript",
        type=Path,
        default=Path("data") / "streamer_transcript.txt",
        help="Path to a manual streamer transcript file.",
    )
    add_utterance_parser.add_argument(
        "--text",
        type=_non_empty_text,
        required=True,
        help="Utterance text to append.",
    )
    add_utterance_parser.add_argument(
        "--speaker",
        type=_non_empty_text,
        help="Optional speaker label to prefix the utterance.",
    )

    clear_transcript_parser = subparsers.add_parser(
        "clear-transcript",
        help="Clear a transcript file.",
    )
    clear_transcript_parser.add_argument(
        "--transcript",
        type=Path,
        default=Path("data") / "streamer_transcript.txt",
        help="Path to a manual streamer transcript file.",
    )

    list_sessions_parser = subparsers.add_parser(
        "list-sessions",
        help="List stored paper trading sessions.",
    )
    list_sessions_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )

    show_session_parser = subparsers.add_parser(
        "show-session",
        help="Show one stored paper trading session.",
    )
    show_session_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    show_session_parser.add_argument("--session-id", required=True)

    show_last_bets_parser = subparsers.add_parser(
        "show-last-bets",
        help="Show recent paper bets.",
    )
    show_last_bets_parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    show_last_bets_parser.add_argument("--session-id")
    show_last_bets_parser.add_argument(
        "--limit",
        type=_positive_int,
        default=10,
        help="Number of recent bets to show.",
    )

    return parser


def main(
    argv: Sequence[str] | None = None,
    sleep_func: Callable[[float], None] = time.sleep,
) -> int:
    parser = create_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 1

    try:
        if args.command == "run-once":
            return _run_once_command(args)
        if args.command == "loop":
            return _loop_command(args, sleep_func)
        if args.command == "report":
            return _report_command(args)
        if args.command == "fetch-matches":
            return _fetch_matches_command(args)
        if args.command == "sync-history":
            return _sync_history_command(args)
        if args.command == "sync-drafts":
            return _sync_drafts_command(args)
        if args.command == "probe-stratz-history":
            return _probe_stratz_history_command(args)
        if args.command == "probe-public-match-pages":
            return _probe_public_match_pages_command(args)
        if args.command == "stratz-trajectory-audit":
            return _stratz_trajectory_audit_command(args)
        if args.command == "sync-rosters":
            return _sync_rosters_command(args)
        if args.command == "history-status":
            return _history_status_command(args)
        if args.command == "draft-history-status":
            return _draft_history_status_command(args)
        if args.command == "roster-status":
            return _roster_status_command(args)
        if args.command == "lineage-status":
            return _lineage_status_command(args)
        if args.command == "feature-status":
            return _feature_status_command(args)
        if args.command == "ewc-status":
            return _ewc_status_command(args)
        if args.command == "fetch-odds":
            return _fetch_odds_command(args)
        if args.command == "export-bets":
            return _export_bets_command(args)
        if args.command == "export-candidates":
            return _export_candidates_command(args)
        if args.command == "export-utterances":
            return _export_utterances_command(args)
        if args.command == "export-history":
            return _export_history_command(args)
        if args.command == "import-settlements":
            return _import_settlements_command(args)
        if args.command == "inspect-dataset":
            return _inspect_dataset_command(args)
        if args.command == "open-bets":
            return _open_bets_command(args)
        if args.command == "settle-bet":
            return _settle_bet_command(args)
        if args.command == "train-ml":
            return _train_ml_command(args)
        if args.command == "ml-status":
            return _ml_status_command(args)
        if args.command == "train-historical-ml":
            return _train_historical_ml_command(args)
        if args.command == "historical-ml-status":
            return _historical_ml_status_command(args)
        if args.command == "evaluate-historical-ml":
            return _evaluate_historical_ml_command(args)
        if args.command == "diagnose-historical-ml":
            return _diagnose_historical_ml_command(args)
        if args.command == "draft-ml-status":
            return _draft_ml_status_command(args)
        if args.command == "train-draft-ml":
            return _train_draft_ml_command(args)
        if args.command == "evaluate-draft-ml":
            return _evaluate_draft_ml_command(args)
        if args.command == "evaluate-ml":
            return _evaluate_ml_command(args)
        if args.command == "analyze-edge":
            return _analyze_edge_command(args)
        if args.command == "show-transcript":
            return _show_transcript_command(args)
        if args.command == "add-utterance":
            return _add_utterance_command(args)
        if args.command == "clear-transcript":
            return _clear_transcript_command(args)
        if args.command == "list-sessions":
            return _list_sessions_command(args)
        if args.command == "show-session":
            return _show_session_command(args)
        if args.command == "show-last-bets":
            return _show_last_bets_command(args)
    except (NotImplementedError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    parser.print_help()
    return 1


def _add_common_run_options(parser: ArgumentParser) -> None:
    parser.add_argument("--tournament", required=True)
    parser.add_argument(
        "--transcript",
        type=Path,
        required=True,
        help="Path to a manual streamer transcript file.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data") / "autopilot.db",
        help="SQLite database path.",
    )
    parser.add_argument(
        "--mode",
        choices=EXECUTION_MODE_CHOICES,
        default="paper",
    )
    parser.add_argument(
        "--target-bets-per-match",
        type=float,
        default=1.2,
    )
    parser.add_argument(
        "--max-bets-per-match",
        type=_positive_int,
        default=3,
    )
    parser.add_argument(
        "--score-threshold",
        type=float,
        default=62.0,
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("data") / "models" / "bet_model.joblib",
        help="Optional ML model path.",
    )
    parser.add_argument(
        "--ml-weight",
        type=float,
        default=0.5,
        help="Weight of ML score in the hybrid score.",
    )
    parser.add_argument(
        "--use-ml",
        action="store_true",
        help="Use optional ML scoring if a trained model exists.",
    )


def _run_once_command(args: Namespace) -> int:
    return _run_command(args, iterations=1, interval_seconds=1, sleep_func=time.sleep)


def _loop_command(
    args: Namespace,
    sleep_func: Callable[[float], None],
) -> int:
    return _run_command(
        args,
        iterations=args.iterations,
        interval_seconds=args.interval_seconds,
        sleep_func=sleep_func,
    )


def _report_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run app.main or app.cli run-once first."
        )
        return 1

    repository = SQLiteRepository(db_path)
    report = build_report_from_repository(repository, args.session_id)
    recent_bets = _recent_bets_for_report(
        repository,
        session_id=args.session_id,
        limit=args.last_bets,
    )

    print(f"Database: {db_path.as_posix()}")
    print(f"Sessions: {report.total_sessions}")
    print(f"Bets: {report.total_bets}")
    print(f"Open bets: {report.open_bets}")
    print(f"Settled bets: {report.settled_bets}")
    print(f"Wins: {report.wins}")
    print(f"Losses: {report.losses}")
    print(f"Pushes: {report.pushes}")
    print(f"Voids: {report.voids}")
    print(f"Profit units: {report.profit_units:.2f}")
    print(f"Total staked units: {report.total_staked_units:.2f}")
    print(f"ROI: {report.roi_pct:.2f}%")
    print(f"Average bets per match: {report.average_bets_per_match:.2f}")
    _print_recent_bets(recent_bets)

    if args.show_utterances:
        recent_utterances = _recent_utterances_for_report(
            repository,
            session_id=args.session_id,
            limit=args.last_utterances,
        )
        _print_recent_utterances(recent_utterances)

    return 0


def _fetch_matches_command(args: Namespace) -> int:
    from app.collectors import PandaScoreError, PandaScoreMatchCollector

    if args.provider != "pandascore":
        print(f"Unsupported provider: {args.provider}")
        return 1

    collector = PandaScoreMatchCollector(
        timeout=args.timeout,
        limit=args.limit,
        status_filter=args.status,
    )
    try:
        matches = collector.collect()
    except PandaScoreError as exc:
        print(str(exc))
        return 1
    if args.scope == "ewc-2026":
        from app.tournaments import belongs_to_ewc_2026

        matches = [match for match in matches if belongs_to_ewc_2026(match)]

    print("Provider: pandascore")
    if args.scope is not None:
        print(f"Scope: {args.scope}")
    print(f"Matches: {len(matches)}")
    if not matches:
        print()
        print("No matches found.")
        return 0

    for index, match in enumerate(matches, start=1):
        print()
        print(f"{index}. {match.team_a} vs {match.team_b}")
        print(f"   Tournament: {match.tournament_name}")
        print(f"   Status: {match.status}")
        print(f"   Starts at: {_format_optional_datetime(match.start_time)}")
    return 0


def _sync_history_command(args: Namespace) -> int:
    import app.history as history

    if args.provider != "pandascore":
        print(f"Unsupported provider: {args.provider}")
        return 1
    if args.since > args.until:
        print("--since must be before or equal to --until.")
        return 1

    repository = SQLiteRepository(args.db)
    collector = history.PandaScoreHistoricalMatchCollector(timeout=args.timeout)
    try:
        result = history.sync_historical_matches(
            repository=repository,
            collector=collector,
            since=args.since,
            until=args.until,
            page_size=args.page_size,
            max_pages=args.max_pages,
        )
    except history.PandaScoreError as exc:
        print(str(exc))
        return 1

    print("Historical Dota sync")
    print()
    print("Provider: pandascore")
    print(f"Since: {_format_history_date(args.since)}")
    print(f"Until: {_format_history_date(args.until)}")
    if args.max_pages is None:
        print("Max pages: provider completion")
    else:
        print(f"Max pages: {args.max_pages}")
    print()
    print(f"Fetched provider rows: {result.fetched_rows}")
    print(f"Mapped historical matches: {result.mapped_matches}")
    print(f"Usable winner records: {result.usable_matches}")
    print(f"Skipped malformed/unresolved: {result.skipped}")
    print()
    print(f"Inserted: {result.inserted}")
    print(f"Updated: {result.updated}")
    print(f"Unchanged: {result.unchanged}")
    if result.warnings:
        print()
        print(f"Warnings: {len(result.warnings)}")
        for warning in result.warnings[:10]:
            print(f"Warning: {warning}")
    return 0


def _sync_drafts_command(args: Namespace) -> int:
    import app.draft_history as draft_history

    if args.provider == "stratz-public":
        import app.public_pages as public_pages

        manifest_name = getattr(args, "manifest", None)
        explicit_match_ids = tuple(
            str(match_id).strip()
            for match_id in args.match_id
            if str(match_id).strip()
        )
        if manifest_name and explicit_match_ids:
            print("Use either --manifest or --match-id for stratz-public, not both.")
            return 1
        manifest = None
        if manifest_name:
            try:
                manifest = public_pages.get_stratz_public_backfill_manifest(
                    manifest_name
                )
            except ValueError as exc:
                print(str(exc))
                return 1
            match_ids = manifest.match_ids
        else:
            match_ids = explicit_match_ids

        if not match_ids:
            print("At least one --match-id or --manifest is required for stratz-public.")
            return 1
        repository = SQLiteRepository(args.db)
        client = public_pages.PublicPageHttpClient(timeout=args.timeout)
        try:
            stratz_result = public_pages.sync_stratz_public_match_pages(
                repository=repository,
                match_ids=match_ids,
                client=client,
                delay_seconds=args.delay_seconds,
                max_retries=args.max_retries,
                retry_backoff_seconds=args.retry_backoff_seconds,
                fetch_referenced_resources=not args.skip_referenced_resources,
            )
        except ValueError as exc:
            print(str(exc))
            return 1

        if manifest is not None:
            print(public_pages.render_stratz_public_backfill_manifest(manifest))
            print()
        print(public_pages.render_stratz_public_sync_result(stratz_result))
        return 0

    if args.provider != "opendota":
        print(f"Unsupported provider: {args.provider}")
        return 1
    if args.since is None or args.until is None:
        print("--since and --until are required for provider opendota.")
        return 1
    if args.since > args.until:
        print("--since must be before or equal to --until.")
        return 1

    repository = SQLiteRepository(args.db)
    collector = draft_history.OpenDotaDraftCollector(timeout=args.timeout)
    try:
        result = draft_history.sync_draft_history(
            repository=repository,
            collector=collector,
            since=args.since,
            until=args.until,
            page_size=args.page_size,
            max_pages=args.max_pages,
        )
    except (
        draft_history.OpenDotaRequestError,
        draft_history.OpenDotaResponseError,
        ValueError,
    ) as exc:
        print(str(exc))
        return 1

    print("Historical Dota draft sync")
    print()
    print("Provider: opendota")
    print(f"Since: {_format_history_date(args.since)}")
    print(f"Until: {_format_history_date(args.until)}")
    print("Schema source: OpenDota structured API /matches/{match_id}")
    print("PandaScore draft data: insufficient in current match mapping")
    if args.max_pages is None:
        print("Max pages: provider completion")
    else:
        print(f"Max pages: {args.max_pages}")
    print()
    print(f"Fetched provider rows: {result.fetched_rows}")
    print(f"Mapped historical games: {result.mapped_games}")
    print(f"Skipped malformed/incomplete: {result.skipped}")
    print()
    print(f"Inserted: {result.inserted}")
    print(f"Updated: {result.updated}")
    print(f"Unchanged: {result.unchanged}")
    if result.warnings:
        print()
        print(f"Warnings: {len(result.warnings)}")
        for warning in result.warnings[:10]:
            print(f"Warning: {warning}")
    return 0


def _probe_stratz_history_command(args: Namespace) -> int:
    import app.stratz as stratz

    client = stratz.StratzGraphQLClient(timeout=args.timeout)
    probe = stratz.StratzFeasibilityProbe(client)
    try:
        result = probe.run(
            sample_size=args.sample_size,
            match_ids=tuple(args.match_id),
            delay_seconds=args.delay_seconds,
            real_source=True,
        )
    except (
        stratz.StratzConfigurationError,
        stratz.StratzRequestError,
        stratz.StratzResponseError,
        ValueError,
    ) as exc:
        print(str(exc))
        return 1

    print(stratz.render_probe_result(result))
    return 0


def _probe_public_match_pages_command(args: Namespace) -> int:
    import app.public_pages as public_pages

    client = public_pages.PublicPageHttpClient(timeout=args.timeout)
    probe = public_pages.PublicMatchPageProbe(client)
    try:
        result = probe.run(
            source=public_pages.PublicPageSource(args.source),
            match_ids=tuple(args.match_id),
            page_urls=tuple(args.page_url),
            delay_seconds=args.delay_seconds,
            fetch_referenced_resources=not args.skip_referenced_resources,
        )
    except ValueError as exc:
        print(str(exc))
        return 1

    print(public_pages.render_public_page_probe_result(result))
    return 0


def _stratz_trajectory_audit_command(args: Namespace) -> int:
    import app.public_pages as public_pages

    db_path = Path(args.db)
    print(f"Database: {db_path.as_posix()}")
    print()

    if not db_path.exists():
        audit = public_pages.build_stratz_public_trajectory_corpus_audit_from_records(
            games=(),
            players_by_game={},
            points_by_game={},
        )
        print(public_pages.render_stratz_public_trajectory_corpus_audit(audit))
        print()
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run app.main or app.cli run-once first."
        )
        return 0

    repository = SQLiteRepository(db_path)
    audit = public_pages.build_stratz_public_trajectory_corpus_audit(repository)
    print(public_pages.render_stratz_public_trajectory_corpus_audit(audit))
    return 0


def _sync_rosters_command(args: Namespace) -> int:
    import app.history as history

    if args.provider != "pandascore":
        print(f"Unsupported provider: {args.provider}")
        return 1

    repository = SQLiteRepository(args.db)
    collector = history.PandaScoreRosterCollector(timeout=args.timeout)
    try:
        result = history.sync_roster_history(
            repository=repository,
            collector=collector,
            max_tournaments=args.max_tournaments,
        )
    except history.PandaScoreError as exc:
        print(str(exc))
        return 1

    print("Roster history sync")
    print()
    print("Provider: pandascore")
    print(f"Max tournaments: {args.max_tournaments}")
    print()
    print(f"Tournaments requested: {result.tournaments_requested}")
    print(f"Rosters fetched: {result.rosters_fetched}")
    print(f"Unique players seen: {result.unique_players_seen}")
    print(f"Unique organizations seen: {result.unique_organizations_seen}")
    print(f"Skipped incomplete records: {result.skipped_records}")
    print()
    print(f"Snapshots inserted: {result.snapshots_inserted}")
    print(f"Snapshots updated: {result.snapshots_updated}")
    print(f"Snapshots unchanged: {result.snapshots_unchanged}")
    if result.warnings:
        print()
        print(f"Warnings: {len(result.warnings)}")
        for warning in result.warnings[:10]:
            print(f"Warning: {warning}")
    return 0


def _history_status_command(args: Namespace) -> int:
    import app.history as history

    db_path = Path(args.db)
    print("Historical Dota dataset")
    print(f"Database: {db_path.as_posix()}")

    if not db_path.exists():
        _print_empty_history_status()
        return 0
    if not _sqlite_table_exists(db_path, "historical_matches"):
        _print_empty_history_status()
        return 0

    repository = SQLiteRepository(db_path)
    status = history.build_historical_status(repository)

    print(f"Historical matches: {status.total_matches}")
    print(f"Usable winner records: {status.usable_winner_records}")
    print(f"Point-in-time ready matches: {status.point_in_time_ready_matches}")
    print(
        "Started range: "
        f"{_format_datetime_range(status.started_at_min, status.started_at_max)}"
    )
    print(
        "Completed range: "
        f"{_format_datetime_range(status.completed_at_min, status.completed_at_max)}"
    )
    print(f"Unique teams: {status.unique_teams}")
    print(f"Unique tournaments: {status.unique_tournaments}")
    print("Competitive stages:")
    for stage, count in status.stage_counts.items():
        print(f"  {stage.value}: {count}")

    if status.total_matches == 0:
        print()
        print("No historical matches found.")
    return 0


def _draft_history_status_command(args: Namespace) -> int:
    import app.draft_history as draft_history

    db_path = Path(args.db)
    provider = "stratz_public" if args.provider == "stratz-public" else args.provider
    print("Historical Dota draft dataset")
    print(f"Database: {db_path.as_posix()}")
    print(f"Provider: {args.provider}")
    print("PandaScore draft data: insufficient in current match fixture mapping")
    print("Fallback draft provider: OpenDota structured API")
    print("Patch provenance: trusted provider patch only; no date inference")

    if not db_path.exists():
        _print_empty_draft_history_status()
        return 0
    if not _sqlite_table_exists(db_path, "historical_dota_games"):
        _print_empty_draft_history_status()
        return 0

    repository = SQLiteRepository(db_path)
    status = draft_history.build_draft_history_status(
        repository,
        provider=provider,
    )
    print(f"Historical games: {status.historical_games}")
    print(f"Games with usable winner: {status.games_with_usable_winner}")
    print(
        "Games with complete 5v5 picks: "
        f"{status.games_with_complete_5v5_picks}"
    )
    print(f"Games with bans: {status.games_with_bans}")
    print(
        "Games with ordered draft actions: "
        f"{status.games_with_ordered_draft_actions}"
    )
    print(
        "Games with patch/version provenance: "
        f"{status.games_with_patch_provenance}"
    )
    print(f"Unique heroes: {status.unique_heroes}")
    print(
        "Source-link coverage: "
        f"{status.linked_games}/{status.historical_games} "
        f"({status.source_link_coverage:.1%})"
    )
    print(
        "Scope-eligible post-draft target games: "
        f"{status.scope_eligible_post_draft_target_games}"
    )
    print(
        "Started range: "
        f"{_format_datetime_range(status.started_at_min, status.started_at_max)}"
    )
    print(
        "Completed range: "
        f"{_format_datetime_range(status.completed_at_min, status.completed_at_max)}"
    )
    if status.historical_games == 0:
        print()
        print("No historical draft games found.")
    return 0


def _roster_status_command(args: Namespace) -> int:
    import app.history as history

    db_path = Path(args.db)
    print("Roster history dataset")
    print(f"Database: {db_path.as_posix()}")

    if not db_path.exists():
        _print_empty_roster_status()
        return 0
    if not _sqlite_table_exists(db_path, "roster_snapshots"):
        _print_empty_roster_status()
        return 0

    repository = SQLiteRepository(db_path)
    status = history.build_roster_history_status(repository)

    print(f"Players: {status.players}")
    print(f"Organizations: {status.organizations}")
    print(f"Roster snapshots: {status.roster_snapshots}")
    print(f"Player memberships: {status.player_memberships}")
    print(f"Coach memberships: {status.coach_memberships}")
    print(
        "Snapshots with temporal validity: "
        f"{status.snapshots_with_temporal_validity}"
    )
    print(
        "Snapshots without explicit validity: "
        f"{status.snapshots_without_explicit_validity}"
    )
    print(
        "Observed range: "
        f"{_format_datetime_range(status.observed_at_min, status.observed_at_max)}"
    )
    print(
        "Unique player-roster fingerprints: "
        f"{status.unique_player_roster_fingerprints}"
    )

    if status.roster_snapshots == 0:
        print()
        print("No roster snapshots found.")
    return 0


def _lineage_status_command(args: Namespace) -> int:
    import app.history as history

    db_path = Path(args.db)
    as_of = args.as_of or datetime.now(timezone.utc)
    print("Roster lineage status")
    print(f"Database: {db_path.as_posix()}")
    print(f"As of: {as_of.isoformat()}")

    if not db_path.exists():
        _print_empty_lineage_status()
        return 0
    if not _sqlite_table_exists(db_path, "roster_snapshots"):
        _print_empty_lineage_status()
        return 0

    repository = SQLiteRepository(db_path)
    status = history.build_roster_lineage_status(repository, as_of=as_of)
    _print_lineage_status(status)

    if status.available_roster_snapshots == 0:
        print()
        print("No roster snapshots found.")
    return 0


def _feature_status_command(args: Namespace) -> int:
    import app.history as history

    db_path = Path(args.db)
    as_of = args.as_of or datetime.now(timezone.utc)
    policy = history.HistoricalFeaturePolicy(
        recency=history.RecencyWeightingPolicy(decay_days=args.decay_days)
    )
    print("Historical feature status")
    print(f"Database: {db_path.as_posix()}")
    print(f"As of: {as_of.isoformat()}")
    print(f"Decay days: {policy.recency.decay_days:g}")

    if not db_path.exists():
        _print_empty_feature_status(policy)
        return 0
    if not _sqlite_table_exists(db_path, "historical_matches"):
        _print_empty_feature_status(policy)
        return 0

    repository = SQLiteRepository(db_path)
    status = history.build_historical_feature_status(
        repository,
        as_of=as_of,
        policy=policy,
    )
    _print_feature_status(status)

    if status.historical_matches_available == 0:
        print()
        print("No point-in-time historical matches available.")
    return 0


def _print_empty_history_status() -> None:
    from app.tournaments import CompetitiveStage

    print("Historical matches: 0")
    print("Usable winner records: 0")
    print("Point-in-time ready matches: 0")
    print("Started range: -")
    print("Completed range: -")
    print("Unique teams: 0")
    print("Unique tournaments: 0")
    print("Competitive stages:")
    for stage in CompetitiveStage:
        print(f"  {stage.value}: 0")
    print()
    print("No historical matches found.")


def _print_empty_draft_history_status() -> None:
    print("Historical games: 0")
    print("Games with usable winner: 0")
    print("Games with complete 5v5 picks: 0")
    print("Games with bans: 0")
    print("Games with ordered draft actions: 0")
    print("Games with patch/version provenance: 0")
    print("Unique heroes: 0")
    print("Source-link coverage: 0/0 (0.0%)")
    print("Scope-eligible post-draft target games: 0")
    print("Started range: -")
    print("Completed range: -")
    print()
    print("No historical draft games found.")


def _print_empty_roster_status() -> None:
    print("Players: 0")
    print("Organizations: 0")
    print("Roster snapshots: 0")
    print("Player memberships: 0")
    print("Coach memberships: 0")
    print("Snapshots with temporal validity: 0")
    print("Snapshots without explicit validity: 0")
    print("Observed range: -")
    print("Unique player-roster fingerprints: 0")
    print()
    print("No roster snapshots found.")


def _print_empty_feature_status(policy: object) -> None:
    import app.history as history

    feature_policy = cast(history.HistoricalFeaturePolicy, policy)
    print("Historical matches available: 0")
    print("Usable match-result records: 0")
    print("Stable teams in strength state: 0")
    print("Teams with no history: 0")
    print("Average raw history matches per team: 0.00")
    print("Average recency weighted history mass: 0.000")
    print("Opponent-adjusted strength range: -")
    print(
        "Cold-start policy: "
        f"raw_win_rate={feature_policy.neutral_win_rate:.2f}, "
        "recency_weighted_win_rate="
        f"{feature_policy.neutral_win_rate:.2f}, "
        "opponent_adjusted_strength="
        f"{feature_policy.neutral_strength:.3f}"
    )
    print()
    print("No point-in-time historical matches available.")


def _print_feature_status(status: object) -> None:
    import app.history as history

    feature_status = cast(history.HistoricalFeatureStatus, status)
    print(
        "Historical matches available: "
        f"{feature_status.historical_matches_available}"
    )
    print(
        "Usable match-result records: "
        f"{feature_status.usable_match_result_records}"
    )
    print(
        "Stable teams in strength state: "
        f"{feature_status.stable_teams_in_strength_state}"
    )
    print(f"Teams with no history: {feature_status.teams_with_no_history}")
    print(
        "Average raw history matches per team: "
        f"{feature_status.average_raw_history_matches_per_team:.2f}"
    )
    print(
        "Average recency weighted history mass: "
        f"{feature_status.average_recency_weighted_history_mass:.3f}"
    )
    print(
        "Opponent-adjusted strength range: "
        f"{_format_strength_range(feature_status)}"
    )
    print(
        "Cold-start policy: "
        f"raw_win_rate={feature_status.neutral_raw_win_rate:.2f}, "
        "recency_weighted_win_rate="
        f"{feature_status.neutral_recency_weighted_win_rate:.2f}, "
        "opponent_adjusted_strength="
        f"{feature_status.neutral_opponent_adjusted_strength:.3f}"
    )


def _format_strength_range(status: object) -> str:
    import app.history as history

    feature_status = cast(history.HistoricalFeatureStatus, status)
    min_strength = feature_status.min_opponent_adjusted_strength
    max_strength = feature_status.max_opponent_adjusted_strength
    if min_strength is None or max_strength is None:
        return "-"
    return f"{min_strength:.3f} to {max_strength:.3f}"


def _print_empty_lineage_status() -> None:
    import app.history as history

    print("Point-in-time available roster snapshots: 0")
    print("Chronology sources:")
    for source in history.RosterChronologySource:
        print(f"  {source.value}: 0")
    print("Exact continuity links: 0")
    print("Strong continuity links: 0")
    print("Coach-supported continuity links: 0")
    print("Ambiguous predecessor resolutions: 0")
    print("Unlinked/root snapshots: 0")
    print("Derived lineage components: 0")
    print("Cross-organization accepted links: 0")
    print("Same-organization accepted links: 0")
    print("Largest predecessor chain size: 0")
    print()
    print("No roster snapshots found.")


def _print_lineage_status(status: object) -> None:
    import app.history as history

    lineage_status = cast(history.RosterLineageStatus, status)
    print(
        "Point-in-time available roster snapshots: "
        f"{lineage_status.available_roster_snapshots}"
    )
    print("Chronology sources:")
    for source in history.RosterChronologySource:
        count = lineage_status.chronology_source_counts.get(source, 0)
        print(f"  {source.value}: {count}")
    print(f"Exact continuity links: {lineage_status.exact_continuity_links}")
    print(f"Strong continuity links: {lineage_status.strong_continuity_links}")
    print(
        "Coach-supported continuity links: "
        f"{lineage_status.coach_supported_continuity_links}"
    )
    print(
        "Ambiguous predecessor resolutions: "
        f"{lineage_status.ambiguous_predecessor_resolutions}"
    )
    print(f"Unlinked/root snapshots: {lineage_status.unlinked_root_snapshots}")
    print(
        "Derived lineage components: "
        f"{lineage_status.derived_lineage_components}"
    )
    print(
        "Cross-organization accepted links: "
        f"{lineage_status.cross_organization_accepted_links}"
    )
    print(
        "Same-organization accepted links: "
        f"{lineage_status.same_organization_accepted_links}"
    )
    print(
        "Largest predecessor chain size: "
        f"{lineage_status.largest_predecessor_chain_size}"
    )


def _sqlite_table_exists(db_path: Path, table_name: str) -> bool:
    from contextlib import closing

    from app.storage import get_connection

    with closing(get_connection(db_path)) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = ?
            """,
            (table_name,),
        ).fetchone()
    return row is not None


def _ewc_status_command(args: Namespace) -> int:
    from app.tournaments import (
        EWC_2026_DOTA,
        CompetitiveStage,
        belongs_to_ewc_2026,
        stage_for_ewc_2026_match,
    )

    db_path = Path(args.db)
    print("EWC 2026 Dota scope")
    print(f"Tournament id: {EWC_2026_DOTA.id}")
    print(f"Tournament name: {EWC_2026_DOTA.canonical_name}")
    print(f"Database: {db_path.as_posix()}")

    if not db_path.exists():
        _print_ewc_status_counts(
            scoped_matches=0,
            stage_counts={stage: 0 for stage in CompetitiveStage},
            upcoming=0,
            live=0,
            completed=0,
        )
        print()
        print("No persisted EWC 2026 Dota matches found.")
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run app.main or app.cli run-once first."
        )
        return 0

    repository = SQLiteRepository(db_path)
    matches = [
        match for match in repository.list_matches() if belongs_to_ewc_2026(match)
    ]
    stage_counts = {stage: 0 for stage in CompetitiveStage}
    for match in matches:
        stage = stage_for_ewc_2026_match(match).competitive_stage
        stage_counts[stage] += 1

    _print_ewc_status_counts(
        scoped_matches=len(matches),
        stage_counts=stage_counts,
        upcoming=sum(1 for match in matches if match.status == "upcoming"),
        live=sum(1 for match in matches if match.status == "live"),
        completed=sum(1 for match in matches if match.status == "finished"),
    )
    if not matches:
        print()
        print("No persisted EWC 2026 Dota matches found.")
    return 0


def _print_ewc_status_counts(
    *,
    scoped_matches: int,
    stage_counts: dict[Any, int],
    upcoming: int,
    live: int,
    completed: int,
) -> None:
    print()
    print(f"Scoped matches: {scoped_matches}")
    print("Competitive stages:")
    for stage in stage_counts:
        print(f"  {stage.value}: {stage_counts[stage]}")
    print()
    print(f"Upcoming: {upcoming}")
    print(f"Live: {live}")
    print(f"Completed: {completed}")


def _fetch_odds_command(args: Namespace) -> int:
    from app.collectors import OddsPapiError, OddsPapiOddsCollector

    if args.provider != "oddspapi":
        print(f"Unsupported provider: {args.provider}")
        return 1

    collector = OddsPapiOddsCollector(
        timeout=args.timeout,
        limit=args.limit,
        bookmakers=_bookmaker_filter(args.bookmakers),
    )
    try:
        fixture_odds = collector.collect()
    except OddsPapiError as exc:
        print(str(exc))
        return 1

    print("Provider: oddspapi")
    print(f"Dota 2 fixtures with odds: {len(fixture_odds)}")
    if not fixture_odds:
        print()
        print("No Dota 2 odds found.")
        return 0

    for index, item in enumerate(fixture_odds, start=1):
        print()
        print(f"{index}. {item.fixture.team_a} vs {item.fixture.team_b}")
        print(f"   Fixture: {item.fixture.id}")
        print(f"   Starts at: {_format_optional_datetime(item.fixture.start_time)}")
        for bookmaker, snapshots in _odds_by_bookmaker(item.snapshots):
            print(f"   Bookmaker: {bookmaker}")
            print("   Market: map_winner")
            for snapshot in snapshots:
                print(f"   {snapshot.selection}: {snapshot.odds:.2f}")
    return 0


def _export_bets_command(args: Namespace) -> int:
    from app.data_io import export_bets_to_csv

    repository = SQLiteRepository(args.db)
    result = export_bets_to_csv(repository, args.out)
    print(f"Exported {result.row_count} bets to {result.output_path.as_posix()}")
    return 0


def _export_candidates_command(args: Namespace) -> int:
    from app.data_io import export_candidates_to_csv

    repository = SQLiteRepository(args.db)
    result = export_candidates_to_csv(repository, args.out)
    print(
        f"Exported {result.row_count} candidates to "
        f"{result.output_path.as_posix()}"
    )
    return 0


def _export_utterances_command(args: Namespace) -> int:
    from app.data_io import export_utterances_to_csv

    repository = SQLiteRepository(args.db)
    result = export_utterances_to_csv(repository, args.out)
    print(
        f"Exported {result.row_count} utterances to "
        f"{result.output_path.as_posix()}"
    )
    return 0


def _export_history_command(args: Namespace) -> int:
    from app.data_io import export_history_to_csv

    repository = SQLiteRepository(args.db)
    result = export_history_to_csv(repository, args.out)
    print(
        f"Exported {result.row_count} historical matches to "
        f"{result.output_path.as_posix()}"
    )
    return 0


def _import_settlements_command(args: Namespace) -> int:
    from app.data_io import import_settlements_from_csv

    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run app.main or app.cli run-once first."
        )
        return 1

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Settlement CSV not found: {csv_path.as_posix()}")
        return 1

    repository = SQLiteRepository(db_path)
    result = import_settlements_from_csv(repository, csv_path)

    print(f"Processed rows: {result.processed_rows}")
    print(f"Updated bets: {result.updated_bets}")
    print(f"Skipped rows: {result.skipped_rows}")
    print(f"Warnings: {len(result.warnings)}")
    for warning in result.warnings:
        print(f"Warning: {warning}")
    return 0


def _inspect_dataset_command(args: Namespace) -> int:
    from app.data_io import format_dataset_inspection_report, inspect_dataset

    db_path = Path(args.db)
    repository = SQLiteRepository(db_path)
    report = inspect_dataset(repository, db_path)
    print(format_dataset_inspection_report(report))
    return 0


def _open_bets_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run app.main or app.cli run-once first."
        )
        return 1

    repository = SQLiteRepository(db_path)
    if args.session_id is None:
        open_bets = repository.list_open_bets()
    else:
        open_bets = repository.list_open_bets_by_session(args.session_id)
    limited_bets = open_bets[: args.limit]

    print(f"Database: {db_path.as_posix()}")
    print(f"Open bets: {len(open_bets)}")
    for index, bet in enumerate(limited_bets, start=1):
        print(f"{index}. {_format_bet_summary(bet)}")
    return 0


def _settle_bet_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run app.main or app.cli run-once first."
        )
        return 1

    repository = SQLiteRepository(db_path)
    result = cast(BetResult, args.result)
    settled_bet = repository.settle_bet(args.bet_id, result)

    print("Settled bet:")
    print(f"id={settled_bet.id}")
    print(f"market={settled_bet.market}")
    print(f"selection={settled_bet.selection}")
    print(f"line={settled_bet.line}")
    print(f"odds={settled_bet.odds}")
    print(f"stake_pct={settled_bet.stake_pct}")
    print(f"result={settled_bet.result}")
    print(f"profit_units={settled_bet.profit_units:.2f}")
    print(f"status={settled_bet.status}")
    return 0


def _train_ml_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run app.main or app.cli run-once first."
        )
        return 1

    from app.ml import train_model_from_repository

    repository = SQLiteRepository(args.db)
    result = train_model_from_repository(
        repository,
        model_path=args.model_path,
        min_rows=args.min_rows,
    )

    print(f"trained: {result.trained}")
    print(f"rows: {result.rows}")
    print(f"positives: {result.positive_rows}")
    print(f"negatives: {result.negative_rows}")
    print(f"model path: {_format_optional_path(result.model_path)}")
    print(f"message: {result.message}")
    return 0


def _ml_status_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run app.main or app.cli run-once first."
        )
        return 1

    from app.ml import build_ml_training_status

    repository = SQLiteRepository(db_path)
    status = build_ml_training_status(repository, min_rows=args.min_rows)

    print(f"Database: {db_path.as_posix()}")
    print(f"ML training rows: {status.training_rows}")
    print(f"Positive rows: {status.positive_rows}")
    print(f"Negative rows: {status.negative_rows}")
    print(f"Ignored bets: {status.ignored_bets}")
    print(f"Unknown/open bets: {status.unknown_open_bets}")
    print(f"Push/void bets: {status.push_void_bets}")
    print(f"Can train: {'yes' if status.can_train else 'no'}")
    print(f"Reason: {status.reason}")
    return 0


def _train_historical_ml_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run sync-history first."
        )
        return 1

    from app.history import DEFAULT_HISTORICAL_COMPETITION_SCOPE
    from app.historical_ml import train_historical_model_from_repository

    repository = SQLiteRepository(db_path)
    result = train_historical_model_from_repository(
        repository,
        model_path=args.model_path,
        decay_days=args.decay_days,
    )
    print("Historical ML v2 training")
    _print_historical_competition_scope(DEFAULT_HISTORICAL_COMPETITION_SCOPE)
    print(f"usable feature rows: {result.rows}")
    print(f"feature count: {result.feature_count}")
    print(f"decay days: {args.decay_days:g}")
    if result.split is not None:
        print(f"train rows: {result.split.train_rows}")
        print(f"validation rows: {result.split.validation_rows}")
        print(f"test rows: {result.split.test_rows}")
    _print_historical_metrics("train", result.train_metrics)
    _print_historical_metrics("validation", result.validation_metrics)
    _print_historical_metrics("test", result.test_metrics)
    print(f"model artifact path: {_format_optional_path(result.model_path)}")
    print(f"trained: {result.trained}")
    print(f"message: {result.message}")
    return 0 if result.trained else 1


def _historical_ml_status_command(args: Namespace) -> int:
    from app.history import DEFAULT_HISTORICAL_COMPETITION_SCOPE

    print("Historical ML v2 status")
    print(f"Database: {Path(args.db).as_posix()}")
    print(f"Model artifact: {Path(args.model_path).as_posix()}")
    print(f"Decay days: {args.decay_days:g}")
    _print_historical_competition_scope(DEFAULT_HISTORICAL_COMPETITION_SCOPE)

    db_path = Path(args.db)
    if not db_path.exists():
        from app.historical_ml import HISTORICAL_ML_FEATURE_NAMES

        print("Raw historical matches: 0")
        print("Raw usable winner records: 0")
        print("Scope-eligible feature-history matches: 0")
        print("Scope-eligible target matches: 0")
        print("Usable labeled feature rows: 0")
        print(f"Feature count: {len(HISTORICAL_ML_FEATURE_NAMES)}")
        print("Configured minimum rows: 100 total, 60 train, 15 validation, 15 test")
        print("Temporal split ready: no")
        print("Reason: Database not found. Run sync-history first.")
        _print_historical_artifact_status(Path(args.model_path))
        return 0

    from app.historical_ml import build_historical_ml_status

    repository = SQLiteRepository(db_path)
    status = build_historical_ml_status(
        repository,
        model_path=args.model_path,
        decay_days=args.decay_days,
    )
    minimums = status.minimum_rows_policy
    print(f"Raw historical matches: {status.historical_matches}")
    print(f"Raw usable winner records: {status.raw_usable_winner_records}")
    print(
        "Scope-eligible feature-history matches: "
        f"{status.scope_eligible_feature_history_matches}"
    )
    print(f"Scope-eligible target matches: {status.scope_eligible_target_matches}")
    print(f"Usable labeled feature rows: {status.usable_feature_rows}")
    print(f"Feature count: {status.feature_count}")
    print(
        "Configured minimum rows: "
        f"{minimums.minimum_total_rows} total, "
        f"{minimums.minimum_train_rows} train, "
        f"{minimums.minimum_validation_rows} validation, "
        f"{minimums.minimum_test_rows} test"
    )
    print(f"Temporal split ready: {'yes' if status.split_ready else 'no'}")
    print(f"Projected train rows: {status.split.train_rows}")
    print(f"Projected validation rows: {status.split.validation_rows}")
    print(f"Projected test rows: {status.split.test_rows}")
    print(f"Reason: {status.readiness_reason}")
    print(f"Model artifact exists: {'yes' if status.model_artifact_exists else 'no'}")
    print(
        "Artifact compatible: "
        f"{'yes' if status.model_artifact_compatible else 'no'}"
    )
    if status.artifact_incompatibility_reason is not None:
        print(f"Artifact reason: {status.artifact_incompatibility_reason}")
    print(
        "Artifact feature schema version: "
        f"{status.artifact_feature_schema_version or 'unknown'}"
    )
    print(
        "Artifact training timestamp: "
        f"{_format_optional_datetime(status.artifact_training_timestamp)}"
    )
    scope_id = (
        status.artifact_competition_scope_policy.get("scope_id")
        if status.artifact_competition_scope_policy is not None
        else None
    )
    feature_history_scope_id = (
        status.artifact_feature_history_scope_policy.get("scope_id")
        if status.artifact_feature_history_scope_policy is not None
        else None
    )
    print(f"Artifact competition scope: {scope_id or 'unknown'}")
    print(
        "Artifact feature history scope: "
        f"{feature_history_scope_id or 'unknown'}"
    )
    print(
        "Artifact feature history semantics: "
        f"{status.artifact_feature_history_scope_semantics or 'unknown'}"
    )
    _print_recorded_historical_metrics(status.artifact_recorded_metrics)
    return 0


def _evaluate_historical_ml_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run sync-history first."
        )
        return 1

    from app.history import DEFAULT_HISTORICAL_COMPETITION_SCOPE
    from app.historical_ml import (
        HistoricalModelCompatibilityError,
        evaluate_historical_model_from_repository,
    )

    repository = SQLiteRepository(db_path)
    try:
        result = evaluate_historical_model_from_repository(
            repository,
            model_path=args.model_path,
            decay_days=args.decay_days,
        )
    except (FileNotFoundError, HistoricalModelCompatibilityError) as exc:
        print(str(exc))
        return 1

    print("Historical ML v2 current-dataset evaluation")
    _print_historical_competition_scope(DEFAULT_HISTORICAL_COMPETITION_SCOPE)
    print(f"usable feature rows: {result.rows}")
    print(f"feature count: {result.feature_count}")
    print(f"decay days: {args.decay_days:g}")
    if result.split is not None:
        print(f"train rows: {result.split.train_rows}")
        print(f"validation rows: {result.split.validation_rows}")
        print(f"test rows: {result.split.test_rows}")
    print("Recorded artifact metrics:")
    _print_recorded_historical_metrics(result.recorded_metrics)
    print("Current dataset metrics:")
    _print_historical_metrics("train", result.train_metrics)
    _print_historical_metrics("validation", result.validation_metrics)
    _print_historical_metrics("test", result.test_metrics)
    print(f"evaluated: {result.evaluated}")
    print(f"message: {result.message}")
    return 0 if result.evaluated else 1


def _diagnose_historical_ml_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path.as_posix()}. Run sync-history first.")
        return 1

    from app.history import DEFAULT_HISTORICAL_COMPETITION_SCOPE
    from app.historical_ml import (
        diagnose_historical_ml_from_repository,
        rank_catboost_candidates,
    )

    repository = SQLiteRepository(db_path)
    result = diagnose_historical_ml_from_repository(
        repository,
        logistic_model_path=args.model_path,
    )
    print("Historical ML diagnostics")
    _print_historical_competition_scope(DEFAULT_HISTORICAL_COMPETITION_SCOPE)
    print("Prediction mode: PRE_MATCH series winner")
    print("Draft usage: forbidden for PRE_MATCH")
    print(f"Rows: {result.rows}")
    print(f"Feature count: {result.feature_count}")
    if result.split is not None:
        print(f"Train rows: {result.split.train_rows}")
        print(f"Validation rows: {result.split.validation_rows}")
        print(f"Test rows: {result.split.test_rows}")
    for partition, rate in result.split_positive_label_rates.items():
        print(f"{partition} positive label rate: {rate:.3f}")
    if not result.evaluated:
        print(f"message: {result.message}")
        return 1

    for baseline in result.baselines:
        print()
        print(f"Baseline: {baseline.name}")
        print(f"Train prior probability: {baseline.train_probability:.6f}")
        _print_historical_metrics("train", baseline.diagnostics.train_metrics)
        _print_historical_metrics(
            "validation",
            baseline.diagnostics.validation_metrics,
        )
        _print_historical_metrics("test", baseline.diagnostics.test_metrics)
    if result.logistic is not None:
        print()
        print("LOGISTIC baseline (in-memory, current pipeline)")
        _print_historical_metrics("train", result.logistic.train_metrics)
        _print_historical_metrics("validation", result.logistic.validation_metrics)
        _print_historical_metrics("test", result.logistic.test_metrics)
        _print_probability_buckets(result.logistic.test_probability_buckets)
        _print_chronological_buckets(result.logistic.test_chronological_buckets)
    print()
    if not result.catboost_available:
        print("CatBoost candidate comparison unavailable: CatBoost is not installed.")
    else:
        print("CatBoost deterministic configuration:")
        print("  loss_function=Logloss")
        print("  random_seed=42")
        print("  verbose=False")
        print("  allow_writing_files=False")
        print("  learning_rate=0.03")
        print("  iterations=300")
        print("Candidate selection: validation Brier, log loss, accuracy")
        print("Top CatBoost candidates:")
        top_candidates = rank_catboost_candidates(result.catboost_candidates)[:5]
        for candidate in top_candidates:
            config = candidate.config
            metrics = candidate.diagnostics.validation_metrics
            print(
                "  "
                f"decay={config.decay_days:g} depth={config.depth} "
                f"l2={config.l2_leaf_reg:g} "
                f"val_brier={metrics.brier_score:.6f} "
                f"val_log_loss={metrics.log_loss:.6f} "
                f"val_accuracy={metrics.accuracy:.3f}"
            )
        if result.selected_catboost is not None:
            selected = result.selected_catboost
            config = selected.config
            print("Selected CatBoost candidate:")
            print(
                f"  decay={config.decay_days:g} depth={config.depth} "
                f"l2={config.l2_leaf_reg:g}"
            )
            _print_historical_metrics("train", selected.diagnostics.train_metrics)
            _print_historical_metrics(
                "validation",
                selected.diagnostics.validation_metrics,
            )
            _print_historical_metrics("test", selected.diagnostics.test_metrics)
            print("CatBoost feature importance:")
            for name, importance in selected.feature_importance:
                print(f"  {name}: {importance:.6f}")
    print()
    _print_feature_drift("Validation feature drift", result.validation_feature_drift)
    _print_feature_drift("Test feature drift", result.test_feature_drift)
    print(f"message: {result.message}")
    return 0


def _draft_ml_status_command(args: Namespace) -> int:
    db_path = Path(args.db)
    print("Draft ML status")
    print(f"Database: {db_path.as_posix()}")
    print(f"Model artifact: {Path(args.model_path).as_posix()}")

    if not db_path.exists() or not _sqlite_table_exists(db_path, "historical_dota_games"):
        _print_empty_draft_ml_status(Path(args.model_path))
        return 0

    from app.draft_ml import build_draft_ml_status

    repository = SQLiteRepository(db_path)
    status = build_draft_ml_status(repository, model_path=args.model_path)
    print(f"Prediction mode: {status.prediction_mode}")
    print(f"Draft provider: {status.provider}")
    print(f"Raw historical games: {status.raw_historical_games}")
    print(f"Complete draft target games: {status.complete_draft_target_games}")
    print(f"Usable post-draft feature rows: {status.usable_post_draft_feature_rows}")
    print(f"Categorical feature count: {status.categorical_feature_count}")
    print(f"Numeric feature count: {status.numeric_feature_count}")
    print(f"Feature schema version: {status.feature_schema_version}")
    print(f"Projected train rows: {status.split.train_rows}")
    print(f"Projected validation rows: {status.split.validation_rows}")
    print(f"Projected test rows: {status.split.test_rows}")
    print(f"Temporal split ready: {'yes' if status.split_ready else 'no'}")
    print(f"Reason: {status.readiness_reason}")
    print(f"Patch semantics: {status.patch_semantics}")
    print(f"Source-link coverage: {status.source_link_coverage:.1%}")
    print(f"Artifact exists: {'yes' if status.artifact_exists else 'no'}")
    print(f"Artifact compatible: {'yes' if status.artifact_compatible else 'no'}")
    if status.artifact_reason is not None:
        print(f"Artifact reason: {status.artifact_reason}")
    return 0


def _train_draft_ml_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path.as_posix()}. Run sync-drafts first.")
        return 1

    from app.draft_ml import train_draft_model_from_repository
    from app.draft_ml.model import DraftModelCompatibilityError

    repository = SQLiteRepository(db_path)
    try:
        result = train_draft_model_from_repository(
            repository,
            model_path=args.model_path,
        )
    except DraftModelCompatibilityError as exc:
        print(str(exc))
        return 1

    print("POST_DRAFT map ML training")
    print("Prediction mode: POST_DRAFT_MAP")
    print("Provider: opendota")
    print(f"Usable rows: {result.rows}")
    print(f"Categorical feature count: {result.categorical_feature_count}")
    print(f"Numeric feature count: {result.numeric_feature_count}")
    if result.split is not None:
        print(f"Train rows: {result.split.train_rows}")
        print(f"Validation rows: {result.split.validation_rows}")
        print(f"Test rows: {result.split.test_rows}")
    if result.selected_candidate is not None:
        candidate = result.selected_candidate
        print(
            "Selected CatBoost parameters: "
            f"depth={candidate.config.depth}, "
            f"l2_leaf_reg={candidate.config.l2_leaf_reg:g}, "
            f"learning_rate={candidate.config.learning_rate:g}"
        )
        _print_historical_metrics("train", candidate.train_metrics)
        _print_historical_metrics("validation", candidate.validation_metrics)
        _print_historical_metrics("test", candidate.test_metrics)
    print(f"model artifact path: {_format_optional_path(result.model_path)}")
    print(f"trained: {result.trained}")
    print(f"message: {result.message}")
    return 0 if result.trained else 1


def _evaluate_draft_ml_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path.as_posix()}. Run sync-drafts first.")
        return 1

    from app.draft_ml import (
        DraftModelCompatibilityError,
        evaluate_draft_model_from_repository,
    )

    repository = SQLiteRepository(db_path)
    try:
        result = evaluate_draft_model_from_repository(
            repository,
            model_path=args.model_path,
        )
    except (FileNotFoundError, DraftModelCompatibilityError) as exc:
        print(str(exc))
        return 1

    print("POST_DRAFT map ML evaluation")
    print(f"Rows: {result.rows}")
    print("Recorded artifact metrics:")
    if isinstance(result.recorded_metrics, Mapping):
        _print_recorded_historical_metrics(
            cast(Mapping[str, Mapping[str, object]], result.recorded_metrics)
        )
    print("Current dataset metrics:")
    _print_historical_metrics("current", result.current_metrics)
    print(f"evaluated: {result.evaluated}")
    print(f"message: {result.message}")
    return 0 if result.evaluated else 1


def _evaluate_ml_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path.as_posix()}.")
        print("Not enough data for meaningful evaluation.")
        return 0

    from app.evaluation import format_evaluation_report, run_evaluation_backtest

    repository = SQLiteRepository(db_path)
    report = run_evaluation_backtest(
        repository,
        test_size=args.test_size,
        min_records=args.min_records,
        seed=args.seed,
    )
    print(format_evaluation_report(report, db_path=db_path))
    return 0


def _analyze_edge_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run app.main or app.cli run-once first."
        )
        return 1

    from app.ml import MLBetPredictor

    repository = SQLiteRepository(db_path)
    candidates = repository.list_bet_candidates()
    snapshots = repository.list_odds_snapshots()
    match_ids = {candidate.match_id for candidate in candidates}
    utterances_by_match = {
        match_id: repository.list_streamer_utterances_by_match(match_id)
        for match_id in match_ids
    }
    predictor = MLBetPredictor(args.model_path)
    analyses = build_edge_analyses(
        candidates=candidates,
        snapshots=snapshots,
        utterances_by_match=utterances_by_match,
        predictor=predictor,
        match_id=args.match_id,
        bookmaker=args.bookmaker,
        min_edge=args.min_edge,
        limit=args.limit,
    )

    print("Edge analysis")
    print(f"Database: {db_path.as_posix()}")
    print(f"Model path: {Path(args.model_path).as_posix()}")

    if not analyses:
        print()
        print("No candidates available for edge analysis.")
        return 0

    for analysis in analyses:
        _print_edge_analysis(analysis)

    available = sum(1 for analysis in analyses if analysis.status == "available")
    print()
    print(f"Analyzed candidates: {len(analyses)}")
    print(f"Edge available: {available}")
    print(f"Unavailable: {len(analyses) - available}")
    return 0


def _show_transcript_command(args: Namespace) -> int:
    transcript_path = Path(args.transcript)
    if not transcript_path.exists():
        print(f"Transcript file not found: {transcript_path.as_posix()}")
        return 1

    text = transcript_path.read_text(encoding="utf-8")

    utterances: list[str] = []
    for line in text.splitlines():
        stripped_line = line.strip()
        if stripped_line:
            utterances.append(stripped_line)

    recent_utterances = utterances[-args.last :]
    print(f"Transcript file: {transcript_path.as_posix()}")

    if not recent_utterances:
        print("No utterances found.")
        return 0

    print(f"Utterances: {len(recent_utterances)}")
    print()

    for index, utterance in enumerate(recent_utterances, start=1):
        print(f"{index}. {utterance}")

    return 0


def _add_utterance_command(args: Namespace) -> int:
    transcript_path = Path(args.transcript)
    ensure_transcript_file(transcript_path)
    text = args.text
    if args.speaker is not None:
        text = f"{args.speaker}: {text}"

    with transcript_path.open("a", encoding="utf-8") as file:
        file.write(text + "\n")

    print(f"Added utterance to {transcript_path.as_posix()}")
    return 0


def _show_last_bets_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(
            f"Database not found: {db_path.as_posix()}. "
            "Run app.main or app.cli run-once first."
        )
        return 1

    repository = SQLiteRepository(db_path)
    if args.session_id is None:
        bets = repository.list_recent_bets(args.limit)
    else:
        bets = sorted(
            repository.list_bets_by_session(args.session_id),
            key=lambda bet: (bet.created_at, bet.id),
            reverse=True,
        )[: args.limit]

    print(f"Database: {db_path.as_posix()}")
    _print_bets("Recent bets", bets)
    return 0


def _clear_transcript_command(args: Namespace) -> int:
    transcript_path = Path(args.transcript)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text("", encoding="utf-8")

    print(f"Cleared transcript: {transcript_path.as_posix()}")
    return 0


def _list_sessions_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path.as_posix()}.")
        print("No sessions found.")
        return 0

    repository = SQLiteRepository(db_path)
    sessions = repository.list_sessions()

    print(f"Database: {db_path.as_posix()}")
    print(f"Sessions: {len(sessions)}")
    if not sessions:
        print("No sessions found.")
        return 0

    for index, session in enumerate(sessions, start=1):
        matches_count = len(repository.list_matches_by_session(session.id))
        bets_count = len(repository.list_bets_by_session(session.id))
        utterances_count = len(
            repository.list_streamer_utterances_by_session(session.id)
        )
        print(
            f"{index}. id={session.id} name={session.name} "
            f"tournament={session.tournament_keyword} "
            f"mode={session.execution_mode} active={session.active} "
            f"created_at={session.created_at.isoformat()} "
            f"matches={matches_count} bets={bets_count} "
            f"utterances={utterances_count}"
        )

    return 0


def _show_session_command(args: Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path.as_posix()}.")
        print(f"Session not found: {args.session_id}")
        return 0

    repository = SQLiteRepository(db_path)
    session = repository.get_session(args.session_id)
    if session is None:
        print(f"Session not found: {args.session_id}")
        return 0

    matches = repository.list_matches_by_session(session.id)
    odds_snapshots = repository.list_odds_snapshots_by_session(session.id)
    candidates = repository.list_bet_candidates_by_session(session.id)
    bets = repository.list_bets_by_session(session.id)
    utterances = repository.list_streamer_utterances_by_session(session.id)

    print(f"Database: {db_path.as_posix()}")
    _print_session_details(session)
    _print_matches(matches)
    _print_odds_snapshots(odds_snapshots)
    _print_bet_candidates(candidates)
    _print_bets("Bets", bets)
    _print_recent_utterances(utterances)
    return 0


def _run_command(
    args: Namespace,
    iterations: int,
    interval_seconds: int,
    sleep_func: Callable[[float], None],
) -> int:
    db_path = Path(args.db)
    transcript_path = Path(args.transcript)
    ensure_transcript_file(transcript_path)
    init_db(db_path)

    print(f"Transcript file: {transcript_path.as_posix()}")
    print("Add streamer phrases as separate lines while bot is running.")

    config = build_config_from_args(args)
    repository = SQLiteRepository(db_path)
    session_manager = SessionManager()
    session = session_manager.start_session(config)
    repository.save_session(session)

    collector = TranscriptFileStreamerSpeechCollector(transcript_path)
    ml_predictor = _build_ml_predictor(args)
    autopilot = create_autopilot_service(
        streamer_speech_collector=collector,
        repository=repository,
        ml_predictor=ml_predictor,
        ml_weight=args.ml_weight,
    )

    try:
        for iteration in range(1, iterations + 1):
            if iterations > 1:
                print(f"Iteration: {iteration}/{iterations}")
            bets = autopilot.run_once(session, config)
            _print_summary(
                session_id=session.id,
                db_path=db_path,
                bets_created=len(bets),
                saved_utterances=len(
                    repository.list_streamer_utterances_by_session(session.id)
                ),
                total_bets=len(repository.list_bets_by_session(session.id)),
                profit_units=build_report(
                    repository.list_bets_by_session(session.id),
                    matches_count=len(autopilot.last_in_scope_matches),
                ).profit_units,
            )
            if iteration < iterations:
                sleep_func(float(interval_seconds))
    finally:
        stopped = session_manager.stop_session(session.id)
        repository.save_session(stopped)

    return 0


def _print_summary(
    *,
    session_id: str,
    db_path: Path,
    bets_created: int,
    saved_utterances: int,
    total_bets: int,
    profit_units: float,
) -> None:
    print(f"Session: {session_id}")
    print(f"Database: {db_path.as_posix()}")
    print(f"Bets created this run: {bets_created}")
    print(f"Saved streamer utterances: {saved_utterances}")
    print(f"Total bets in session: {total_bets}")
    print(f"Profit units: {profit_units:.2f}")


def _build_ml_predictor(args: Namespace) -> BetScorePredictor | None:
    if not args.use_ml:
        return None

    from app.ml import MLBetPredictor

    predictor = MLBetPredictor(args.model_path)
    if not predictor.is_available():
        print("ML model not found, using rule-based scoring fallback")
    return predictor


def _format_optional_path(path: Path | None) -> str:
    if path is None:
        return "-"
    return path.as_posix()


def _print_historical_metrics(label: str, metrics: Any | None) -> None:
    if metrics is None:
        print(f"{label} metrics: unavailable")
        return

    print(
        f"{label} Brier / log loss / accuracy: "
        f"{metrics.brier_score:.6f} / {metrics.log_loss:.6f} / "
        f"{metrics.accuracy:.3f}"
    )
    print(f"{label} positive label rate: {metrics.positive_label_rate:.3f}")
    print(
        f"{label} average predicted probability: "
        f"{metrics.average_predicted_probability:.3f}"
    )


def _print_probability_buckets(buckets: Sequence[Any]) -> None:
    if not buckets:
        print("Test probability buckets: unavailable")
        return
    print("Test probability buckets:")
    for bucket in buckets:
        print(
            f"  [{bucket.lower:.1f}, {bucket.upper:.1f}): "
            f"rows={bucket.rows}, "
            f"mean_p={bucket.mean_predicted_probability:.3f}, "
            f"observed={bucket.observed_positive_rate:.3f}, "
            f"gap={bucket.absolute_calibration_gap:.3f}"
        )


def _print_chronological_buckets(buckets: Sequence[Any]) -> None:
    if not buckets:
        print("Test chronological buckets: unavailable")
        return
    print("Test chronological buckets:")
    for index, bucket in enumerate(buckets, start=1):
        metrics = bucket.metrics
        print(
            f"  {index}. rows={bucket.rows}, "
            f"range={bucket.timestamp_start.isoformat()} to "
            f"{bucket.timestamp_end.isoformat()}, "
            f"label_rate={metrics.positive_label_rate:.3f}, "
            f"avg_p={metrics.average_predicted_probability:.3f}, "
            f"brier={metrics.brier_score:.6f}, "
            f"log_loss={metrics.log_loss:.6f}, "
            f"accuracy={metrics.accuracy:.3f}"
        )


def _print_feature_drift(label: str, drifts: Sequence[Any]) -> None:
    print(label + ":")
    if not drifts:
        print("  unavailable")
        return
    for drift in drifts:
        print(
            f"  {drift.feature_name}: "
            f"shift={drift.standardized_mean_shift:.3f}, "
            f"train_mean={drift.train_mean:.3f}, "
            f"other_mean={drift.other_mean:.3f}, "
            f"train_std={drift.train_std:.3f}"
        )


def _print_empty_draft_ml_status(model_path: Path) -> None:
    print("Prediction mode: POST_DRAFT_MAP")
    print("Draft provider: opendota")
    print("Raw historical games: 0")
    print("Complete draft target games: 0")
    print("Usable post-draft feature rows: 0")
    print("Categorical feature count: 0")
    print("Numeric feature count: 0")
    print("Feature schema version: 1")
    print("Projected train rows: 0")
    print("Projected validation rows: 0")
    print("Projected test rows: 0")
    print("Temporal split ready: no")
    print("Reason: No draft history found. Run sync-drafts first.")
    print("Patch semantics: trusted provider patch when present; no date inference")
    print("Source-link coverage: 0.0%")
    print(f"Artifact exists: {'yes' if model_path.exists() else 'no'}")
    print("Artifact compatible: no")


def _print_recorded_historical_metrics(
    metrics: Mapping[str, Mapping[str, object]],
) -> None:
    if not metrics:
        print("Recorded metrics: unavailable")
        return

    for partition in ("train", "validation", "test"):
        partition_metrics = metrics.get(partition)
        if partition_metrics is None:
            continue
        brier = _metric_float(partition_metrics, "brier_score")
        logloss = _metric_float(partition_metrics, "log_loss")
        accuracy = _metric_float(partition_metrics, "accuracy")
        row_count = _metric_int(partition_metrics, "row_count")
        print(
            f"Recorded {partition}: rows={row_count}, "
            f"Brier={brier:.6f}, log_loss={logloss:.6f}, "
            f"accuracy={accuracy:.3f}"
        )


def _print_historical_competition_scope(policy: Any) -> None:
    from app.history import HISTORICAL_COMPETITION_CLASSIFICATION_PRECEDENCE

    ordered_families = [
        family
        for family in HISTORICAL_COMPETITION_CLASSIFICATION_PRECEDENCE
        if family in policy.allowed_families
    ]
    family_names = ", ".join(family.name for family in ordered_families)
    qualifier_policy = "excluded" if policy.exclude_qualifiers else "included"
    print(f"Competition scope: {policy.scope_id}")
    print(
        "Scope target start: "
        f"{policy.target_start_at.isoformat()} inclusive"
    )
    print(f"Allowed competition families: {family_names}")
    print(f"Qualifier policy: {qualifier_policy}")
    print(f"Match-history universe: {policy.scope_id} (same as target scope)")


def _print_historical_artifact_status(model_path: Path) -> None:
    from app.historical_ml import HistoricalModelCompatibilityError
    from app.historical_ml import load_historical_model

    if not model_path.exists():
        print("Model artifact exists: no")
        print("Artifact compatible: no")
        print("Artifact feature schema version: unknown")
        print("Artifact training timestamp: unavailable")
        print("Artifact competition scope: unknown")
        print("Artifact feature history scope: unknown")
        print("Artifact feature history semantics: unknown")
        return

    print("Model artifact exists: yes")
    try:
        artifact = load_historical_model(model_path)
    except (FileNotFoundError, HistoricalModelCompatibilityError) as exc:
        print("Artifact compatible: no")
        print(f"Artifact reason: {exc}")
        print("Artifact feature schema version: unknown")
        print("Artifact training timestamp: unavailable")
        print("Artifact competition scope: unknown")
        print("Artifact feature history scope: unknown")
        print("Artifact feature history semantics: unknown")
        return

    print("Artifact compatible: yes")
    print(f"Artifact feature schema version: {artifact.feature_schema_version}")
    print(
        "Artifact training timestamp: "
        f"{_format_optional_datetime(artifact.training_timestamp)}"
    )
    print(
        "Artifact competition scope: "
        f"{artifact.competition_scope_policy.get('scope_id', 'unknown')}"
    )
    print(
        "Artifact feature history scope: "
        f"{artifact.feature_history_scope_policy.get('scope_id', 'unknown')}"
    )
    print(
        "Artifact feature history semantics: "
        f"{artifact.feature_history_scope_semantics}"
    )
    _print_recorded_historical_metrics(artifact.evaluation_metrics)


def _metric_float(metrics: Mapping[str, object], key: str) -> float:
    value = metrics.get(key, 0.0)
    if isinstance(value, int | float | str):
        return float(value)
    return 0.0


def _metric_int(metrics: Mapping[str, object], key: str) -> int:
    value = metrics.get(key, 0)
    if isinstance(value, int | str):
        return int(value)
    return 0


def _recent_bets_for_report(
    repository: SQLiteRepository,
    session_id: str | None,
    limit: int,
) -> list[Bet]:
    if session_id is None:
        return repository.list_recent_bets(limit)

    return sorted(
        repository.list_bets_by_session(session_id),
        key=lambda bet: (bet.created_at, bet.id),
        reverse=True,
    )[:limit]


def _recent_utterances_for_report(
    repository: SQLiteRepository,
    session_id: str | None,
    limit: int,
) -> list[StreamerUtterance]:
    if session_id is None:
        return repository.list_recent_streamer_utterances(limit)

    return sorted(
        repository.list_streamer_utterances_by_session(session_id),
        key=lambda utterance: (utterance.created_at, utterance.id),
        reverse=True,
    )[:limit]


def _print_session_details(session: Session) -> None:
    print()
    print("Session:")
    print(f"id={session.id}")
    print(f"name={session.name}")
    print(f"tournament={session.tournament_keyword}")
    print(f"streamer_channel={session.streamer_channel}")
    print(f"execution_mode={session.execution_mode}")
    print(f"target_bets_per_match={session.target_bets_per_match}")
    print(f"max_bets_per_match={session.max_bets_per_match}")
    print(f"score_threshold={session.score_threshold}")
    print(f"active={session.active}")
    print(f"created_at={session.created_at.isoformat()}")
    print(f"ended_at={_format_optional_datetime(session.ended_at)}")


def _print_matches(matches: list[Match]) -> None:
    print()
    print(f"Matches: {len(matches)}")
    if not matches:
        print("No matches found.")
        return

    for index, match in enumerate(matches, start=1):
        print(
            f"{index}. id={match.id} {match.team_a} vs {match.team_b} "
            f"format={match.format} status={match.status} "
            f"start_time={_format_optional_datetime(match.start_time)}"
        )


def _print_odds_snapshots(snapshots: list[OddsSnapshot]) -> None:
    print()
    print(f"Odds snapshots: {len(snapshots)}")
    if not snapshots:
        print("No odds snapshots found.")
        return

    for index, snapshot in enumerate(snapshots, start=1):
        print(
            f"{index}. id={snapshot.id} match={snapshot.match_id} "
            f"{snapshot.market} {snapshot.selection} line={snapshot.line} "
            f"odds={snapshot.odds} phase={snapshot.phase} "
            f"live={snapshot.is_live} suspended={snapshot.is_suspended}"
        )


def _print_bet_candidates(candidates: list[BetCandidate]) -> None:
    print()
    print(f"Bet candidates: {len(candidates)}")
    if not candidates:
        print("No bet candidates found.")
        return

    for index, candidate in enumerate(candidates, start=1):
        print(
            f"{index}. id={candidate.id} match={candidate.match_id} "
            f"{candidate.market} {candidate.selection} line={candidate.line} "
            f"odds={candidate.odds} phase={candidate.phase} "
            f"final_score={candidate.final_score} decision={candidate.decision}"
        )


def _print_bets(title: str, bets: list[Bet]) -> None:
    print()
    print(f"{title}: {len(bets)}")
    if not bets:
        print("No bets found.")
        return

    for index, bet in enumerate(bets, start=1):
        print(f"{index}. {_format_bet_summary(bet)}")


def _print_recent_bets(bets: list[Bet]) -> None:
    print()
    print("Recent bets:")
    if not bets:
        print("No bets yet.")
        return

    for index, bet in enumerate(bets, start=1):
        print(f"{index}. {_format_bet_summary(bet)}")


def _format_bet_summary(bet: Bet) -> str:
    return (
        f"id={bet.id} {bet.market} {bet.selection} "
        f"line={bet.line} odds={bet.odds} stake_pct={bet.stake_pct} "
        f"status={bet.status} result={bet.result} "
        f"profit_units={bet.profit_units:.2f}"
    )


def _format_optional_datetime(value: object) -> str:
    if value is None:
        return "-"
    return str(value)


def _format_datetime_range(
    start: datetime | None,
    end: datetime | None,
) -> str:
    if start is None or end is None:
        return "-"
    return f"{start.isoformat()} to {end.isoformat()}"


def _format_history_date(value: datetime) -> str:
    return value.date().isoformat()


def _print_recent_utterances(utterances: list[StreamerUtterance]) -> None:
    print()
    print("Recent streamer utterances:")
    if not utterances:
        print("No streamer utterances yet.")
        return

    for index, utterance in enumerate(utterances, start=1):
        print(
            f'{index}. "{utterance.text}" '
            f"signal={utterance.signal_type} "
            f"strength={utterance.strength} confidence={utterance.confidence}"
        )


def _print_edge_analysis(analysis: CandidateEdgeAnalysis) -> None:
    print()
    print(f"Candidate: {analysis.selection}")
    print(f"Market: {analysis.market}")
    print(f"Bookmaker: {_format_optional_text(analysis.bookmaker)}")
    print(f"Odds: {analysis.decimal_odds:.2f}")
    print(
        "Raw implied probability: "
        f"{_format_optional_probability(analysis.raw_implied_probability)}"
    )
    print(
        "Fair market probability: "
        f"{_format_optional_probability(analysis.fair_market_probability)}"
    )
    print(
        "Model probability: "
        f"{_format_optional_probability(analysis.model_probability)}"
    )
    print(f"Probability source: {analysis.probability_source}")
    print(f"Estimated edge: {_format_optional_edge(analysis.edge)}")
    print(f"Expected value: {_format_optional_units(analysis.expected_value_units)}")
    print(f"Status: {analysis.status}")
    if analysis.reason and analysis.status != "available":
        print(f"Reason: {analysis.reason}")


def _format_optional_probability(value: float | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value * 100:.2f}%"


def _format_optional_edge(value: float | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value * 100:+.2f} pp"


def _format_optional_units(value: float | None) -> str:
    if value is None:
        return "unavailable"
    return f"{value:+.3f} units"


def _format_optional_text(value: str | None) -> str:
    if value is None:
        return "unavailable"
    return value


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be an integer") from exc

    if parsed < 1:
        raise ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be an integer") from exc

    if parsed < 0:
        raise ArgumentTypeError("must not be negative")
    return parsed


def _pandascore_page_size(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 100:
        raise ArgumentTypeError("must be at most 100")
    return parsed


def _utc_start_date(value: str) -> datetime:
    parsed = _date_value(value)
    return datetime.combine(parsed, datetime_time.min, tzinfo=timezone.utc)


def _utc_end_date(value: str) -> datetime:
    parsed = _date_value(value)
    return datetime.combine(parsed, datetime_time.max, tzinfo=timezone.utc)


def _date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be a date in YYYY-MM-DD format") from exc


def _utc_datetime(value: str) -> datetime:
    raw_value = value.strip()
    if raw_value.endswith("Z"):
        raw_value = f"{raw_value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError as exc:
        raise ArgumentTypeError(
            "must be an ISO-8601 timestamp with timezone, "
            "for example 2026-07-07T12:00:00Z"
        ) from exc

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArgumentTypeError(
            "must include a timezone, for example 2026-07-07T12:00:00Z"
        )
    return parsed.astimezone(timezone.utc)


def _non_empty_text(value: str) -> str:
    parsed = value.strip()
    if not parsed:
        raise ArgumentTypeError("must not be empty")
    return parsed


def _test_size(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be a number") from exc

    if not 0 < parsed < 1:
        raise ArgumentTypeError("must be between 0 and 1")
    return parsed


def _float_value(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be a number") from exc

    if not math.isfinite(parsed):
        raise ArgumentTypeError("must be finite")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be a number") from exc

    if parsed <= 0:
        raise ArgumentTypeError("must be greater than 0")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be a number") from exc

    if parsed < 0:
        raise ArgumentTypeError("must not be negative")
    return parsed


def _bookmaker_filter(value: str | None) -> list[str]:
    if value is None:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _odds_by_bookmaker(
    snapshots: Sequence[OddsSnapshot],
) -> list[tuple[str, list[OddsSnapshot]]]:
    grouped: dict[str, list[OddsSnapshot]] = {}
    for snapshot in snapshots:
        grouped.setdefault(snapshot.bookmaker, []).append(snapshot)
    return list(grouped.items())


if __name__ == "__main__":
    raise SystemExit(main())
