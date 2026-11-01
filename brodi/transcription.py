"""Current cloud transcription with fast, model-compatible command endpointing."""

import os

from livekit.plugins import openai

from .endpointing import SilenceEndpoint

DEFAULT_MODEL = "gpt-live-transcribe"
DEFAULT_KEYWORDS = "Brodi,Chrome,Notepad,GitHub,YouTube,Google Keep,Slack"


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
