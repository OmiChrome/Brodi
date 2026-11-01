"""Arithmetic requests operate Calculator; numeric answers must not become speech."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from brodi import agent as agent_module
from brodi import commands, policy
from brodi.agent import DynamicMacAgent
from brodi.applescript import MacContext, ScriptResult
from brodi.calculator import CalculatorSession, mac_script, prepare
from brodi.calculator_uia import display_number, press_calculation
from brodi.registry import ToolRegistry
from brodi.voice import spoken_reply


@pytest.mark.parametrize(
    "phrase,answer",
    [
        ("what is 500 x 450..", "225000"),
        ("Hey bro, what's 500×450?", "225000"),
        ("500x450", "225000"),
        ("multiply 500 by 450", "225000"),
        ("divide 450 by 5", "90"),
        ("add 500 and 450", "950"),
        ("subtract 450 from 500", "50"),
        ("calculate 2 + 3 * 4", "14"),
        ("compute (2 + 3) * 4", "20"),
        ("what is twenty percent of five hundred", "100"),
        ("what is 20% of 500", "100"),
        ("what is five hundred times four hundred and fifty", "225000"),
        ("what is 500 times 450", "225000"),
        ("what is five hundred times four fifty", "225000"),
        ("Bro, what is five hundred times four twenty?", "210000"),
        ("Brodi, can you calculate for me? What is five hundred times four fifty?", "225000"),
        ("Can you calculate what is five hundred times four hundred and fifty", "225000"),
        ("could you please calculate 500 times 450 for me", "225000"),
        ("how much is 500 times 450", "225000"),
        ("calculate four twenty five times two", "850"),
        ("what is 5.5 divided by 2", "2.75"),
        ("calculate -4 + 0.5", "-3.5"),
        ("calculate 2 to the power of 10", "1024"),
        ("what is the square root of 81", "9"),
        ("calculate 1,234 plus 5 in calculator", "1239"),
        ("what is 5 squared", "25"),
    ],
)
def test_math_phrases_route_to_visible_calculator(phrase, answer):
    command = commands.parse_command(phrase)
    assert command and command.tool == "calculate"
    assert prepare(command.args["expression"]).expected == answer


@pytest.fixture
def calculator_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_OS", "win32")
    monkeypatch.setenv("BRODI_MEMORY_ENABLED", "false")
    monkeypatch.setenv("BRODI_ENABLE_MULTI_STEP", "false")
    agent = DynamicMacAgent(ToolRegistry(learned_path=tmp_path / "learned.json"), enable_learning=False)
    agent.context._ctx = MacContext("Explorer", [], [])
    calculations = []

    async def calculate(self, expression, **kwargs):
        calculations.append(expression)
        return ScriptResult(True, json.dumps({"display": prepare(expression).expected, "verified": True}))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Arithmetic must not need a model or generic desktop script")

    monkeypatch.setattr(CalculatorSession, "calculate", calculate)
    monkeypatch.setattr(agent.router.client, "evaluate", forbidden)
    monkeypatch.setattr(agent_module, "run_applescript", forbidden)
    return agent, calculations


@pytest.mark.parametrize(
    "turns",
    [
        ["calculate", "500 times 450"],
        ["Can you calculate for me? What is five hundred times", "four hundred and fifty"],
        ["Can you calculate what is five hundred", "times four fifty"],
        ["calculate", "five hundred", "times", "four fifty"],
        ["calculate 2 plus", "what is 500 times 450"],
    ],
)
def test_calculation_survives_speech_pauses_without_losing_operands(calculator_agent, turns):
    agent, calculations = calculator_agent

    async def scenario():
        for turn in turns[:-1]:
            result = await agent.handle(turn)
            assert not result.executed and not calculations
            assert agent.pending_question[0].missing_arg == "expression"
            assert spoken_reply(result, missing="expression") == commands.CALCULATION_QUESTION
        result = await agent.handle(turns[-1])
        assert result.executed and result.route.tool.name == "calculate"
        assert json.loads(result.observation["output"])["display"] == "225000"
        assert spoken_reply(result) == "Got it."
        assert agent.pending_question is None
        await agent.aclose()

    asyncio.run(scenario())
    assert len(calculations) == 1


def test_invalid_calculation_answer_keeps_question_until_valid_or_cancelled(calculator_agent):
    agent, calculations = calculator_agent

    async def scenario():
        await agent.handle("calculate 500 times")
        invalid = await agent.handle("I haven't decided")
        assert not invalid.executed and agent.pending_question
        assert not calculations
        done = await agent.handle("450")
        assert done.executed and json.loads(done.observation["output"])["display"] == "225000"
        await agent.handle("calculate 2 plus")
        cancelled = await agent.handle("cancel")
        assert cancelled.speak == "Cancelled." and agent.pending_question is None
        assert len(calculations) == 1
        await agent.aclose()

    asyncio.run(scenario())


def test_calculation_failure_speaks_the_actual_error(calculator_agent):
    agent, calculations = calculator_agent

    async def scenario():
        result = await agent.handle("what is 500 divided by zero")
        assert not result.executed and not calculations
        assert result.observation["error"] == "Cannot divide by zero."
        assert spoken_reply(result) == "Cannot divide by zero."
        await agent.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text",
    [
        "what is the capital of France",
        "calculate __import__('os')",
        "what is 500 x 450 and delete my files",
        "calculate 2 + unknown",
        "open 500 x 450",
        "what is one two",
        "search YouTube for 500 x 450",
    ],
)
def test_non_math_or_extra_instructions_are_not_silently_consumed(text):
    command = commands.parse_command(text)
    assert command is None or command.tool != "calculate"


@pytest.mark.parametrize("expression", ["1/0", "2**1000000", "2**-100", "(-1)**.5", "1e999", "abs(-3)"])
def test_invalid_calculations_reject_before_launch(expression):
    with pytest.raises((ValueError, ArithmeticError, SyntaxError)):
        prepare(expression)


def controls_for(calculation, display):
    pressed = []
    controls = {}
    for name in {"clearButton", "equalButton", "CalculatorResults", *calculation.buttons}:
        controls[name] = SimpleNamespace(
            iface_invoke=SimpleNamespace(Invoke=lambda key=name: pressed.append(key)),
            window_text=lambda: display,
        )
    return controls, pressed


def test_real_operand_buttons_are_invoked_instead_of_injecting_the_answer():
    calculation = prepare("500*450")
    controls, pressed = controls_for(calculation, "Display is 225,000")
    result = press_calculation(controls, calculation)
    assert result["verified"] and result["display"] == "Display is 225,000"
    assert pressed == [
        "clearButton",
        "num5Button",
        "num0Button",
        "num0Button",
        "multiplyButton",
        "num4Button",
        "num5Button",
        "num0Button",
        "equalButton",
    ]


def test_wrong_display_never_reports_success():
    from brodi.windows_uia import UIAError

    calculation = prepare("500*450")
    controls, _ = controls_for(calculation, "Display is 0")
    with pytest.raises(UIAError, match="verify"):
        press_calculation(controls, calculation, timeout=0)


def test_parentheses_preserve_precedence_and_negatives():
    assert prepare("2+3*4").expression == "2+(3*4)"
    assert prepare("(2+3)*4").expression == "(2+3)*4"
    assert prepare("2*-3").expression == "2*(0-3)"
    assert prepare("0.0000000000000001").expression == "0.0000000000000001"


@pytest.mark.parametrize(
    "text,decimal,expected",
    [
        ("Display is 225,000", ".", 225000),
        ("Display is −3.5", ".", -3.5),
        ("Display is 225.000,5", ",", 225000.5),
        ("Display is 1.25e-5", ".", 0.0000125),
        ("Display is 0,125", ",", 0.125),
        ("Cannot divide by zero", ".", None),
    ],
)
def test_display_readback_understands_grouping_and_decimal_separator(text, decimal, expected):
    assert display_number(text, decimal) == expected


def test_agent_shows_result_and_closes_its_own_calculator(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_OS", "win32")
    agent = DynamicMacAgent(ToolRegistry(learned_path=tmp_path / "learned.json"), enable_learning=False)
    agent.context._ctx = MacContext("Explorer", [], [])
    calls = []

    async def calculate(self, expression, **kwargs):
        self.has_window = True
        calls.append(("calculate", expression))
        return ScriptResult(True, json.dumps({"display": "225,000", "verified": True, "handle": 123}))

    async def close(self, **kwargs):
        calls.append(("close", 123))
        self.has_window = False
        return ScriptResult(True, '{"closed":true,"handle":123}')

    async def forbidden(*args, **kwargs):
        raise AssertionError("No model call or generic close is allowed")

    monkeypatch.setattr(CalculatorSession, "calculate", calculate)
    monkeypatch.setattr(CalculatorSession, "close_window", close)
    monkeypatch.setattr(agent.router.client, "evaluate", forbidden)
    monkeypatch.setattr(agent_module, "run_applescript", forbidden)
    monkeypatch.setattr("brodi.native_launch.execute", forbidden)

    async def scenario():
        first = await agent.handle("what is 500 x 450")
        assert first.executed and first.speak == "Got it."
        assert json.loads(first.observation["output"])["display"] == "225,000"
        second = await agent.handle("close calculatar")
        assert second.executed and second.speak == "On it."
        await agent.aclose()

    asyncio.run(scenario())
    assert calls == [("calculate", "500 * 450"), ("close", 123)]


def test_mac_uses_calculator_keystrokes_without_speaking_the_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_OS", "darwin")
    agent = DynamicMacAgent(ToolRegistry(learned_path=tmp_path / "learned.json"), enable_learning=False)
    agent.context._ctx = MacContext("Finder", [], [])
    scripts = []

    async def execute(script, **kwargs):
        scripts.append(script)
        return ScriptResult(True, "Calculation entered in Calculator.")

    monkeypatch.setattr(agent_module, "run_applescript", execute)

    async def scenario():
        result = await agent.handle("what is 500 x 450")
        assert result.executed and result.speak == "Got it."
        await agent.aclose()

    asyncio.run(scenario())
    assert len(scripts) == 1 and '"500*450="' in scripts[0] and "225000" not in scripts[0]
    assert not policy.check(mac_script(prepare("500*450")))


def test_transport_reuses_worker_and_timeout_kills_only_helper(monkeypatch):
    import brodi.calculator as module

    class Reader:
        def __init__(self):
            self.rows = [
                b'{"ready":true}',
                b'{"ok":true,"result":{"has_window":true}}',
                b'{"ok":true,"result":{"has_window":true}}',
            ]

        async def readline(self):
            if self.rows:
                return self.rows.pop(0)
            await asyncio.sleep(60)

    class Process:
        def __init__(self):
            self.stdout, self.returncode, self.writes = Reader(), None, []
            self.stdin = SimpleNamespace(write=self.writes.append, drain=self.drain)
            self.kill = Mock(side_effect=lambda: setattr(self, "returncode", -1))

        async def drain(self):
            pass

        async def wait(self):
            return self.returncode

    process, starts = Process(), []

    async def spawn(*args, **kwargs):
        starts.append(args)
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)

    async def scenario():
        session = CalculatorSession()
        assert (await session.calculate("500*450")).ok
        assert (await session.calculate("23*7")).ok
        timed = await session.calculate("1+1", timeout=0.02)
        assert not timed.ok and "timed out" in timed.error
        assert not session.has_window
        await session.aclose()

    asyncio.run(scenario())
    assert len(starts) == 1 and len(process.writes) == 3
    process.kill.assert_called_once()


def test_invalid_expression_never_starts_helper(monkeypatch):
    session = CalculatorSession()
    start = Mock(side_effect=AssertionError("Must validate before any UI action"))
    monkeypatch.setattr(session, "_start", start)
    assert not asyncio.run(session.calculate("1/0")).ok
    start.assert_not_called()
