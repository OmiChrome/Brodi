"""Wake speech must finish before the ready cue and the follow-up window."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from livekit.agents import Agent, AgentSession
from livekit.agents.voice import io
from test_voice_session import voice_entrypoint

from brodi.agent import DynamicMacAgent, Outcome
from brodi.microphone import chime_pcm, ready_chime_frames
from brodi.registry import ToolRegistry
from brodi.voice import WAKE_REPLY


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["darwin", "win32"])
@pytest.mark.parametrize("phrase", ["Hey bro wake up", "Hey Brodi, wake up!"])
async def test_wake_speech_then_beep_then_full_command_window(monkeypatch, tmp_path, platform, phrase):
    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("BRODI_OS", platform)
    monkeypatch.setenv("BRODI_WAKE_REQUIRED", "true")
    monkeypatch.setenv("BRODI_WAKE_WINDOW_S", "20")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    core = DynamicMacAgent(ToolRegistry(learned_path=tmp_path / "tools.json"), enable_learning=False)
    core.context.latest = AsyncMock(side_effect=AssertionError("Wake must not inspect the desktop"))
    core.router.client.evaluate = AsyncMock(side_effect=AssertionError("Wake must not call the router API"))
    agent = entrypoint.BrodiAgent(core)
    started, speech_finished, beep_started, beep_finished = (asyncio.Event() for _ in range(4))
    calls = []

    async def say(text, **options):
        calls.append(text)
        assert options["allow_interruptions"] is False
        assert options["add_to_chat_ctx"] is False
        if text:
            assert text == WAKE_REPLY
            assert "audio" not in options
            started.set()
            await speech_finished.wait()
        else:
            assert speech_finished.is_set()
            frames = [frame async for frame in options["audio"]]
            assert b"".join(bytes(frame.data) for frame in frames) == chime_pcm(True)
            assert sum(frame.duration for frame in frames) == pytest.approx(0.18)
            assert all(frame.sample_rate == 24000 and frame.num_channels == 1 for frame in frames)
            beep_started.set()
            await beep_finished.wait()
            # Simulate the earlier window having elapsed during slow speech.
            agent.activation.active_until = 0

    session = SimpleNamespace(say=AsyncMock(side_effect=say))
    monkeypatch.setattr(entrypoint.BrodiAgent, "session", property(lambda self: session))

    async def wake():
        with pytest.raises(entrypoint.StopResponse):
            await agent.on_user_turn_completed(None, SimpleNamespace(text_content=phrase))

    task = asyncio.create_task(wake())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert calls == [WAKE_REPLY]  # Speech is queued/playing; no beep yet.
        speech_finished.set()
        await asyncio.wait_for(beep_started.wait(), timeout=2)
        assert not task.done()
        beep_finished.set()
        await asyncio.wait_for(task, timeout=2)
        assert calls == [WAKE_REPLY, ""]
        assert 19 < agent.activation.active_until - time.monotonic() <= 20
        assert agent.activation.accepts("open YouTube")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await core.aclose()


@pytest.mark.asyncio
async def test_no_ready_beep_when_wake_speech_cannot_start(monkeypatch):
    entrypoint = voice_entrypoint(monkeypatch)
    core = SimpleNamespace(
        pending=None, pending_question=None, handle=AsyncMock(return_value=Outcome(speak="Hey bro, I'm listening."))
    )
    agent = entrypoint.BrodiAgent(core)
    session = SimpleNamespace(say=AsyncMock(side_effect=RuntimeError("session closing")))
    monkeypatch.setattr(entrypoint.BrodiAgent, "session", property(lambda self: session))
    with pytest.raises(entrypoint.StopResponse):
        await agent.on_user_turn_completed(None, SimpleNamespace(text_content="Hey bro wake up"))
    session.say.assert_awaited_once_with(WAKE_REPLY, add_to_chat_ctx=False, allow_interruptions=False)


@pytest.mark.asyncio
async def test_ready_beep_reaches_session_output_and_waits_for_playout_without_tts():
    class Output(io.AudioOutput):
        def __init__(self):
            super().__init__(label="test speaker", capabilities=io.AudioOutputCapabilities(pause=False))
            self.frames = []
            self.flushed = asyncio.Event()

        async def capture_frame(self, frame):
            await super().capture_frame(frame)
            self.frames.append(frame)

        def flush(self):
            super().flush()
            self.flushed.set()

        def clear_buffer(self):
            self.on_playback_finished(playback_position=0, interrupted=True)

    session = AgentSession()
    output = Output()
    session.output.audio = output
    await session.start(agent=Agent(instructions="Offline beep check"))
    try:
        handle = session.say("", audio=ready_chime_frames(), add_to_chat_ctx=False, allow_interruptions=False)
        await asyncio.wait_for(output.flushed.wait(), timeout=2)
        assert b"".join(bytes(frame.data) for frame in output.frames) == chime_pcm(True)
        assert not handle.done()  # Queuing the beep is not the same as hearing it finish.
        output.on_playback_finished(playback_position=0.18, interrupted=False)
        await asyncio.wait_for(handle.wait_for_playout(), timeout=2)
    finally:
        await session.aclose()
