"""User-facing voice examples, through the real registry and execution/policy gates."""

import asyncio
import time

import pytest

from brodi import agent as agent_module
from brodi import commands, os_backend
from brodi.agent import DynamicMacAgent, State
from brodi.applescript import MacContext, ScriptResult
from brodi.registry import ToolRegistry
from brodi.router import Route


@pytest.fixture
def assistant(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_FAST_LAUNCH", "0")
    monkeypatch.setenv("BRODI_OS", "win32")
    agent = DynamicMacAgent(ToolRegistry(learned_path=tmp_path / "learned.json"), enable_learning=False)
    scripts = []

    async def context():
        return MacContext("Explorer", ["Explorer"], [])  # Chrome need not already be running.

    async def run(script):
        scripts.append(script)
        return ScriptResult(True, "opened")

    async def no_network(*args, **kwargs):
        raise AssertionError("A simple command should not need the routing API")

    monkeypatch.setattr(agent.context, "latest", context)
    monkeypatch.setattr(agent_module, "run_applescript", run)
    monkeypatch.setattr(agent.router.client, "evaluate", no_network)
    return agent, scripts


@pytest.mark.parametrize("text", ["Hey bro", "Yo bro!", "hey, bro.", "Brodi", "Hey Brody", " hi bro "])
def test_wake_only_acknowledges_without_api_or_action(assistant, text):
    agent, scripts = assistant
    result = asyncio.run(agent.handle(text))
    assert result.speak == "Hey bro, I'm listening."
    assert not result.executed and not result.handoff_to_llm and scripts == []


@pytest.mark.parametrize(
    "text,app",
    [
        ("Hey bro, open Chrome", "Google Chrome"),
        ("Yo bro open notepad", "Notepad"),
        (" open notepad ", "Notepad"),
        ("please launch Chrome", "Google Chrome"),
        ("Hey bro, open Edge please.", "Microsoft Edge"),
    ],
)
def test_open_apps_without_model_or_confirmation(assistant, text, app):
    agent, scripts = assistant
    result = asyncio.run(agent.handle(text))
    assert result.executed and result.route.tool.name == "open_app"
    assert result.route.args["app"] == app
    assert len(scripts) == 1 and agent.state is State.IDLE


@pytest.mark.parametrize(
    "text,url",
    [
        ("go to YouTube", "https://www.youtube.com/"),
        ("Hey bro, open my Google Keep", "https://keep.google.com/"),
        ("open gmail", "https://mail.google.com/"),
        ("open github dot com", "https://github.com"),
        ("open google calendar", "https://calendar.google.com/"),
        ("open youtube.com", "https://youtube.com"),
    ],
)
def test_open_sites_even_when_chrome_is_closed(assistant, text, url):
    agent, scripts = assistant
    result = asyncio.run(agent.handle(text))
    assert result.executed and result.route.tool.name == "chrome_open"
    assert url in scripts[0]


@pytest.mark.parametrize(
    "text",
    [
        "search youtube for cats & dogs",
        "search cats & dogs on youtube",
        "serach youtube for cats & dogs",
        "Hey bro, search YouTube cats & dogs",
    ],
)
def test_youtube_search_encodes_query_and_does_not_play(assistant, text):
    agent, scripts = assistant
    result = asyncio.run(agent.handle(text))
    assert result.executed
    assert "https://www.youtube.com/results?search_query=cats+%26+dogs" in scripts[0]
    assert "watch?v=" not in scripts[0]


def test_search_question_then_wake_then_answer(assistant):
    agent, scripts = assistant

    async def conversation():
        asked = await agent.handle("serach youtube")
        assert "What should I search" in asked.speak
        assert not asked.executed and scripts == []
        await agent.handle("Hey bro")
        answer = await agent.handle("lofi beats")
        assert answer.executed and agent.pending_question is None

    asyncio.run(conversation())
    assert "search_query=lofi+beats" in scripts[0]


@pytest.mark.parametrize("text", ["search", "search for", "search youtube for"])
def test_incomplete_search_asks_for_topic(assistant, text):
    agent, scripts = assistant
    result = asyncio.run(agent.handle(text))
    assert "What should I search" in result.speak
    assert not scripts and not result.executed


def test_keep_browser_unavailable_opens_homepage_without_model_calls(assistant, monkeypatch):
    agent, scripts = assistant
    monkeypatch.setattr(agent_module.browser_task, "access_error", lambda: "Enable browser access in Chrome.")

    async def no_composition(*args, **kwargs):
        raise AssertionError("No model call is needed when browser access is missing")

    monkeypatch.setattr(agent_module.browser_task, "compose_goal", no_composition)
    result = asyncio.run(agent.handle("open my google keep and show the top note"))
    assert not result.executed
    assert "I opened Google Keep" in result.speak
    assert "couldn't show the note" in result.speak
    assert "Enable browser access" in result.speak
    assert "https://keep.google.com/" in scripts[0]


@pytest.mark.parametrize("next_command", ["cancel", "never mind", "open notepad", "go to youtube"])
def test_new_command_or_cancel_replaces_pending_search(assistant, next_command):
    agent, scripts = assistant

    async def conversation():
        await agent.handle("search youtube")
        result = await agent.handle(next_command)
        assert agent.pending_question is None
        if next_command in {"cancel", "never mind"}:
            assert result.speak == "Cancelled." and scripts == []
        else:
            assert result.executed and "search_query=" not in scripts[0]

    asyncio.run(conversation())


def test_keep_note_is_browser_task_and_preserves_whole_goal(assistant, monkeypatch):
    agent, scripts = assistant
    goals = []

    async def run(tool, args, outcome, utterance):
        goals.append((args, utterance))
        outcome.executed = True

    monkeypatch.setattr(agent, "_execute_browser", run)
    result = asyncio.run(agent.handle("Hey bro, open my google keep and show the top note"))
    assert result.executed and not scripts
    assert goals == [({"site": "google_keep"}, "open my google keep and show the top note")]


def test_execution_failure_never_reports_done(assistant, monkeypatch):
    agent, _ = assistant

    async def fail(script):
        return ScriptResult(False, "", "Notepad is unavailable")

    monkeypatch.setattr(agent_module, "run_applescript", fail)
    result = asyncio.run(agent.handle("open notepad"))
    assert not result.executed and "didn't work" in result.speak


def test_exception_does_not_kill_next_turn(assistant, monkeypatch):
    agent, _ = assistant

    async def fail(*args, **kwargs):
        raise ConnectionError("network unavailable")

    monkeypatch.setattr(agent.router, "route", fail)
    result = asyncio.run(agent.handle("something unusual"))
    assert not result.executed and "try again" in result.speak
    assert asyncio.run(agent.handle("Hey bro")).speak == "Hey bro, I'm listening."


def test_expired_confirmation_cannot_run_old_action(assistant):
    agent, scripts = assistant
    agent._stage(agent.registry.get("open_app"), {"app": "Notepad"})
    agent.pending.staged_at = time.time() - 61
    result = asyncio.run(agent.handle("yes"))
    assert "expired" in result.speak and not scripts
    assert agent.state is State.IDLE


def test_low_confidence_argument_accepts_answer_instead_of_confirming_guess(assistant, monkeypatch):
    agent, scripts = assistant

    async def route(*args, **kwargs):
        return Route(
            "tool",
            tool=agent.registry.get("chrome_open"),
            args={"site": "youtube", "query": "bad guess"},
            weakest_arg=("query", 0.1),
        )

    monkeypatch.setattr(agent.router, "route", route)

    async def conversation():
        asked = await agent.handle("look for that")
        assert "Which query" in asked.speak and scripts == []
        result = await agent.handle("piano music")
        assert result.executed

    asyncio.run(conversation())
    assert "piano+music" in scripts[0] and "bad+guess" not in scripts[0]


@pytest.mark.parametrize("site", list(commands.SITE_URLS))
def test_every_named_site_opens_homepage(site):
    assert commands.browser_url(site, "") == commands.SITE_URLS[site]
    assert commands.browser_url(site, site.replace("_", " ")) == commands.SITE_URLS[site]


def test_compound_search_is_not_silently_reduced_to_plain_search():
    assert commands.parse_command("search youtube for cats and play the first result") is None


def test_rendered_search_argument_cannot_inject_powershell(assistant):
    agent, _ = assistant
    rendered = agent.registry.get("chrome_open").render({"site": "youtube", "query": 'cats " $(calc) ` & dogs'})
    assert "$(calc)" not in rendered
    assert "%24%28calc%29" in rendered


def test_searching_youtube_for_a_url_stays_on_youtube():
    assert (
        commands.browser_url("youtube", "https://example.com")
        == "https://www.youtube.com/results?search_query=https%3A%2F%2Fexample.com"
    )


def test_timing_delay_is_not_a_machine_sleep_command():
    from brodi import policy

    assert not policy.check("Start-Sleep -Milliseconds 400")
    assert policy.check('tell application "System Events" to sleep')


def test_known_windows_apps_are_recognized_without_start_menu_scan(monkeypatch):
    monkeypatch.setenv("BRODI_OS", "win32")
    assert commands.parse_command("open notepad").args == {"app": "Notepad"}
    monkeypatch.setenv("BRODI_OS", "darwin")
    assert commands.parse_command("open textedit").args == {"app": "TextEdit"}
    assert os_backend.is_windows() is False


def test_activity_exposes_command_action_output_and_waiting_state(assistant, tmp_path):
    from brodi.activity import ActivityLog, JournalReader

    agent, _ = assistant
    agent.activity = ActivityLog(tmp_path / "events")
    result = asyncio.run(agent.handle("Hey bro, open Chrome"))
    assert result.executed
    events = JournalReader(agent.activity.path).poll()
    assert events[0]["message"] == "Hey bro, open Chrome"
    assert any(e["kind"] == "action" and e["message"] == "Opening Google Chrome" for e in events)
    assert any(e["kind"] == "observation" and e["details"]["output"] == "opened" for e in events)
    assert any(e["kind"] == "output" for e in events)
    assert events[-1]["message"] == "Listening for a task"


def test_activity_shows_pending_question_instead_of_false_completion(assistant, tmp_path):
    from brodi.activity import ActivityLog, JournalReader

    agent, _ = assistant
    agent.activity = ActivityLog(tmp_path / "events")
    asyncio.run(agent.handle("search youtube"))
    events = JournalReader(agent.activity.path).poll()
    assert events[-1]["message"] == "Waiting for your answer"
    assert not any(e["kind"] == "result" for e in events)


def test_activity_never_reports_failed_action_as_completed(assistant, tmp_path, monkeypatch):
    from brodi.activity import ActivityLog, JournalReader

    async def failed(script):
        return ScriptResult(False, "", "Notepad could not start")

    agent, _ = assistant
    agent.activity = ActivityLog(tmp_path / "events")
    monkeypatch.setattr(agent_module, "run_applescript", failed)
    result = asyncio.run(agent.handle("open Notepad"))
    events = JournalReader(agent.activity.path).poll()
    assert not result.executed
    assert any(e["kind"] == "error" for e in events)
    assert not any(e["kind"] == "result" for e in events)
    assert events[-1]["message"] == "Listening for a task"
