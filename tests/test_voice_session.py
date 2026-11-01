"""Validate the actual installed session configuration without audio or network calls."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
        assert session.tts._opts.speed == 1.25
        assert "natural" in session.tts._opts.instructions.lower()
        await session.aclose()

    asyncio.run(check())


def test_realtime_waits_for_router_before_generation(monkeypatch):
    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(entrypoint, "VOICE_MODE", "realtime")
    monkeypatch.setenv("BRODI_TTS_SPEED", "1.25")

    async def check():
        session = entrypoint.build_session()
        assert session.turn_detection == "realtime_llm"
        assert session.options.preemptive_generation["enabled"] is False
        assert session.vad is None
        assert session.llm._opts.speed == 1.25
        await session.aclose()

    asyncio.run(check())


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
