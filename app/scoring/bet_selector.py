from app.domain import BetCandidate
from app.scoring.market_classifier import classify_market


def select_bets(
    candidates: list[BetCandidate],
    max_bets_per_match: int,
    threshold: float,
) -> list[BetCandidate]:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.decision == "bet" and candidate.final_score >= threshold
    ]
    eligible.sort(key=lambda candidate: candidate.final_score, reverse=True)

    selected: list[BetCandidate] = []
    market_line_keys: set[tuple[str, float | None]] = set()
    has_map_winner = False
    c_tier_count = 0

    for candidate in eligible:
        if len(selected) >= max_bets_per_match:
            break

        key = (candidate.market, candidate.line)
        if key in market_line_keys:
            continue

        if candidate.market == "map_winner" and has_map_winner:
            continue

        tier = classify_market(candidate.market)
        if tier == "C" and c_tier_count >= 1:
            continue

        selected.append(candidate)
        market_line_keys.add(key)
        if candidate.market == "map_winner":
            has_map_winner = True
        if tier == "C":
            c_tier_count += 1

    return selected
