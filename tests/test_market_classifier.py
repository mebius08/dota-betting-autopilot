from app.scoring import classify_market, market_quality_score


def test_classify_market_tiers() -> None:
    assert classify_market("total_kills") == "A"
    assert classify_market("map_winner") == "B"
    assert classify_market("first_blood") == "C"
    assert classify_market("unknown_market") == "C"


def test_market_quality_score_by_tier() -> None:
    assert market_quality_score("total_kills") == 25
    assert market_quality_score("map_winner") == 15
    assert market_quality_score("first_blood") == -10
