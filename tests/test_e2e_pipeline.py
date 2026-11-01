"""End-to-end tests of DynamicMacAgent across simulated macOS and Windows 11."""

import asyncio

import pytest

from brodi import agent as agent_module
from brodi.agent import DynamicMacAgent, State
from brodi.applescript import MacContext, ScriptResult
from brodi.browser_task import BrowserResult, ComposedGoal
from brodi.decisions import ChoiceAnswer, DecisionResponse, PredicateAnswer
from brodi.registry import ArgSpec, Tool, ToolRegistry

PLATFORMS = ["darwin", "win32"]


class ScriptedDecisions:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def evaluate(self, state, questions):
        self.calls.append((state, questions))
        return self.handler(state, questions)


def responder(
    intent="chat",
    confidence=0.9,
    args=None,
    web_complete=1.0,
    web_forbidden=0.0,
    missing="nothing",
    followup=0.0,
    confirm=0.0,
    deny=0.0,
    text=None,
    achieved=0.9,
):
    """A scripted Decisions API that answers the router and verifier questions."""

    def respond(state, questions):
        choices, predicates = {}, {}
        for name, q in questions.items():
            if name == "intent":
                choices[name] = ChoiceAnswer(intent, confidence, {intent: confidence})
            elif name.startswith("arg::"):
                arg = name.split("::")[2]
                value = (args or {}).get(arg) or next(iter(q.criteria), "")
                choices[name] = ChoiceAnswer(value, 0.95, {value: 0.95})
            elif name == "web_goal_missing":
                choices[name] = ChoiceAnswer(missing, 0.9, {missing: 0.9})
            elif name == "why":
                choices[name] = ChoiceAnswer("unclear", 0.9, {"unclear": 0.9})
            elif name == "web_goal_complete":
                predicates[name] = PredicateAnswer(web_complete)
            elif name == "web_goal_forbidden":
                predicates[name] = PredicateAnswer(web_forbidden)
            elif name == "browser_followup":
                predicates[name] = PredicateAnswer(followup)
            elif name == "confirm":
                predicates[name] = PredicateAnswer(confirm)
            elif name == "deny":
                predicates[name] = PredicateAnswer(deny)
            elif name in ("achieved", "works"):
                predicates[name] = PredicateAnswer(achieved)
            else:  # free-text span selection
                criteria = list(getattr(q, "criteria", {}) or {})
                want = (text or {}).get(name, "")
                value = next((c for c in criteria if want and c.lower() == want.lower()), None)
                if value is None:
                    value = next((c for c in criteria if want and want.lower() in c.lower()), None)
                if value is None:
                    value = criteria[0] if criteria else ""
                choices[name] = ChoiceAnswer(value, 0.9, {value: 0.9})
        return DecisionResponse(choices, predicates)

    return respond


def make_agent(tmp_path, monkeypatch, platform, tools, handler, on_learning=None):
    monkeypatch.setattr(agent_module.browser_task, "access_error", lambda: None)
    monkeypatch.setenv("BRODI_OS", platform)
    seed = tmp_path / "seed.json"
    seed.write_text("[]")
    registry = ToolRegistry(seed_path=seed, learned_path=tmp_path / "learned.json")
    registry.tools = {t.name: t for t in tools}
    agent = DynamicMacAgent(registry, enable_learning=False, on_learning=on_learning)
    agent.router.client = ScriptedDecisions(handler)
    ctx = MacContext("Finder", ["Finder", "Slack"], ["Finder", "Slack", "Google Chrome"])

    async def latest():
        return ctx

    monkeypatch.setattr(agent.context, "latest", latest)
    return agent


@pytest.fixture
def captured_scripts(monkeypatch):
    scripts = []

    async def fake_run(script, timeout=20.0):
        scripts.append(script)
        return ScriptResult(True, "")

    monkeypatch.setattr(agent_module, "run_applescript", fake_run)
    return scripts


def _mute_tool():
    return Tool(
        name="mute_volume",
        description="Mute the system volume.",
        script="set volume with output muted",
        script_win="(New-Object -ComObject WScript.Shell).SendKeys([char]173)",
    )


def _note_tool():
    return Tool(
        name="send_note",
        description="Send a short note.",
        script='display notification "{{message}}" with title "{{recipient}}"',
        script_win='[System.Windows.Forms.MessageBox]::Show("{{message}}", "{{recipient}}")',
        args=[
            ArgSpec(name="recipient", kind="text", instructions="who"),
            ArgSpec(name="message", kind="text", instructions="what"),
        ],
    )


def _risky_tool():
    return Tool(
        name="risky_thing",
        description="Do the risky thing.",
        script='display notification "risky"',
        risky=True,
    )


def _web_tool():
    return Tool(
        name="web_task",
        description="Do a multi-step task on a website.",
        script="",
        runner="browser",
        args=[
            ArgSpec(
                name="site",
                kind="enum",
                instructions="which site",
                criteria={"google_flights": None, "google": None, "amazon": None},
            )
        ],
    )


@pytest.mark.parametrize("platform", PLATFORMS)
def test_flow_a_deterministic_tool_execution(tmp_path, monkeypatch, captured_scripts, platform):
    agent = make_agent(
        tmp_path, monkeypatch, platform, [_mute_tool()], responder(intent="mute_volume", confidence=0.97)
    )
    outcome = asyncio.run(agent.handle("mute the volume"))

    assert outcome.executed is True
    assert outcome.speak == "Done."
    assert len(captured_scripts) == 1
    if platform == "win32":
        assert "SendKeys" in captured_scripts[0]
    else:
        assert "set volume" in captured_scripts[0]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_flow_b_pronoun_memory(tmp_path, monkeypatch, captured_scripts, platform):
    agent = make_agent(
        tmp_path,
        monkeypatch,
        platform,
        [_note_tool()],
        responder(intent="send_note", text={"recipient": "Alex", "message": "hello"}),
    )
    first = asyncio.run(agent.handle("send Alex a note saying hello"))
    assert first.executed is True

    agent.router.client = ScriptedDecisions(
        responder(intent="send_note", text={"recipient": "her", "message": "thanks"})
    )
    second = asyncio.run(agent.handle("send her another saying thanks"))

    assert second.executed is True
    assert "Alex" in captured_scripts[-1]
    assert "thanks" in captured_scripts[-1]
    assert "her" not in captured_scripts[-1]
    assert agent.last_args["send_note"]["recipient"] == "Alex"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_flow_c_risky_staging_confirm_and_cancel(tmp_path, monkeypatch, captured_scripts, platform):
    agent = make_agent(tmp_path, monkeypatch, platform, [_risky_tool()], responder(intent="risky_thing"))

    staged = asyncio.run(agent.handle("do the risky thing"))
    assert agent.state is State.AWAITING_CONFIRM
    assert "Should I go ahead?" in staged.speak
    assert captured_scripts == []

    agent.router.client = ScriptedDecisions(responder(intent="chat", confidence=0.2, confirm=0.95, deny=0.02))
    done = asyncio.run(agent.handle("yes go ahead"))
    assert done.executed is True and len(captured_scripts) == 1

    agent.router.client = ScriptedDecisions(responder(intent="risky_thing"))
    asyncio.run(agent.handle("do the risky thing"))
    agent.router.client = ScriptedDecisions(responder(intent="chat", confidence=0.2, confirm=0.02, deny=0.95))
    cancelled = asyncio.run(agent.handle("cancel"))
    assert cancelled.speak == "Cancelled."
    assert agent.state is State.IDLE
    assert len(captured_scripts) == 1


@pytest.mark.parametrize("platform", PLATFORMS)
def test_flow_d_learning_then_confirm(tmp_path, monkeypatch, captured_scripts, platform):
    fillers = []

    class FakeGenerator:
        async def generate(self, utterance, ctx):
            return (
                Tool(
                    name="new_thing",
                    description="Do the thing.",
                    script='display notification "new"',
                    script_win='[System.Windows.Forms.MessageBox]::Show("new")',
                    source="learned",
                ),
                "",
            )

    agent = make_agent(
        tmp_path,
        monkeypatch,
        platform,
        [],
        responder(intent="new_action", confidence=0.95),
        on_learning=fillers.append,
    )
    agent.generator = FakeGenerator()

    learned = asyncio.run(agent.handle("do the thing"))
    assert fillers and "Give me a moment" in fillers[0]
    assert learned.learned is not None and learned.learned.name == "new_thing"
    assert "I wrote a new action" in learned.speak
    assert agent.state is State.AWAITING_CONFIRM

    agent.router.client = ScriptedDecisions(responder(intent="chat", confidence=0.2, confirm=0.95))
    done = asyncio.run(agent.handle("yes run it"))
    assert done.executed is True and len(captured_scripts) == 1


@pytest.mark.parametrize("platform", PLATFORMS)
def test_flow_e_browser_task_with_missing_detail(tmp_path, monkeypatch, platform):
    agent = make_agent(
        tmp_path,
        monkeypatch,
        platform,
        [_web_tool()],
        responder(
            intent="web_task",
            confidence=0.9,
            args={"site": "google_flights"},
            web_complete=0.1,
            missing="exact_dates",
        ),
    )

    asked = asyncio.run(agent.handle("find me flights from Paris to Tokyo"))
    assert "Which exact dates?" in asked.speak
    assert agent.last_browser is not None and agent.last_browser["status"] == "needs_info"

    async def compose(utterance, context):
        return ComposedGoal(
            objective="flights from Paris to Tokyo Nov 2-7", success="flights listed", search_query=None
        )

    async def run_task(start_url, goal, on_progress=None, reuse_target=None):
        return BrowserResult(
            "done",
            2,
            15,
            url="https://flights.test/results",
            title="Paris to Tokyo flights",
            page_text="flights listed",
            target_id="t1",
        )

    monkeypatch.setattr(agent_module.browser_task, "compose_goal", compose)
    monkeypatch.setattr(agent_module.browser_task, "run_task", run_task)
    monkeypatch.setattr(agent_module.browser_task, "start_url_for", lambda *a, **k: "https://flights.test/")
    monkeypatch.setattr(agent_module.browser_task, "tab_exists", lambda *a, **k: False)

    agent.router.client = ScriptedDecisions(
        responder(intent="chat", confidence=0.3, web_complete=0.95, followup=0.9, achieved=0.95)
    )
    done = asyncio.run(agent.handle("November 2 to November 7"))

    assert done.executed is True
    assert done.speak.startswith("Done in 2 steps")
    assert agent.last_browser["url"] == "https://flights.test/results"
