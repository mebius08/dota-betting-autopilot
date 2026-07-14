from __future__ import annotations

from argparse import ArgumentParser, ArgumentTypeError, Namespace
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
import math
import os
from pathlib import Path
import sys
import time

from app.fonbet_history.client import (
    FONBET_CLIENT_ID_ENV,
    FONBET_FSID_ENV,
    FonbetCouponClient,
    FonbetHistoryError,
)
from app.fonbet_history.exporter import export_personal_history


FONBET_BET_TYPE_NAME_ENV = "FONBET_BET_TYPE_NAME"
FONBET_SYS_ID_ENV = "FONBET_SYS_ID"
FONBET_LANG_ENV = "FONBET_LANG"


def create_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="python -m app.fonbet_history",
        description=(
            "Export and normalize the authenticated user's own FONBET coupon "
            "history for local research."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser(
        "export",
        help="Fetch missing coupon details and write normalized JSON and CSV.",
    )
    export_parser.add_argument(
        "--summary",
        type=Path,
        default=Path("local-data") / "fonbet-history" / "summary.json",
        help="Local exported coupon-summary JSON.",
    )
    export_parser.add_argument(
        "--local-data-dir",
        type=Path,
        default=Path("local-data") / "fonbet-history",
        help="Personal output directory; must be under a local-data directory.",
    )
    export_parser.add_argument(
        "--amount-divisor",
        type=_positive_decimal,
        required=True,
        help=(
            "Explicit source-to-ruble divisor: use 100 for kopecks or 1 for "
            "amounts already in rubles."
        ),
    )
    export_parser.add_argument(
        "--fsid",
        help=(
            "FONBET session credential. Prefer the FONBET_FSID environment "
            "variable to avoid shell history."
        ),
    )
    export_parser.add_argument(
        "--client-id",
        help=(
            "FONBET client credential. Prefer the FONBET_CLIENT_ID environment "
            "variable to avoid shell history."
        ),
    )
    export_parser.add_argument(
        "--bet-type-name",
        help="Value observed in coupon/info, or FONBET_BET_TYPE_NAME.",
    )
    export_parser.add_argument(
        "--sys-id",
        type=int,
        help="Integer sysId observed in coupon/info, or FONBET_SYS_ID.",
    )
    export_parser.add_argument(
        "--lang",
        help="Request language. Defaults to FONBET_LANG or ru.",
    )
    export_parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        help="Per-request HTTP timeout in seconds.",
    )
    export_parser.add_argument(
        "--delay-seconds",
        type=_non_negative_float,
        default=1.0,
        help="Delay between sequential network attempts.",
    )
    export_parser.add_argument(
        "--max-fetches",
        type=_non_negative_int,
        default=100,
        help=(
            "Maximum missing details fetched in this run. Existing raw files "
            "do not count; use 0 for offline normalization."
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    sleep_func: Callable[[float], None] = time.sleep,
) -> int:
    parser = create_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    credential_values = tuple(
        value
        for value in (
            _argument_or_env(args.fsid, FONBET_FSID_ENV),
            _argument_or_env(args.client_id, FONBET_CLIENT_ID_ENV),
        )
        if value is not None
    )
    try:
        if args.command != "export":
            parser.print_help()
            return 1
        client = _client_from_args(args)
        result = export_personal_history(
            summary_path=args.summary,
            local_data_dir=args.local_data_dir,
            amount_divisor=args.amount_divisor,
            client=client,
            max_fetches=args.max_fetches,
            delay_seconds=args.delay_seconds,
            sleep_func=sleep_func,
        )
    except FonbetHistoryError as exc:
        print(_redact_credentials(str(exc), credential_values), file=sys.stderr)
        return 1
    except Exception:
        print(
            "FONBET personal history export failed unexpectedly; session "
            "credentials were not included in this error.",
            file=sys.stderr,
        )
        return 1

    print("FONBET personal history export")
    print(f"Summary coupons: {result.summary_count}")
    print(f"Fetched details: {result.fetched_count}")
    print(f"Resumed raw details: {result.resumed_count}")
    print(f"Deferred by fetch limit: {result.deferred_count}")
    print(f"Failures: {result.failure_count}")
    print(
        "Normalized JSON: "
        f"{_redact_credentials(str(result.normalized_json_path), credential_values)}"
    )
    print(
        "Normalized CSV: "
        f"{_redact_credentials(str(result.normalized_csv_path), credential_values)}"
    )
    return 1 if result.failure_count else 0


def _client_from_args(args: Namespace) -> FonbetCouponClient | None:
    fsid = _argument_or_env(args.fsid, FONBET_FSID_ENV)
    client_id = _argument_or_env(args.client_id, FONBET_CLIENT_ID_ENV)
    bet_type_name = _argument_or_env(
        args.bet_type_name,
        FONBET_BET_TYPE_NAME_ENV,
    )
    sys_id = args.sys_id
    if sys_id is None:
        sys_id = _optional_env_int(FONBET_SYS_ID_ENV)
    lang = _argument_or_env(args.lang, FONBET_LANG_ENV) or "ru"

    if fsid is None or client_id is None or bet_type_name is None or sys_id is None:
        return None
    return FonbetCouponClient(
        fsid=fsid,
        client_id=client_id,
        bet_type_name=bet_type_name,
        sys_id=sys_id,
        lang=lang,
        timeout=args.timeout,
    )


def _argument_or_env(argument: str | None, env_name: str) -> str | None:
    if argument is not None:
        stripped = argument.strip()
        return stripped or None
    value = os.environ.get(env_name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _optional_env_int(env_name: str) -> int | None:
    value = os.environ.get(env_name)
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise FonbetHistoryError(f"{env_name} must be an integer.") from exc


def _redact_credentials(value: str, credentials: tuple[str, ...]) -> str:
    redacted = value
    for credential in credentials:
        if credential:
            redacted = redacted.replace(credential, "[redacted]")
    return redacted


def _positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ArgumentTypeError("must be a number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ArgumentTypeError("must be finite and greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ArgumentTypeError("must be finite and greater than zero")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ArgumentTypeError("must be finite and not negative")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise ArgumentTypeError("must not be negative")
    return parsed
