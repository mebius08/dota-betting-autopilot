A_TIER_MARKETS = {
    "total_kills",
    "map_duration",
    "team_total_kills",
    "total_maps",
    "map_handicap",
    "kill_handicap",
}
B_TIER_MARKETS = {
    "map_winner",
    "live_total_kills",
    "live_duration",
    "live_team_total_kills",
    "correct_score",
}
C_TIER_MARKETS = {
    "first_blood",
    "next_kill",
    "next_tower",
    "next_player_kill",
}


def classify_market(market: str) -> str:
    normalized = market.strip().casefold()
    if normalized in A_TIER_MARKETS:
        return "A"
    if normalized in B_TIER_MARKETS:
        return "B"
    return "C"


def market_quality_score(market: str) -> float:
    tier = classify_market(market)
    if tier == "A":
        return 25.0
    if tier == "B":
        return 15.0
    return -10.0
