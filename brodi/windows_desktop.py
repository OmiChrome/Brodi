"""Reviewed Windows actions without a shell, model call, or UIA process startup.

Window commands use asynchronous Win32 messages. A close request preserves the
application's save dialog; successful dispatch does not assert that it exited.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import psutil

from .os_backend import ScriptResult

PROCESSES = {
    "Notepad": {"notepad.exe"},
    "Google Chrome": {"chrome.exe"},
    "Microsoft Edge": {"msedge.exe"},
    "Calculator": {"calculatorapp.exe", "calculator.exe"},
    "Paint": {"mspaint.exe"},
    "Explorer": {"explorer.exe"},
    "Snipping Tool": {"snippingtool.exe"},
}
FOLDER_IDS = {
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "home": "5E6C858F-0E22-4760-9AFE-EA3317B67173",
    "applications": "905E63B6-C1BF-494E-B29C-65B732D3D21A",
}
SHOW = {"minimize": 6, "maximize": 3, "restore": 9}


@dataclass(frozen=True)
class Window:
    handle: int
    pid: int
    title: str


@lru_cache(maxsize=1)
def user32():
    api = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "GetForegroundWindow": ([], wintypes.HWND),
        "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
        "IsIconic": ([wintypes.HWND], wintypes.BOOL),
        "GetWindow": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        "PostMessageW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        "ShowWindowAsync": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        "SetForegroundWindow": ([wintypes.HWND], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(api, name)
        fn.argtypes, fn.restype = args, result
    return api


def _process_matches(pid: int, app: str) -> bool:
    try:
        return psutil.Process(pid).name().casefold() in PROCESSES.get(app, set())
    except psutil.Error:
        return False


def _window_matches(hwnd: int, pid: int, app: str) -> bool:
    if _process_matches(pid, app):
        return True
    # Some Windows 11 builds host Calculator inside ApplicationFrameHost.
    # Identify the actual child executable; a shared host or caption is not enough.
    try:
        if psutil.Process(pid).name().casefold() != "applicationframehost.exe":
            return False
    except psutil.Error:
        return False
    api, matched = user32(), False
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(child, _):
        nonlocal matched
        child_pid = wintypes.DWORD()
        api.GetWindowThreadProcessId(child, ctypes.byref(child_pid))
        if child_pid.value != pid and _process_matches(child_pid.value, app):
            matched = True
        return True

    api.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    api.EnumChildWindows.restype = wintypes.BOOL
    api.EnumChildWindows(hwnd, visit, 0)
    return matched


def windows(app: str) -> list[Window]:
    """Visible, unowned top-level windows, matched by executable, never title alone."""
    if app not in PROCESSES:
        raise ValueError("That app has no reviewed native window control")
    api, result = user32(), []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visit(hwnd, _):
        if not api.IsWindowVisible(hwnd) or api.GetWindow(hwnd, 4):  # GW_OWNER
            return True
        pid = wintypes.DWORD()
        api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not _window_matches(hwnd, pid.value, app):
            return True
        if app == "Explorer":
            cls = ctypes.create_unicode_buffer(256)
            api.GetClassNameW(hwnd, cls, len(cls))
            if cls.value not in {"CabinetWClass", "ExploreWClass"}:
                return True  # Never target the Windows desktop/taskbar.
        title = ctypes.create_unicode_buffer(1024)
        api.GetWindowTextW(hwnd, title, len(title))
        if title.value:
            result.append(Window(hwnd, pid.value, title.value))
        return True

    api.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    api.EnumWindows.restype = wintypes.BOOL
    if not api.EnumWindows(visit, 0):
        raise OSError("Could not inspect desktop windows")
    return result


def choose_window(candidates: list[Window], foreground: int) -> Window:
    if not candidates:
        raise ValueError("That app has no open window")
    for window in candidates:
        if window.handle == foreground:
            return window
    if len(candidates) != 1:
        raise ValueError("Several windows match. Bring the one you want to the front first.")
    return candidates[0]


def control_window(app: str, action: str) -> ScriptResult:
    if action not in {*SHOW, "focus", "close"}:
        raise ValueError("Unsupported window action")
    api = user32()
    window = choose_window(windows(app), api.GetForegroundWindow())
    # Revalidate immediately before sending a message in case a handle was reused.
    pid = wintypes.DWORD()
    api.GetWindowThreadProcessId(window.handle, ctypes.byref(pid))
    if pid.value != window.pid or not _window_matches(window.handle, pid.value, app):
        raise ValueError("The selected window changed. Try again.")
    if action == "close":
        ok = api.PostMessageW(window.handle, 0x0010, 0, 0)  # WM_CLOSE; never terminate the process.
        output = f"Close requested for {app}. Save any unsaved work if prompted."
    elif action == "focus":
        if api.IsIconic(window.handle):
            api.ShowWindowAsync(window.handle, 9)
        ok = api.SetForegroundWindow(window.handle) or api.GetForegroundWindow() == window.handle
        output = f"Focused {app}."
    else:
        ok = api.ShowWindowAsync(window.handle, SHOW[action])
        output = f"{action.title()} requested for {app}."
    return ScriptResult(bool(ok), output if ok else "", "Windows did not accept the window action." if not ok else "")


def known_folder(name: str) -> Path:
    if name not in FOLDER_IDS:
        raise ValueError("Unknown folder")
    # UUID.bytes_le matches the Windows GUID memory layout, including DWORD alignment.
    guid = (ctypes.c_uint32 * 4).from_buffer_copy(uuid.UUID(FOLDER_IDS[name]).bytes_le)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    ole = ctypes.WinDLL("ole32")
    shell.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    pointer = ctypes.c_void_p()
    try:
        if shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer)) != 0 or not pointer.value:
            raise OSError("Windows could not locate that folder")
        path = Path(ctypes.wstring_at(pointer))
    finally:
        if pointer.value:
            ole.CoTaskMemFree(pointer)
    if not path.is_dir():
        raise OSError("That folder is unavailable")
    return path


def media_key(key: int, repeats: int = 1) -> None:
    # INPUT includes the full MOUSEINPUT union size, even for keyboard events.
    class Keyboard(ctypes.Structure):
        _fields_ = [
            ("vk", wintypes.WORD),
            ("scan", wintypes.WORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("extra", ctypes.c_size_t),
        ]

    class Mouse(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("data", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("extra", ctypes.c_size_t),
        ]

    class Payload(ctypes.Union):
        _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]

    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("payload", Payload)]

    events = (Input * (repeats * 2))()
    for i, event in enumerate(events):
        event.type = 1  # INPUT_KEYBOARD
        event.payload.keyboard.vk = key
        event.payload.keyboard.flags = 0 if i % 2 == 0 else 2  # KEYEVENTF_KEYUP
    api = user32()
    api.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    api.SendInput.restype = wintypes.UINT
    if api.SendInput(len(events), events, ctypes.sizeof(Input)) != len(events):
        raise OSError("Windows did not accept the media key")


def run(tool: str, args: dict[str, str]) -> ScriptResult | None:
    try:
        if tool == "quit_app":
            if args.get("app") not in PROCESSES:
                return None
            return control_window(args["app"], "close")
        if tool == "window_control":
            return control_window(args.get("app", ""), args.get("action", ""))
        if tool == "finder_open_folder":
            path = known_folder(args.get("folder", ""))
            subprocess.Popen(
                [str(Path(os.environ["WINDIR"]) / "explorer.exe"), str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return ScriptResult(True, f"Opening {args['folder']}.")
        if tool == "take_screenshot":
            from PIL import ImageGrab

            path = known_folder("desktop") / f"Brodi-screenshot-{uuid.uuid4().hex[:12]}.png"
            with ImageGrab.grab(all_screens=True) as capture:
                capture.save(path, compress_level=1)
            return ScriptResult(True, f"Screenshot saved: {path}")
        if tool == "media_control":
            key = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}.get(args.get("action", ""))
            if key is None:
                raise ValueError("Unknown media action")
            media_key(key)
            return ScriptResult(True, "Media key sent.")
        if tool == "set_volume" and args.get("level") in {"louder", "quieter"}:
            media_key(0xAF if args["level"] == "louder" else 0xAE, repeats=5)
            return ScriptResult(True, "Volume key sent.")
        return None
    except (OSError, ValueError, KeyError) as exc:
        return ScriptResult(False, "", str(exc))
