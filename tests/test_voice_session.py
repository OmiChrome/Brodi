"""Validate the actual installed session configuration without audio or network calls."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from brodi import config


def voice_entrypoint(monkeypatch):
    # Importing the runnable entrypoint must not load the user's real key into offline tests.
    monkeypatch.setattr(config, "load_environment", lambda: None)
    return importlib.import_module("agent")


def test_pipeline_uses_openai_turns_without_local_models(monkeypatch):
    entrypoint = voice_entrypoint(monkeypatch)
    from livekit.agents import inference

    def no_local_model(*args, **kwargs):
        raise AssertionError("OpenAI voice must not load a local VAD model")

    monkeypatch.setattr(inference, "VAD", no_local_model)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(entrypoint, "VOICE_MODE", "pipeline")
    monkeypatch.setattr(entrypoint, "STT_MODEL", "gpt-live-transcribe")
    monkeypatch.setenv("BRODI_TTS_SPEED", "1.25")

    async def check():
        session = entrypoint.build_session()
        assert session.turn_detection == "stt"
        assert session.vad is None
        assert session.stt.capabilities.streaming
        assert session.stt._opts.turn_detection.type == "server_vad"
        assert session.stt._opts.model == "gpt-live-transcribe"
        assert "Brodi" in session.stt._opts.keywords
        assert session.stt._vad.silence_s == 0.25
        assert session.options.preemptive_generation["enabled"] is False
        assert session.llm is None
        assert session.tts._opts.speed == 1.25
        assert "natural" in session.tts._opts.instructions.lower()
        await session.aclose()

    asyncio.run(check())


def test_conversational_realtime_mode_is_no_longer_supported(monkeypatch):
    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(entrypoint, "VOICE_MODE", "realtime")
    with pytest.raises(ValueError, match="command-only"):
        entrypoint.build_session()


def test_openai_voice_accepts_a_task_without_wake_phrase(monkeypatch):
    from brodi.agent import Outcome

    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.delenv("BRODI_WAKE_REQUIRED", raising=False)
    core = SimpleNamespace(
        pending=None, pending_question=None, handle=AsyncMock(return_value=Outcome(executed=True, speak="Got it."))
    )
    agent = entrypoint.BrodiAgent(core)
    speech = SimpleNamespace(say=AsyncMock())
    monkeypatch.setattr(entrypoint.BrodiAgent, "session", property(lambda self: speech))

    async def check():
        with pytest.raises(entrypoint.StopResponse):
            await agent.on_user_turn_completed(None, SimpleNamespace(text_content="open Chrome"))
        core.handle.assert_awaited_once_with("open Chrome")
        speech.say.assert_awaited_once()

    asyncio.run(check())


def test_cloud_start_does_not_need_local_wake_model(tmp_path, monkeypatch):
    from brodi import tray

    controller = tray.AgentController(tmp_path)
    monkeypatch.setattr(controller, "processes", list)
    monkeypatch.delenv("BRODI_LOCAL_WAKE", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setenv("BRODI_WAKE_MODEL_PATH", str(tmp_path / "missing-offline-model"))
    controller.log_path = tmp_path / "voice.log"
    launches = []
    monkeypatch.setattr(tray.subprocess, "Popen", lambda *args, **kwargs: launches.append(args))
    controller.start()
    assert len(launches) == 1


def test_background_wake_preserves_combined_command_and_expires(monkeypatch):
    from brodi import voice
    from brodi.agent import Outcome

    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("BRODI_WAKE_REQUIRED", "true")
    clock = [100.0]
    monkeypatch.setattr(voice.time, "monotonic", lambda: clock[0])
    core = SimpleNamespace(
        pending=None, pending_question=None, handle=AsyncMock(return_value=Outcome(executed=True, speak="Got it."))
    )
    agent = entrypoint.BrodiAgent(core)
    monkeypatch.setattr(entrypoint.BrodiAgent, "session", property(lambda self: SimpleNamespace(say=AsyncMock())))

    async def turn(text):
        with pytest.raises(entrypoint.StopResponse):
            await agent.on_user_turn_completed(None, SimpleNamespace(text_content=text))

    async def check():
        await turn("open GitHub")
        core.handle.assert_not_called()
        await turn("Hey bro, open GitHub")
        core.handle.assert_awaited_once_with("Hey bro, open GitHub")
        await turn("open Chrome")  # Brief follow-up after wake is allowed.
        assert core.handle.await_count == 2
        clock[0] += 21
        await turn("open Calculator")
        assert core.handle.await_count == 2
        await turn("Hey bro, open Calculator")
        assert core.handle.await_count == 3

    asyncio.run(check())


def test_spoken_stop_stops_supervisor_instead_of_auto_restarting(monkeypatch):
    from brodi import background
    from brodi.agent import Outcome

    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("BRODI_BACKGROUND", "true")
    monkeypatch.setenv("BRODI_WAKE_REQUIRED", "true")
    events = []
    monkeypatch.setattr(background, "request_stop", lambda: events.append("stop-supervisor"))
    monkeypatch.setattr(
        entrypoint, "get_job_context", lambda: SimpleNamespace(shutdown=lambda **kw: events.append("shutdown"))
    )
    core = SimpleNamespace(pending=None, pending_question=None, handle=AsyncMock(return_value=Outcome(stop=True)))
    agent = entrypoint.BrodiAgent(core)
    monkeypatch.setattr(entrypoint.BrodiAgent, "session", property(lambda self: SimpleNamespace(say=AsyncMock())))

    async def check():
        with pytest.raises(entrypoint.StopResponse):
            await agent.on_user_turn_completed(None, SimpleNamespace(text_content="Hey bro, stop listening"))
        assert events == ["stop-supervisor", "shutdown"]

    asyncio.run(check())


@pytest.mark.parametrize("background", [True, False])
def test_closed_background_session_releases_worker_for_recovery(monkeypatch, background):
    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("BRODI_BACKGROUND", str(background).lower())
    monkeypatch.setenv("BRODI_STARTUP_GREETING", "false")
    callbacks = {}

    def register(name):
        def decorator(callback):
            callbacks[name] = callback
            return callback

        return decorator

    core = Mock(start=AsyncMock(), aclose=AsyncMock())
    session = SimpleNamespace(on=register, start=AsyncMock())
    monkeypatch.setattr(entrypoint, "DynamicMacAgent", lambda **kw: core)
    monkeypatch.setattr(entrypoint, "build_session", lambda: session)
    monkeypatch.setattr(entrypoint, "ActivityLog", Mock)
    ctx = Mock(room=object())
    asyncio.run(entrypoint.entrypoint(ctx))
    callbacks["close"](SimpleNamespace())
    assert ctx.shutdown.call_count == int(background)


@pytest.mark.parametrize("ending", ["close", "shutdown", "start_failure"])
def test_console_audio_cleanup_on_every_session_exit(monkeypatch, ending):
    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("BRODI_BACKGROUND", "true")
    monkeypatch.setenv("BRODI_STARTUP_GREETING", "false")
    events = []
    callbacks = {}

    def register(name):
        def decorator(callback):
            callbacks[name] = callback
            return callback

        return decorator

    core = Mock(start=AsyncMock(), aclose=AsyncMock(side_effect=lambda: events.append("core-close")))
    session = SimpleNamespace(on=register, start=AsyncMock())
    if ending == "start_failure":
        session.start.side_effect = RuntimeError("startup failed after audio acquisition")
    monkeypatch.setattr(entrypoint, "DynamicMacAgent", lambda **kw: core)
    monkeypatch.setattr(entrypoint, "build_session", lambda: session)
    monkeypatch.setattr(entrypoint, "ActivityLog", Mock)
    cleanup = Mock(side_effect=lambda sess: events.append("audio-close"))
    monkeypatch.setattr(entrypoint, "close_console_audio", cleanup)
    ctx = Mock(room=object(), shutdown=Mock(side_effect=lambda **kw: events.append("shutdown")))

    async def check():
        if ending == "start_failure":
            with pytest.raises(RuntimeError, match="startup failed"):
                await entrypoint.entrypoint(ctx)
        else:
            await entrypoint.entrypoint(ctx)
            if ending == "close":
                callbacks["close"](SimpleNamespace())
            else:
                await ctx.add_shutdown_callback.call_args.args[0]()

    asyncio.run(check())
    cleanup.assert_called_once_with(session)
    assert (
        events
        == {
            "close": ["audio-close", "shutdown"],
            "shutdown": ["audio-close", "core-close"],
            "start_failure": ["audio-close"],
        }[ending]
    )
