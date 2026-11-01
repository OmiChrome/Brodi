"""Calculator's reviewed button workflow, run only inside the isolated UIA worker."""

from __future__ import annotations

import ctypes
import json
import locale
import math
import re
import sys
import time
from ctypes import wintypes

from .calculator import Calculation, prepare
from .native_launch import launch
from .windows_desktop import Window, user32, windows
from .windows_uia import UIAError, WindowsUIA


def display_number(text: str, decimal: str = ".") -> float | None:
    match = re.search(r"[-+−]?\d[\d.,\s\u00a0\u202f]*(?:[eE][+−-]?\d+)?", text)
    if not match:
        return None
    number = re.sub(r"\s", "", match[0]).replace("−", "-")
    number = number.replace("," if decimal == "." else ".", "").replace(decimal, ".")
    try:
        return float(number)
    except ValueError:
        return None


def press_calculation(controls: dict, calculation: Calculation, *, decimal: str = ".", timeout: float = 2) -> dict:
    """Invoke actual buttons; a Python calculation is only the independent verifier."""
    required = {"clearButton", "equalButton", "CalculatorResults", *calculation.buttons}
    missing = required - controls.keys()
    if missing:
        raise UIAError("Calculator is missing the required controls: " + ", ".join(sorted(missing)))
    controls["clearButton"].iface_invoke.Invoke()
    for name in calculation.buttons:
        controls[name].iface_invoke.Invoke()
    controls["equalButton"].iface_invoke.Invoke()
    deadline = time.monotonic() + timeout
    expected = float(calculation.expected)
    while True:
        display = controls["CalculatorResults"].window_text()
        value = display_number(display, decimal)
        if value is not None and math.isclose(value, expected, rel_tol=1e-12, abs_tol=0):
            return {
                "expression": calculation.expression,
                "display": display,
                "verified": True,
                "buttons_pressed": len(calculation.buttons) + 2,
            }
        if time.monotonic() >= deadline:
            raise UIAError("I couldn't verify Calculator's result. Check its display before trying again.")
        time.sleep(0.01)


class WindowsCalculator:
    def __init__(self, helper: WindowsUIA):
        self.helper = helper
        self.target: Window | None = None
        self.wrapper = None
        locale.setlocale(locale.LC_NUMERIC, "")
        self.decimal = locale.localeconv()["decimal_point"]

    def _valid(self) -> bool:
        if self.target is None:
            return False
        api, pid = user32(), wintypes.DWORD()
        api.GetWindowThreadProcessId(self.target.handle, ctypes.byref(pid))
        if pid.value != self.target.pid:
            return False
        # UWP initially exposes its own window, then reparents it into a frame.
        # Follow the existing verified handle to its root; never adopt an unrelated new window.
        root = api.GetAncestor(self.target.handle, 2) or self.target.handle
        match = next((w for w in windows("Calculator") if w.handle == root), None)
        if match is None:
            return False
        self.target = match
        return True

    @staticmethod
    def _controls(window) -> dict:
        return {c.element_info.automation_id: c for c in window.descendants() if c.element_info.automation_id}

    def _open(self):
        if self._valid():
            return self.wrapper
        self.target, self.wrapper = None, None
        before = {w.handle for w in windows("Calculator")}
        result = launch("open_app", {"app": "Calculator"})
        if not result or not result.ok:
            raise UIAError(result.error if result else "Calculator is unavailable")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            found = [w for w in windows("Calculator") if w.handle not in before]
            if len(found) == 1:
                self.target = found[0]
                self.wrapper = self.helper.desktop.window(handle=self.target.handle).wrapper_object()
                return self.wrapper
            if len(found) > 1:
                raise UIAError("Several Calculator windows opened. I stopped before changing a calculation.")
            time.sleep(0.03)
        raise UIAError("Calculator did not open a separate window in time.")

    def calculate(self, expression: str) -> dict:
        calculation = prepare(expression)  # Before app launch or clearing the display.
        window = self._open()
        api = user32()
        if api.IsIconic(self.target.handle):
            api.ShowWindowAsync(self.target.handle, 9)
        window.element_info.element.SetFocus()
        controls = self._controls(window)
        desired = "Scientific" if calculation.scientific else "Standard"
        is_scientific = "openParenthesisButton" in controls
        if is_scientific != calculation.scientific or "num0Button" not in controls:
            controls["TogglePaneButton"].iface_invoke.Invoke()
            deadline = time.monotonic() + 2
            while True:
                controls = self._controls(window)
                if desired in controls:
                    controls[desired].iface_selection_item.Select()
                    break
                if time.monotonic() >= deadline:
                    raise UIAError("Could not select Calculator's arithmetic mode.")
                time.sleep(0.02)
            while True:
                controls = self._controls(window)
                if "num0Button" in controls and ("openParenthesisButton" in controls) == calculation.scientific:
                    break
                if time.monotonic() >= deadline:
                    raise UIAError("Calculator's buttons did not finish loading.")
                time.sleep(0.02)
        if not self._valid():
            raise UIAError("The Calculator window changed; nothing entered.")
        # Scientific mode presents CE after '=' and exposes C after clearing the entry.
        # Reset both the current entry and any previous expression before entering a new one.
        extra_press = 0
        if "clearButton" not in controls and "clearEntryButton" in controls:
            controls["clearEntryButton"].iface_invoke.Invoke()
            extra_press = 1
            deadline = time.monotonic() + 1
            while "clearButton" not in controls:
                controls = self._controls(window)
                if time.monotonic() >= deadline:
                    raise UIAError("Could not clear the previous Calculator expression.")
                time.sleep(0.01)
        result = press_calculation(controls, calculation, decimal=self.decimal)
        result["buttons_pressed"] += extra_press
        return {**result, "handle": self.target.handle, "process": self.target.pid, "has_window": True}

    def close(self) -> dict:
        if not self._valid():
            self.target, self.wrapper = None, None
            return {"has_window": False, "closed": True}
        handle = self.target.handle
        if not user32().PostMessageW(handle, 0x0010, 0, 0):
            raise UIAError("Calculator did not accept the close request.")
        deadline = time.monotonic() + 2
        while self._valid():
            if time.monotonic() >= deadline:
                raise UIAError("Calculator has not closed yet. Check its window.")
            time.sleep(0.01)
        self.target, self.wrapper = None, None
        return {"has_window": False, "closed": True, "handle": handle}


def worker() -> None:
    try:
        controller = WindowsCalculator(WindowsUIA())
        print(json.dumps({"ready": True}), flush=True)
    except Exception as exc:
        print(json.dumps({"ready": False, "error": str(exc)}), flush=True)
        return
    for line in sys.stdin:
        try:
            request = json.loads(line)
            operation = request["operation"]
            if operation == "ping":
                result = {"has_window": controller.target is not None}
            elif operation == "calculate":
                result = controller.calculate(request["expression"])
            elif operation == "close":
                result = controller.close()
            else:
                raise UIAError("Unsupported Calculator action")
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": str(exc), "result": {"has_window": controller.target is not None}}
        print(json.dumps(response, ensure_ascii=False), flush=True)
