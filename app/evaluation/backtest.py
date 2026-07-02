from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import random
from typing import TYPE_CHECKING, Literal

import pandas as pd

from app.domain import BetResult
from app.evaluation.metrics import (
    ClassificationMetrics,
    build_classification_metrics,
)
from app.ml.features import build_feature_row, feature_row_to_dict
from app.ml.model import create_model_pipeline, select_model_features

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


EvaluationStatus = Literal["evaluated", "not_enough_data"]
ModelStatus = Literal["evaluated", "unavailable"]
Conclusion = Literal[
    "ML looks better",
    "Rule looks better",
    "Not enough data",
    "Inconclusive",
]


@dataclass(frozen=True)
class EvaluationRecord:
    bet_id: str
    candidate_id: str
    result: BetResult
    target: int
    profit_units: float
    stake_pct: float
    rule_score: float
    features: dict[str, object]


@dataclass(frozen=True)
class EvaluationDataset:
    records: list[EvaluationRecord]
    total_bets: int
    settled_bets: int
    ignored_unknown_open: int
    ignored_push_void: int
    missing_candidates: int

    @property
    def usable_records(self) -> int:
        return len(self.records)

    @property
    def wins(self) -> int:
        return sum(1 for record in self.records if record.target == 1)

    @property
    def losses(self) -> int:
        return sum(1 for record in self.records if record.target == 0)


@dataclass(frozen=True)
class ModelEvaluationResult:
    name: str
    status: ModelStatus
    metrics: ClassificationMetrics
    message: str


@dataclass(frozen=True)
class EvaluationReport:
    status: EvaluationStatus
    dataset: EvaluationDataset
    train_records: int
    test_records: int
    rule_result: ModelEvaluationResult
    ml_result: ModelEvaluationResult
    conclusion: Conclusion
    message: str


def build_evaluation_dataset(repository: "SQLiteRepository") -> EvaluationDataset:
    bets = repository.list_bets()
    candidates = repository.list_bet_candidates()
    candidates_by_id = {candidate.id: candidate for candidate in candidates}
    match_ids = {candidate.match_id for candidate in candidates}
    utterances_by_match = {
        match_id: repository.list_streamer_utterances_by_match(match_id)
        for match_id in match_ids
    }

    records: list[EvaluationRecord] = []
    ignored_unknown_open = 0
    ignored_push_void = 0
    missing_candidates = 0
    settled_bets = 0

    for bet in bets:
        if bet.status != "settled" or bet.result == "unknown":
            ignored_unknown_open += 1
            continue

        settled_bets += 1
        if bet.result in ("push", "void"):
            ignored_push_void += 1
            continue

        candidate = candidates_by_id.get(bet.candidate_id)
        if candidate is None:
            missing_candidates += 1
            continue

        target = 1 if bet.result == "win" else 0
        feature_row = build_feature_row(
            candidate,
            utterances_by_match.get(candidate.match_id, []),
        )
        records.append(
            EvaluationRecord(
                bet_id=bet.id,
                candidate_id=candidate.id,
                result=bet.result,
                target=target,
                profit_units=bet.profit_units,
                stake_pct=bet.stake_pct,
                rule_score=candidate.final_score,
                features=feature_row_to_dict(feature_row),
            )
        )

    return EvaluationDataset(
        records=records,
        total_bets=len(bets),
        settled_bets=settled_bets,
        ignored_unknown_open=ignored_unknown_open,
        ignored_push_void=ignored_push_void,
        missing_candidates=missing_candidates,
    )


def split_train_test(
    records: Sequence[EvaluationRecord],
    test_size: float,
    seed: int,
) -> tuple[list[EvaluationRecord], list[EvaluationRecord]]:
    if not 0 < test_size < 1:
        raise ValueError("test_size must be between 0 and 1")

    shuffled = list(records)
    random.Random(seed).shuffle(shuffled)

    if len(shuffled) < 2:
        return shuffled, []

    test_count = round(len(shuffled) * test_size)
    test_count = max(1, min(test_count, len(shuffled) - 1))
    return shuffled[test_count:], shuffled[:test_count]


def evaluate_rule_based(
    records: Sequence[EvaluationRecord],
    score_threshold: float = 50.0,
) -> ModelEvaluationResult:
    predicted_labels = [
        1 if record.rule_score >= score_threshold else 0 for record in records
    ]
    return _build_result(
        name="rule-based",
        records=records,
        predicted_labels=predicted_labels,
        status="evaluated",
        message=f"Rule score threshold: {score_threshold:.1f}",
    )


def evaluate_ml_model(
    train_records: Sequence[EvaluationRecord],
    test_records: Sequence[EvaluationRecord],
) -> ModelEvaluationResult:
    if not train_records or not test_records:
        return _unavailable_result(
            "ml",
            test_records,
            "Need both train and test records",
        )

    train_targets = {record.target for record in train_records}
    if len(train_targets) < 2:
        return _unavailable_result(
            "ml",
            test_records,
            "Need both win and loss examples in train split",
        )

    train_dataframe = _records_to_dataframe(train_records)
    test_dataframe = _records_to_dataframe(test_records)
    pipeline = create_model_pipeline()
    pipeline.fit(select_model_features(train_dataframe), train_dataframe["target"])

    probabilities = pipeline.predict_proba(select_model_features(test_dataframe))
    classes = list(pipeline.classes_)
    if 1 not in classes:
        return _unavailable_result("ml", test_records, "Model has no positive class")

    positive_index = classes.index(1)
    predicted_labels = [
        1 if float(row[positive_index]) >= 0.5 else 0 for row in probabilities
    ]
    return _build_result(
        name="ml",
        records=test_records,
        predicted_labels=predicted_labels,
        status="evaluated",
        message="In-memory LogisticRegression on train split",
    )


def compare_evaluation_results(
    rule_result: ModelEvaluationResult,
    ml_result: ModelEvaluationResult,
) -> Conclusion:
    if rule_result.metrics.total_records == 0:
        return "Not enough data"

    if ml_result.status != "evaluated":
        return "Inconclusive"

    rule_accuracy = rule_result.metrics.accuracy
    ml_accuracy = ml_result.metrics.accuracy
    if rule_accuracy is None or ml_accuracy is None:
        return "Inconclusive"

    accuracy_delta = ml_accuracy - rule_accuracy
    roi_delta = ml_result.metrics.roi_pct - rule_result.metrics.roi_pct

    if accuracy_delta > 0.05:
        return "ML looks better"
    if accuracy_delta < -0.05:
        return "Rule looks better"
    if roi_delta > 5:
        return "ML looks better"
    if roi_delta < -5:
        return "Rule looks better"
    return "Inconclusive"


def run_evaluation_backtest(
    repository: "SQLiteRepository",
    test_size: float = 0.3,
    min_records: int = 10,
    seed: int = 42,
) -> EvaluationReport:
    dataset = build_evaluation_dataset(repository)
    if dataset.usable_records < min_records:
        empty_rule = evaluate_rule_based([])
        empty_ml = _unavailable_result("ml", [], "Not enough data")
        return EvaluationReport(
            status="not_enough_data",
            dataset=dataset,
            train_records=0,
            test_records=0,
            rule_result=empty_rule,
            ml_result=empty_ml,
            conclusion="Not enough data",
            message="Not enough data for meaningful evaluation",
        )

    train_records, test_records = split_train_test(dataset.records, test_size, seed)
    rule_result = evaluate_rule_based(test_records)
    ml_result = evaluate_ml_model(train_records, test_records)
    conclusion = compare_evaluation_results(rule_result, ml_result)

    return EvaluationReport(
        status="evaluated",
        dataset=dataset,
        train_records=len(train_records),
        test_records=len(test_records),
        rule_result=rule_result,
        ml_result=ml_result,
        conclusion=conclusion,
        message="Evaluation completed",
    )


def format_evaluation_report(
    report: EvaluationReport,
    db_path: Path | None = None,
) -> str:
    lines = ["Evaluation / backtest"]
    if db_path is not None:
        lines.append(f"Database: {db_path.as_posix()}")

    lines.extend(
        [
            f"Status: {report.status}",
            f"Message: {report.message}",
            f"Total bets: {report.dataset.total_bets}",
            f"Settled bets: {report.dataset.settled_bets}",
            f"Usable settled records: {report.dataset.usable_records}",
            f"Train records: {report.train_records}",
            f"Test records: {report.test_records}",
            (
                "Win/loss distribution: "
                f"wins={report.dataset.wins} losses={report.dataset.losses}"
            ),
            f"Ignored unknown/open: {report.dataset.ignored_unknown_open}",
            f"Ignored push/void: {report.dataset.ignored_push_void}",
            f"Missing candidates: {report.dataset.missing_candidates}",
            "",
            _format_model_result(report.rule_result),
            "",
            _format_model_result(report.ml_result),
            "",
            f"Conclusion: {report.conclusion}",
        ]
    )
    return "\n".join(lines)


def _build_result(
    *,
    name: str,
    records: Sequence[EvaluationRecord],
    predicted_labels: Sequence[int],
    status: ModelStatus,
    message: str,
) -> ModelEvaluationResult:
    actual_labels = [record.target for record in records]
    selected_profit_units = [
        record.profit_units
        for record, predicted_label in zip(records, predicted_labels)
        if predicted_label == 1
    ]
    selected_stake_units = [
        record.stake_pct
        for record, predicted_label in zip(records, predicted_labels)
        if predicted_label == 1
    ]
    return ModelEvaluationResult(
        name=name,
        status=status,
        metrics=build_classification_metrics(
            actual_labels,
            predicted_labels,
            selected_profit_units,
            selected_stake_units,
        ),
        message=message,
    )


def _unavailable_result(
    name: str,
    records: Sequence[EvaluationRecord],
    message: str,
) -> ModelEvaluationResult:
    return _build_result(
        name=name,
        records=records,
        predicted_labels=[0 for _ in records],
        status="unavailable",
        message=message,
    )


def _records_to_dataframe(records: Sequence[EvaluationRecord]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for record in records:
        row = dict(record.features)
        row["target"] = record.target
        rows.append(row)
    return pd.DataFrame(rows)


def _format_model_result(result: ModelEvaluationResult) -> str:
    metrics = result.metrics
    accuracy = "-"
    if metrics.accuracy is not None:
        accuracy = f"{metrics.accuracy * 100:.2f}%"

    return "\n".join(
        [
            f"{result.name}:",
            f"  status: {result.status}",
            f"  records: {metrics.total_records}",
            f"  accuracy: {accuracy}",
            f"  predicted positives: {metrics.predicted_positive_records}",
            f"  profit units: {metrics.total_profit_units:.2f}",
            f"  average profit units: {metrics.average_profit_units:.2f}",
            f"  simulated ROI: {metrics.roi_pct:.2f}%",
            f"  message: {result.message}",
        ]
    )
