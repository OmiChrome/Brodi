"""DynamicMacAgent: the state machine that ties routing, execution and learning together.

States
------
IDLE                -> every utterance is routed by the OpenAI Decisions API.
AWAITING_CONFIRM    -> a risky tool is staged; the Decisions API judges whether
                       the next utterance confirms or cancels it (and, if
                       neither, the utterance is routed as a fresh command).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from . import browser_task, commands, os_backend, policy, resolvers
from .activity import ActivityLog
from .applescript import ContextPoller, run_applescript
from .automations import predefined_plan
from .config import flag
from .generator import DUPLICATE, ToolGenerator
from .memory import MemoryStore
from .multi_step import Plan, PlanRunner, Step, StepResult, cacheable, compose_plan, deterministic_plan, observe_desktop
from .profiler import profiled, span
from .registry import Tool, ToolRegistry
from .router import JevRouter, Route

log = logging.getLogger("brodi.agent")

CONFIRM_THRESHOLD = 0.6
FOLLOWUP_THRESHOLD = 0.5
GOAL_COMPLETE_THRESHOLD = 0.4  # below: ask for the missing detail instead of driving Chrome
FORBIDDEN_THRESHOLD = 0.6  # p(goal requires buying/paying/signing in) at or above which we refuse
ACHIEVED_THRESHOLD = 0.5  # p(final page shows the success criterion); below: one more round, then be honest
BROWSER_MEMORY_S = 15 * 60  # how long a finished browser task stays "recent" for follow-ups
ARG_CONFIDENCE_FLOOR = 0.35  # weakest argument below this -> ask instead of act
PENDING_TTL_S = 60


PRONOUNS = {
    "her",
    "him",
    "them",
    "it",
    "that",
    "this",
    "that person",
    "the same person",
    "same",
    "same one",
    "that one",
}


class State(StrEnum):
    IDLE = "idle"
    AWAITING_CONFIRM = "awaiting_confirm"


@dataclass
class Outcome:
    """What the voice layer should do after an utterance."""

    speak: str | None = None  # text to say directly (fast path)
    handoff_to_llm: bool = False  # let the conversational LLM answer instead
    stop: bool = False  # user asked the assistant itself to stop
    route: Route | None = None
    executed: bool = False
    learned: Tool | None = None
    timings: dict[str, float] = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    memory_hit: bool = False
    observation: dict = field(default_factory=dict)


@dataclass
class Pending:
    tool: Tool
    args: dict[str, str]
    staged_at: float
    utterance: str = ""


class DynamicMacAgent:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        *,
        enable_learning: bool = True,
        on_learning: Callable[[str], Any] | None = None,
        activity: ActivityLog | None = None,
        memory: MemoryStore | None = None,
    ):
        """``on_learning(message)`` is called right before the slow codegen tier runs, so the
        voice layer can say a filler line instead of leaving silence."""
        self.on_learning = on_learning
        self.activity = activity
        self.memory = memory
        if memory is None and registry is None and flag("BRODI_MEMORY_ENABLED"):
            self.memory = MemoryStore(
                retention_days=int(os.environ.get("BRODI_MEMORY_RETENTION_DAYS", "30")),
                max_entries=int(os.environ.get("BRODI_MEMORY_MAX_ENTRIES", "500")),
            )
        self._pending_plan: dict | None = None
        self._script_timeout: float | None = None
        self.registry = registry or ToolRegistry()
        self.router = JevRouter(self.registry)
        self.generator = ToolGenerator(self.registry, decisions=self.router.client) if enable_learning else None
        self.state = State.IDLE
        self.pending: Pending | None = None
        self.pending_question: tuple[Route, float] | None = None
        self._turn_lock = asyncio.Lock()
        self.context = ContextPoller()
        self.last_browser: dict[str, Any] | None = None  # goal, url, title, target_id, finished_at
        self.last_args: dict[str, dict[str, str]] = {}  # tool name -> last used arguments (for "send her another")

    @profiled("startup")
    async def start(self) -> None:
        """Warm the context poller (optional; handle() does it lazily)."""
        await self.context.start()

    async def aclose(self) -> None:
        await self.context.stop()
        if self.generator is not None:
            await self.generator.aclose()
        await self.router.aclose()

    # ----------------------------------------------------------------- public
    @profiled("turn", root=True)
    async def handle(self, utterance: str) -> Outcome:
        # A second transcript must not race a running browser task or consume its confirmation.
        queued = time.perf_counter()
        async with self._turn_lock:
            started = time.perf_counter()
            if utterance.strip():
                self._event("input", utterance)
                self._event("status", "Understanding your task")
            try:
                with span("dispatch"):
                    outcome = await self._handle(utterance)
            except Exception:
                log.exception("command failed")
                self.pending, self.pending_question, self.state = None, None, State.IDLE
                self._pending_plan = None
                outcome = Outcome(speak="That didn't work. Please try again.")
                self._event("error", "The task failed unexpectedly; ready to try again")
            if outcome.speak:
                self._event("output", outcome.speak)
            if outcome.executed:
                if (
                    self.memory
                    and not outcome.memory_hit
                    and cacheable(outcome.steps)
                    and commands.normalize(utterance)[0].casefold().rstrip(".!?")
                    not in {"open browser", "open my browser", "launch browser"}
                ):
                    try:
                        self.memory.store_shortcut(utterance, outcome.steps)
                    except (OSError, ValueError):
                        log.exception("could not store shortcut")
                self._event("result", "Task completed", duration=f"{time.perf_counter() - started:.1f}s")
            if outcome.handoff_to_llm:
                self._event("status", "Thinking of a reply")
            else:
                self._event("status", "Stopping" if outcome.stop else self.waiting_status())
            outcome.timings["queue_ms"] = (started - queued) * 1000
            outcome.timings["total_ms"] = (time.perf_counter() - started) * 1000
            return outcome

    def _event(self, kind: str, message: str, **details: Any) -> None:
        if self.activity is not None:
            self.activity.emit(kind, message, **details)

    def waiting_status(self) -> str:
        if self.pending:
            return "Waiting for your confirmation"
        if self.pending_question or (self.last_browser and self.last_browser.get("status") == "needs_info"):
            return "Waiting for your answer"
        return "Listening for a task"

    def _memory_command(self, text: str) -> Outcome | None:
        if not self.memory:
            return None
        lower = text.lower().rstrip(".!?")
        if lower in {"what do you remember", "show memory", "list memory"}:
            saved = self.memory.list()
            facts = [f"{row['key'].replace('_', ' ')}: {row['value']}" for row in saved["facts"] if not row["expired"]]
            count = sum(not row["expired"] for row in saved["shortcuts"])
            return Outcome(
                speak=("; ".join(facts[:8]) + f". {count} saved shortcuts.") if facts or count else "Nothing saved yet."
            )
        forgetting = re.fullmatch(r"(?:forget|clear memory about)\s+(.+)", text, re.I)
        if forgetting:
            key = forgetting[1].strip().rstrip(".!?")
            key = {
                "my preferred browser": "preferred_browser",
                "preferred browser": "preferred_browser",
                "where i work": "work_directory",
            }.get(key.lower(), key)
            key = "" if key.lower() in {"everything", "all", "all memory"} else key
            self._stage(
                Tool(name="memory_change", description="Delete saved memory", script="", runner="memory", risky=True),
                {"operation": "delete", "key": key},
            )
            return Outcome(speak=f"Forget {key or 'all saved memory'}? Say yes to confirm.")
        remembering = re.fullmatch(r"remember(?: that)?\s+(.+)", text, re.I)
        if not remembering:
            return None
        fact = remembering[1].strip().rstrip(".!?")
        browser = re.fullmatch(r"my preferred browser is\s+(.+)", fact, re.I)
        directory = re.fullmatch(r"I work in\s+(.+)", fact, re.I)
        if browser:
            key, value = "preferred_browser", browser[1]
        elif directory:
            key, value = "work_directory", directory[1]
        else:
            key, value = fact.casefold(), fact
        try:
            self.memory.set_fact(key, value)
        except PermissionError:
            self._stage(
                Tool(name="memory_change", description="Replace a saved fact", script="", runner="memory", risky=True),
                {"operation": "set", "key": key, "value": value},
            )
            return Outcome(speak=f"Replace {key.replace('_', ' ')} with {value}? Say yes to confirm.")
        except ValueError as exc:
            return Outcome(speak=str(exc))
        return Outcome(speak="Got it. Remembered.", executed=True)

    def _plan_catalog(self, ctx) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "args": [
                    {"name": a.name, "kind": a.kind, "criteria": a.resolve_criteria(ctx), "default": a.default}
                    for a in tool.args
                ],
            }
            for tool in self.registry.available(ctx)
            if tool.runner not in {"browser", "memory"}
        ]

    async def _verify_step(self, outcome: Outcome, criterion: str) -> dict:
        observation = {"tool_ok": outcome.executed, "output": (outcome.speak or "")[:600], **outcome.observation}
        if outcome.executed and criterion not in {"", "Tool reports success"}:
            from .decisions import PredicateQuestion

            observation.update(await observe_desktop())
            judged = await self.router.client.evaluate(
                state={"criterion": criterion, "observed": observation},
                questions={
                    "met": PredicateQuestion(
                        instructions="Does the observed result demonstrate the criterion? Do not assume unobserved state."
                    )
                },
            )
            outcome.executed = judged.predicates["met"].probability >= 0.6
            if not outcome.executed:
                outcome.speak = "I couldn't verify that step. Please check the app."
        return observation

    @profiled("plan")
    async def _run_plan(self, plan: Plan, utterance: str, *, context: dict | None = None) -> Outcome:
        result = Outcome(route=Route("plan"))
        ctx = await self.context.latest()
        runner = PlanRunner()
        used = context["used"] if context else 0
        deadline = context["deadline"] if context else time.monotonic() + runner.total_timeout
        previous = list(context["completed"]) if context else []
        if used >= runner.max_steps or deadline <= time.monotonic():
            result.speak = "I reached the plan limit. Tell me a smaller task."
            return result
        runner.max_steps -= used
        runner.total_timeout = deadline - time.monotonic()

        def validate(step: Step) -> Tool:
            tool = self.registry.get(step.tool)
            if tool is None or not tool.is_available(ctx) or tool.runner in {"browser", "memory"}:
                raise ValueError("That plan contains an unavailable or unsupported tool.")
            specs = {a.name: a for a in tool.args}
            if set(step.args) - set(specs):
                raise ValueError("That plan contains an unexpected argument.")
            for name, spec in specs.items():
                value = step.args.get(name, spec.default)
                if value is None or (not value and spec.default is None):
                    raise ValueError(f"I need the {name} before running that task.")
                criteria = spec.resolve_criteria(ctx)
                if spec.kind == "enum" and criteria and value not in criteria:
                    raise ValueError(f"That plan selected an unknown {name}.")
            violations = policy.check(tool.render(step.args), tool.active_scope)
            if violations:
                raise ValueError("That action is blocked by the safety policy.")
            return tool

        try:
            for step in plan.steps:
                validate(step)
        except ValueError as exc:
            result.speak = str(exc)
            return result

        async def execute_step(step: Step, timeout: float) -> StepResult:
            try:
                tool = validate(step)  # Recovery steps use exactly the same gate.
            except ValueError as exc:
                return StepResult(False, str(exc))
            if tool.risky:
                self._stage(tool, dict(step.args), utterance)
                return StepResult(False, f"That will {_say_tool(tool, step.args)}. Go ahead?", paused=True)
            self._event("progress", "Running the next step", action=tool.description)
            outcome = Outcome()
            self._script_timeout = timeout
            try:
                await self._execute(tool, dict(step.args), outcome, utterance=utterance)
            finally:
                self._script_timeout = None
            observation = await self._verify_step(outcome, step.success)
            return StepResult(outcome.executed, outcome.speak or "", observation)

        async def recover(step: Step, observation: dict) -> Step | None:
            self._event("progress", "The step failed twice; planning a bounded recovery", observation=observation)
            alternative = await compose_plan(
                f"Recover only this failed action: {step.model_dump_json()}. Never repeat earlier completed actions.",
                self._plan_catalog(ctx),
                self.router.client.openai_client,
                observation,
            )
            if len(alternative.steps) == 1:
                alternative.steps[0].success = step.success
                return alternative.steps[0]
            return None

        ran = await runner.run(
            plan,
            execute_step,
            observe_desktop,
            retry_safe=lambda step: step.tool in {"open_app", "chrome_open"} and not validate(step).risky,
            recover=recover,
        )
        completed = previous + [
            {"tool": step.tool, "args": step.args, "success": step.success} for step in ran.completed
        ]
        if ran.paused_at is not None:
            self._pending_plan = {
                "remaining": plan.steps[ran.paused_at + 1 :],
                "completed": completed,
                "used": used + ran.attempts,
                "deadline": deadline,
                "utterance": utterance,
                "success": plan.steps[ran.paused_at].success,
            }
        result.executed, result.speak, result.steps = ran.ok, ran.message, completed if ran.ok else []
        return result

    async def _handle(self, utterance: str) -> Outcome:
        utterance, wake = commands.normalize(utterance)
        if not utterance:
            return Outcome(speak="Hey bro, I'm listening." if wake else None)
        if self.pending and time.time() - self.pending.staged_at > PENDING_TTL_S:
            self.pending, self.state = None, State.IDLE
            self._pending_plan = None
            if utterance.lower().rstrip(".!?") in {"yes", "sure", "go ahead", "do it"}:
                return Outcome(speak="That confirmation expired. Tell me the command again.")
        if self.memory and not self.pending and not self.pending_question:
            memory_result = self._memory_command(utterance)
            if memory_result is not None:
                return memory_result
            shortcut = self.memory.match_shortcut(utterance)
            if shortcut:
                self._event("progress", "Using your saved shortcut; no model routing needed")
                result = await self._run_plan(Plan(steps=[Step(**step) for step in shortcut.steps]), utterance)
                result.memory_hit = True
                return result
            if utterance.casefold().rstrip(".!?") in {"open browser", "open my browser", "launch browser"}:
                if preferred := self.memory.get_fact("preferred_browser"):
                    utterance = "open " + preferred
        t0 = time.perf_counter()
        ctx = await self.context.latest()
        t_ctx = time.perf_counter()

        recent = self._recent_browser()
        if not self.pending and not self.pending_question and flag("BRODI_ENABLE_MULTI_STEP"):
            plan = predefined_plan(utterance) or deterministic_plan(utterance, ctx.running_apps + ctx.installed_apps)
            if plan:
                return await self._run_plan(plan, utterance)
        route = None
        if self.pending_question:
            question, asked_at = self.pending_question
            self.pending_question = None
            if utterance.lower().rstrip(".!?") in {"cancel", "never mind", "nevermind", "no"}:
                return Outcome(speak="Cancelled.")
            if (
                time.time() - asked_at <= PENDING_TTL_S
                and commands.parse_command(utterance, ctx.running_apps + ctx.installed_apps) is None
                and not re.match(r"^(?:open|launch|start|stop|quit|go to|search)\b", utterance, re.I)
            ):
                spec = (
                    next((a for a in question.tool.args if a.name == question.missing_arg), None)
                    if question.tool
                    else None
                )
                answer = utterance
                if spec and spec.kind == "enum":
                    choices = spec.resolve_criteria(ctx)
                    answer = next((c for c in choices if c.lower() == utterance.lower().rstrip(".!?")), "")
                    if not answer:
                        self.pending_question = (question, asked_at)
                        return Outcome(speak=f"Which {spec.name}? Choose from {', '.join(list(choices)[:8])}.")
                question.args[question.missing_arg] = answer
                question.weakest_arg = None
                question.kind, question.reply, question.missing_arg = "tool", "", ""
                route = question
        if route is None:
            answer = utterance.casefold().rstrip(".!?")
            if self.pending and answer in {
                "yes",
                "yeah",
                "yep",
                "go ahead",
                "do it",
                "confirmed",
                "sure",
                "no",
                "cancel",
                "never mind",
                "nevermind",
            }:
                denied = answer in {"no", "cancel", "never mind", "nevermind"}
                route = Route("chat", is_confirmation=0 if denied else 1, is_denial=1 if denied else 0)
            else:
                route = await self.router.route(
                    utterance, ctx, awaiting_confirmation=self.state is State.AWAITING_CONFIRM, recent_browser=recent
                )
        # A follow-up to the browser task wins over whatever else Jev matched (often a mis-scoped web_task).
        if (
            recent
            and route.browser_followup >= FOLLOWUP_THRESHOLD
            and route.kind in ("tool", "uncertain", "new_action", "chat")
        ):
            wt = self.registry.get("web_task")
            if wt is not None:
                route.kind, route.tool, route.args = "tool", wt, {"site": "current_tab"}
                route.weakest_arg = None
            lb = self.last_browser or {}
            if lb.get("status") == "needs_info" and wt is not None:
                # Answer to a clarifying question: merge, then check whether anything else is still missing.
                merged = f"{lb['goal']} {utterance.strip()}"
                asked = list(lb.get("asked") or [])
                p_complete, missing = await self.router.web_goal_check(merged, ctx)
                log.info("merged goal completeness %.2f missing=%s asked=%s", p_complete, missing, asked)
                # Re-ask an item at most once (the user may have answered a different gap first); three asks total.
                if (
                    p_complete < GOAL_COMPLETE_THRESHOLD
                    and missing != "nothing"
                    and asked.count(missing) < 2
                    and len(asked) < 3
                ):
                    question = _ask_for(missing, lb.get("site"))
                    if missing in asked:
                        question = "Sorry, I still need this. " + question
                    self.last_browser = {
                        **lb,
                        "goal": merged,
                        "question": question,
                        "asked": asked + [missing],
                        "finished_at": time.time(),
                    }
                    return Outcome(
                        route=route,
                        speak=question,
                        timings={"context_ms": (t_ctx - t0) * 1e3, "route_ms": (time.perf_counter() - t_ctx) * 1e3},
                    )
                self.last_browser = {**lb, "goal": merged, "finished_at": time.time()}
                utterance = merged  # run the fully specified goal
                recent = self._recent_browser()
        t_route = time.perf_counter()
        outcome = Outcome(route=route, timings={"context_ms": (t_ctx - t0) * 1e3, "route_ms": (t_route - t_ctx) * 1e3})

        if route.kind in {"greeting", "clarify"}:
            outcome.speak = route.reply
            if route.kind == "clarify":
                self.pending_question = (route, time.time())
            return outcome

        if self.state is State.AWAITING_CONFIRM and self.pending:
            if route.is_confirmation >= CONFIRM_THRESHOLD and route.is_confirmation > route.is_denial:
                pending, self.pending, self.state = self.pending, None, State.IDLE
                if self._pending_plan and time.monotonic() >= self._pending_plan["deadline"]:
                    self._pending_plan = None
                    outcome.speak = "That plan expired. Tell me the task again."
                    return outcome
                if self._pending_plan:
                    timeout = min(
                        int(os.environ.get("BRODI_STEP_TIMEOUT_MS", "8000")) / 1000,
                        self._pending_plan["deadline"] - time.monotonic(),
                    )
                    self._script_timeout = timeout
                    try:

                        async def confirmed_step():
                            await self._execute(pending.tool, pending.args, outcome, utterance=pending.utterance)
                            await self._verify_step(outcome, self._pending_plan["success"])

                        await asyncio.wait_for(confirmed_step(), timeout)
                    except TimeoutError:
                        self._pending_plan = None
                        return Outcome(speak="That step timed out. Check the app before retrying.")
                    finally:
                        self._script_timeout = None
                else:
                    await self._execute(pending.tool, pending.args, outcome, utterance=pending.utterance)
                if self._pending_plan:
                    context, self._pending_plan = self._pending_plan, None
                    if not outcome.executed:
                        return outcome
                    context["completed"].append(
                        {"tool": pending.tool.name, "args": pending.args, "success": context["success"]}
                    )
                    if context["remaining"]:
                        return await self._run_plan(
                            Plan(steps=context["remaining"]), context["utterance"], context=context
                        )
                    outcome.steps = context["completed"]
                return outcome
            if route.is_denial >= CONFIRM_THRESHOLD:
                self.pending, self.state = None, State.IDLE
                self._pending_plan = None
                outcome.speak = "Cancelled."
                return outcome
            # Neither: drop the pending action and treat this as a new command.
            self.pending, self.state = None, State.IDLE
            self._pending_plan = None

        if route.multi_step and flag("BRODI_ENABLE_MULTI_STEP"):
            plan = await asyncio.wait_for(
                compose_plan(utterance, self._plan_catalog(ctx), self.router.client.openai_client),
                min(
                    int(os.environ.get("BRODI_STEP_TIMEOUT_MS", "8000")) / 1000,
                    float(os.environ.get("BRODI_PLAN_TIMEOUT_S", "40")),
                ),
            )
            return await self._run_plan(plan, utterance)

        if route.kind == "chat":
            outcome.handoff_to_llm = True
            return outcome

        if route.kind == "stop":
            outcome.stop = True
            outcome.speak = "Okay, stopping. Goodbye."
            return outcome

        if route.tool:
            unresolved = self._resolve_pronouns(route.tool, route.args)
            if unresolved:
                outcome.speak = f"Who do you mean by {unresolved[1]!r} for {_say_tool(route.tool)}?"
                return outcome

        if route.kind == "uncertain" and route.tool:
            top = sorted(route.probabilities.items(), key=lambda kv: -kv[1])[:2]
            if len(top) == 2 and top[1][0] not in ("chat", "new_action") and top[1][1] > 0.2:
                outcome.speak = f"Did you want {_say_tool(route.tool)} or {_say_tool(self.registry.get(top[1][0]))}?"
            else:
                outcome.speak = f"Should I {_say_tool(route.tool)}?"
            self._stage(route.tool, route.args)
            return outcome

        if route.kind == "tool" and route.tool:
            if (
                route.tool.runner == "browser"
                and route.weakest_arg
                and route.weakest_arg[0] == "site"
                and route.weakest_arg[1] < ARG_CONFIDENCE_FLOOR
            ):
                route.args["site"] = "current_tab" if recent else "google"
                route.weakest_arg = None
            if route.weakest_arg and route.weakest_arg[1] < ARG_CONFIDENCE_FLOOR:
                name, _ = route.weakest_arg
                outcome.speak = f"Which {name} did you mean? I heard {route.args.get(name, 'nothing')}."
                route.missing_arg = name
                self.pending_question = (route, time.time())
                return outcome
            if route.tool.runner == "browser":
                refusal = _browser_refusal(utterance, route.web_goal_forbidden)
                if refusal:
                    outcome.speak = refusal
                    return outcome
                is_followup = bool(recent) and route.browser_followup >= FOLLOWUP_THRESHOLD
                if (
                    not is_followup
                    and route.web_goal_complete < GOAL_COMPLETE_THRESHOLD
                    and route.web_goal_missing != "nothing"
                ):
                    question = _ask_for(route.web_goal_missing, route.args.get("site"))
                    self.last_browser = {  # a clarification is a "recent task" the answer follows up on
                        "goal": utterance,
                        "url": "",
                        "title": "",
                        "target_id": None,
                        "status": "needs_info",
                        "site": route.args.get("site", "google"),
                        "question": question,
                        "asked": [route.web_goal_missing],
                        "finished_at": time.time(),
                    }
                    outcome.speak = question
                    return outcome
                if policy.browser_goal_needs_confirm(utterance):
                    self._stage(route.tool, route.args, utterance)
                    outcome.speak = f"I'll do this in Chrome: {utterance.rstrip('.?!')}. Go ahead?"
                    return outcome
                await self._execute(route.tool, route.args, outcome, utterance=utterance)
                return outcome
            if route.tool.risky:
                self._stage(route.tool, route.args)
                outcome.speak = f"That will {_say_tool(route.tool, route.args)}. Should I go ahead?"
                return outcome
            await self._execute(route.tool, route.args, outcome, utterance=utterance)
            return outcome

        # new_action -> heavy tier
        if self.generator is None:
            outcome.speak = "I don't have a tool for that yet."
            return outcome
        if self.on_learning:
            try:
                self.on_learning("I don't know that one yet. Give me a moment.")
            except Exception:  # never let UX sugar break the pipeline
                log.exception("on_learning callback failed")
        self._event("status", "Learning how to do this task")
        tool, msg = await self.generator.generate(utterance, ctx)
        outcome.timings["codegen_ms"] = (time.perf_counter() - t_route) * 1e3
        if tool is None:
            outcome.speak = msg
            return outcome

        # Fill the tool's arguments with Jev now that it is in the registry / identified.
        route2 = await self.router.route(utterance, ctx)
        args = route2.args if route2.tool and route2.tool.name == tool.name else {}

        if msg == DUPLICATE:
            # The router missed an existing tool; use it through the normal gates.
            log.info("codegen resolved to existing tool %s", tool.name)
            if tool.risky:
                self._stage(tool, args)
                outcome.speak = f"That will {_say_tool(tool, args)}. Should I go ahead?"
                return outcome
            await self._execute(tool, args, outcome)
            return outcome

        outcome.learned = tool
        # A freshly written script always gets a spoken confirmation before its first run;
        # generated code can compile and pass review yet still do the wrong thing.
        self._stage(tool, args)
        hedge = ", though I'm not certain it works" if tool.verified is not None and tool.verified < 0.6 else ""
        outcome.speak = f"I wrote a new action to {_say_tool(tool, args)}{hedge}. Should I run it?"
        return outcome

    # ---------------------------------------------------------------- helpers
    @profiled("browser")
    async def _execute_browser(self, tool: Tool, args: dict[str, str], outcome: Outcome, utterance: str) -> None:
        refusal = _browser_refusal(utterance, 0.0)
        if refusal:
            outcome.speak = refusal
            return
        site = args.get("site", "other")
        self._event("status", "Checking browser access")
        access_error = await asyncio.to_thread(browser_task.access_error)
        if access_error:
            self._event("error", access_error)
            outcome.speak = access_error
            # At least show the requested site when note interaction is unavailable.
            if site == "google_keep" and (opener := self.registry.get("chrome_open")):
                opened = Outcome()
                await self._execute(opener, {"site": site, "query": ""}, opened)
                prefix = (
                    "I opened Google Keep, but couldn't show the note. "
                    if opened.executed
                    else (opened.speak or "") + " "
                )
                outcome.speak = prefix + access_error
            return
        recent = self._recent_browser()
        lb = self.last_browser or {}
        reuse = None
        if site == "current_tab" and recent and await asyncio.to_thread(browser_task.tab_exists, lb.get("target_id")):
            reuse = lb.get("target_id")
        if site == "current_tab":
            site = lb.get("site") or "google"
        if self.on_learning:
            try:
                self.on_learning("On it. Driving Chrome now." if not reuse else "Continuing in the same tab.")
            except Exception:
                log.exception("on_learning callback failed")

        # Rewrite the raw sentence into a self-contained objective with a visible success criterion.
        t0 = time.perf_counter()
        self._event("status", "Planning the browser task")
        context: dict[str, Any] = {"today": time.strftime("%A %d %B %Y")}
        if recent:
            context["conversation_so_far"] = recent["goal"]
            if recent.get("page_title"):
                context["current_page"] = {"title": recent["page_title"], "url": recent["page_url"][:200]}
        try:
            composed = await browser_task.compose_goal(utterance, context)
            objective, success, search_query = composed.objective, composed.success, composed.search_query
        except Exception:
            log.exception("goal composition failed; using the raw utterance")
            objective, success, search_query = utterance.strip(), "", None
        outcome.timings["compose_ms"] = (time.perf_counter() - t0) * 1e3
        log.info("browser objective: %s | success: %s | search: %s", objective, success, search_query)
        self._event("action", "Working in Chrome", goal=objective)
        goal = objective + (f"\nDone when: {success}" if success else "")
        if reuse:
            goal += "\nContinue from the current page state; do not redo completed steps."
        if search_query and site in browser_task.SEARCH_URLS:
            # A refined search: land straight on the new results, in the same tab when we have one.
            start_url = browser_task.start_url_for(site, objective, search_query)
        else:
            start_url = "" if reuse else browser_task.start_url_for(site, objective, None)

        t0 = time.perf_counter()
        activity_options = {"on_activity": self._browser_event} if self.activity is not None else {}
        result = await browser_task.run_task(
            start_url, goal, on_progress=self.on_learning, reuse_target=reuse, **activity_options
        )
        achieved: float | None = None
        if result.status == "blocked" and success and result.page_text:
            # The agent may have stopped because the results were already the answer.
            achieved = await self._verify_outcome(objective, success, result)
            if achieved is not None and achieved >= ACHIEVED_THRESHOLD:
                log.info("blocked run judged achieved (%.2f); reporting done", achieved)
                result.status = "done"
        if result.status == "done" and success:
            achieved = await self._verify_outcome(objective, success, result)
            if (
                achieved is not None
                and achieved < ACHIEVED_THRESHOLD
                and await asyncio.to_thread(browser_task.tab_exists, result.target_id)
            ):
                log.info("outcome not confirmed (%.2f); one more round", achieved)
                nudge = goal + f"\nNOT yet achieved: the page does not show {success}. Keep going from here."
                result = await browser_task.run_task(
                    "", nudge, on_progress=self.on_learning, reuse_target=result.target_id, **activity_options
                )
                if result.status == "done":
                    achieved = await self._verify_outcome(objective, success, result)
        outcome.timings["browser_ms"] = (time.perf_counter() - t0) * 1e3
        self._event(
            "observation",
            "Browser result",
            status=result.status,
            title=result.title,
            url=result.url,
            visible_text=result.page_text,
        )
        outcome.executed = result.status == "done" and (achieved is None or achieved >= ACHIEVED_THRESHOLD)
        log.info(
            "browser task %s: %s steps=%d %.0fms achieved=%s url=%s reuse=%s",
            result.status,
            objective[:70],
            result.steps,
            result.elapsed_ms,
            f"{achieved:.2f}" if achieved is not None else "n/a",
            result.url,
            bool(reuse),
        )
        if result.status != "error":
            self.last_browser = {
                "goal": (recent["goal"] + " | " + objective) if recent else objective,
                "url": result.url,
                "title": result.title,
                "target_id": result.target_id or reuse,
                "status": result.status,
                "site": site,
                "finished_at": time.time(),
            }
        if result.status == "done" and achieved is not None and achieved < ACHIEVED_THRESHOLD:
            where = f" Chrome is on {browser_task.spoken_title(result.title)}." if result.title else ""
            outcome.speak = (
                f"I got as far as I could, but I can't confirm the page shows {success}.{where} Take a look."
            )
            return
        outcome.speak = result.spoken
        if result.status in ("blocked", "timeout"):
            reason = await self._explain_stall(goal, result)
            if reason:
                outcome.speak = f"{result.spoken} {reason}"

    def _browser_event(self, kind: str, message: str, details: dict[str, Any]) -> None:
        self._event(kind, message, **details)

    async def _verify_outcome(self, objective: str, success: str, result: browser_task.BrowserResult) -> float | None:
        """Independent check that the final page actually shows what the user asked for."""
        from .decisions import PredicateQuestion

        try:
            resp = await self.router.client.evaluate(
                state={
                    "objective": objective,
                    "success_criterion": success,
                    "final_page": {
                        "title": result.title,
                        "url": result.url[:300],
                        "visible_text": result.page_text[:3000],
                    },
                },
                questions={
                    "achieved": PredicateQuestion(
                        instructions=(
                            "A browser agent claims it completed `objective`. Judging only from `final_page`, does the "
                            "page visibly satisfy `success_criterion`? A search box containing the right words, or a "
                            "results list for a different or broader query, does not count."
                        ),
                        criteria={
                            "true": "The page shows what the success criterion describes",
                            "false": "It does not, or shows something else (wrong item, unsubmitted search, generic results)",
                        },
                    )
                },
            )
            return float(resp.predicates["achieved"].noul)
        except Exception:
            log.exception("outcome verification failed; skipping")
            return None

    async def _explain_stall(self, goal: str, result: browser_task.BrowserResult) -> str:
        """One Jev judgment: why did the browser agent stop short? Returns a spoken hint or an empty string."""
        from .decisions import ChoiceQuestion

        try:
            resp = await self.router.client.evaluate(
                state={
                    "goal": goal[:1500],
                    "final_page": {"title": result.title, "url": result.url[:300]},
                    "steps_taken": [f"{h['operation']} {h['action'][:60]}" for h in result.history[-8:]],
                    "stopped_because": result.error or result.status,
                },
                questions={
                    "why": ChoiceQuestion(
                        instructions="A browser agent stopped before finishing `goal`. From the final page and the steps taken, what is the most likely reason?",
                        criteria={
                            "missing_details": "The request lacked a concrete value a form needs (exact dates, a specific item, a name)",
                            "login_required": "The site wants sign-in, a password, or a verification step",
                            "unsupported_page": "The page uses controls the agent cannot operate (canvas, custom widgets, popups)",
                            "already_satisfied": "The goal looks achieved on the final page; the agent just did not recognise it",
                            "wandered": "The agent drifted into unrelated features or pages",
                            "unclear": "Cannot tell",
                        },
                    )
                },
            )
            why = resp.choices["why"]
            if float(why.probabilities.get(why.choice, 0)) < 0.55:
                return ""
            return _STALL_HINTS.get(why.choice, "")
        except Exception:
            log.exception("stall explanation failed")
            return ""

    def _recent_browser(self) -> dict[str, str] | None:
        lb = self.last_browser
        if not lb or time.time() - lb["finished_at"] > BROWSER_MEMORY_S:
            return None
        rec = {"goal": lb["goal"], "page_title": lb["title"], "page_url": lb["url"], "status": lb["status"]}
        if lb.get("question"):
            rec["question"] = lb["question"]
        return rec

    def _stage(self, tool: Tool, args: dict[str, str], utterance: str = "") -> None:
        self.pending = Pending(tool=tool, args=args, staged_at=time.time(), utterance=utterance)
        self.state = State.AWAITING_CONFIRM

    def _resolve_pronouns(self, tool: Tool, args: dict[str, str]) -> tuple[str, str] | None:
        """Replace pronoun text arguments ("her", "him", "them", "that") with the value used the
        last time this tool ran. Returns (arg, pronoun) if one can't be resolved."""
        previous = self.last_args.get(tool.name, {})
        for spec in tool.args:
            if spec.kind != "text":
                continue
            value = args.get(spec.name, "")
            if value.strip().lower().strip(".,!?") in PRONOUNS:
                if spec.name in previous:
                    args[spec.name] = previous[spec.name]
                else:
                    return spec.name, value
        return None

    @profiled("execute")
    async def _execute(
        self, tool: Tool, args: dict[str, str], outcome: Outcome, prefix: str = "", utterance: str = ""
    ) -> None:
        if tool.name in {"open_app", "quit_app", "type_text"}:
            app = args.get("app", "").strip().casefold().removesuffix(".exe")
            if app in policy.BLOCKED_APPS | policy.WINDOWS_BLOCKED_APPS:
                outcome.speak = "That application is blocked by the safety policy."
                return
        if tool.runner == "browser":
            await self._execute_browser(tool, args, outcome, utterance)
            return
        if tool.runner == "memory":
            if not self.memory:
                outcome.speak = "Memory is disabled."
                return
            if args["operation"] == "delete":
                self.memory.delete(args.get("key") or None, confirmed=True)
            else:
                self.memory.set_fact(args["key"], args["value"], confirmed=True)
            outcome.executed, outcome.speak = True, "Got it."
            return
        if tool.runner == "native_text" and os_backend.is_windows():
            from .windows_uia import WindowTarget, execute

            self._event("action", "Typing into " + args.get("app", "the selected app"))
            result = await execute(
                "set_text",
                WindowTarget(title=args.get("app", "")),
                text=args.get("text", ""),
                append=True,
                timeout=min(6, self._script_timeout or 6),
            )
            outcome.executed = result.ok
            outcome.speak = "Got it." if result.ok else result.error
            self._event(
                "observation" if result.ok else "error", "Text input result", output=result.output or result.error
            )
            return
        if tool.runner == "windows_uia" and os_backend.is_windows():
            from .windows_uia import WindowTarget, execute

            operation = {
                "focus_window": "focus_window",
                "window_state": "get_window_state",
                "click_native_button": "click_button",
            }.get(tool.name)
            if not operation:
                outcome.speak = "Unsupported Windows control action."
                return
            self._event("action", tool.description)
            result = await execute(
                operation,
                WindowTarget(title=args.get("app", "")),
                timeout=min(6, self._script_timeout or 6),
                **({"name": args.get("name", "")} if operation == "click_button" else {}),
            )
            outcome.executed = result.ok
            if result.ok and operation == "get_window_state":
                import json

                outcome.speak = json.loads(result.output).get("title", "That window") + " is open."
            else:
                outcome.speak = "Got it." if result.ok else result.error
            self._event(
                "observation" if result.ok else "error", "Window control result", output=result.output or result.error
            )
            return
        if tool.computed:
            self._event("status", "Looking up information for your task")
            args = dict(args)
            t_r = time.perf_counter()
            for name, spec in tool.computed.items():
                try:
                    args[name] = await resolvers.run(spec["fn"], args.get(spec.get("from", ""), ""))
                except resolvers.ResolveError as e:
                    outcome.speak = f"{prefix}{e}"
                    return
            outcome.timings["resolve_ms"] = (time.perf_counter() - t_r) * 1e3
        script = tool.render(args)
        violations = policy.check(script, tool.active_scope)
        if violations:
            log.warning("policy blocked %s(%s): %s", tool.name, args, "; ".join(map(str, violations)))
            outcome.speak = f"{prefix}I can't do that. Voice control isn't allowed to {_say_violation(violations[0])}."
            self._event("error", "The action was blocked by the safety policy", reason=_say_violation(violations[0]))
            return
        self._event("action", _activity_action(tool, args))
        t0 = time.perf_counter()
        result = None
        fast_launch = False
        if (
            os_backend.is_windows()
            and tool.source == "seed"
            and tool.name in {"open_app", "chrome_open"}
            and flag("BRODI_FAST_LAUNCH")
        ):
            from .native_launch import execute

            result = await execute(tool.name, args)
            fast_launch = result is not None
        if result is None:
            result = (
                await run_applescript(script, timeout=self._script_timeout)
                if self._script_timeout
                else await run_applescript(script)
            )
        outcome.timings["exec_ms"] = (time.perf_counter() - t0) * 1e3
        outcome.executed = result.ok
        outcome.observation = {"tool_ok": result.ok, "output": result.output[:1000], "error": result.error[:300]}
        if result.output:
            self._event("observation", "Tool output", output=result.output)
        if result.ok and args:
            self.last_args[tool.name] = dict(args)
        if result.ok:
            outcome.steps.append({"tool": tool.name, "args": dict(args)})
        if not result.ok:
            self._event("error", "The action did not finish", reason=result.error)
            log.warning("tool %s failed: %s", tool.name, result.error)
            outcome.speak = f"{prefix}That didn't work: {_short(result.error)}"
            return
        out = result.output
        if tool.name in BROWSER_OPENERS:
            # A plain open/search is context for the next web task ("show me a black one").
            self.last_browser = {
                "goal": f"{tool.description.rstrip('.')}: {', '.join(f'{k}={v}' for k, v in args.items())}",
                "url": out if out.startswith("http") else "",
                "title": "",
                "target_id": None,
                "status": "done",
                "site": BROWSER_OPENERS[tool.name](args),
                "finished_at": time.time(),
            }
        if fast_launch:
            outcome.speak = "Got it."
        elif tool.speak == "done":
            outcome.speak = f"{prefix}Done." if prefix else "Done."
        elif tool.speak == "result":
            outcome.speak = f"{prefix}{out or 'Done.'}"
        else:
            outcome.speak = prefix + tool.speak.replace("{result}", out or "nothing")


_ASK_FOR = {
    "exact_dates": "Which exact date should I use?",
    "destination": "Which city or airport? A country alone isn't enough for the search.",
    "origin": "Where are you flying from?",
    "product": "Which product exactly should I look for?",
    "recipient": "Who is it for?",
    "content": "What should it say?",
}
_STALL_HINTS = {
    "missing_details": "It looks like it needed more specifics from you, such as exact dates.",
    "login_required": "The site is asking to sign in, which I don't do.",
    "unsupported_page": "That page has controls I can't operate.",
    "already_satisfied": "Though the page may already show what you asked for.",
    "wandered": "It drifted off the main form; try a shorter, more specific request.",
}


# Tools that open a page without the browser agent; they seed the "recent browser" context for follow-ups.
BROWSER_OPENERS: dict[str, Callable[[dict[str, str]], str]] = {
    "chrome_open": lambda a: "google" if a.get("site") in (None, "other") else a.get("site", "google"),
    "safari_open": lambda a: "google",
    "youtube_play": lambda a: "youtube",
}


def _activity_action(tool: Tool, args: dict[str, str]) -> str:
    if tool.name == "open_app":
        return "Opening " + args.get("app", "the app")
    if tool.name == "quit_app":
        return "Closing " + args.get("app", "the app")
    if tool.name == "chrome_open":
        site = args.get("site", "other")
        name = {"youtube": "YouTube", "google_keep": "Google Keep", "gmail": "Gmail", "github": "GitHub"}.get(
            site, site.replace("_", " ").title()
        )
        query = args.get("query", "")
        if query and site in commands.SEARCH_URLS and commands.site_named(query) != site:
            return f"Searching {name} for {query}"
        return "Opening " + (query if site == "other" else name)
    return {
        "youtube_play": "Finding a video on YouTube",
        "set_volume": "Changing the volume",
        "take_screenshot": "Taking a screenshot",
        "notes_create": "Creating a note",
        "finder_open_folder": "Opening a folder",
    }.get(tool.name, tool.description.rstrip("."))


def _browser_refusal(utterance: str, forbidden_p: float) -> str | None:
    """Spoken refusal for a browser goal, or None. Hard terms come from the policy regex; whether the task
    needs buying/paying/signing in is Jev's judgment (probability passed in)."""
    bad = policy.check_browser_goal(utterance)
    if bad:
        return f"I can't do that in the browser. Voice control doesn't handle {bad[0].detail}."
    if forbidden_p >= FORBIDDEN_THRESHOLD:
        return "That would need me to buy, pay, sign in, or change an account, which voice control doesn't do. I can search and compare for you."
    return None


def _ask_for(missing: str, site: str | None) -> str:
    if missing == "exact_dates" and site == "google_flights":
        return "Which exact dates? I need the departure and the return."
    return _ASK_FOR.get(missing, "Can you give me the missing details?")


def _say_tool(tool: Tool | None, args: dict[str, str] | None = None) -> str:
    if tool is None:
        return "that"
    text = tool.description.rstrip(".")
    text = text[0].lower() + text[1:]
    if args:
        text += " with " + ", ".join(f"{k} {v}" for k, v in args.items())
    return text


def _say_violation(v: policy.Violation) -> str:
    spoken = {
        "blocked app": f"control {v.detail}",
        "protected path": "touch system or configuration files",
        "shell": "run system commands",
        "destructive script": "run destructive system commands",
        "power/session control": "control power or the session",
        "administrator privileges": "use administrator privileges",
        "system preferences": "open System Settings",
        "system preference writes": "change system preferences",
        "emptying the trash": "empty the trash",
        "deleting items": "delete files",
        "moving files": "move files",
    }
    return spoken.get(v.rule, v.rule)


def _short(s: str, n: int = 140) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
