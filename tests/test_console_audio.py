"""Exercise the installed LiveKit callbacks without opening audio hardware."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from livekit.agents import AgentSession
from livekit.agents.cli import _legacy
from livekit.agents.cli.cli import AgentsConsole

from brodi.console_audio import close_console_audio


@pytest.mark.parametrize("device_failure", [False, True])
def test_session_audio_is_released_before_loop_closes(monkeypatch, device_failure):
    console = _legacy.AgentsConsole()
    monkeypatch.setattr(AgentsConsole, "_instance", console)
    loop = asyncio.new_event_loop()
    closed = []

    def close_device(name):
        assert not loop.is_closed()
        assert not console.io_acquired  # Also proves close runs outside the lock.
        closed.append(name)
        if device_failure and name == "microphone":
            raise RuntimeError("device disconnected")

    console._input_stream = Mock(close=lambda: close_device("microphone"))
    console._output_stream = Mock(close=lambda: close_device("speaker"))

    async def run_session():
        session = AgentSession()
        console.acquire_io(loop=loop, session=session)
        # Reproduce the original crash with the installed output implementation.
        samples = np.zeros((240, 1), dtype=np.int16)
        console._io_audio_output.read_into(samples, len(samples))
        close_console_audio(session)
        close_console_audio(session)  # Session-close and job-shutdown both call it.
        await console._io_transcription_sync.aclose()
        await session.aclose()

    try:
        loop.run_until_complete(run_session())
    finally:
        loop.close()

    assert closed == ["microphone", "speaker"]
    # Even a late callback (or disconnected device whose close failed) cannot
    # dispatch into the now-closed event loop. Output must be fully silenced.
    samples = np.full((240, 1), 100, dtype=np.int16)
    timing = SimpleNamespace(currentTime=0, inputBufferAdcTime=0, outputBufferDacTime=0)
    console._sd_output_callback(samples, len(samples), timing)
    assert not samples.any()
    console._sd_input_callback(samples, len(samples), timing)


def test_cleanup_does_not_close_another_sessions_devices(monkeypatch):
    console = _legacy.AgentsConsole()
    monkeypatch.setattr(AgentsConsole, "_instance", console)
    console._io_acquired = True
    console._io_session = object()
    console._input_stream = Mock()
    console._output_stream = Mock()

    close_console_audio(object())

    assert console.io_acquired
    console._input_stream.close.assert_not_called()
    console._output_stream.close.assert_not_called()


@pytest.mark.parametrize("console", [None, AgentsConsole()])
def test_room_and_tcp_sessions_have_no_local_devices(monkeypatch, console):
    monkeypatch.setattr(AgentsConsole, "_instance", console)
    close_console_audio(object())
