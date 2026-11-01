"""Bounded, isolated Windows UIA helper. No desktop imports on other platforms."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any

from . import policy
from .os_backend import ScriptResult, is_windows


class UIAError(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowTarget:
    title: str = ""
    process: int | None = None
    handle: int | None = None


def check_target(name: str) -> None:
    name = name.strip().lower().removesuffix(".exe")
    if not name:
        raise UIAError("Name the app or window to control.")
    blocked = policy.BLOCKED_APPS | policy.WINDOWS_BLOCKED_APPS
    blocked = blocked | {
        "windowsterminal",
        "openconsole",
        "conhost",
        "python",
        "pythonw",
        "node",
        "wscript",
        "cscript",
        "mshta",
    }
    if name in blocked or any(
        re.search(r"\b" + re.escape(n) + r"\b", name)
        for n in (
            "powershell",
            "regedit",
            "windows terminal",
            "command prompt",
            "password manager",
            "devtools",
            "developer tools",
        )
    ):
        raise UIAError("That application is blocked by the safety policy.")


def literal_keys(text: str) -> str:
    return "".join("{" + char + "}" if char in "+^%~(){}" else char for char in text)


def is_password(control) -> bool:
    info = control.element_info
    if hasattr(info, "is_password"):
        return bool(info.is_password)
    try:
        return bool(info.element.CurrentIsPassword)
    except Exception:
        return True  # Do not expose or type into a control whose security flag is unknown.


class WindowsUIA:
    def __init__(self, desktop: Any = None, *, timeout: float = 2.0):
        if desktop is None:
            if not is_windows():
                raise UIAError("Windows UI Automation is only available on Windows.")
            from pywinauto import Desktop

            desktop = Desktop(backend="uia")
        self.desktop = desktop
        self.timeout = timeout

    def connect(self, target: WindowTarget):
        if target.title:
            check_target(target.title)
        if not (target.title or target.process or target.handle):
            raise UIAError("Specify a window title, process, or handle.")
        deadline = time.monotonic() + self.timeout
        while True:
            windows = [w for w in self.desktop.windows() if w.is_visible()]
            if target.handle:
                windows = [w for w in windows if w.handle == target.handle]
            if target.process:
                windows = [w for w in windows if w.process_id() == target.process]
            if target.title:
                needle = target.title.casefold()
                scored = [
                    (
                        1.0
                        if needle == w.window_text().casefold()
                        else 0.95
                        if needle in w.window_text().casefold()
                        else SequenceMatcher(None, needle, w.window_text().casefold()).ratio(),
                        w,
                    )
                    for w in windows
                ]
                windows = [w for score, w in scored if score >= 0.82]
            if len(windows) == 1:
                window = windows[0]
                check_target(window.window_text())
                import psutil

                check_target(psutil.Process(window.process_id()).name())
                return window
            if len(windows) > 1:
                raise UIAError("More than one window matches. Give me the exact window title.")
            if time.monotonic() >= deadline:
                raise UIAError("I couldn't find that window. Open it and try again.")
            time.sleep(0.1)

    def focus_window(self, target: WindowTarget):
        window = self.connect(target)
        if window.is_minimized():
            window.restore()
        window.set_focus()
        return window

    def find_edit(self, window, automation_id: str = ""):
        deadline = time.monotonic() + self.timeout
        while True:
            controls = [
                c
                for c in window.descendants()
                if c.element_info.control_type in {"Edit", "Document"}
                and c.is_visible()
                and c.is_enabled()
                and not is_password(c)
            ]
            if automation_id:
                controls = [c for c in controls if c.element_info.automation_id == automation_id]
            if controls or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        focused = [c for c in controls if c.has_keyboard_focus()]
        if len(focused) == 1:
            return focused[0]
        # Prefer document editors over search boxes in applications such as Notepad.
        docs = [c for c in controls if c.element_info.control_type == "Document"]
        if len(docs) == 1:
            return docs[0]
        if len(controls) != 1:
            raise UIAError("I couldn't identify one text field. Focus the field or specify its automation ID.")
        return controls[0]

    @staticmethod
    def read_text(control) -> str:
        try:
            return control.get_value()
        except Exception:
            try:
                return control.iface_text.DocumentRange.GetText(-1)
            except Exception as exc:
                raise UIAError("The text field cannot be read back reliably.") from exc

    def set_text(
        self, target: WindowTarget, text: str, *, automation_id: str = "", append: bool = True, confirmed: bool = False
    ) -> dict:
        if not text or len(text) > 10000:
            raise UIAError("Text must contain between 1 and 10000 characters.")
        window = self.focus_window(target)
        edit = self.find_edit(window, automation_id)
        before = self.read_text(edit)
        if before and not append and not confirmed:
            raise UIAError("Replacing existing text requires confirmation.")
        expected = before + text if append else text
        edit.set_focus()
        try:
            edit.set_edit_text(expected)
        except Exception:
            # A failed COM call may already have changed the document. Check before fallback.
            current = self.read_text(edit)
            if current == expected:
                return {"title": window.window_text(), "text": current, "verified": True}
            if current != before or not append:
                raise UIAError("The field changed unexpectedly. I stopped to avoid typing twice.") from None
            edit.set_focus()
            edit.type_keys("^{END}", set_foreground=False)
            edit.type_keys(
                literal_keys(text),
                with_spaces=True,
                with_newlines=True,
                pause=0.001,
                vk_packet=True,
                set_foreground=False,
            )
        actual = self.read_text(edit)
        if actual.replace("\r\n", "\n").rstrip("\r") != expected.replace("\r\n", "\n").rstrip("\r"):
            raise UIAError("I typed into the field but couldn't verify the text. Please check it before retrying.")
        return {"title": window.window_text(), "text": actual, "verified": True}

    def click_button(self, target: WindowTarget, *, name: str = "", automation_id: str = "") -> dict:
        if not (name or automation_id):
            raise UIAError("Name the button to click.")
        if re.search(r"delete|remove|send|submit|buy|pay|confirm|install", name + " " + automation_id, re.I):
            raise UIAError("This button needs a separately confirmed tool; the generic helper cannot click it.")
        window = self.focus_window(target)
        buttons = [
            b
            for b in window.descendants(control_type="Button")
            if b.is_visible()
            and b.is_enabled()
            and (not name or b.window_text().casefold() == name.casefold())
            and (not automation_id or b.element_info.automation_id == automation_id)
        ]
        if len(buttons) != 1:
            raise UIAError("I couldn't identify one matching button.")
        buttons[0].click()
        return self.get_window_state(target)

    def get_window_state(self, target: WindowTarget) -> dict:
        window = self.connect(target)
        return {
            "title": window.window_text(),
            "handle": window.handle,
            "process": window.process_id(),
            "controls": [
                {
                    "name": c.window_text()[:80],
                    "type": c.element_info.control_type,
                    "automation_id": c.element_info.automation_id,
                }
                for c in window.descendants()[:30]
                if not is_password(c)
            ],
        }

    def get_text(self, target: WindowTarget) -> dict:
        window = self.connect(target)
        text = self.read_text(self.find_edit(window))
        return {"title": window.window_text(), "text": text, "verified": True}


def request(operation: str, target: WindowTarget, *, timeout: float = 3.0, **options) -> ScriptResult:
    if not is_windows():
        return ScriptResult(False, "", "Windows UI Automation is only available on Windows.")
    try:
        if target.title:
            check_target(target.title)
        payload = {"operation": operation, "target": asdict(target), "timeout": min(timeout, 2), "options": options}
        process = subprocess.Popen(
            [sys.executable, "-m", "brodi.windows_uia", "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            stdout, _ = process.communicate(json.dumps(payload), timeout=timeout)
        except subprocess.TimeoutExpired:
            import psutil

            try:
                for child in psutil.Process(process.pid).children(recursive=True):
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
            except psutil.Error:
                pass
            process.kill()
            process.communicate(timeout=1)
            raise
        data = json.loads(stdout.strip().splitlines()[-1])
        return ScriptResult(
            data["ok"],
            json.dumps(data.get("result", {}), ensure_ascii=False) if data["ok"] else "",
            data.get("error", ""),
        )
    except subprocess.TimeoutExpired:
        return ScriptResult(False, "", "UI Automation timed out. Check the app before repeating text input.")
    except Exception as exc:
        return ScriptResult(False, "", str(exc))


async def execute(operation: str, target: WindowTarget, **options) -> ScriptResult:
    return await asyncio.to_thread(request, operation, target, **options)


def _worker() -> None:
    try:
        data = json.loads(sys.stdin.read())
        helper = WindowsUIA(timeout=data.get("timeout", 2))
        operation = data["operation"]
        if operation not in {"set_text", "get_text", "get_window_state", "focus_window", "click_button"}:
            raise UIAError("Unknown UI Automation operation.")
        value = getattr(helper, operation)(WindowTarget(**data["target"]), **data.get("options", {}))
        if operation == "focus_window":
            value = {"title": value.window_text(), "focused": True}
        output = {"ok": True, "result": value}
    except Exception as exc:
        output = {"ok": False, "error": str(exc)}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    _worker()
