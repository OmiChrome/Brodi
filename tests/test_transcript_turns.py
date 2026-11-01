"""Delayed provider transcripts must still reach the real voice hook exactly once."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit.agents import AgentSession, stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from livekit.agents.voice import io
from test_voice_session import voice_entrypoint

from brodi.agent import Outcome
from brodi.microphone import MicrophoneControl
from brodi.transcription import complete_transcript_turns


class Source(io.AudioInput):
    def __init__(self):
        super().__init__(label="test audio")
        self.frames = asyncio.Queue()

    async def __anext__(self):
        return await self.frames.get()


class ScriptedSTT(stt.STT):
    def __init__(self):
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=False))
        self.events = asyncio.Queue()

    async def _recognize_impl(self, *args, **kwargs):
        raise AssertionError("No network transcription")

    def stream(self, **kwargs):
        owner = self

        class Stream(stt.RecognizeStream):
            async def _run(self):
                while True:
                    self._event_ch.send_nowait(await owner.events.get())

        return Stream(stt=self, conn_options=DEFAULT_API_CONNECT_OPTIONS)

    def emit(self, kind, text="", **kwargs):
        self.events.put_nowait(
            stt.SpeechEvent(
                kind,
                alternatives=[stt.SpeechData(language="en", text=text)] if text else [],
                **kwargs,
            )
        )


@pytest.mark.asyncio
async def test_delayed_text_does_not_end_new_speech_in_progress():
    kind = stt.SpeechEventType
    events = [
        stt.SpeechEvent(kind.START_OF_SPEECH, speech_start_time=10),
        stt.SpeechEvent(kind.END_OF_SPEECH, speech_end_time=11),
        stt.SpeechEvent(kind.START_OF_SPEECH, speech_start_time=12),
        stt.SpeechEvent(kind.FINAL_TRANSCRIPT, alternatives=[stt.SpeechData(language="en", text="Hey bro")]),
        stt.SpeechEvent(kind.END_OF_SPEECH, speech_end_time=13),
    ]

    async def source():
        for event in events:
            yield event

    assert [event async for event in complete_transcript_turns(source())] == events


@pytest.mark.asyncio
async def test_late_transcript_keeps_original_timing_and_provider_identity():
    kind = stt.SpeechEventType
    final = stt.SpeechEvent(
        kind.FINAL_TRANSCRIPT,
        request_id="original-request",
        alternatives=[stt.SpeechData(language="en", text="Hey bro, open YouTube")],
        created_at=20,
    )

    async def source():
        yield stt.SpeechEvent(kind.START_OF_SPEECH, speech_start_time=10)
        yield stt.SpeechEvent(kind.END_OF_SPEECH, speech_end_time=11)
        yield final

    events = [event async for event in complete_transcript_turns(source())]
    assert events[-3].speech_start_time == 10
    assert events[-2] is final
    assert events[-1].speech_end_time == 11
    assert sum(event.type == kind.FINAL_TRANSCRIPT for event in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed_after_speech", [False, True])
async def test_late_final_after_previous_turn_committed(monkeypatch, resumed_after_speech):
    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("BRODI_WAKE_REQUIRED", "true")
    provider = ScriptedSTT()
    session = AgentSession(
        stt=provider,
        vad=None,
        turn_handling={
            "turn_detection": "stt",
            "endpointing": {"min_delay": 0, "max_delay": 0},
            "interruption": {"enabled": False},
            "preemptive_generation": {"enabled": False},
        },
    )
    session.input.audio = Source()
    core = SimpleNamespace(pending=None, pending_question=None, handle=AsyncMock(return_value=Outcome()))
    mic = MicrophoneControl(session, Mock(), mailbox_factory=Mock)
    mic.listening = True
    mic.accept_after = time.time() - 10
    agent = entrypoint.BrodiAgent(core, mic)
    await session.start(agent=agent)
    await asyncio.sleep(0.05)
    kind = stt.SpeechEventType
    try:
        provider.emit(kind.START_OF_SPEECH)
        provider.emit(kind.FINAL_TRANSCRIPT, "background conversation")
        # The next segment's end arrives while the previous transcript is queued.
        onset = time.time()
        provider.emit(kind.START_OF_SPEECH, speech_start_time=onset)
        provider.emit(kind.END_OF_SPEECH)
        await asyncio.sleep(0.15)  # The previous non-wake turn is committed and ignored.
        core.handle.assert_not_called()
        if resumed_after_speech:
            mic.accept_after = time.time()  # A mute/unmute boundary invalidates the old segment.
        provider.emit(kind.FINAL_TRANSCRIPT, "Hey bro, open YouTube", request_id="youtube-turn")
        await asyncio.sleep(0.2)
        if resumed_after_speech:
            core.handle.assert_not_called()
        else:
            core.handle.assert_awaited_once_with("Hey bro, open YouTube")
        # An extra provider end marker must not execute the command again.
        provider.emit(kind.END_OF_SPEECH)
        await asyncio.sleep(0.1)
        assert core.handle.await_count == int(not resumed_after_speech)
    finally:
        await session.aclose()
        await provider.aclose()
