from app.collectors import (
    FakeMatchCollector,
    FakeOddsCollector,
    StreamerSpeechCollector,
)
from app.execution import ExecutionEngine, PaperExecutor
from app.services.autopilot_service import AutopilotService
from app.storage import SQLiteRepository


def create_autopilot_service(
    streamer_speech_collector: StreamerSpeechCollector,
    repository: SQLiteRepository | None = None,
) -> AutopilotService:
    paper_executor = PaperExecutor()
    return AutopilotService(
        match_collector=FakeMatchCollector(),
        odds_collector=FakeOddsCollector(),
        streamer_speech_collector=streamer_speech_collector,
        execution_engine=ExecutionEngine(paper_executor),
        repository=repository,
    )
