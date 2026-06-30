from pathlib import Path

from app import cli
from app.storage import SQLiteRepository


def test_cli_help_runs() -> None:
    exit_code = cli.main(["--help"])

    assert exit_code == 0


def test_build_config_from_args() -> None:
    parser = cli.create_parser()
    args = parser.parse_args(
        [
            "run-once",
            "--tournament",
            "DreamLeague",
            "--transcript",
            "data/streamer_transcript.txt",
            "--mode",
            "paper",
            "--target-bets-per-match",
            "1.2",
            "--max-bets-per-match",
            "3",
            "--score-threshold",
            "62",
        ]
    )

    config = cli.build_config_from_args(args)

    assert config["mode"] == {"execution": "paper"}
    assert config["session"]["tournament_keyword"] == "DreamLeague"
    assert config["betting"]["target_bets_per_match"] == 1.2


def test_run_once_command_creates_db(tmp_path: Path) -> None:
    db_path = tmp_path / "autopilot.db"
    transcript_path = tmp_path / "streamer_transcript.txt"
    transcript_path.write_text(
        "тут овер киллов выглядит норм\nкарта будет долгая\n",
        encoding="utf-8",
    )

    exit_code = cli.main(
        [
            "run-once",
            "--tournament",
            "DreamLeague",
            "--transcript",
            str(transcript_path),
            "--db",
            str(db_path),
        ]
    )

    repository = SQLiteRepository(db_path)
    sessions = repository.list_sessions()

    assert exit_code == 0
    assert db_path.exists()
    assert len(repository.list_bets_by_session(sessions[0].id)) >= 1
    assert len(repository.list_streamer_utterances_by_session(sessions[0].id)) == 2


def test_loop_command_with_two_iterations(tmp_path: Path) -> None:
    db_path = tmp_path / "autopilot.db"
    transcript_path = tmp_path / "streamer_transcript.txt"
    transcript_path.write_text("тут овер киллов выглядит норм\n", encoding="utf-8")
    sleep_calls: list[float] = []

    exit_code = cli.main(
        [
            "loop",
            "--tournament",
            "DreamLeague",
            "--transcript",
            str(transcript_path),
            "--db",
            str(db_path),
            "--iterations",
            "2",
            "--interval-seconds",
            "1",
        ],
        sleep_func=sleep_calls.append,
    )

    repository = SQLiteRepository(db_path)
    sessions = repository.list_sessions()

    assert exit_code == 0
    assert sleep_calls == [1.0]
    assert len(repository.list_streamer_utterances_by_session(sessions[0].id)) == 2


def test_invalid_mode_fails(tmp_path: Path) -> None:
    exit_code = cli.main(
        [
            "run-once",
            "--tournament",
            "DreamLeague",
            "--transcript",
            str(tmp_path / "streamer_transcript.txt"),
            "--mode",
            "casino",
        ]
    )

    assert exit_code != 0


def test_invalid_interval_fails(tmp_path: Path) -> None:
    exit_code = cli.main(
        [
            "loop",
            "--tournament",
            "DreamLeague",
            "--transcript",
            str(tmp_path / "streamer_transcript.txt"),
            "--interval-seconds",
            "0",
        ]
    )

    assert exit_code != 0


def test_ensure_transcript_file_creates_parent_and_file(tmp_path: Path) -> None:
    transcript_path = tmp_path / "nested" / "streamer_transcript.txt"

    cli.ensure_transcript_file(transcript_path)

    assert transcript_path.exists()
