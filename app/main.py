from pathlib import Path

from app.collectors import FakeMatchCollector, FakeOddsCollector, FakeTwitchCollector
from app.config import load_config
from app.execution import ExecutionEngine, PaperExecutor
from app.reports import build_report
from app.services import AutopilotService, SessionManager


def main() -> None:
    config_path = Path(__file__).resolve().parent.parent / "config.example.yaml"
    config = load_config(config_path)

    session_manager = SessionManager()
    session = session_manager.start_session(config)
    print(
        "Started session "
        f"{session.id} for {session.tournament_keyword} "
        f"in {session.execution_mode} mode"
    )

    paper_executor = PaperExecutor()
    autopilot = AutopilotService(
        match_collector=FakeMatchCollector(),
        odds_collector=FakeOddsCollector(),
        twitch_collector=FakeTwitchCollector(),
        execution_engine=ExecutionEngine(paper_executor),
    )

    try:
        bets = autopilot.run_once(session, config)
        for bet in bets:
            line = f" line={bet.line}" if bet.line is not None else ""
            print(
                "Paper bet: "
                f"{bet.market} {bet.selection}{line} odds={bet.odds} "
                f"stake_pct={bet.stake_pct}"
            )

        report = build_report(bets, matches_count=len(autopilot.last_in_scope_matches))
        print(
            "Summary: "
            f"bets={report.total_bets}, "
            f"profit_units={report.profit_units:.2f}, "
            f"avg_bets_per_match={report.average_bets_per_match:.2f}"
        )
    finally:
        stopped = session_manager.stop_session(session.id)
        print(f"Stopped session {stopped.id}")


if __name__ == "__main__":
    main()
