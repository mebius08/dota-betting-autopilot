from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

from app.domain import Session
from app.domain.entities import EXECUTION_MODES, ExecutionMode


class SessionManager:
    def __init__(self) -> None:
        self._active_session: Session | None = None

    def start_session(self, config: Any) -> Session:
        if self._active_session is not None:
            raise RuntimeError("Active session already exists")

        execution_mode = self._get_config_value(config, ("mode", "execution"))
        tournament_keyword = self._get_config_value(
            config,
            ("session", "tournament_keyword"),
        )
        streamer_channel = self._get_config_value(config, ("streamer", "channel"))
        target_bets_per_match = self._get_config_value(
            config,
            ("betting", "target_bets_per_match"),
        )
        max_bets_per_match = self._get_config_value(
            config,
            ("betting", "max_bets_per_match"),
        )
        score_threshold = self._get_config_value(
            config,
            ("betting", "score_threshold"),
        )

        self._validate_start_values(
            execution_mode=execution_mode,
            tournament_keyword=tournament_keyword,
            streamer_channel=streamer_channel,
            target_bets_per_match=target_bets_per_match,
            max_bets_per_match=max_bets_per_match,
            score_threshold=score_threshold,
        )

        session = Session(
            id=str(uuid4()),
            name=str(tournament_keyword),
            tournament_keyword=str(tournament_keyword),
            streamer_channel=str(streamer_channel),
            execution_mode=cast(ExecutionMode, execution_mode),
            target_bets_per_match=float(target_bets_per_match),
            max_bets_per_match=int(max_bets_per_match),
            score_threshold=float(score_threshold),
            active=True,
            created_at=datetime.now(timezone.utc),
            ended_at=None,
        )
        self._active_session = session

        return session

    def stop_session(self, session_id: str) -> Session:
        if self._active_session is None:
            raise RuntimeError("Active session does not exist")

        if self._active_session.id != session_id:
            raise ValueError("session_id does not match active session")

        stopped_session = self._active_session
        stopped_session.active = False
        stopped_session.ended_at = datetime.now(timezone.utc)
        self._active_session = None

        return stopped_session

    def get_active_session(self) -> Session | None:
        return self._active_session

    def is_active(self) -> bool:
        return self._active_session is not None

    def _get_config_value(self, config: Any, path: tuple[str, ...]) -> Any:
        value = config
        for key in path:
            if isinstance(value, Mapping):
                value = value.get(key)
            else:
                value = getattr(value, key, None)

            if value is None:
                dotted_path = ".".join(path)
                raise ValueError(f"Missing config value: {dotted_path}")

        return value

    def _validate_start_values(
        self,
        *,
        execution_mode: Any,
        tournament_keyword: Any,
        streamer_channel: Any,
        target_bets_per_match: Any,
        max_bets_per_match: Any,
        score_threshold: Any,
    ) -> None:
        if execution_mode not in EXECUTION_MODES:
            allowed = ", ".join(EXECUTION_MODES)
            raise ValueError(f"execution_mode must be one of: {allowed}")

        if not isinstance(tournament_keyword, str) or not tournament_keyword.strip():
            raise ValueError("tournament_keyword must not be empty")

        if not isinstance(streamer_channel, str) or not streamer_channel.strip():
            raise ValueError("streamer_channel must not be empty")

        target_bets = self._as_number(
            target_bets_per_match,
            "target_bets_per_match",
        )
        if target_bets <= 0:
            raise ValueError("target_bets_per_match must be greater than 0")

        max_bets = self._as_int(max_bets_per_match, "max_bets_per_match")
        if max_bets < 1:
            raise ValueError("max_bets_per_match must be greater than or equal to 1")

        threshold = self._as_number(score_threshold, "score_threshold")
        if threshold < 0 or threshold > 100:
            raise ValueError("score_threshold must be between 0 and 100")

    def _as_number(self, value: Any, field_name: str) -> float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a number") from exc

    def _as_int(self, value: Any, field_name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be an integer")

        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer") from exc

        if parsed != float(value):
            raise ValueError(f"{field_name} must be an integer")

        return parsed
