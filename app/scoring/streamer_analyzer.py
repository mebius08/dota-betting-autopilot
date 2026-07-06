from typing import cast
from uuid import uuid4

from app.collectors.streamer_speech_collector import RawStreamerUtterance
from app.domain import BetCandidate, OddsSnapshot, StreamerUtterance


OVER_KILLS_KEYWORDS = (
    "овер киллов",
    "много киллов",
    "мясо",
    "заруба",
    "bloodbath",
    "будут драться",
    "много фрагов",
)
UNDER_KILLS_KEYWORDS = (
    "андер киллов",
    "мало киллов",
    "душная карта",
    "фарм",
    "никто не умрет",
)
DURATION_OVER_KEYWORDS = (
    "долгая карта",
    "карта будет долгая",
    "лейт",
    "затянется",
    "40 минут",
    "late game",
)
DURATION_UNDER_KEYWORDS = (
    "быстрая карта",
    "быстрый стомп",
    "стомп",
    "закончат быстро",
    "без шансов",
)
TEAM_POSITIVE_KEYWORDS = (
    "хороший драфт",
    "драфт у первой команды хороший",
    "сильный драфт",
    "забирают карту",
    "выглядят сильнее",
)
SKIP_WARNING_KEYWORDS = (
    "не лезть",
    "мутно",
    "непонятно",
    "лучше скип",
    "не ставка",
)
HYPE_KEYWORDS = (
    "all in",
    "хата",
    "фри бабки",
    "залетайте",
)


def analyze_streamer_utterance_text(text: str) -> dict[str, object]:
    normalized = text.casefold()
    result: dict[str, object] = {
        "detected_market": None,
        "detected_selection": None,
        "detected_team": None,
        "signal_type": None,
        "strength": 0.0,
        "confidence": 0.0,
        "hype_flag": False,
    }

    if _contains_any(normalized, SKIP_WARNING_KEYWORDS):
        result.update(
            {
                "signal_type": "skip_warning",
                "strength": -8.0,
                "confidence": 0.9,
            }
        )
    elif _contains_any(normalized, OVER_KILLS_KEYWORDS):
        result.update(
            {
                "detected_market": "total_kills",
                "detected_selection": "over",
                "signal_type": "over_kills",
                "strength": 7.0,
                "confidence": 0.8,
            }
        )
    elif _contains_any(normalized, UNDER_KILLS_KEYWORDS):
        result.update(
            {
                "detected_market": "total_kills",
                "detected_selection": "under",
                "signal_type": "under_kills",
                "strength": 7.0,
                "confidence": 0.8,
            }
        )
    elif _contains_any(normalized, DURATION_OVER_KEYWORDS):
        result.update(
            {
                "detected_market": "map_duration",
                "detected_selection": "over",
                "signal_type": "duration_over",
                "strength": 6.0,
                "confidence": 0.75,
            }
        )
    elif _contains_any(normalized, DURATION_UNDER_KEYWORDS):
        result.update(
            {
                "detected_market": "map_duration",
                "detected_selection": "under",
                "signal_type": "duration_under",
                "strength": 6.0,
                "confidence": 0.75,
            }
        )
    elif _contains_any(normalized, TEAM_POSITIVE_KEYWORDS):
        result.update(
            {
                "detected_market": "map_winner",
                "detected_selection": "team_a",
                "detected_team": "team_a",
                "signal_type": "team_positive",
                "strength": 4.0,
                "confidence": 0.6,
            }
        )

    if _contains_any(normalized, HYPE_KEYWORDS):
        result["hype_flag"] = True
        if result["signal_type"] is None:
            result["signal_type"] = "hype"
            result["strength"] = -3.0
            result["confidence"] = 0.8
        else:
            result["strength"] = _clamp(float(str(result["strength"])) - 3.0)

    result["strength"] = _clamp(float(str(result["strength"])))
    result["confidence"] = _clamp(float(str(result["confidence"])), 0.0, 1.0)
    return result


def map_raw_utterances_to_entities(
    raw_utterances: list[RawStreamerUtterance],
    session_id: str,
    match_id: str | None = None,
) -> list[StreamerUtterance]:
    utterances: list[StreamerUtterance] = []
    for raw_utterance in raw_utterances:
        analysis = analyze_streamer_utterance_text(raw_utterance.text)
        utterances.append(
            StreamerUtterance(
                id=str(uuid4()),
                session_id=session_id,
                match_id=match_id,
                source=raw_utterance.source,
                text=raw_utterance.text,
                detected_market=_optional_string(analysis["detected_market"]),
                detected_selection=_optional_string(
                    analysis["detected_selection"]
                ),
                detected_team=_optional_string(analysis["detected_team"]),
                signal_type=_optional_string(analysis["signal_type"]),
                strength=float(str(analysis["strength"])),
                confidence=float(str(analysis["confidence"])),
                hype_flag=bool(analysis["hype_flag"]),
                created_at=raw_utterance.created_at,
            )
        )

    return utterances


def streamer_score_for_candidate(
    candidate_or_snapshot: BetCandidate | OddsSnapshot,
    utterances: list[StreamerUtterance],
) -> float:
    matching_score = 0.0
    contradiction_score = 0.0
    hype_penalty = 0.0
    has_high_confidence_skip = False
    direction = _selection_direction(candidate_or_snapshot.selection)

    for utterance in utterances:
        if utterance.signal_type == "skip_warning" and utterance.confidence >= 0.7:
            has_high_confidence_skip = True
            continue

        if utterance.hype_flag:
            hype_penalty -= 3.0

        signal_type = utterance.signal_type
        if candidate_or_snapshot.market == "total_kills":
            if direction == "over" and signal_type == "over_kills":
                matching_score += utterance.strength
            elif direction == "over" and signal_type == "under_kills":
                contradiction_score -= abs(utterance.strength)
            elif direction == "under" and signal_type == "under_kills":
                matching_score += utterance.strength
            elif direction == "under" and signal_type == "over_kills":
                contradiction_score -= abs(utterance.strength)

        if candidate_or_snapshot.market == "map_duration":
            if direction == "over" and signal_type == "duration_over":
                matching_score += utterance.strength
            elif direction == "over" and signal_type == "duration_under":
                contradiction_score -= abs(utterance.strength)
            elif direction == "under" and signal_type == "duration_under":
                matching_score += utterance.strength
            elif direction == "under" and signal_type == "duration_over":
                contradiction_score -= abs(utterance.strength)

        if candidate_or_snapshot.market == "map_winner":
            if signal_type == "team_positive":
                matching_score += min(utterance.strength, 6.0)

    if has_high_confidence_skip and matching_score <= 0:
        return -8.0

    skip_penalty = -2.0 if has_high_confidence_skip else 0.0
    return _clamp(
        matching_score + contradiction_score + hype_penalty + skip_penalty
    )


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _selection_direction(selection: str) -> str | None:
    normalized = selection.casefold()
    if "over" in normalized or "больше" in normalized:
        return "over"
    if "under" in normalized or "меньше" in normalized:
        return "under"
    return None


def _optional_string(value: object) -> str | None:
    return cast(str | None, value)


def _clamp(value: float, minimum: float = -8.0, maximum: float = 8.0) -> float:
    return max(minimum, min(maximum, value))


def rank_utterances(utterances: list[StreamerUtterance]) -> list[StreamerUtterance]:
    return sorted(
        utterances,
        key=lambda utterance: (utterance.confidence, utterance.strength),
        reverse=True,
    )
