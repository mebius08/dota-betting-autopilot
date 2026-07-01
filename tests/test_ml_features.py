from app.ml.features import MLFeatureRow, build_feature_row, feature_row_to_dict
from tests.ml_test_helpers import make_candidate, make_utterance


def test_build_feature_row_basic_fields() -> None:
    candidate = make_candidate()

    row = build_feature_row(candidate, [])

    assert row.candidate_id == candidate.id
    assert row.market == candidate.market
    assert row.selection == candidate.selection
    assert row.phase == candidate.phase
    assert row.odds == candidate.odds
    assert row.line == candidate.line
    assert row.rule_final_score == candidate.final_score


def test_build_feature_row_hype_flag() -> None:
    row = build_feature_row(make_candidate(), [make_utterance(hype_flag=True)])

    assert row.hype_flag is True


def test_build_feature_row_skip_warning() -> None:
    row = build_feature_row(
        make_candidate(),
        [make_utterance(signal_type="skip_warning")],
    )

    assert row.has_skip_warning is True


def test_build_feature_row_streamer_strength_sum() -> None:
    row = build_feature_row(
        make_candidate(),
        [
            make_utterance(strength=3.0),
            make_utterance(text="second", strength=-1.5),
            make_utterance(text="third", strength=2.0),
        ],
    )

    assert row.streamer_strength_sum == 3.5


def test_build_feature_row_confidence_max() -> None:
    row = build_feature_row(
        make_candidate(),
        [
            make_utterance(confidence=0.2),
            make_utterance(text="second", confidence=0.8),
            make_utterance(text="third", confidence=0.5),
        ],
    )

    assert row.streamer_confidence_max == 0.8


def test_build_feature_row_extracts_candidate_and_streamer_fields() -> None:
    candidate = make_candidate()
    utterances = [
        make_utterance(strength=7.0, confidence=0.6, hype_flag=True),
        make_utterance(
            text="skip this market",
            signal_type="skip_warning",
            strength=-5.0,
            confidence=0.9,
        ),
    ]

    row = build_feature_row(candidate, utterances)

    assert row.candidate_id == candidate.id
    assert row.market == "total_kills"
    assert row.selection == "over"
    assert row.phase == "after_draft"
    assert row.rule_final_score == candidate.final_score
    assert row.hype_flag is True
    assert row.has_skip_warning is True
    assert row.streamer_strength_sum == 2.0
    assert row.streamer_confidence_max == 0.9


def test_feature_row_to_dict_contains_expected_keys() -> None:
    row = build_feature_row(make_candidate(), [])

    data = feature_row_to_dict(row)

    assert set(data) == set(MLFeatureRow.__dataclass_fields__)
