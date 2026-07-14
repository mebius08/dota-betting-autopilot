from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.fonbet_history import cli


def test_help_describes_dedicated_export(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["--help"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "authenticated user's own FONBET coupon history" in output
    assert "export" in output


def test_missing_credentials_is_friendly_and_value_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for env_name in (
        "FONBET_FSID",
        "FONBET_CLIENT_ID",
        "FONBET_BET_TYPE_NAME",
        "FONBET_SYS_ID",
    ):
        monkeypatch.delenv(env_name, raising=False)
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps({"coupons": [{"couponId": "fixture-one"}]}),
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "export",
            "--summary",
            str(summary_path),
            "--local-data-dir",
            str(tmp_path / "local-data" / "fonbet-history"),
            "--amount-divisor",
            "100",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Network fetch requires FONBET_FSID" in captured.err
    assert "Traceback" not in captured.err


def test_file_errors_redact_explicit_credential_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential = "fixture-session-secret"
    missing_summary = tmp_path / credential / "missing.json"

    exit_code = cli.main(
        [
            "export",
            "--summary",
            str(missing_summary),
            "--local-data-dir",
            str(tmp_path / "local-data" / "fonbet-history"),
            "--amount-divisor",
            "100",
            "--fsid",
            credential,
            "--client-id",
            "fixture-client",
            "--bet-type-name",
            "fixture-type",
            "--sys-id",
            "1",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert credential not in captured.err
    assert "[redacted]" in captured.err
