"""Current cloud transcription with fast, model-compatible command endpointing."""

import os
from collections.abc import AsyncIterable, AsyncIterator

from livekit.agents import stt
from livekit.plugins import openai

from .endpointing import SilenceEndpoint

DEFAULT_MODEL = "gpt-live-transcribe"
DEFAULT_KEYWORDS = "Brodi,Chrome,Notepad,GitHub,YouTube,Google Keep,Slack"


async def complete_transcript_turns(events: AsyncIterable[stt.SpeechEvent]) -> AsyncIterator[stt.SpeechEvent]:
    """Keep a final transcript actionable even if its earlier end marker was consumed.

    A provider can deliver the final text after LiveKit has already committed an
    older transcript at the latest end-of-speech marker. Bracket that late text
    with its original speech times so it gets a turn and stale speech still fails
    the microphone's mute/unmute boundary. Never end speech that is still active.
    """
    speaking = False
    started_at = ended_at = None
    kind = stt.SpeechEventType
    async for event in events:
        if event.type == kind.START_OF_SPEECH:
            speaking = True
            started_at = event.speech_start_time if event.speech_start_time is not None else event.created_at
            ended_at = None
        elif event.type == kind.END_OF_SPEECH:
            speaking = False
            ended_at = event.speech_end_time if event.speech_end_time is not None else event.created_at
        elif (
            event.type == kind.FINAL_TRANSCRIPT
            and event.alternatives
            and event.alternatives[0].text.strip()
            and not speaking
        ):
            yield stt.SpeechEvent(
                kind.START_OF_SPEECH,
                speech_start_time=started_at if started_at is not None else event.created_at,
            )
            yield event
            yield stt.SpeechEvent(
                kind.END_OF_SPEECH,
                speech_end_time=ended_at if ended_at is not None else event.created_at,
            )
            continue
        yield event


def create_stt(*, model: str | None = None, language: str | None = None):
    if language not in (None, "en"):
        raise ValueError("Brodi supports English only (en).")
    selected = model or os.environ.get("BRODI_STT_MODEL", DEFAULT_MODEL)
    silence_ms = int(os.environ.get("BRODI_STT_SILENCE_MS", "250"))
    if not 100 <= silence_ms <= 2000:
        raise ValueError("BRODI_STT_SILENCE_MS must be between 100 and 2000")
    options = {
        "model": selected,
        # Product policy, not a user-selectable multilingual hint. Old environment
        # overrides must not silently re-enable other recognition languages.
        "language": "en",
        "use_realtime": True,
        "vad": None,
        "turn_detection": {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": silence_ms,
        },
        "noise_reduction_type": "near_field",
        "prompt": "English desktop commands, application and website names. Transcribe English speech without translating other languages. Preserve dictated text and URLs exactly.",
    }
    if selected.startswith(("gpt-transcribe", "gpt-live-transcribe")):
        options["language"] = ["en"]
        options["keywords"] = [
            s.strip() for s in os.environ.get("BRODI_STT_KEYWORDS", DEFAULT_KEYWORDS).split(",") if s.strip()
        ]
    if selected.startswith(("gpt-live-transcribe", "gpt-realtime-whisper")):
        options.pop("turn_detection")
        options["vad"] = SilenceEndpoint(
            silence_s=silence_ms / 1000,
            rms_threshold=float(os.environ.get("BRODI_STT_RMS_THRESHOLD", "160")),
        )
    return openai.STT(**options)
