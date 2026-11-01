"""Small read-only helpers. No eval, generated code, or model requests."""

import ast
import math
import operator
from datetime import datetime

import psutil

from .os_backend import ScriptResult

OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def calculate(expression: str) -> str:
    if not expression or len(expression) > 80:
        raise ValueError("Use a short arithmetic expression")
    tree = ast.parse(expression.strip(), mode="eval")

    def number(node):
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            result = node.value
        elif isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            left, right = number(node.left), number(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 12:
                raise ValueError("Use a power between -12 and 12.")
            result = OPERATORS[type(node.op)](left, right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = number(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        else:
            raise ValueError("Use numbers with +, -, *, /, %, and parentheses")
        if isinstance(result, complex) or not math.isfinite(result) or abs(result) > 1e15:
            raise ValueError("That number is too large")
        return result

    return format(number(tree.body), ".15g")


def run(tool: str, args: dict[str, str]) -> ScriptResult:
    try:
        if tool == "system_info":
            kind = args.get("kind")
            if kind == "time":
                return ScriptResult(True, datetime.now().strftime("%I:%M %p").lstrip("0"))
            if kind == "date":
                return ScriptResult(True, datetime.now().strftime("%A, %B %d, %Y"))
            if kind == "battery":
                battery = psutil.sensors_battery()
                if battery is None:
                    return ScriptResult(True, "No battery detected.")
                return ScriptResult(
                    True, f"{battery.percent:.0f}% battery. {'Plugged in.' if battery.power_plugged else 'On battery.'}"
                )
        raise ValueError("Unknown built-in action")
    except (ValueError, SyntaxError, ArithmeticError) as exc:
        return ScriptResult(False, "", str(exc))
