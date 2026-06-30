from app.domain import Bet, BetCandidate, ExecutionMode
from app.execution.paper_executor import PaperExecutor


AUTO_EXECUTION_MESSAGE = (
    "Auto execution is intentionally not implemented. "
    "Use an official permitted API adapter only."
)


class ExecutionEngine:
    def __init__(self, paper_executor: PaperExecutor) -> None:
        self.paper_executor = paper_executor

    def handle_candidate(
        self,
        candidate: BetCandidate,
        execution_mode: ExecutionMode,
        stake_pct: float,
    ) -> Bet | None:
        if execution_mode == "paper":
            return self.paper_executor.place(candidate, stake_pct, mode=execution_mode)
        if execution_mode == "signal":
            return None
        if execution_mode == "confirm":
            # TODO: connect a confirmation UI or CLI before placing anything.
            return None
        if execution_mode == "auto":
            raise NotImplementedError(AUTO_EXECUTION_MESSAGE)

        raise ValueError(f"Unsupported execution mode: {execution_mode}")
