import asyncio

import pytest

from brodi import agent as core
from brodi.agent import DynamicMacAgent, State
from brodi.applescript import MacContext, ScriptResult
from brodi.memory import MemoryStore
from brodi.multi_step import Plan, PlanRunner, Step, StepResult
from brodi.registry import Tool, ToolRegistry


@pytest.fixture
def assistant(tmp_path, monkeypatch):
    monkeypatch.setenv("BRODI_FAST_LAUNCH", "0")
    monkeypatch.setenv("BRODI_OS", "win32")
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    agent = DynamicMacAgent(ToolRegistry(learned_path=tmp_path / "tools.json"), enable_learning=False, memory=memory)
    scripts = []

    async def context():
        return MacContext("Explorer", ["Explorer"], ["Google Chrome", "Notepad"])

    async def execute(script, timeout=20):
        scripts.append(script)
        return ScriptResult(True, "opened")

    async def no_network(*args, **kwargs):
        raise AssertionError("The direct/cache path must not call a model")

    monkeypatch.setattr(agent.context, "latest", context)
    monkeypatch.setattr(core, "run_applescript", execute)
    monkeypatch.setattr(agent.router.client, "evaluate", no_network)
    return agent, memory, scripts


def test_second_command_skips_router_entirely(assistant, monkeypatch):
    agent, memory, scripts = assistant

    async def scenario():
        assert (await agent.handle("open chrome")).executed
        assert memory.match_shortcut("open chrome")

        async def no_router(*args, **kwargs):
            raise AssertionError("Memory must precede model and deterministic routing")

        monkeypatch.setattr(agent.router, "route", no_router)
        repeated = await agent.handle("hey bro open chrome")
        assert repeated.executed and repeated.memory_hit

    asyncio.run(scenario())
    assert len(scripts) == 2


def test_first_model_routed_request_is_a_memory_hit_next_time(assistant, monkeypatch):
    from brodi.decisions import ChoiceAnswer, DecisionResponse

    agent, _, _ = assistant
    calls = []

    async def decisions(state, questions):
        calls.append(state)
        return DecisionResponse(
            {
                "intent": ChoiceAnswer("open_app", 0.99, {"open_app": 0.99}),
                "arg::open_app::app": ChoiceAnswer("Google Chrome", 0.99, {"Google Chrome": 0.99}),
            },
            {},
        )

    monkeypatch.setattr(agent.router.client, "evaluate", decisions)

    async def scenario():
        assert (await agent.handle("bring the web browser up")).executed
        assert len(calls) == 1
        repeated = await agent.handle("bring the web browser up")
        assert repeated.executed and repeated.memory_hit and len(calls) == 1

    asyncio.run(scenario())


def test_compound_plan_is_persisted_and_repeated_without_models(assistant):
    agent, memory, scripts = assistant

    async def scenario():
        result = await agent.handle("open chrome and go to github.com")
        assert result.executed
        assert len(memory.match_shortcut("open chrome and go to github.com").steps) == 2
        repeated = await agent.handle("open chrome and go to github.com")
        assert repeated.executed and repeated.memory_hit

    asyncio.run(scenario())
    assert len(scripts) == 4


def test_facts_persist_and_delete_requires_confirmation(assistant):
    agent, memory, _ = assistant

    async def scenario():
        assert (await agent.handle("remember that my preferred browser is Arc")).executed
        assert MemoryStore(memory.path).get_fact("preferred_browser") == "Arc"
        await agent.handle("forget preferred browser")
        assert agent.state is State.AWAITING_CONFIRM and memory.get_fact("preferred_browser") == "Arc"
        await agent.handle("no")
        assert memory.get_fact("preferred_browser") == "Arc"
        await agent.handle("forget preferred browser")
        assert (await agent.handle("yes")).executed
        assert memory.get_fact("preferred_browser") is None

    asyncio.run(scenario())


def test_overwriting_fact_requires_confirmation(assistant):
    agent, memory, _ = assistant
    memory.set_fact("preferred_browser", "Arc")
    asyncio.run(agent.handle("remember my preferred browser is Chrome"))
    assert memory.get_fact("preferred_browser") == "Arc"
    asyncio.run(agent.handle("yes"))
    assert memory.get_fact("preferred_browser") == "Chrome"


def test_cached_blocked_tool_cannot_bypass_policy(assistant):
    agent, memory, scripts = assistant
    memory.store_shortcut("bad shortcut", [{"tool": "terminal_run", "args": {"command": "echo hacked"}}])
    result = asyncio.run(agent.handle("bad shortcut"))
    assert not result.executed and scripts == []


def test_memory_refuses_secrets_and_unconfirmed_clear(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    with pytest.raises(ValueError):
        memory.set_fact("my password", "abc")
    with pytest.raises(PermissionError):
        memory.delete()
    with pytest.raises(ValueError):
        memory.store_shortcut("bad", [{"tool": "open_app", "script": "evil"}])


def test_memory_does_not_evict_silently_and_never_fuzzes_changed_arguments(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db", max_entries=1)
    memory.set_fact("one", "value")
    with pytest.raises(ValueError, match="full"):
        memory.set_fact("two", "value")
    memory.store_shortcut("open notepad", [{"tool": "open_app", "args": {"app": "Notepad"}}])
    assert memory.match_shortcut("open note pad")
    assert memory.match_shortcut("close notepad") is None


def test_retry_only_failed_step():
    calls = []

    async def execute(step, timeout):
        calls.append(step.tool)
        return StepResult(step.tool != "second" or calls.count("second") > 1, "try again")

    async def observe():
        return {"active_app": "Chrome"}

    result = asyncio.run(
        PlanRunner(max_steps=5).run(
            Plan(steps=[Step(tool="first"), Step(tool="second")]), execute, observe, retry_safe=lambda s: True
        )
    )
    assert result.ok and calls == ["first", "second", "second"]


def test_action_limit_counts_retries():
    async def execute(step, timeout):
        return StepResult(False, "failed")

    async def observe():
        return {}

    result = asyncio.run(
        PlanRunner(max_steps=1).run(Plan(steps=[Step(tool="first")]), execute, observe, retry_safe=lambda s: True)
    )
    assert not result.ok and result.attempts == 1 and "limit" in result.message


def test_hard_timeout_does_not_retry_stateful_input():
    calls = []

    async def execute(step, timeout):
        calls.append(step.tool)
        await asyncio.sleep(10)

    async def observe():
        return {}

    result = asyncio.run(
        PlanRunner(step_timeout=0.02).run(
            Plan(steps=[Step(tool="type_text")]), execute, observe, retry_safe=lambda s: False
        )
    )
    assert not result.ok and "timed out" in result.message
    assert calls == ["type_text"]


def test_too_many_steps_abort_before_any_execution():
    async def never(*args):
        raise AssertionError("No execution allowed")

    result = asyncio.run(PlanRunner(max_steps=5).run(Plan(steps=[Step(tool="x") for _ in range(6)]), never, never))
    assert not result.ok and "5 steps" in result.message


def test_risky_plan_pauses_and_resumes_without_repeating_completed_steps(assistant):
    agent, _, scripts = assistant
    agent.registry.tools["risky_step"] = Tool(
        name="risky_step",
        description="Perform the confirmed action",
        script="",
        script_win="'confirmed action'",
        risky=True,
    )

    async def scenario():
        plan = Plan(
            steps=[
                Step(tool="open_app", args={"app": "Google Chrome"}),
                Step(tool="risky_step"),
                Step(tool="open_app", args={"app": "Notepad"}),
            ]
        )
        staged = await agent._run_plan(plan, "example plan")
        assert not staged.executed and len(scripts) == 1
        assert agent.state is State.AWAITING_CONFIRM
        result = await agent.handle("yes")
        assert result.executed and len(scripts) == 3
        assert sum('$app = "Google Chrome"' in script for script in scripts) == 1

    asyncio.run(scenario())


def test_plan_recovery_does_not_restart_successful_steps():
    calls = []

    async def execute(step, timeout):
        calls.append(step.tool)
        return StepResult(step.tool != "broken", "failed")

    async def observe():
        return {"active_app": "Explorer"}

    async def recover(step, observation):
        assert observation["step_error"] == "failed"
        return Step(tool="alternative")

    result = asyncio.run(
        PlanRunner(max_steps=5).run(
            Plan(steps=[Step(tool="first"), Step(tool="broken")]),
            execute,
            observe,
            retry_safe=lambda step: True,
            recover=recover,
        )
    )
    assert result.ok and calls == ["first", "broken", "broken", "alternative"]


def test_memory_schema_rejects_future_version(tmp_path):
    import sqlite3

    path = tmp_path / "future.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=2")
    with pytest.raises(ValueError, match="newer"):
        MemoryStore(path)


def test_shortcut_replacement_is_not_silently_destructive(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.store_shortcut("open my app", [{"tool": "open_app", "args": {"app": "Notepad"}}])
    with pytest.raises(PermissionError):
        store.store_shortcut("open my app", [{"tool": "open_app", "args": {"app": "Google Chrome"}}])
    assert store.match_shortcut("open my app").steps[0]["args"]["app"] == "Notepad"


def test_memory_cli_clear_requires_typed_or_flag_confirmation(tmp_path, monkeypatch):
    import io
    import sys

    from brodi import cli

    monkeypatch.setenv("BRODI_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(cli, "load_environment", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    store = MemoryStore()
    store.set_fact("name", "Brodi")
    assert asyncio.run(cli._main(["--memory", "clear"])) == 2
    assert store.get_fact("name") == "Brodi"
    assert asyncio.run(cli._main(["--memory", "clear", "--yes"])) == 0
    assert store.get_fact("name") is None


def test_confirmed_plan_step_still_verifies_its_success_criterion(assistant, monkeypatch):
    from brodi.decisions import DecisionResponse, PredicateAnswer

    agent, _, scripts = assistant
    agent.registry.tools["risky_step"] = Tool(
        name="risky_step", description="Perform confirmed action", script="", script_win="'confirmed'", risky=True
    )

    async def observe():
        return {"window_title": "Still unchanged"}

    async def judge(state, questions):
        assert state["criterion"] == "Expected window appears"
        return DecisionResponse(predicates={"met": PredicateAnswer(0.1)})

    monkeypatch.setattr(core, "observe_desktop", observe)
    monkeypatch.setattr(agent.router.client, "evaluate", judge)

    async def scenario():
        await agent._run_plan(
            Plan(
                steps=[
                    Step(tool="risky_step", success="Expected window appears"),
                    Step(tool="open_app", args={"app": "Notepad"}),
                ]
            ),
            "test",
        )
        assert scripts == []
        result = await agent.handle("yes")
        assert not result.executed and "couldn't verify" in result.speak
        assert len(scripts) == 1

    asyncio.run(scenario())


def test_planner_wire_schema_is_strict_and_arguments_round_trip():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from openai.lib._pydantic import to_strict_json_schema

    from brodi.multi_step import PlanProposal, compose_plan

    schema = to_strict_json_schema(PlanProposal)
    assert schema["$defs"]["PlanArgument"]["additionalProperties"] is False
    proposal = {
        "steps": [
            {"tool": "open_app", "arguments": [{"name": "app", "value": "Notepad"}], "success": "Tool reports success"}
        ]
    }
    parse = AsyncMock(return_value=SimpleNamespace(output_parsed=proposal))
    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    result = asyncio.run(compose_plan("open notepad", [], client))
    assert result.steps[0].args == {"app": "Notepad"}
    assert parse.call_args.kwargs["text_format"] is PlanProposal

    proposal["steps"][0]["arguments"].append({"name": "app", "value": "Chrome"})
    with pytest.raises(ValueError, match="duplicate"):
        asyncio.run(compose_plan("open notepad", [], client))
