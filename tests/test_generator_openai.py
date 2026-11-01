"""Codegen tests: OpenAI Responses API, OS-aware prompts, compile/verify/duplicate gates."""

import asyncio
from types import SimpleNamespace

from brodi import generator
from brodi.agent import DynamicMacAgent  # noqa: F401  (ensures imports stay coherent)
from brodi.applescript import MacContext
from brodi.decisions import ChoiceAnswer, DecisionResponse, PredicateAnswer
from brodi.generator import DUPLICATE, GeneratedTool, ToolGenerator
from brodi.registry import Tool, ToolRegistry

CTX = MacContext("Finder", ["Finder"], ["Finder"])


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


class FakeDecisions:
    def __init__(self, duplicate="__none__", verify=0.9, verify_seq=None):
        self.duplicate = duplicate
        self.verify_seq = list(verify_seq or [])
        self.verify = verify
        self.calls = []

    async def evaluate(self, state, questions):
        self.calls.append((state, questions))
        choices = {}
        predicates = {}
        if "same_as" in questions:
            choices["same_as"] = ChoiceAnswer(self.duplicate, 0.9, {self.duplicate: 0.9})
        if "works" in questions:
            value = self.verify_seq.pop(0) if self.verify_seq else self.verify
            predicates["works"] = PredicateAnswer(value)
        return DecisionResponse(choices, predicates)

    async def aclose(self):
        pass


def _registry(tmp_path, tools=None):
    seed = tmp_path / "seed.json"
    seed.write_text("[]")
    reg = ToolRegistry(seed_path=seed, learned_path=tmp_path / "learned.json")
    reg.tools = {t.name: t for t in (tools or [])}
    return reg


def _generated(name="notify_me", script="Set-ItemProperty -Path 'HKCU:\\Software\\x' -Name y -Value 1"):
    return GeneratedTool(
        feasible=True,
        reason="",
        tool_name=name,
        description="Change a thing.",
        scope=None,
        args=[],
        script=script,
        speak="done",
        examples=["change it"],
    )


def test_generate_uses_openai_responses_parse(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_OS", "win32")
    client = FakeOpenAI([_generated()])
    decisions = FakeDecisions()

    async def compiled(script):
        return True, ""

    monkeypatch.setattr(generator, "_compiles", compiled)
    reg = _registry(tmp_path)
    gen = ToolGenerator(reg, client=client, decisions=decisions)

    tool, msg = asyncio.run(gen.generate("change the thing", CTX))

    assert msg == "" and tool is not None and tool.name == "notify_me"
    call = client.responses.calls[0]
    assert call["model"] == "gpt-6.1-sol"
    assert call["text_format"] is GeneratedTool
    assert call["reasoning"] == {"effort": "low"}
    assert call["max_output_tokens"] == 1500
    assert reg.get("notify_me") is not None and reg.get("notify_me").source == "learned"
    assert tool.verified == 0.9


def test_policy_violation_is_sent_back_for_repair(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_OS", "win32")
    bad = _generated("bad_tool", 'Remove-Item "C:\\Users\\me\\x" -Recurse -Force')
    good = _generated("good_tool")
    client = FakeOpenAI([bad, good])

    async def compiled(script):
        return True, ""

    monkeypatch.setattr(generator, "_compiles", compiled)
    gen = ToolGenerator(_registry(tmp_path), client=client, decisions=FakeDecisions())

    tool, msg = asyncio.run(gen.generate("change the thing", CTX))
    assert msg == "" and tool is not None and tool.name == "good_tool"
    assert len(client.responses.calls) == 2
    repair = client.responses.calls[1]["input"][-1]["content"]
    assert "POLICY" in repair and "Remove-Item" in repair


def test_compile_failure_repairs_then_registers(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_OS", "win32")
    client = FakeOpenAI([_generated(), _generated()])
    results = [(False, "line 1: Missing closing '}'"), (True, "")]

    async def compiled(script):
        return results.pop(0)

    monkeypatch.setattr(generator, "_compiles", compiled)
    gen = ToolGenerator(_registry(tmp_path), client=client, decisions=FakeDecisions())

    tool, msg = asyncio.run(gen.generate("change the thing", CTX))
    assert msg == "" and tool is not None
    assert len(client.responses.calls) == 2
    repair = client.responses.calls[1]["input"][-1]["content"]
    assert "Missing closing" in repair


def test_low_verify_confidence_triggers_repair(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_OS", "win32")
    client = FakeOpenAI([_generated(), _generated()])

    async def compiled(script):
        return True, ""

    monkeypatch.setattr(generator, "_compiles", compiled)
    decisions = FakeDecisions(verify_seq=[0.1, 0.95])
    gen = ToolGenerator(_registry(tmp_path), client=client, decisions=decisions)

    tool, msg = asyncio.run(gen.generate("change the thing", CTX))
    assert msg == "" and tool is not None and tool.verified == 0.95
    assert len(client.responses.calls) == 2
    assert "reviewer judged" in client.responses.calls[1]["input"][-1]["content"]


def test_duplicate_gate_returns_existing_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("BRODI_OS", "win32")
    existing = Tool(
        name="notify_me",
        description="Change a thing.",
        script="Set-ItemProperty -Path 'HKCU:\\Software\\x' -Name y -Value 1",
        script_win="Set-ItemProperty -Path 'HKCU:\\Software\\x' -Name y -Value 1",
    )
    client = FakeOpenAI([_generated("notify_me_v2")])

    async def compiled(script):
        return True, ""

    monkeypatch.setattr(generator, "_compiles", compiled)
    gen = ToolGenerator(_registry(tmp_path, [existing]), client=client, decisions=FakeDecisions("notify_me"))
    tool, msg = asyncio.run(gen.generate("change the thing", CTX))
    assert msg == DUPLICATE and tool is existing


def test_system_prompt_is_os_aware(monkeypatch):
    monkeypatch.setenv("BRODI_OS", "win32")
    assert "PowerShell" in generator.system_prompt()
    assert "AppleScript" not in generator.system_prompt()
    monkeypatch.setenv("BRODI_OS", "darwin")
    assert "AppleScript" in generator.system_prompt()
    assert "PowerShell" not in generator.system_prompt()
