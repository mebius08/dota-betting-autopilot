from pathlib import Path

from app.collectors import (
    FakeMatchCollector,
    FakeOddsCollector,
    FakeStreamerSpeechCollector,
)
from app.config import load_config
from app.execution import ExecutionEngine, PaperExecutor
from app.reports import build_report
from app.services import AutopilotService, SessionManager
from app.storage import SQLiteRepository, init_db


def main() -> None:
    project_root = Path(__file__).resolve().parent.parent
    config_path = project_root / "config.example.yaml"
    db_path = project_root / "data" / "autopilot.db"

    init_db(db_path)
    repository = SQLiteRepository(db_path)

    config = load_config(config_path)

    session_manager = SessionManager()
    session = session_manager.start_session(config)
    repository.save_session(session)
    print(f"Database: {Path('data', 'autopilot.db').as_posix()}")
    print(
        "Started session "
        f"{session.id} for {session.tournament_keyword} "
        f"in {session.execution_mode} mode"
    )

    paper_executor = PaperExecutor()
    autopilot = AutopilotService(
        match_collector=FakeMatchCollector(),
        odds_collector=FakeOddsCollector(),
        streamer_speech_collector=FakeStreamerSpeechCollector(),
        execution_engine=ExecutionEngine(paper_executor),
        repository=repository,
    )

    try:
        bets = autopilot.run_once(session, config)
        saved_utterances = repository.list_streamer_utterances_by_session(session.id)
        print(f"Saved streamer utterances: {len(saved_utterances)}")

        for bet in bets:
            line = f" line={bet.line}" if bet.line is not None else ""
            print(
                "Paper bet: "
                f"{bet.market} {bet.selection}{line} odds={bet.odds} "
                f"stake_pct={bet.stake_pct}"
            )

        stored_bets = repository.list_bets_by_session(session.id)
        report = build_report(
            stored_bets,
            matches_count=len(autopilot.last_in_scope_matches),
        )
        print(
            "Summary: "
            f"bets={report.total_bets}, "
            f"profit_units={report.profit_units:.2f}, "
            f"avg_bets_per_match={report.average_bets_per_match:.2f}"
        )
    finally:
        stopped = session_manager.stop_session(session.id)
        repository.save_session(stopped)
        print(f"Stopped session {stopped.id}")


if __name__ == "__main__":
    main()
