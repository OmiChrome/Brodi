"""Fallback tier: OpenAI writes a new tool for an unseen request.

One ``OPENAI_API_KEY`` drives model services: the Responses API writes a tool (``gpt-6-luna``)
and the Decisions API reviews it. Generated tools are compiled without executing
(``osacompile`` on macOS, the PowerShell AST parser on Windows 11) before they are
registered and persisted to ``tools/learned.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Literal

import openai
from pydantic import BaseModel, Field

from . import os_backend, policy
from .applescript import MacContext
from .decisions import ChoiceQuestion, DecisionsClient, PredicateQuestion
from .models import cache_key, model
from .registry import BUILTIN_PLACEHOLDERS, RISKY_PATTERNS, ArgSpec, PolicyError, Tool, ToolRegistry

log = logging.getLogger("brodi.generator")

MODEL = model("codegen")
REASONING_EFFORT = os.environ.get("BRODI_REASONING_EFFORT", "none")
MAX_ATTEMPTS = int(os.environ.get("BRODI_CODEGEN_ATTEMPTS", "3"))
MAX_OUTPUT_TOKENS = int(os.environ.get("BRODI_CODEGEN_MAX_TOKENS", "1500"))
VERIFY_THRESHOLD = float(os.environ.get("BRODI_VERIFY_THRESHOLD", "0.4"))


_EFFECT_APPLESCRIPT = re.compile(
    r"(do shell script|keystroke|key code|\bclick\b|open location|\bopen\b|activate|\bquit\b|make new|"
    r"\bdelete\b|set volume|display (notification|dialog|alert)|reload|\bclose\b|execute .*javascript|"
    r"do javascript|playpause|\bplay\b|\bpause\b|next track|previous track|empty trash|"
    r"set [\w\s']+ of [\w\s']+ to|set (autohide|dark mode|visible|frontmost|value|url|zoom|volume|"
    r"minimized|bounds|position|size|selected|current tab|index) to|\bsave\b|\bprint\b|\bsend\b|"
    r"\bmove\b|\bduplicate\b|\breveal\b|\bselect\b|\bsleep\b|\brestart\b|\bshut down\b|\blog out\b|"
    r"\breturn\b.*\b(of|'s)\b|\bget\b.*\bof\b)",
    re.IGNORECASE,
)
_EFFECT_WINDOWS = re.compile(
    r"(Start-Process|Set-ItemProperty|Get-ItemProperty|SendKeys|SendWait|Stop-Process|New-Object|"
    r"Add-Type|Set-Content|Add-Content|Set-Clipboard|CopyFromScreen|rundll32|CloseMainWindow|"
    r"\[System\.|\[Windows\.|Invoke-|Clear-RecycleBin|Remove-Item|Move-Item)",
    re.IGNORECASE,
)


def _has_effect_windows(script: str) -> bool:
    return any(_EFFECT_WINDOWS.search(line) for line in script.splitlines() if not line.strip().startswith(("#", "//")))


def _has_effect(script: str) -> bool:
    if os_backend.is_windows():
        return _has_effect_windows(script)
    return any(_EFFECT_APPLESCRIPT.search(line) for line in script.splitlines() if not line.strip().startswith("--"))


class GeneratedCriterion(BaseModel):
    value: str = Field(description="The enum value as it will appear in the script placeholder.")
    description: str = Field(description="Short meaning of this value, or empty string.")


class GeneratedArg(BaseModel):
    name: str = Field(description="Placeholder name used in the script as {{name}}.")
    kind: Literal["enum", "text"] = Field(description="enum for a closed set of values, text for free-form content.")
    instructions: str = Field(description="Question the router asks to fill this slot from the user's words.")
    criteria: list[GeneratedCriterion] = Field(description="For enum args: 2-30 allowed values. Empty for text args.")
    default: str | None = Field(description="Value used when the user does not specify one, or null.")


class GeneratedTool(BaseModel):
    feasible: bool = Field(description="False only if the request truly cannot be done on this operating system.")
    reason: str = Field(description="One spoken sentence explaining why, if not feasible; else empty string.")
    tool_name: str = Field(description="snake_case, prefixed with the app name when app-specific.")
    description: str = Field(description="One sentence: what the tool does. Used by the router.")
    scope: str | None = Field(description="Exact application name the tool controls, or null for system-wide tools.")
    args: list[GeneratedArg] = Field(description="Argument slots referenced as {{name}}.")
    script: str = Field(
        description="Complete script with {{arg}} placeholders. Ends by returning a short result string."
    )
    speak: str = Field(description="'done', 'result', or a template containing {result}.")
    examples: list[str] = Field(description="2-4 alternative phrasings of the request.")


SYSTEM_MAC = """You write a NEW AppleScript tool for a voice-controlled macOS assistant.

You receive the user's spoken request, the app in front, the running apps, and the names of
tools that ALREADY exist. Create one tool that does NOT exist yet and fulfils the request.
The existing list is only there so you pick a different, non-clashing tool_name; it is never
a reason to say the request is infeasible.

Rules:
- Set feasible=true whenever AppleScript (optionally with `do shell script`) can plausibly do
  it. Only set feasible=false for things macOS truly cannot do this way.
- Prefer the app's scripting dictionary (`tell application "X"`). If the app has no
  dictionary, use `do shell script`, URL schemes, or System Events GUI scripting last.
- Implement exactly the requested action and nothing more. Short scripts (under 15 lines) win.
- Google Chrome: never use `tell application "Google Chrome"` to open URLs or windows (it lands
  in the wrong profile). Launch through the shell with the built-in placeholder
  {{chrome_profile}}: do shell script "open -na 'Google Chrome' --args " & quoted form of
  ("--profile-directory=" & "{{chrome_profile}}") & " " & quoted form of theURL
- Parameterise values the user may vary as {{arg}} placeholders. Enum args need 2-30 concrete
  values; use kind "text" only for free-form content. Placeholders are substituted as escaped
  string literals, so write them inside double quotes.
- AppleScript has NO inline if/ternary. Use a block if. String concatenation is &. Do not name
  variables text, type, status, name, result, or other reserved words.
- Never return a script that only pretends: assigning variables and returning success is not
  an action. If nothing can reach the feature, set feasible=false and say so.
- Never embed secrets, never ask for passwords, never call osascript from the script.
- End the script with `return` of a short result string that can be read aloud.
- speak: "done" to confirm, "result" to read the return value, or a template with {result}.

"""

SYSTEM_WIN = """You write a NEW PowerShell tool for a voice-controlled Windows 11 assistant.

You receive the user's spoken request, the app in front, the running apps, and the names of
tools that ALREADY exist. Create one tool that does NOT exist yet and fulfils the request.
The existing list is only there so you pick a different, non-clashing tool_name.

Rules:
- Set feasible=true whenever PowerShell 5.1/7 can plausibly do it. Only set feasible=false for
  things Windows truly cannot do this way.
- Prefer built-in cmdlets and .NET/COM APIs. Use `Start-Process` to launch apps and URLs,
  `Stop-Process` to quit them, `SendKeys`/COM for keyboard-driven apps, and `Set-ItemProperty`
  only for the current user's appearance settings.
- Implement exactly the requested action and nothing more. Short scripts (under 15 lines) win.
- Parameterise values the user may vary as {{arg}} placeholders. Enum args need 2-30 concrete
  values; use kind "text" only for free-form content. Placeholders are substituted inside
  double-quoted strings with backtick escaping, so write them inside double quotes.
- Never use: Remove-Item, Move-Item, Copy-Item, Clear-RecycleBin, Format-Volume, Stop-Computer,
  Restart-Computer, Invoke-Expression/iex, Invoke-WebRequest, Invoke-RestMethod, Set-ExecutionPolicy,
  administrator elevation (-Verb RunAs), reg.exe/sc.exe/netsh/schtasks/takeown/icacls.
- Never touch C:\\Windows, C:\\Program Files, AppData, HKLM:, or the Windows Run key. Never open
  powershell/cmd/regedit/task manager/Settings/Control Panel or a password manager.
- Never return a script that only pretends: assigning variables and printing success is not an
  action. If nothing reaches the feature, set feasible=false and say so.
- Never embed secrets, never ask for passwords.
- End the script by returning a short result string that can be read aloud.
- speak: "done" to confirm, "result" to read the return value, or a template with {result}.

"""


def system_prompt() -> str:
    prompt = SYSTEM_WIN if os_backend.is_windows() else SYSTEM_MAC
    return prompt + policy.describe_for_llm()


class ToolGenerator:
    def __init__(
        self,
        registry: ToolRegistry,
        client: openai.AsyncOpenAI | None = None,
        decisions: DecisionsClient | None = None,
    ):
        self.registry = registry
        self.decisions = decisions
        self.client = client or openai.AsyncOpenAI()

    async def generate(self, utterance: str, ctx: MacContext) -> tuple[Tool | None, str]:
        """Returns (tool, spoken_message). tool is None when generation failed.

        If the generated tool duplicates an existing available one, that existing tool is
        returned instead with message == DUPLICATE and nothing new is registered.
        """
        user = "Create a NEW tool for this request.\n" + json.dumps(
            {
                "request": utterance,
                "platform": "Windows 11" if os_backend.is_windows() else "macOS",
                "frontmost_app": ctx.active_app,
                "running_apps": ctx.running_apps,
                "existing_tool_names_to_avoid": [t.name for t in self.registry.tools.values()],
            },
            ensure_ascii=False,
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": user},
        ]
        last_error = ""
        policy_strikes = 0
        platform = "Windows 11" if os_backend.is_windows() else "macOS"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                text, finish = await self._complete(messages)
            except _Unavailable as exc:
                log.error("codegen backend unavailable: %s", exc)
                return None, str(exc)
            try:
                data = GeneratedTool.model_validate_json(text).model_dump() if text.strip() else None
            except ValueError:  # JSONDecodeError / pydantic ValidationError
                data = None
            if data is None:
                last_error = f"unparseable output (finish_reason={finish}, {len(text)} chars)"
                log.warning("codegen attempt %d/%d: %s", attempt, MAX_ATTEMPTS, last_error)
                continue
            if not data.get("feasible", False):
                return None, data.get("reason") or f"I can't do that on {platform}."

            tool = self._to_tool(data)
            rendered = tool.render({a.name: (a.default or "x") for a in tool.args})
            violations = policy.check(rendered, tool.active_scope)
            if violations:
                policy_strikes += 1
                log.warning(
                    "codegen attempt %d/%d violates policy: %s",
                    attempt,
                    MAX_ATTEMPTS,
                    "; ".join(map(str, violations)),
                )
                if policy_strikes >= 2:
                    return (
                        None,
                        f"That would change how {os_backend.platform_noun()} is set up, which voice control isn't allowed to do.",
                    )
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That script violates the POLICY: "
                            + "; ".join(map(str, violations))
                            + ". Either achieve the request within the allowed actions, or set feasible=false "
                            "with a one-sentence spoken reason."
                        ),
                    }
                )
                continue
            ok, err = await _compiles(rendered)
            if ok and not _has_effect(tool.script):
                ok, err = (
                    False,
                    (
                        "The script compiles but has no effect: it only assigns variables and returns a string. "
                        "It must actually perform the action. If that isn't possible, set feasible=false."
                    ),
                )
            if ok:
                tool.verified = await self._verify(utterance, tool)
                if tool.verified is not None and tool.verified < VERIFY_THRESHOLD:
                    ok, err = (
                        False,
                        (
                            f"A reviewer judged this script unlikely to actually perform the request "
                            f"(confidence {tool.verified:.0%}). Re-check every command, application, and flag, "
                            "simplify, or set feasible=false."
                        ),
                    )
            if ok:
                break
            last_error = err
            log.warning("codegen attempt %d/%d failed to compile: %s", attempt, MAX_ATTEMPTS, err)
            messages.append({"role": "assistant", "content": text})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"The {platform} parser rejected that script:\n{err}\n\n"
                        "Return the corrected tool as JSON. Fix only what is needed, or set feasible=false."
                    ),
                }
            )
        else:
            log.warning("codegen gave up after %d attempts: %s", MAX_ATTEMPTS, last_error)
            if "unparseable" in last_error:
                return None, "The model kept returning something I couldn't parse."
            return None, "I tried to write a script for that but couldn't get a working one, so I didn't run it."

        duplicate = await self.find_duplicate(tool, self.registry.available(ctx))
        if duplicate is not None:
            return duplicate, DUPLICATE
        try:
            tool = self.registry.add_learned(tool)  # final gate
        except PolicyError as exc:
            log.error("policy rejected tool at registration: %s", exc)
            return None, "That would change how this machine is set up, which voice control isn't allowed to do."
        log.info("learned tool %s (scope=%s, args=%s)", tool.name, tool.scope, [a.name for a in tool.args])
        return tool, ""

    async def _complete(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        """One structured-output completion on OpenAI -> (JSON text, finish_reason)."""
        try:
            resp = await self.client.responses.parse(
                model=MODEL,
                input=messages,  # type: ignore[arg-type]
                text_format=GeneratedTool,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                reasoning={"effort": REASONING_EFFORT},
                prompt_cache_key=cache_key("codegen", messages[0]),
            )
        except openai.APIConnectionError as exc:
            raise _Unavailable("I can't reach OpenAI right now, so I can't learn that action.") from exc
        except openai.APIStatusError as exc:
            raise _Unavailable(f"OpenAI couldn't write that action ({exc.status_code}).") from exc
        parsed = getattr(resp, "output_parsed", None)
        if isinstance(parsed, GeneratedTool):
            text = parsed.model_dump_json()
        elif parsed is not None:
            text = parsed if isinstance(parsed, str) else json.dumps(parsed)
        else:
            text = getattr(resp, "output_text", "") or ""
        return text, "stop"

    async def aclose(self) -> None:
        if hasattr(self.client, "close"):
            await self.client.close()

    async def find_duplicate(self, new_tool: Tool, available: list[Tool]) -> Tool | None:
        """If an existing available tool already does what the new one does, return it."""
        if self.decisions is None or not available:
            return None
        criteria: dict[str, Any] = {t.name: t.choice_description() for t in available[:250]}
        criteria["__none__"] = "No existing tool does the same thing as the new tool."
        try:
            resp = await self.decisions.evaluate(
                state={
                    "new_tool": {
                        "description": new_tool.description,
                        "app": new_tool.scope,
                        "arguments": [a.name for a in new_tool.args],
                    }
                },
                questions={
                    "same_as": ChoiceQuestion(
                        instructions=(
                            "`new_tool` was just written for a voice assistant. Which existing tool performs the same "
                            "action with the same kind of arguments (differences in wording do not matter)? "
                            "Choose __none__ if the new tool does something no listed tool does."
                        ),
                        criteria=criteria,
                    )
                },
            )
        except Exception:
            log.exception("duplicate check failed; skipping")
            return None
        answer = resp.choices["same_as"]
        if answer.choice != "__none__" and answer.confidence >= 0.6:
            log.info("new tool %s duplicates existing %s (conf %.2f)", new_tool.name, answer.choice, answer.confidence)
            return self.registry.get(answer.choice)
        return None

    async def _verify(self, request: str, tool: Tool) -> float | None:
        """Ask the Decisions API whether the script would really perform the request."""
        if self.decisions is None:
            return None
        try:
            resp = await self.decisions.evaluate(
                state={"request": request, "script": tool.script, "arguments": [a.name for a in tool.args]},
                questions={
                    "works": PredicateQuestion(
                        instructions=(
                            "`script` is a PowerShell or AppleScript a voice assistant wrote to carry out `request` "
                            "(`arguments` are placeholders filled in before running). Would running this script "
                            "actually accomplish the request? Judge whether every command, application, shell tool, "
                            "and flag used really exists and does what the script assumes; a script that only assigns "
                            "variables, invents commands or flags, or drives the wrong UI does not accomplish it."
                        ),
                        criteria={
                            "true": "The script would really perform the request as written.",
                            "false": "It would fail, do nothing, do something else, or relies on commands that do not exist.",
                        },
                    )
                },
            )
            return float(resp.predicates["works"].noul)
        except Exception:
            log.exception("verification failed; skipping")
            return None

    def _to_tool(self, data: dict[str, Any]) -> Tool:
        tool = Tool(
            name=data["tool_name"],
            description=data["description"],
            script=data["script"],
            scope=data.get("scope") or None,
            script_win=data["script"] if os_backend.is_windows() else "",
            scope_win=(data.get("scope") or None) if os_backend.is_windows() else None,
            args=[
                ArgSpec(
                    name=a["name"],
                    kind=a["kind"],
                    instructions=a["instructions"],
                    criteria={c["value"]: (c["description"] or None) for c in (a.get("criteria") or [])},
                    default=a.get("default"),
                )
                for a in data.get("args", [])
            ],
            speak=data.get("speak") or "done",
            risky=bool(RISKY_PATTERNS.search(data["script"])),
            source="learned",
            examples=list(data.get("examples", [])),
        )
        # Built-in placeholders are filled by the system, never by the model: drop them if declared.
        tool.args = [a for a in tool.args if a.name not in BUILTIN_PLACEHOLDERS]
        # Every other {{placeholder}} in the script must be a declared arg.
        declared = {a.name for a in tool.args}
        for placeholder in set(re.findall(r"\{\{(\w+)\}\}", tool.script)) - BUILTIN_PLACEHOLDERS:
            if placeholder not in declared:
                tool.args.append(
                    ArgSpec(
                        name=placeholder,
                        kind="text",
                        instructions=f"The value for {placeholder} in the user's request.",
                    )
                )
        return tool


DUPLICATE = "__duplicate__"  # generate() message meaning: returned tool is an existing one


class _Unavailable(RuntimeError):
    """The codegen backend could not serve the request; message is user-speakable."""


async def _compiles(script: str) -> tuple[bool, str]:
    """Syntax validation without execution (osacompile or the PowerShell AST parser)."""
    return await asyncio.to_thread(os_backend.compile_script, script)
