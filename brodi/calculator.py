"""Validated arithmetic and an isolated, reusable Calculator UI worker."""

from __future__ import annotations

import ast
import asyncio
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal

from .os_backend import ScriptResult
from .quick_actions import calculate

BUTTONS = {str(n): f"num{n}Button" for n in range(10)} | {
    ".": "decimalSeparatorButton",
    "+": "plusButton",
    "-": "minusButton",
    "*": "multiplyButton",
    "/": "divideButton",
    "%": "modButton",
    "^": "powerButton",
    "(": "openParenthesisButton",
    ")": "closeParenthesisButton",
}
WORDS = dict(
    zip(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
        ],
        range(20),
        strict=True,
    )
) | dict(
    zip(["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"], range(20, 100, 10), strict=True)
)


def _number_words(match: re.Match) -> str:
    words = match[0].lower().split()
    # Common spoken hundreds: "four fifty" and "four twenty five".
    if (
        len(words) in {2, 3}
        and 1 <= WORDS.get(words[0], 0) <= 9
        and 10 <= WORDS.get(words[1], 0) <= 90
        and (len(words) == 2 or (WORDS[words[1]] >= 20 and 1 <= WORDS.get(words[2], 0) <= 9))
    ):
        return str(WORDS[words[0]] * 100 + sum(WORDS[word] for word in words[1:]))
    total, current = 0, 0
    previous = None
    for word in words:
        if word in WORDS:
            if previous in WORDS and not (WORDS[previous] >= 20 and WORDS[word] < 10):
                raise ValueError("Please say that number as digits or a complete number.")
            current += WORDS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word in {"thousand", "million"}:
            total += (current or 1) * (1000 if word == "thousand" else 1000000)
            current = 0
        if word != "and":
            previous = word
    return str(total + current)


def normalize_expression(text: str) -> str:
    text = text.strip().casefold().replace("×", "*").replace("÷", "/").replace("−", "-")
    # Remove conventional thousands separators, never arbitrary punctuation/code.
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    text = re.sub(r"(?<=\d)\s*x\s*(?=[\d(])", " * ", text)
    for phrase, symbol in (
        ("multiplied by", "*"),
        ("divided by", "/"),
        ("to the power of", "**"),
        ("plus", "+"),
        ("minus", "-"),
        ("times", "*"),
        ("over", "/"),
        ("mod", "%"),
        ("squared", "**2"),
        ("cubed", "**3"),
    ):
        text = re.sub(rf"\b{phrase}\b", symbol, text)
    text = text.replace("^", "**")
    text = re.sub(r"(?:percent|%)\s+of\s+", "/100*", text)
    text = re.sub(r"\bpercent\b", "/100", text)
    vocabulary = "|".join([*WORDS, "hundred", "thousand", "million"])
    text = re.sub(rf"\b(?:{vocabulary})(?:\s+(?:and\s+)?(?:{vocabulary}))*\b", _number_words, text)
    text = re.sub(r"(?<=[\d)])\s*x\s*(?=[\d(+\-])", " * ", text)
    root = re.fullmatch(r"(?:the )?square root of\s+(.+)", text)
    if root:
        text = f"({root[1]})**0.5"
    return text.strip()


def request_expression(text: str, *, incomplete: bool = False) -> str | None:
    """Conservative math-only questions; unrelated 'what is' questions stay with chat."""
    text = text.strip().replace("’", "'").rstrip(".!?")
    text = re.sub(r"\s+(?:in|using|on)\s+(?:the\s+)?calculator$", "", text, flags=re.I)
    text = re.sub(r"\s+for me$", "", text, flags=re.I)
    text = re.sub(r"^(?:um|uh)[,\s]+", "", text, flags=re.I)
    explicit = False
    prefix = re.compile(
        r"^(?:(?:can|could|would|will) you\s+(?:please\s+)?)?"
        r"(?:calculate|compute|work out|what is|what's|whats|how much is)\b"
        r"(?:\s+(?:this|something))?(?:\s+for me)?[\s,.:;!?]*",
        re.I,
    )
    # Speech often includes both: "calculate for me? What is ...".
    while match := prefix.match(text):
        explicit = True
        text = text[match.end() :]
    if not text:
        return "" if explicit and incomplete else None
    for pattern, template in (
        (r"multiply (.+) by (.+)", "({0})*({1})"),
        (r"divide (.+) by (.+)", "({0})/({1})"),
        (r"add (.+) and (.+)", "({0})+({1})"),
        (r"subtract (.+) from (.+)", "({1})-({0})"),
    ):
        if match := re.fullmatch(pattern, text, re.I):
            text, explicit = template.format(*match.groups()), True
            break
    try:
        expression = normalize_expression(text)
    except ValueError:
        return None
    if not re.fullmatch(r"[\d.\s()+*/%\-]{1,80}", expression):
        return None
    if not explicit and not re.search(r"\d\s*[+*/%\-]|\*\*", expression):
        return None
    if not incomplete:
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError:
            return None
        # Keep a lone operand pending, so a pause after "five hundred" doesn't
        # execute it before the user has finished saying "times four fifty".
        if not any(isinstance(node, ast.BinOp) for node in ast.walk(tree)):
            return None
    return expression


@dataclass(frozen=True)
class Calculation:
    expression: str
    buttons: tuple[str, ...]
    expected: str
    scientific: bool


def prepare(expression: str) -> Calculation:
    expression = normalize_expression(expression)
    if not re.fullmatch(r"[\d.\s()+*/%\-]{1,80}", expression):
        raise ValueError("Use a short numeric calculation with +, -, times, divide, parentheses, or powers.")
    expected = calculate(expression)  # Validation and independent readback check; never pasted into the app.
    tree = ast.parse(expression, mode="eval")
    operators = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Mod: "%", ast.Pow: "^"}

    def keys(node: ast.AST, *, outer: bool = False) -> str:
        if isinstance(node, ast.Constant):
            return format(Decimal(str(node.value)), "f") if isinstance(node.value, float) else str(node.value)
        if isinstance(node, ast.UnaryOp):
            return "(0-" + keys(node.operand) + ")" if isinstance(node.op, ast.USub) else keys(node.operand)
        value = keys(node.left) + operators[type(node.op)] + keys(node.right)
        return value if outer else "(" + value + ")"

    typed = keys(tree.body, outer=True)
    scientific = any(char in typed for char in "()%^") or sum(isinstance(n, ast.BinOp) for n in ast.walk(tree)) > 1
    buttons = tuple(BUTTONS[char] for char in typed)
    if len(buttons) > 120:
        raise ValueError("That calculation is too long for a quick desktop action.")
    return Calculation(typed, buttons, expected, scientific)


def mac_script(calculation: Calculation) -> str:
    # Only validated digits/operators reach System Events. Never shell/paste the answer.
    return (
        '''tell application "Calculator" to activate
tell application "System Events"
  tell process "Calculator"
    set frontmost to true
    keystroke "2" using command down
    key code 53 using option down
    repeat with k in characters of "'''
        + calculation.expression
        + """="
      if not frontmost then error "Calculator lost focus; input stopped."
      keystroke (k as text)
    end repeat
  end tell
end tell
return "Calculation entered in Calculator."
"""
    )


class CalculatorSession:
    """One UIA-owning process per agent, serialized requests, hard deadline, no replay."""

    def __init__(self):
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self.has_window = False

    async def _start(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        self.has_window = False
        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "brodi.windows_uia",
            "--calculator-worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        response = json.loads(await self._process.stdout.readline())
        if not response.get("ready"):
            raise RuntimeError(response.get("error", "Calculator helper did not start"))

    async def _stop(self) -> None:
        process, self._process = self._process, None
        self.has_window = False
        if process is not None and process.returncode is None:
            process.kill()  # Only the helper, never the user's Calculator process.
            await process.wait()

    async def _request(self, operation: str, *, timeout: float = 8, **options) -> ScriptResult:
        async with self._lock:
            try:
                async with asyncio.timeout(timeout):
                    await self._start()
                    self._process.stdin.write((json.dumps({"operation": operation, **options}) + "\n").encode())
                    await self._process.stdin.drain()
                    response = json.loads(await self._process.stdout.readline())
                    self.has_window = response.get("result", {}).get("has_window", self.has_window)
                    if not response.get("ok"):
                        return ScriptResult(False, "", response.get("error", "Calculator action failed"))
                    return ScriptResult(True, json.dumps(response.get("result", {})))
            except TimeoutError:
                await self._stop()
                return ScriptResult(False, "", "Calculator timed out. Check its window before trying again.")
            except asyncio.CancelledError:
                await self._stop()
                raise
            except (OSError, ValueError, RuntimeError, ConnectionError) as exc:
                await self._stop()
                return ScriptResult(False, "", str(exc))

    async def warm(self) -> ScriptResult:
        return await self._request("ping")

    async def calculate(self, expression: str, *, timeout: float = 8) -> ScriptResult:
        try:
            prepare(expression)
        except (ValueError, SyntaxError, ArithmeticError) as exc:
            return ScriptResult(False, "", "Cannot divide by zero." if isinstance(exc, ZeroDivisionError) else str(exc))
        return await self._request("calculate", timeout=timeout, expression=expression)

    async def close_window(self, *, timeout: float = 3) -> ScriptResult:
        return await self._request("close", timeout=timeout)

    async def aclose(self) -> None:
        async with self._lock:
            await self._stop()
