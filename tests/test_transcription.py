import asyncio

import pytest
from livekit import rtc
from livekit.agents import vad
from livekit.plugins.openai.stt import _session_update

from brodi.endpointing import SilenceEndpoint
from brodi.transcription import create_stt


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    for name in (
        "BRODI_STT_MODEL",
        "BRODI_STT_LANGUAGES",
        "BRODI_STT_KEYWORDS",
        "BRODI_STT_SILENCE_MS",
        "BRODI_STT_RMS_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)


def test_fast_cloud_default_sends_supported_schema_without_server_vad():
    async def scenario():
        engine = create_stt()
        try:
            payload = _session_update(engine._opts).model_dump(exclude_none=True)
            audio = payload["session"]["audio"]["input"]
            assert audio["transcription"]["model"] == "gpt-live-transcribe"
            assert audio["transcription"]["languages"] == ["en"]
            assert "Brodi" in audio["transcription"]["keywords"]
            assert "turn_detection" not in audio
            assert isinstance(engine._vad, SilenceEndpoint)
            assert engine.capabilities.streaming
        finally:
            await engine.aclose()

    asyncio.run(scenario())


def test_accuracy_cloud_model_uses_cloud_endpointing_and_configurable_hints(monkeypatch):
    monkeypatch.setenv("BRODI_STT_MODEL", "gpt-transcribe")
    monkeypatch.setenv("BRODI_STT_LANGUAGES", "en,hi")
    monkeypatch.setenv("BRODI_STT_KEYWORDS", "Brodi,GitHub")
    monkeypatch.setenv("BRODI_STT_SILENCE_MS", "400")

    async def scenario():
        engine = create_stt()
        try:
            audio = _session_update(engine._opts).model_dump(exclude_none=True)["session"]["audio"]["input"]
            assert audio["transcription"]["model"] == "gpt-transcribe"
            assert audio["transcription"]["languages"] == ["en"]
            assert audio["transcription"]["keywords"] == ["Brodi", "GitHub"]
            assert audio["turn_detection"]["silence_duration_ms"] == 400
            assert engine._vad is None
        finally:
            await engine.aclose()

    asyncio.run(scenario())


def test_legacy_language_settings_cannot_reenable_multilingual_recognition(monkeypatch):
    monkeypatch.setenv("BRODI_LANG", "hi")
    monkeypatch.setenv("BRODI_STT_LANGUAGES", "hi,en")

    async def scenario():
        engine = create_stt()
        try:
            audio = _session_update(engine._opts).model_dump(exclude_none=True)["session"]["audio"]["input"]
            assert audio["transcription"]["languages"] == ["en"]
            assert "English" in audio["transcription"]["prompt"]
        finally:
            await engine.aclose()

    asyncio.run(scenario())


def test_explicit_non_english_language_is_unsupported():
    with pytest.raises(ValueError, match="English only"):
        create_stt(language="hi")


@pytest.mark.parametrize(
    "setting,value",
    [("BRODI_STT_SILENCE_MS", "10"), ("BRODI_STT_SILENCE_MS", "9999"), ("BRODI_STT_RMS_THRESHOLD", "nan")],
)
def test_invalid_endpoint_configuration_fails_clearly(monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError):
        create_stt()


def test_silence_endpoints_once_after_pause_ignores_clicks_and_resets_on_flush():
    async def scenario():
        stream = SilenceEndpoint(silence_s=0.25).stream()

        def send(count, loud):
            data = (b"\xe8\x03" if loud else b"\x00\x00") * 480
            for _ in range(count):
                stream.push_frame(rtc.AudioFrame(data, 24000, 1, 480))

        send(20, False)
        send(1, True)  # A single transient is not a speech turn.
        send(20, False)
        send(10, True)
        send(5, False)  # A 100 ms pause inside speech must not commit.
        send(10, True)
        send(15, False)
        send(20, False)  # Continued silence must not repeatedly commit.
        send(10, True)
        stream.flush()
        send(20, False)
        stream.end_input()
        try:
            events = [e async for e in stream]
            assert [e.type for e in events] == [vad.VADEventType.START_OF_SPEECH, vad.VADEventType.END_OF_SPEECH] * 2
            assert 0.25 <= events[1].silence_duration <= 0.27
            assert events[1].speech_duration >= 0.49
        finally:
            await stream.aclose()

    asyncio.run(scenario())
