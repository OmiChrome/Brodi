"""The audio boundary permits only brief English command feedback."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_voice_session import voice_entrypoint

from brodi.agent import Outcome
from brodi.router import Route
from brodi.voice import english_script, spoken_reply


@pytest.mark.parametrize(
    "outcome,options,expected",
    [
        (Outcome(speak="Hey bro, I'm listening."), {}, "Listening."),
        (Outcome(speak="Done with every requested step.", executed=True), {}, "Got it."),
        (Outcome(speak="That didn't work. Please try again."), {}, "Didn't work."),
        (Outcome(speak="Cancelled."), {}, "Cancelled."),
        (Outcome(speak="That confirmation expired. Tell me the command again."), {}, "Please repeat."),
        (Outcome(speak="That step timed out. Check the app before retrying."), {}, "Timed out."),
        (Outcome(speak="That action is blocked by the safety policy."), {}, "Action blocked."),
        (Outcome(speak="Delete the chosen file?", executed=False), {"confirmation": True}, "Review action."),
        (Outcome(speak="What should I search for on YouTube?"), {"missing": "query"}, "Search terms?"),
        (Outcome(speak="Which app should I use?"), {"missing": "app"}, "Which app?"),
        (Outcome(speak="Where are you flying from?"), {"missing": "origin"}, "Which origin?"),
        (Outcome(speak="Which exact date should I use?"), {"missing": "exact_dates"}, "Which date?"),
        (Outcome(speak="I got partway but couldn't verify the result."), {}, "Didn't work."),
        (Outcome(speak="An arbitrary long note or tool return value."), {}, "Check activity."),
        (Outcome(speak="नमस्ते दोस्त"), {}, "Check activity."),
        (Outcome(speak="Okay, stopping. Goodbye.", stop=True), {}, "Goodbye."),
        (Outcome(handoff_to_llm=True), {}, "Commands only."),
        (Outcome(route=Route("chat"), speak="Let's have a lengthy conversation."), {}, "Commands only."),
        (Outcome(), {}, None),
    ],
)
def test_spoken_feedback_preserves_state_without_reading_arbitrary_text(outcome, options, expected):
    detailed = outcome.speak
    reply = spoken_reply(outcome, **options)
    assert reply == expected
    assert outcome.speak == detailed  # Full result/approval details remain available to the activity UI.
    if reply:
        assert len(reply.split()) < 3
        assert reply.isascii()


@pytest.mark.parametrize("text", ["Hey bro, open GitHub", "Open Beyoncé's playlist", "Calculate 500 × 450"])
def test_english_commands_allow_names_and_math(text):
    assert english_script(text)


@pytest.mark.parametrize("text", ["Hey bro, गिटहब खोलो", "नमस्ते", "打开浏览器"])
def test_non_latin_transcripts_are_not_routed(text):
    assert not english_script(text)


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (Outcome(handoff_to_llm=True), "Commands only."),
        (Outcome(speak="Here is a very long response from an action.", executed=True), "Got it."),
        (Outcome(speak="That didn't work: a long technical error follows."), "Didn't work."),
    ],
)
def test_real_voice_hook_never_falls_through_to_chat_or_long_tts(monkeypatch, outcome, expected):
    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("BRODI_WAKE_REQUIRED", "true")
    core = SimpleNamespace(pending=None, pending_question=None, handle=AsyncMock(return_value=outcome))
    agent = entrypoint.BrodiAgent(core)
    speech = SimpleNamespace(say=AsyncMock())
    monkeypatch.setattr(entrypoint.BrodiAgent, "session", property(lambda self: speech))

    async def scenario():
        with pytest.raises(entrypoint.StopResponse):
            await agent.on_user_turn_completed(None, SimpleNamespace(text_content="Hey bro, open GitHub"))
        speech.say.assert_awaited_once_with(expected, add_to_chat_ctx=False, allow_interruptions=False)

    asyncio.run(scenario())


def test_non_english_voice_is_rejected_before_any_action(monkeypatch):
    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("BRODI_WAKE_REQUIRED", "true")
    core = SimpleNamespace(pending=None, pending_question=None, handle=AsyncMock())
    agent = entrypoint.BrodiAgent(core)
    speech = SimpleNamespace(say=AsyncMock())
    monkeypatch.setattr(entrypoint.BrodiAgent, "session", property(lambda self: speech))

    async def scenario():
        with pytest.raises(entrypoint.StopResponse):
            await agent.on_user_turn_completed(None, SimpleNamespace(text_content="Hey bro, गिटहब खोलो"))
        core.handle.assert_not_called()
        speech.say.assert_awaited_once_with("English only.", add_to_chat_ctx=False, allow_interruptions=False)

    asyncio.run(scenario())


def test_readback_results_are_shown_without_reading_the_whole_result():
    outcome = Outcome(
        route=Route("tool", tool=SimpleNamespace(speak="result")),
        executed=True,
        speak="A long note containing text from the user's page.",
    )
    assert spoken_reply(outcome) == "Check activity."


def test_startup_and_progress_use_short_speech_without_losing_details(monkeypatch):
    entrypoint = voice_entrypoint(monkeypatch)
    monkeypatch.setenv("BRODI_STARTUP_GREETING", "true")
    monkeypatch.setattr(entrypoint.policy, "ENABLED", True)
    callbacks, captured = {}, {}
    session = SimpleNamespace(start=AsyncMock(), say=Mock())
    session.on = lambda name: lambda callback: callbacks.update({name: callback})
    core = Mock(start=AsyncMock(), aclose=AsyncMock())

    def make_core(**options):
        captured.update(options)
        return core

    monkeypatch.setattr(entrypoint, "DynamicMacAgent", make_core)
    monkeypatch.setattr(entrypoint, "build_session", lambda: session)
    journal = Mock()
    monkeypatch.setattr(entrypoint, "ActivityLog", lambda: journal)

    asyncio.run(entrypoint.entrypoint(Mock(room=object())))
    progress = captured["on_learning"]
    details = "I am creating a tool and checking its generated script."
    progress(details)
    progress("Still working on the same command.")
    assert [call.args[0] for call in session.say.call_args_list] == ["Ready.", "On it."]
    journal.emit.assert_any_call("progress", details)
    voice_agent = session.start.call_args.kwargs["agent"]
    voice_agent.on_command()
    progress("This is the next command.")
    assert [call.args[0] for call in session.say.call_args_list] == ["Ready.", "On it.", "On it."]
