"""Router tests against the OpenAI Decisions wrapper (batching + span selection)."""

import asyncio
import json

from brodi.applescript import MacContext
from brodi.decisions import ChoiceAnswer, DecisionResponse, PredicateAnswer
from brodi.registry import ArgSpec, Tool, ToolRegistry
from brodi.router import JevRouter, OpenAIRouter

CTX = MacContext("Finder", ["Finder", "Slack"], ["Finder", "Slack", "Google Chrome"])


def _registry(tmp_path, tools):
    seed = tmp_path / "seed.json"
    seed.write_text("[]")
    reg = ToolRegistry(seed_path=seed, learned_path=tmp_path / "learned.json")
    reg.tools = {t.name: t for t in tools}
    return reg


def _tools():
    return [
        Tool(
            name="open_app",
            description="Open an application.",
            script='tell application "{{app}}" to activate',
            args=[ArgSpec(name="app", kind="enum", instructions="which app", criteria={"Slack": None, "Safari": None})],
        ),
        Tool(
            name="send_note",
            description="Send a short note.",
            script='display notification "{{message}}" with title "{{recipient}}"',
            args=[
                ArgSpec(name="recipient", kind="text", instructions="who"),
                ArgSpec(name="message", kind="text", instructions="what"),
            ],
        ),
        Tool(
            name="web_task",
            description="Do a multi-step task on a website.",
            script="",
            runner="browser",
            args=[ArgSpec(name="site", kind="enum", instructions="site", criteria={"google": None, "amazon": None})],
        ),
    ]


class FakeClient:
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[dict, dict]] = []

    async def evaluate(self, state, questions):
        self.calls.append((state, questions))
        return self.responder(state, questions)


def _respond(state, questions):
    choices = {}
    predicates = {}
    for name, q in questions.items():
        if name == "intent":
            choices[name] = ChoiceAnswer("open_app", 0.9, {"open_app": 0.9, "chat": 0.1})
        elif name.startswith("arg::"):
            arg = name.split("::")[2]
            value = "Slack" if arg == "app" else next(iter(q.criteria))
            choices[name] = ChoiceAnswer(value, 0.95, {value: 0.95})
        elif name == "web_goal_missing":
            choices[name] = ChoiceAnswer("nothing", 0.9, {"nothing": 0.9})
        elif name == "web_goal_complete":
            predicates[name] = PredicateAnswer(0.9)
        elif name == "web_goal_forbidden":
            predicates[name] = PredicateAnswer(0.0)
        elif name == "browser_followup":
            predicates[name] = PredicateAnswer(0.8)
        elif name == "confirm":
            predicates[name] = PredicateAnswer(0.9)
        elif name == "deny":
            predicates[name] = PredicateAnswer(0.1)
    return DecisionResponse(choices, predicates)


def test_route_batches_intent_args_and_web_questions_in_one_call(tmp_path):
    router = JevRouter(_registry(tmp_path, _tools()), client=FakeClient(_respond))
    route = asyncio.run(router.route("open Slack", CTX))

    assert len(router.client.calls) == 1
    _, questions = router.client.calls[0]
    assert set(questions) >= {
        "intent",
        "arg::open_app::app",
        "web_goal_complete",
        "web_goal_forbidden",
        "web_goal_missing",
    }
    assert route.tool is not None and route.tool.name == "open_app"
    assert route.args == {"app": "Slack"}
    assert route.web_goal_complete == 0.9
    assert route.web_goal_missing == "nothing"


def test_confirm_deny_and_followup_questions_only_when_relevant(tmp_path):
    router = JevRouter(_registry(tmp_path, _tools()), client=FakeClient(_respond))
    route = asyncio.run(
        router.route(
            "yes go ahead",
            CTX,
            awaiting_confirmation=True,
            recent_browser={"goal": "find flights", "status": "done"},
        )
    )
    _, questions = router.client.calls[0]
    assert {"confirm", "deny", "browser_followup"} <= set(questions)
    assert route.is_confirmation == 0.9
    assert route.is_denial == 0.1
    assert route.browser_followup == 0.8


def test_text_args_filled_by_span_selection_in_second_call(tmp_path):
    def respond(state, questions):
        choices = {}
        if "intent" in questions:
            choices["intent"] = ChoiceAnswer("send_note", 0.9, {"send_note": 0.9})
            return DecisionResponse(choices, {})
        for name, q in questions.items():
            want = "Alex" if name == "recipient" else "hello"
            options = list(q.criteria)
            picked = next((c for c in options if c.lower() == want.lower()), None)
            if picked is None:
                picked = next((c for c in options if want.lower() in c.lower()), "__none__")
            choices[name] = ChoiceAnswer(picked, 0.9, {picked: 0.9})
        return DecisionResponse(choices, {})

    client = FakeClient(respond)
    router = JevRouter(_registry(tmp_path, [t for t in _tools() if t.name == "send_note"]), client=client)
    route = asyncio.run(router.route("send Alex a note saying hello", CTX))

    assert len(client.calls) == 2
    span_questions = client.calls[1][1]
    assert set(span_questions) == {"recipient", "message"}
    assert route.args == {"recipient": "Alex", "message": "hello"}


def test_router_keeps_jev_class_name_and_openai_alias(tmp_path):
    assert OpenAIRouter is JevRouter
    router = JevRouter(_registry(tmp_path, _tools()), client=FakeClient(_respond))
    assert router.client.responder is _respond


def test_router_state_includes_browser_memory(tmp_path):
    router = JevRouter(_registry(tmp_path, _tools()), client=FakeClient(_respond))
    asyncio.run(router.route("open Slack", CTX, recent_browser={"goal": "search bags", "status": "done"}))
    state, _ = router.client.calls[0]
    assert state["recent_browser_task"] == {"goal": "search bags", "status": "done"}
    assert json.dumps(state)  # JSON-serialisable for the decisions API
