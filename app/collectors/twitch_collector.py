from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class TwitchMessage:
    username: str
    message: str
    created_at: datetime


class TwitchCollector:
    def fetch_recent_messages(self, channel: str) -> list[TwitchMessage]:
        raise NotImplementedError


class FakeTwitchCollector(TwitchCollector):
    def fetch_recent_messages(self, channel: str) -> list[TwitchMessage]:
        now = datetime.now(timezone.utc)
        return [
            TwitchMessage(
                username="viewer_1",
                message="тут овер киллов выглядит норм",
                created_at=now,
            ),
            TwitchMessage(
                username="viewer_2",
                message="карта будет долгая",
                created_at=now,
            ),
            TwitchMessage(
                username="viewer_3",
                message="all in хата",
                created_at=now,
            ),
            TwitchMessage(
                username="viewer_4",
                message="драфт говно",
                created_at=now,
            ),
            TwitchMessage(
                username="viewer_5",
                message="быстрый стомп",
                created_at=now,
            ),
        ]
