from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class RawStreamerUtterance:
    text: str
    created_at: datetime
    source: str = "fake"


class StreamerSpeechCollector:
    def fetch_recent_utterances(self) -> list[RawStreamerUtterance]:
        raise NotImplementedError


class FakeStreamerSpeechCollector(StreamerSpeechCollector):
    def fetch_recent_utterances(self) -> list[RawStreamerUtterance]:
        now = datetime.now(timezone.utc)
        return [
            RawStreamerUtterance("тут овер киллов выглядит норм", now),
            RawStreamerUtterance("карта будет долгая", now),
            RawStreamerUtterance("драфт у первой команды хороший", now),
            RawStreamerUtterance("тут будет быстрый стомп", now),
            RawStreamerUtterance("all in хата, фри бабки", now),
            RawStreamerUtterance("лучше не лезть, слишком мутно", now),
        ]


class TranscriptFileStreamerSpeechCollector(StreamerSpeechCollector):
    def __init__(self, transcript_path: str | Path) -> None:
        self.transcript_path = Path(transcript_path)

    def fetch_recent_utterances(self) -> list[RawStreamerUtterance]:
        if not self.transcript_path.exists():
            return []

        now = datetime.now(timezone.utc)
        lines = self.transcript_path.read_text(encoding="utf-8").splitlines()
        return [
            RawStreamerUtterance(
                text=line.strip(),
                created_at=now,
                source="manual_transcript",
            )
            for line in lines
            if line.strip()
        ]
