from argparse import ArgumentParser, ArgumentTypeError, Namespace
from collections.abc import Callable, Sequence
from pathlib import Path
import sys
import time
from typing import Any

from app.collectors import TranscriptFileStreamerSpeechCollector
from app.domain import Bet, StreamerUtterance
from app.reports import build_report, build_report_from_repository
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
    print(f"Profit units: {report.profit_units:.2f}")
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
    autopilot = create_autopilot_service(
        streamer_speech_collector=collector,
        repository=repository,
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


def _print_recent_bets(bets: list[Bet]) -> None:
    print()
    print("Recent bets:")
    if not bets:
        print("No bets yet.")
        return

    for index, bet in enumerate(bets, start=1):
        print(
            f"{index}. {bet.market} {bet.selection} "
            f"line={bet.line} odds={bet.odds} stake_pct={bet.stake_pct} "
            f"status={bet.status} result={bet.result}"
        )


def _print_recent_utterances(utterances: list[StreamerUtterance]) -> None:
    print()
    print("Recent streamer utterances:")
    if not utterances:
        print("No streamer utterances yet.")
        return

    for index, utterance in enumerate(utterances, start=1):
        print(
            f'{index}. "{utterance.text}" '
            f"signal={utterance.signal_type} strength={utterance.strength}"
        )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be an integer") from exc

    if parsed < 1:
        raise ArgumentTypeError("must be at least 1")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
