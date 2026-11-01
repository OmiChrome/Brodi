"""Validate the actual installed session configuration without audio or network calls."""

import asyncio
import importlib

from brodi import config


def voice_entrypoint(monkeypatch):
    # Importing the runnable entrypoint must not load the user's real key into offline tests.
    monkeypatch.setattr(config, "load_environment", lambda: None)
    return importlib.import_module("agent")


def test_pipeline_uses_local_turns_and_waits_for_router_before_generation(monkeypatch):
    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(entrypoint, "VOICE_MODE", "pipeline")

    async def check():
        session = entrypoint.build_session()
        assert session.turn_detection == "vad"
        assert session.interruption_detection == "vad"
        assert session.options.preemptive_generation["enabled"] is False
        await session.aclose()

    asyncio.run(check())


def test_realtime_waits_for_router_before_generation(monkeypatch):
    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(entrypoint, "VOICE_MODE", "realtime")

    async def check():
        session = entrypoint.build_session()
        assert session.turn_detection == "realtime_llm"
        assert session.options.preemptive_generation["enabled"] is False
        assert session.vad is None
        await session.aclose()

    asyncio.run(check())
