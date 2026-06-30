from app.collectors import TwitchMessage
from app.domain import BetCandidate, OddsSnapshot


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
HYPE_KEYWORDS = (
    "all in",
    "хата",
    "фри бабки",
    "залетайте",
)


def analyze_streamer_messages(messages: list[TwitchMessage]) -> dict[str, float]:
    signals = {
        "total_kills_over": 0.0,
        "total_kills_under": 0.0,
        "map_duration_over": 0.0,
        "map_duration_under": 0.0,
        "hype_penalty": 0.0,
    }

    for message in messages:
        text = message.message.casefold()
        if _contains_any(text, OVER_KILLS_KEYWORDS):
            signals["total_kills_over"] += 8.0
        if _contains_any(text, UNDER_KILLS_KEYWORDS):
            signals["total_kills_under"] += 8.0
        if _contains_any(text, DURATION_OVER_KEYWORDS):
            signals["map_duration_over"] += 5.0
        if _contains_any(text, DURATION_UNDER_KEYWORDS):
            signals["map_duration_under"] += 5.0
        if _contains_any(text, HYPE_KEYWORDS):
            signals["hype_penalty"] -= 4.0

    return {key: _clamp(value) for key, value in signals.items() if value != 0}


def streamer_score_for_candidate(
    candidate: BetCandidate | OddsSnapshot,
    signals: dict[str, float],
) -> float:
    score = signals.get("hype_penalty", 0.0)
    direction = _selection_direction(candidate.selection)

    if candidate.market == "total_kills" and direction == "over":
        score += signals.get("total_kills_over", 0.0)
    elif candidate.market == "total_kills" and direction == "under":
        score += signals.get("total_kills_under", 0.0)
    elif candidate.market == "map_duration" and direction == "over":
        score += signals.get("map_duration_over", 0.0)
    elif candidate.market == "map_duration" and direction == "under":
        score += signals.get("map_duration_under", 0.0)

    return _clamp(score)


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _selection_direction(selection: str) -> str | None:
    normalized = selection.casefold()
    if "over" in normalized or "больше" in normalized:
        return "over"
    if "under" in normalized or "меньше" in normalized:
        return "under"
    return None


def _clamp(value: float, minimum: float = -8.0, maximum: float = 8.0) -> float:
    return max(minimum, min(maximum, value))
