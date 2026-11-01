"""Browser task tests: OpenAI structured helpers + self-contained CDP decision loop."""

import asyncio
import json
from types import SimpleNamespace

from brodi import browser_task
from brodi.browser_task import (
    OBSERVE_JS,
    BrowserResult,
    ComposedGoal,
    FieldText,
    run_task_sync,
)
from brodi.decisions import ChoiceAnswer, DecisionResponse


class FakeResponses:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.outputs.pop(0))


class FakeOpenAI:
    def __init__(self, outputs):
        self.responses = FakeResponses(outputs)

    async def close(self):
        pass


def test_compose_goal_uses_responses_parse(monkeypatch):
    fake = FakeOpenAI([ComposedGoal(objective="find a bag", success="bags listed", search_query="bag")])
    monkeypatch.setattr(browser_task, "_get_client", lambda: fake)
    composed = browser_task.compose_goal_sync("find me a bag", {"today": "Sunday 01 November 2026"})
    assert composed.objective == "find a bag"
    call = fake.responses.calls[0]
    assert call["model"] == "gpt-6-luna"
    assert call["text_format"] is ComposedGoal
    assert call["reasoning"] == {"effort": "none"}


def test_field_text_uses_responses_parse(monkeypatch):
    fake = FakeOpenAI([FieldText(text="Nov 7")])
    monkeypatch.setattr(browser_task, "_get_client", lambda: fake)
    value, meta = browser_task._field_text_sync({"goal": "leave Nov 7", "field": {"label": "Departure"}})
    assert value == "Nov 7"
    assert meta["model"] == "gpt-6-luna"
    assert fake.responses.calls[0]["text_format"] is FieldText


def test_compose_goal_is_async(monkeypatch):
    fake = FakeOpenAI([ComposedGoal(objective="o", success="s", search_query=None)])
    monkeypatch.setattr(browser_task, "_get_client", lambda: fake)
    composed = asyncio.run(browser_task.compose_goal("go", {}))
    assert composed.objective == "o"


OBS = {
    "url": "https://shop.test/search?q=bag",
    "title": "Shop",
    "text": "results",
    "actions": [
        {"id": "n1", "node": 1, "kind": "click", "label": "Search", "role": "button", "value": ""},
        {"id": "n2", "node": 2, "kind": "fill", "label": "Departure date", "role": "textbox", "value": ""},
    ],
}


class FakeCDP:
    def __init__(self):
        self.calls = []

    def call(self, method, session_id=None, **params):
        self.calls.append((method, session_id, params))
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "t1", "type": "page", "url": OBS["url"]}]}
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Runtime.evaluate":
            expression = params.get("expression", "")
            if expression == OBSERVE_JS:
                return {"result": {"value": json.dumps(OBS)}}
            return {"result": {"value": {"x": 10, "y": 20}}}
        return {}

    def close(self):
        pass


class LoopDecisions:
    def __init__(self, operations):
        self.operations = list(operations)
        self.calls = []

    async def evaluate(self, state, questions):
        self.calls.append((state, questions))
        if "operation" not in questions:
            return DecisionResponse({}, {})
        operation = self.operations.pop(0)
        choices = {"operation": ChoiceAnswer(operation, 0.9, {operation: 0.9})}
        if "target" in questions:
            criteria = questions["target"].criteria
            if operation == "fill":
                target = next(key for key, value in criteria.items() if value.startswith("fill"))
            else:
                target = next(iter(criteria))
            choices["target"] = ChoiceAnswer(target, 0.9, {target: 0.9})
        return DecisionResponse(choices, {})


def test_cdp_click_then_done_loop():
    cdp = FakeCDP()
    decisions = LoopDecisions(["click", "done"])
    result = run_task_sync("https://shop.test/", "search for a bag", cdp=cdp, decisions=decisions)

    assert isinstance(result, BrowserResult)
    assert result.status == "done"
    assert result.steps == 2
    assert result.url == OBS["url"]
    assert result.target_id == "t1"
    methods = [c[0] for c in cdp.calls]
    assert "Target.createTarget" in methods
    assert "Input.dispatchMouseEvent" in methods
    assert decisions.calls[0][1]["operation"].criteria["click"]
    assert "Search" in decisions.calls[0][1]["target"].criteria["n1"]


def test_cdp_fill_uses_field_text(monkeypatch):
    cdp = FakeCDP()
    decisions = LoopDecisions(["fill", "done"])
    monkeypatch.setattr(browser_task, "_field_text_sync", lambda context: ("Nov 7", {"model": "gpt-6-luna"}))
    result = run_task_sync("https://shop.test/", "set the departure date", cdp=cdp, decisions=decisions)
    assert result.status == "done"
    inserted = [c for c in cdp.calls if c[0] == "Input.insertText"]
    assert inserted and inserted[0][2]["text"] == "Nov 7"
    # date fields are committed with Enter
    assert any(c[0] == "Input.dispatchKeyEvent" and c[2].get("key") == "Enter" for c in cdp.calls)


def test_observation_script_collects_interactive_controls():
    assert "__brodiNodes" in OBSERVE_JS
    for selector in ("button", "input", "textarea", "contenteditable", '[role="button"]', '[role="textbox"]'):
        assert selector in OBSERVE_JS


def test_blocked_operation_stops_cleanly():
    cdp = FakeCDP()
    result = run_task_sync("https://shop.test/", "sign in", cdp=cdp, decisions=LoopDecisions(["blocked"]))
    assert result.status == "blocked"
    assert result.steps == 1


def test_multistep_browser_uses_one_async_loop_and_closes_client(monkeypatch):
    loops = []
    closed = []

    class LoopBoundClient(LoopDecisions):
        def __init__(self):
            super().__init__(["click", "done"])

        async def evaluate(self, **kwargs):
            loops.append(asyncio.get_running_loop())
            return await super().evaluate(**kwargs)

        async def aclose(self):
            closed.append(asyncio.get_running_loop())

    monkeypatch.setattr(browser_task, "DecisionsClient", LoopBoundClient)
    result = run_task_sync("https://shop.test/", "open the result", cdp=FakeCDP())
    assert result.status == "done"
    assert len(loops) == 2 and loops[0] is loops[1]
    assert closed == [loops[0]] and loops[0].is_closed()


def test_changing_target_reobserves_without_sending_input(monkeypatch):
    cdp = FakeCDP()
    points = iter([None, {"x": 10, "y": 20}])
    monkeypatch.setattr(browser_task, "_element_point", lambda *args: next(points))
    result = run_task_sync(
        "https://shop.test/", "Open search", cdp=cdp, decisions=LoopDecisions(["click", "click", "done"])
    )
    assert result.status == "done"
    assert result.history[0]["operation"] == "observe_again"
    assert len([c for c in cdp.calls if c[0] == "Input.dispatchMouseEvent"]) == 2


def test_closed_followup_tab_navigates_replacement_instead_of_waiting_on_blank():
    cdp = FakeCDP()
    result = run_task_sync(
        "https://shop.test/", "Continue search", reuse_target="missing-tab", cdp=cdp, decisions=LoopDecisions(["done"])
    )
    assert result.status == "done"
    assert any(c[0] == "Page.navigate" and c[2]["url"] == "https://shop.test/" for c in cdp.calls)


def test_browser_activity_shows_gathered_page_and_action():
    events = []
    result = run_task_sync(
        "https://shop.test/",
        "Open search",
        cdp=FakeCDP(),
        decisions=LoopDecisions(["click", "done"]),
        on_activity=lambda *args: events.append(args),
    )
    assert result.status == "done"
    assert events[0][0] == "observation" and events[0][2]["visible_text"] == "results"
    assert any(kind == "action" and message == "Clicking Search" for kind, message, _ in events)
