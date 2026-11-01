import asyncio

import pytest

from brodi.wake import WakeController


@pytest.mark.parametrize("text", ["hey bro open chrome", "Yo bro, open chrome", "hey bro please open chrome"])
def test_remainder_reaches_command_callback(text):
    commands = []
    controller = WakeController(commands.append)
    controller.feed_text(text)
    assert commands == ["open chrome"]


def test_partial_wake_does_not_execute_partial_command_twice():
    commands = []
    controller = WakeController(commands.append)
    controller.feed_text("hey bro", final=False)
    controller.feed_text("hey bro open chrome", final=False)
    assert commands == []
    controller.feed_text("hey bro open chrome", final=True)
    assert commands == ["open chrome"]
    assert controller.stats.detections == 1


def test_hotkey_mode_ignores_spoken_wake_until_hotkey():
    commands = []
    controller = WakeController(commands.append, mode="hotkey")
    controller.feed_text("hey bro open chrome")
    assert not commands
    controller.activate()
    controller.feed_text("open chrome")
    assert commands == ["open chrome"]


def test_inactive_background_speech_never_reaches_command_callback():
    commands = []
    controller = WakeController(commands.append)
    controller.feed_text("open chrome")
    assert commands == []


def test_false_alarm_counters_are_user_reported_and_bounded():
    controller = WakeController(lambda text: None)
    controller.feed_text("hey bro")
    controller.mark_false_positive()
    controller.mark_false_positive()
    assert controller.stats.false_positive_rate == 1
    assert controller.stats.false_positives == 1


def test_wake_command_reaches_existing_router_without_prefix(tmp_path, monkeypatch):
    from brodi.agent import DynamicMacAgent
    from brodi.applescript import MacContext
    from brodi.registry import ToolRegistry
    from brodi.router import Route

    agent = DynamicMacAgent(ToolRegistry(learned_path=tmp_path / "learned.json"), enable_learning=False)
    routed = []

    async def route(text, *args, **kwargs):
        routed.append(text)
        return Route("chat")

    async def context():
        return MacContext("Explorer", [], [])

    monkeypatch.setattr(agent.router, "route", route)
    monkeypatch.setattr(agent.context, "latest", context)
    controller = WakeController(lambda text: asyncio.run(agent.handle(text)))
    controller.feed_text("hey bro open chrome")
    assert routed == ["open chrome"]


@pytest.mark.parametrize("tts", [False, True])
def test_local_runtime_does_not_create_a_cloud_stt_session(tmp_path, monkeypatch, tts):
    import openai
    import sounddevice

    from brodi import wake_runtime
    from brodi.activity import ActivityLog
    from brodi.agent import Outcome

    received = []
    closed = []
    playback = []

    class FakeAgent:
        pending = pending_question = None

        def __init__(self, **kwargs):
            pass

        async def start(self):
            pass

        async def handle(self, text):
            received.append(text)
            return Outcome(executed=True, speak="Got it.", stop=True)

        async def aclose(self):
            closed.append("agent")

    class OfflineListener:
        running = True
        engine = "offline test"

        def __init__(self, controller):
            self.controller = controller

        def start(self):
            self.controller.feed_text("hey bro open chrome")

        def stop(self):
            closed.append("listener")

        def suspend_for_playback(self):
            playback.append("suspended")

        def resume_after_playback(self):
            playback.append("resumed")

    class SpeechResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def read(self):
            return bytes(480)

    class NoRequests:
        def __init__(self, **kwargs):
            if tts:
                from types import SimpleNamespace

                self.audio = SimpleNamespace(
                    speech=SimpleNamespace(
                        with_streaming_response=SimpleNamespace(create=lambda **kw: SpeechResponse())
                    )
                )

        def __getattr__(self, name):
            raise AssertionError(f"No cloud request expected: {name}")

        async def close(self):
            closed.append("client")

    monkeypatch.setenv("BRODI_TTS_ENABLED", "1" if tts else "0")
    monkeypatch.setattr(openai, "AsyncOpenAI", NoRequests)
    monkeypatch.setattr(sounddevice, "stop", lambda: None)
    monkeypatch.setattr(sounddevice, "play", lambda *args, **kwargs: playback.append("played"))
    monkeypatch.setattr(wake_runtime, "LocalListener", OfflineListener)
    monkeypatch.setattr(wake_runtime, "DynamicMacAgent", FakeAgent)
    monkeypatch.setattr(wake_runtime, "ActivityLog", lambda: ActivityLog(tmp_path / "events"))
    asyncio.run(wake_runtime.run())
    assert received == ["open chrome"]
    assert set(closed) == {"agent", "listener", "client"}
    assert playback == (["suspended", "played", "resumed"] if tts else [])


def test_local_listener_discards_playback_echo_and_resets_before_listening():
    from brodi.wake import LocalListener

    listener = LocalListener(WakeController(lambda _: None))
    listener.suspend_for_playback()
    assert listener._playback.is_set() and listener._reset_requested.is_set()
    listener._reset_requested.clear()
    listener.resume_after_playback()
    assert not listener._playback.is_set() and listener._reset_requested.is_set()
