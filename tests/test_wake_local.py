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
