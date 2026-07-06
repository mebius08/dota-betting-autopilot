import math

import pytest

from app.edge import (
    MarketProbabilityError,
    calculate_expected_value_units,
    calculate_probability_edge,
    calculate_two_way_market_probabilities,
    decimal_odds_to_implied_probability,
)


@pytest.mark.parametrize(
    ("odds", "expected"),
    [
        (2.0, 0.5),
        (4.0, 0.25),
        (1.25, 0.8),
    ],
)
def test_decimal_odds_to_implied_probability(odds: float, expected: float) -> None:
    assert decimal_odds_to_implied_probability(odds) == pytest.approx(expected)


@pytest.mark.parametrize(
    "odds",
    [
        1.0,
        0.99,
        True,
        float("nan"),
        float("inf"),
        float("-inf"),
        "2.0",
    ],
)
def test_decimal_odds_to_implied_probability_rejects_invalid_values(
    odds: object,
) -> None:
    with pytest.raises(MarketProbabilityError):
        decimal_odds_to_implied_probability(odds)


def test_calculate_two_way_market_probabilities_for_symmetric_market() -> None:
    result = calculate_two_way_market_probabilities(
        selection_a="Team Spirit",
        odds_a=2.0,
        selection_b="PARIVISION",
        odds_b=2.0,
    )

    assert result.raw_implied_probability_a == pytest.approx(0.5)
    assert result.raw_implied_probability_b == pytest.approx(0.5)
    assert result.overround == pytest.approx(1.0)
    assert result.margin == pytest.approx(0.0)
    assert result.fair_probability_a == pytest.approx(0.5)
    assert result.fair_probability_b == pytest.approx(0.5)


def test_calculate_two_way_market_probabilities_removes_overround() -> None:
    result = calculate_two_way_market_probabilities(
        selection_a="Team Spirit",
        odds_a=1.80,
        selection_b="PARIVISION",
        odds_b=2.10,
    )

    assert result.raw_implied_probability_a == pytest.approx(1 / 1.80)
    assert result.raw_implied_probability_b == pytest.approx(1 / 2.10)
    assert result.overround == pytest.approx((1 / 1.80) + (1 / 2.10))
    assert result.margin == pytest.approx(result.overround - 1.0)
    assert result.fair_probability_a + result.fair_probability_b == pytest.approx(1.0)


def test_calculate_two_way_market_probabilities_rejects_invalid_odds() -> None:
    with pytest.raises(MarketProbabilityError):
        calculate_two_way_market_probabilities(
            selection_a="Team Spirit",
            odds_a=1.0,
            selection_b="PARIVISION",
            odds_b=2.0,
        )


def test_calculate_two_way_market_probabilities_rejects_duplicate_selection() -> None:
    with pytest.raises(MarketProbabilityError):
        calculate_two_way_market_probabilities(
            selection_a="Team Spirit",
            odds_a=2.0,
            selection_b="Team Spirit",
            odds_b=2.0,
        )


@pytest.mark.parametrize(
    ("model_probability", "market_probability", "expected"),
    [
        (0.58, 0.50, 0.08),
        (0.45, 0.50, -0.05),
        (0.50, 0.50, 0.0),
    ],
)
def test_calculate_probability_edge(
    model_probability: float,
    market_probability: float,
    expected: float,
) -> None:
    assert calculate_probability_edge(
        model_probability=model_probability,
        fair_market_probability=market_probability,
    ) == pytest.approx(expected)


@pytest.mark.parametrize("probability", [-0.01, 1.01, float("nan"), math.inf, True])
def test_calculate_probability_edge_rejects_invalid_probability(
    probability: object,
) -> None:
    with pytest.raises(MarketProbabilityError):
        calculate_probability_edge(
            model_probability=probability,
            fair_market_probability=0.5,
        )


@pytest.mark.parametrize(
    ("probability", "odds", "expected"),
    [
        (0.50, 2.00, 0.00),
        (0.60, 2.00, 0.20),
        (0.40, 2.00, -0.20),
    ],
)
def test_calculate_expected_value_units(
    probability: float,
    odds: float,
    expected: float,
) -> None:
    assert calculate_expected_value_units(
        model_probability=probability,
        decimal_odds=odds,
    ) == pytest.approx(expected)


def test_calculate_expected_value_units_rejects_invalid_inputs() -> None:
    with pytest.raises(MarketProbabilityError):
        calculate_expected_value_units(model_probability=1.2, decimal_odds=2.0)
    with pytest.raises(MarketProbabilityError):
        calculate_expected_value_units(model_probability=0.5, decimal_odds=1.0)
