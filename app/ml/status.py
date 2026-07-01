from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.storage import SQLiteRepository


@dataclass
class MLTrainingStatus:
    training_rows: int
    positive_rows: int
    negative_rows: int
    ignored_bets: int
    unknown_open_bets: int
    push_void_bets: int
    can_train: bool
    reason: str


def build_ml_training_status(
    repository: "SQLiteRepository",
    min_rows: int = 30,
) -> MLTrainingStatus:
    bets = repository.list_bets()
    positive_rows = 0
    negative_rows = 0
    unknown_open_bets = 0
    push_void_bets = 0

    for bet in bets:
        if bet.status == "settled" and bet.result == "win":
            positive_rows += 1
        elif bet.status == "settled" and bet.result == "loss":
            negative_rows += 1
        elif bet.status != "settled" or bet.result == "unknown":
            unknown_open_bets += 1
        elif bet.result in ("push", "void"):
            push_void_bets += 1

    training_rows = positive_rows + negative_rows
    ignored_bets = unknown_open_bets + push_void_bets
    can_train = (
        training_rows >= min_rows and positive_rows > 0 and negative_rows > 0
    )

    if training_rows < min_rows:
        reason = "Not enough settled win/loss bets."
    elif positive_rows == 0 or negative_rows == 0:
        reason = "Need both win and loss examples."
    else:
        reason = "Ready to train."

    return MLTrainingStatus(
        training_rows=training_rows,
        positive_rows=positive_rows,
        negative_rows=negative_rows,
        ignored_bets=ignored_bets,
        unknown_open_bets=unknown_open_bets,
        push_void_bets=push_void_bets,
        can_train=can_train,
        reason=reason,
    )
