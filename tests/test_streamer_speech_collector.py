from pathlib import Path

from app.collectors import (
    FakeStreamerSpeechCollector,
    TranscriptFileStreamerSpeechCollector,
)


def test_fake_streamer_speech_collector_returns_messages() -> None:
    collector = FakeStreamerSpeechCollector()

    utterances = collector.fetch_recent_utterances()

    assert utterances
    assert all(utterance.text for utterance in utterances)
    assert all(utterance.created_at is not None for utterance in utterances)


def test_transcript_file_collector_returns_empty_for_missing_file(
    tmp_path: Path,
) -> None:
    collector = TranscriptFileStreamerSpeechCollector(tmp_path / "missing.txt")

    assert collector.fetch_recent_utterances() == []


def test_transcript_file_collector_reads_non_empty_lines(tmp_path: Path) -> None:
    transcript_path = tmp_path / "streamer_transcript.txt"
    transcript_path.write_text(
        "тут овер киллов выглядит норм\n\nкарта будет долгая\n",
        encoding="utf-8",
    )
    collector = TranscriptFileStreamerSpeechCollector(transcript_path)

    utterances = collector.fetch_recent_utterances()

    assert [utterance.text for utterance in utterances] == [
        "тут овер киллов выглядит норм",
        "карта будет долгая",
    ]
    assert all(utterance.source == "manual_transcript" for utterance in utterances)
