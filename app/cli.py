from argparse import ArgumentParser, ArgumentTypeError, Namespace
from collections.abc import Callable, Sequence
from pathlib import Path
import sys
import time
from typing import Any, cast

from app.collectors import TranscriptFileStreamerSpeechCollector
from app.domain import Bet, BetCandidate, BetResult, Match, OddsSnapshot, Session
from app.domain import StreamerUtterance
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
        if args.command == "export-bets":
            return _export_bets_command(args)
        if args.command == "export-candidates":
            return _export_candidates_command(args)
        if args.command == "export-utterances":
            return _export_utterances_command(args)
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
        if args.command == "evaluate-ml":
            return _evaluate_ml_command(args)
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


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be an integer") from exc

    if parsed < 1:
        raise ArgumentTypeError("must be at least 1")
    return parsed


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


if __name__ == "__main__":
    raise SystemExit(main())
