from dataclasses import dataclass
import math
from numbers import Real


class MarketProbabilityError(ValueError):
    pass


@dataclass(frozen=True)
class TwoWayMarketProbability:
    selection_a: str
    selection_b: str
    odds_a: float
    odds_b: float
    raw_implied_probability_a: float
    raw_implied_probability_b: float
    overround: float
    margin: float
    fair_probability_a: float
    fair_probability_b: float


def decimal_odds_to_implied_probability(decimal_odds: object) -> float:
    odds = _finite_float(decimal_odds, "decimal_odds")
    if odds <= 1.0:
        raise MarketProbabilityError("decimal_odds must be greater than 1.0")
    return 1.0 / odds


def calculate_two_way_market_probabilities(
    *,
    selection_a: str,
    odds_a: object,
    selection_b: str,
    odds_b: object,
) -> TwoWayMarketProbability:
    if selection_a == selection_b:
        raise MarketProbabilityError("two-way market selections must be different")

    parsed_odds_a = _valid_decimal_odds(odds_a, "odds_a")
    parsed_odds_b = _valid_decimal_odds(odds_b, "odds_b")
    raw_a = decimal_odds_to_implied_probability(parsed_odds_a)
    raw_b = decimal_odds_to_implied_probability(parsed_odds_b)
    overround = raw_a + raw_b
    if overround <= 0:
        raise MarketProbabilityError("overround must be greater than 0")

    return TwoWayMarketProbability(
        selection_a=selection_a,
        selection_b=selection_b,
        odds_a=parsed_odds_a,
        odds_b=parsed_odds_b,
        raw_implied_probability_a=raw_a,
        raw_implied_probability_b=raw_b,
        overround=overround,
        margin=overround - 1.0,
        fair_probability_a=raw_a / overround,
        fair_probability_b=raw_b / overround,
    )


def calculate_probability_edge(
    *,
    model_probability: object,
    fair_market_probability: object,
) -> float:
    model = validate_probability(model_probability, "model_probability")
    market = validate_probability(fair_market_probability, "fair_market_probability")
    return model - market


def calculate_expected_value_units(
    *,
    model_probability: object,
    decimal_odds: object,
) -> float:
    probability = validate_probability(model_probability, "model_probability")
    odds = _valid_decimal_odds(decimal_odds, "decimal_odds")
    return probability * odds - 1.0


def validate_probability(value: object, field_name: str = "probability") -> float:
    probability = _finite_float(value, field_name)
    if probability < 0.0 or probability > 1.0:
        raise MarketProbabilityError(f"{field_name} must be between 0.0 and 1.0")
    return probability


def _valid_decimal_odds(value: object, field_name: str) -> float:
    odds = _finite_float(value, field_name)
    if odds <= 1.0:
        raise MarketProbabilityError(f"{field_name} must be greater than 1.0")
    return odds


def _finite_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise MarketProbabilityError(f"{field_name} must be a finite number")

    parsed = float(value)
    if not math.isfinite(parsed):
        raise MarketProbabilityError(f"{field_name} must be finite")
    return parsed
