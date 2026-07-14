"""Isolated personal FONBET history research export utilities."""

from app.fonbet_history.client import (
    FONBET_CLIENT_ID_ENV,
    FONBET_COUPON_INFO_ENDPOINT,
    FONBET_FSID_ENV,
    FonbetConfigurationError,
    FonbetCouponClient,
    FonbetHistoryError,
    FonbetRequestError,
    FonbetResponseError,
)
from app.fonbet_history.exporter import (
    ExportResult,
    export_personal_history,
    raw_response_path,
)
from app.fonbet_history.normalize import (
    LEG_COLUMNS,
    NORMALIZED_COLUMNS,
    SINGLE_EVENT_SEQUENCE_COLUMNS,
    FonbetDataError,
    build_single_event_sequences,
    load_coupon_summaries,
    normalize_coupon,
    normalize_coupon_legs,
)

__all__ = [
    "FONBET_CLIENT_ID_ENV",
    "FONBET_COUPON_INFO_ENDPOINT",
    "FONBET_FSID_ENV",
    "LEG_COLUMNS",
    "NORMALIZED_COLUMNS",
    "SINGLE_EVENT_SEQUENCE_COLUMNS",
    "ExportResult",
    "FonbetConfigurationError",
    "FonbetCouponClient",
    "FonbetDataError",
    "FonbetHistoryError",
    "FonbetRequestError",
    "FonbetResponseError",
    "export_personal_history",
    "build_single_event_sequences",
    "load_coupon_summaries",
    "normalize_coupon",
    "normalize_coupon_legs",
    "raw_response_path",
]
