import asyncio
import json
from types import SimpleNamespace

import pytest

from brodi import policy
from brodi.automations import predefined_plan
from brodi.demo import TASKS, run_tasks
from brodi.profiler import Profile, profiled, span
from brodi.registry import Tool, _template_violations
from brodi.speech import SpeechAudio


def test_trace_records_nested_thread_work_errors_and_no_arguments():
    @profiled("worker")
    def work(secret):
        raise ValueError(secret)

    async def scenario():
        profile = Profile()
        with profile.capture(), span("turn"), pytest.raises(ValueError):
            await asyncio.to_thread(work, "private utterance")
        return profile

    profile = asyncio.run(scenario())
    events = profile.report()["events"]
    assert [e["stage"] for e in events] == ["turn", "worker"]
    assert events[1]["parent"] == events[0]["id"]
    assert events[1]["error"] == "ValueError"
    assert all(e["duration_ms"] >= 0 for e in events)
    assert "private utterance" not in json.dumps(profile.report())
    assert len(profile.trace()["traceEvents"]) == 2


def test_root_trace_is_opt_in_and_export_failure_does_not_break_command(monkeypatch, tmp_path):
    @profiled("turn", root=True)
    async def command(text):
        return text

    monkeypatch.setenv("BRODI_PROFILE", str(tmp_path / "trace.jsonl"))
    assert asyncio.run(command("secret")) == "secret"
    record = json.loads((tmp_path / "trace.jsonl").read_text())
    assert record["stages"][0]["stage"] == "turn"
    assert "secret" not in json.dumps(record)
    # A directory cannot be opened as a trace file; command still succeeds.
    monkeypatch.setenv("BRODI_PROFILE", str(tmp_path))
    assert asyncio.run(command("still works")) == "still works"


def test_catalog_cache_invalidates_when_script_or_policy_mode_changes(monkeypatch):
    monkeypatch.setenv("BRODI_OS", "win32")
    _template_violations.cache_clear()
    tool = Tool("test", "test", "", script_win='Write-Output "hello"')
    assert not tool.blocked
    assert not tool.blocked
    assert _template_violations.cache_info().hits >= 1
    tool.script_win = 'Remove-Item "C:\\Users\\someone\\document.txt"'
    assert tool.blocked
    monkeypatch.setattr(policy, "ENABLED", False)
    assert not tool.blocked
    monkeypatch.setattr(policy, "ENABLED", True)
    assert tool.blocked


def test_context_poller_does_not_retain_the_callers_profile(monkeypatch):
    from brodi.applescript import ContextPoller, MacContext

    poller = ContextPoller(interval=0.001)
    refreshed = []

    async def refresh():
        with span("context.refresh"):
            refreshed.append(True)
            return MacContext("Explorer", ["Explorer"], [])

    monkeypatch.setattr(poller, "_refresh", refresh)

    async def scenario():
        profile = Profile()
        with profile.capture():
            await poller.start()
            # Allow background refreshes; they must use their own context.
            await asyncio.sleep(0.02)
            await poller.stop()
        return profile

    profile = asyncio.run(scenario())
    assert len(refreshed) > 1
    assert len(profile.events) == 1


@pytest.mark.parametrize(
    "text",
    ["Run presentation workspace", "Hey bro, run presentation workspace!", "Could you run presentation workspace"],
)
def test_predefined_plan_is_exact_and_returns_independent_arguments(text):
    plan = predefined_plan(text)
    assert [s.tool for s in plan.steps] == ["open_app", "chrome_open"]
    plan.steps[0].args["app"] = "Terminal"
    assert predefined_plan(text).steps[0].args["app"] == "Google Chrome"
    assert predefined_plan(text + " and delete my files") is None


def test_all_stage_tasks_rehearse_without_launching_or_network(monkeypatch):
    monkeypatch.setenv("BRODI_OS", "win32")
    result, profile = asyncio.run(run_tasks(list(TASKS), repeat=2))
    assert result["ok"] and len(result["rows"]) == 14
    assert not any(e["stage"].startswith(("os.", "decisions")) for e in profile.events)


def test_named_automation_executes_through_policy_and_memory(tmp_path, monkeypatch):
    from brodi import agent as module
    from brodi.agent import DynamicMacAgent
    from brodi.applescript import MacContext, ScriptResult
    from brodi.memory import MemoryStore
    from brodi.registry import ToolRegistry

    monkeypatch.setenv("BRODI_OS", "win32")
    monkeypatch.setenv("BRODI_FAST_LAUNCH", "false")
    registry = ToolRegistry(learned_path=tmp_path / "learned.json")
    assistant = DynamicMacAgent(registry, enable_learning=False, memory=MemoryStore(tmp_path / "memory.db"))
    assistant.context._ctx = MacContext("Explorer", ["Explorer"], ["Google Chrome"])
    scripts = []

    async def run(script, **kwargs):
        scripts.append(script)
        return ScriptResult(True, "opened")

    async def no_network(**kwargs):
        raise AssertionError("No model requests allowed")

    monkeypatch.setattr(module, "run_applescript", run)
    monkeypatch.setattr(assistant.router.client, "evaluate", no_network)

    async def scenario():
        first = await assistant.handle("run presentation workspace")
        second = await assistant.handle("run presentation workspace")
        assert first.executed and second.executed and second.memory_hit
        # Editing a tool after catalog validation must still be blocked.
        registry.get("chrome_open").script_win = 'Remove-Item "document.txt"'
        blocked = await assistant.handle("run presentation research")
        assert not blocked.executed
        await assistant.aclose()

    asyncio.run(scenario())
    assert len(scripts) == 4


def test_speech_reuses_only_fixed_acknowledgements_and_separates_voice(monkeypatch, tmp_path):
    calls = []

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_bytes(self, chunk_size):
            yield b"pcm!"

    def create(**kwargs):
        calls.append(kwargs)
        return Response()

    client = SimpleNamespace(
        audio=SimpleNamespace(speech=SimpleNamespace(with_streaming_response=SimpleNamespace(create=create)))
    )

    async def scenario():
        audio = SpeechAudio(tmp_path)
        assert await audio.pcm(client, "Got it.") == b"pcm!"
        assert await audio.pcm(client, "Got it.") == b"pcm!"
        await audio.pcm(client, "private note")
        await audio.pcm(client, "private note")
        monkeypatch.setenv("BRODI_VOICE", "different")
        await audio.pcm(client, "Got it.")

    asyncio.run(scenario())
    assert len(calls) == 4


def test_failed_speech_download_is_not_cached(tmp_path):
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        raise TimeoutError

    client = SimpleNamespace(
        audio=SimpleNamespace(speech=SimpleNamespace(with_streaming_response=SimpleNamespace(create=fail)))
    )

    async def scenario():
        audio = SpeechAudio(tmp_path)
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await audio.pcm(client, "Got it.")

    asyncio.run(scenario())
    assert len(calls) == 2
