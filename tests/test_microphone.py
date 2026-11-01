"""Mute privacy boundary, cues, queued speech and same-session tray controls."""

import asyncio
import importlib
import os
import time
from array import array
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from livekit import rtc
from livekit.agents.voice import io

from brodi import config, mic_control
from brodi.activity import ActivityLog, JournalReader
from brodi.microphone import MicrophoneControl, chime_pcm


class Source(io.AudioInput):
    """Like TCP console input: continues producing even after on_detached."""

    def __init__(self):
        super().__init__(label="test microphone")
        self.frames = asyncio.Queue()

    async def __anext__(self):
        return await self.frames.get()


class Hotkey:
    def __init__(self, toggle, error):
        self.toggle, self.error = toggle, error
        self.closed = False

    async def start(self):
        pass

    async def aclose(self):
        self.closed = True


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_ACTIVITY_LOG", str(tmp_path / "activity.jsonl"))
    source = Source()
    input_io = io.AgentInput(lambda: None, lambda: None)
    input_io.audio = source
    session = SimpleNamespace(input=input_io, clear_user_turn=Mock(), output=SimpleNamespace(audio_enabled=True))
    cue = AsyncMock()
    activity = ActivityLog()
    control = MicrophoneControl(session, activity, cue=cue, hotkey_factory=Hotkey)
    return control, source, session, cue, activity


async def settle(control):
    await control.transition


@pytest.mark.asyncio
async def test_hotkey_blocks_input_before_cue_and_resumes_after_it(setup):
    control, source, session, cue, activity = setup
    control.attach()
    await control.start()
    try:
        assert control.listening and session.input.audio_enabled
        cue.assert_awaited_once_with(True)
        assert mic_control.read_state(os.getpid())["mic_muted"] is False

        control.hotkey.toggle()
        assert control.muted and not control.listening and not session.input.audio_enabled
        assert session.output.audio_enabled  # output and ongoing tasks are unaffected
        assert not control.accepts(SimpleNamespace(created_at=time.time()))
        await settle(control)
        assert cue.await_args.args == (False,)

        release_cue = asyncio.Event()
        cue.side_effect = lambda listening: None

        async def slow_cue(listening):
            await release_cue.wait()

        cue.side_effect = slow_cue
        control.hotkey.toggle()
        await asyncio.sleep(0)
        assert not control.listening and not session.input.audio_enabled
        release_cue.set()
        await settle(control)
        assert control.listening and session.input.audio_enabled
        assert session.clear_user_turn.call_count >= 5
        states = JournalReader(activity.path).poll()
        assert any(e["details"].get("mic_muted") is True for e in states)
        assert states[-1]["details"]["mic_muted"] is False
    finally:
        await control.aclose()
    assert control.hotkey.closed and not session.input.audio_enabled
    assert mic_control.read_state(os.getpid()) is None
    control.publish()  # late SDK shutdown state event cannot recreate a stale mailbox
    assert mic_control.read_state(os.getpid()) is None


@pytest.mark.asyncio
async def test_muting_during_startup_cue_does_not_cancel_session(setup):
    control, _, session, cue, _ = setup
    entered = asyncio.Event()

    async def slow_start(listening):
        if listening:
            entered.set()
            await asyncio.Event().wait()

    cue.side_effect = slow_start
    control.attach()
    starting = asyncio.create_task(control.start())
    await entered.wait()
    control.hotkey.toggle()
    try:
        await starting
        await settle(control)
        assert control.muted and not session.input.audio_enabled
    finally:
        await control.aclose()


@pytest.mark.asyncio
async def test_unwritable_tray_mailbox_does_not_disable_keyboard_mute(setup, monkeypatch):
    control, _, session, _, _ = setup
    monkeypatch.setattr(control.mailbox, "publish", Mock(side_effect=OSError("disk full")))
    control.attach()
    await control.start()
    try:
        assert control.listening
        control.hotkey.toggle()
        assert not control.listening and not session.input.audio_enabled
        await settle(control)
    finally:
        await control.aclose()


@pytest.mark.asyncio
async def test_tcp_audio_is_dropped_not_replayed_after_unmute(setup):
    control, source, session, _, _ = setup
    control.attach()
    await control.start()
    received = []

    async def consume():
        async for frame in session.input.audio:
            received.append(frame)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0)
        source.frames.put_nowait("first live frame")
        await asyncio.sleep(0)
        assert received == ["first live frame"]
        control.toggle()
        for _ in range(100):
            source.frames.put_nowait("private demo speech")
        await asyncio.sleep(0)
        assert source.frames.empty() and received == ["first live frame"]
        await settle(control)
        control.toggle()
        source.frames.put_nowait("cue echo")
        await asyncio.sleep(0)
        await settle(control)
        source.frames.put_nowait("cross-boundary frame")
        source.frames.put_nowait("fresh command")
        await asyncio.sleep(0)
        assert received == ["first live frame", "fresh command"]
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await control.aclose()


@pytest.mark.asyncio
async def test_rapid_toggle_never_reopens_mic_from_obsolete_cue(setup):
    control, _, session, cue, _ = setup
    control.attach()
    await control.start()
    try:
        for _ in range(21):
            control.toggle()
            await asyncio.sleep(0)
        await settle(control)
        assert control.muted and not control.listening and not session.input.audio_enabled
        assert cue.await_args.args == (False,)
        control.toggle()
        await settle(control)
        assert control.listening
    finally:
        await control.aclose()
    assert not control._transitions


@pytest.mark.asyncio
async def test_unavailable_shortcut_fails_closed_but_tray_can_control_mic(setup):
    control, _, session, _, _ = setup
    control.attach()

    class Unavailable(Hotkey):
        async def start(self):
            raise RuntimeError("Control+Space is in use")

    control.hotkey_factory = Unavailable
    await control.start()
    try:
        await settle(control)
        assert not control.listening and not session.input.audio_enabled
        assert mic_control.read_state(os.getpid())["hotkey_available"] is False
        mic_control.request_mute(os.getpid(), False)
        async with asyncio.timeout(2):
            while not control.listening:
                await asyncio.sleep(0.01)
        assert session.input.audio_enabled
    finally:
        await control.aclose()


@pytest.mark.asyncio
async def test_helper_death_mutes_immediately_and_sound_failure_preserves_control(setup):
    control, _, session, cue, _ = setup
    control.attach()
    await control.start()
    try:
        control.hotkey.error("helper exited")
        assert not control.listening and not session.input.audio_enabled
        await settle(control)
        cue.side_effect = RuntimeError("speaker unplugged")
        control.set_muted(False)
        await settle(control)
        assert control.listening
        control.set_muted(True)
        await settle(control)
        assert not control.listening
    finally:
        await control.aclose()


@pytest.mark.asyncio
async def test_muted_pending_answers_and_old_transcripts_never_reach_router(setup, monkeypatch):
    monkeypatch.setattr(config, "load_environment", lambda: None)
    entrypoint = importlib.import_module("agent")
    control, _, _, _, _ = setup
    core = SimpleNamespace(pending=SimpleNamespace(staged_at=time.time()), pending_question=None, handle=AsyncMock())
    agent = entrypoint.BrodiAgent(core, control)
    control.attach()
    await control.start()
    try:
        old = SimpleNamespace(text_content="yes", created_at=time.time())
        control.toggle()
        with pytest.raises(entrypoint.StopResponse):
            await agent.on_user_turn_completed(None, old)
        await settle(control)
        control.toggle()
        await settle(control)
        # A late result created now still belongs to an utterance before mute.
        late = SimpleNamespace(
            text_content="yes", created_at=time.time(), metrics={"started_speaking_at": old.created_at}
        )
        with pytest.raises(entrypoint.StopResponse):
            await agent.on_user_turn_completed(None, late)
        core.handle.assert_not_awaited()
    finally:
        await control.aclose()


def test_mailbox_ignores_stale_sessions_and_repeated_reads(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_ACTIVITY_LOG", str(tmp_path / "activity.jsonl"))
    first = mic_control.MicrophoneMailbox()
    first.publish(muted=False, available=True, hotkey_available=True)
    mic_control.request_mute(first.pid, True)
    assert first.poll() is True
    assert first.poll() is None
    restarted = mic_control.MicrophoneMailbox()
    assert restarted.poll() is None
    restarted.publish(muted=True, available=True, hotkey_available=True)
    mic_control.request_mute(first.pid, False)
    assert first.poll() is None
    assert restarted.poll() is False
    restarted.close()
    with pytest.raises(RuntimeError, match="not ready"):
        mic_control.request_mute(first.pid, True)


def test_cues_are_short_distinct_quiet_pcm_with_smooth_edges():
    listening, muted = chime_pcm(True), chime_pcm(False)
    assert listening != muted
    for pcm in (listening, muted):
        assert len(pcm) == 24000 * 2 * 0.18
        samples = array("h", pcm)
        assert samples[0] == samples[-1] == 0
        assert 0 < max(abs(value) for value in samples) <= 4500


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["darwin", "win32"])
async def test_end_to_end_audio_mute_router_and_resume(setup, monkeypatch, tmp_path, platform):
    """Real LiveKit session and Brodi router; scripted STT and OS boundary only."""
    from livekit.agents import AgentSession, stt
    from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

    from brodi import agent as core_module
    from brodi.agent import DynamicMacAgent
    from brodi.applescript import MacContext, ScriptResult
    from brodi.registry import ToolRegistry

    monkeypatch.setattr(config, "load_environment", lambda: None)
    monkeypatch.setenv("BRODI_OS", platform)
    monkeypatch.setenv("BRODI_FAST_LAUNCH", "0")
    monkeypatch.setenv("BRODI_MEMORY_ENABLED", "false")
    monkeypatch.setenv("BRODI_WAKE_REQUIRED", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    entrypoint = importlib.import_module("agent")
    provider_frames, scripts = [], []

    class ScriptedSTT(stt.STT):
        def __init__(self):
            super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=False))

        async def _recognize_impl(self, *args, **kwargs):
            raise AssertionError("No batch/network transcription")

        def stream(self, **kwargs):
            return Stream(stt=self, conn_options=DEFAULT_API_CONNECT_OPTIONS)

    class Stream(stt.RecognizeStream):
        async def _run(self):
            async for frame in self._input_ch:
                if not isinstance(frame, rtc.AudioFrame):
                    continue
                marker = frame.data[0]
                provider_frames.append(marker)
                if not marker:
                    continue
                self._event_ch.send_nowait(stt.SpeechEvent(stt.SpeechEventType.START_OF_SPEECH))
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        stt.SpeechEventType.FINAL_TRANSCRIPT,
                        alternatives=[stt.SpeechData(language="en", text="open Chrome")],
                    )
                )
                self._event_ch.send_nowait(stt.SpeechEvent(stt.SpeechEventType.END_OF_SPEECH))

    core = DynamicMacAgent(ToolRegistry(learned_path=tmp_path / "learned.json"), enable_learning=False)
    core.context.latest = AsyncMock(return_value=MacContext("Finder", [], ["Google Chrome"]))

    async def execute(script):
        scripts.append(script)
        return ScriptResult(True, "")

    monkeypatch.setattr(core_module, "run_applescript", execute)
    core.router.client.evaluate = AsyncMock(side_effect=AssertionError("No routing API expected"))
    session = AgentSession(
        stt=ScriptedSTT(), turn_handling={"turn_detection": "stt", "endpointing": {"min_delay": 0, "max_delay": 0}}
    )
    source = Source()
    session.input.audio = source
    cues = AsyncMock()
    control = MicrophoneControl(session, ActivityLog(), cue=cues, hotkey_factory=Hotkey)
    await session.start(agent=entrypoint.BrodiAgent(core, control))
    control.attach()
    await control.start()

    async def feed(marker):
        # Drain the source read begun before the resume boundary, then deliver the command.
        source.frames.put_nowait(
            rtc.AudioFrame(data=bytes(480), sample_rate=24000, num_channels=1, samples_per_channel=240)
        )
        source.frames.put_nowait(
            rtc.AudioFrame(
                data=array("h", [marker] * 240).tobytes(), sample_rate=24000, num_channels=1, samples_per_channel=240
            )
        )
        await asyncio.sleep(0.1)

    async def wait_actions(count):
        async with asyncio.timeout(3):
            while len(scripts) < count:
                await asyncio.sleep(0.01)

    try:
        await feed(1)
        await wait_actions(1)
        control.hotkey.toggle()
        await settle(control)
        await feed(2)
        await asyncio.sleep(0.15)
        assert 2 not in provider_frames and len(scripts) == 1
        mic_control.request_mute(os.getpid(), False)
        async with asyncio.timeout(2):
            while not control.listening:
                await asyncio.sleep(0.01)
        await feed(3)
        await wait_actions(2)
        assert 3 in provider_frames and len(scripts) == 2
        assert [call.args[0] for call in cues.await_args_list] == [True, False, True]
        assert all("Google Chrome" in script for script in scripts)
    finally:
        await control.aclose()
        await session.aclose()
        await core.aclose()
