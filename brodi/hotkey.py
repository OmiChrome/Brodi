"""Native global Control+Space registration, isolated from LiveKit's event loop.

The helper owns the OS event loop on its main thread (required by Carbon). Its
stdin pipe is a lifetime lease: closing the voice process also releases the key.
Only this one shortcut is registered; no general keyboard listener is installed.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path


class WindowsHotkey:
    def __init__(self):
        from ctypes import wintypes

        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.api.RegisterHotKey.restype = wintypes.BOOL
        self.api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.UnregisterHotKey.restype = wintypes.BOOL
        self.api.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self.api.PeekMessageW.restype = wintypes.BOOL
        self.message = wintypes.MSG()
        # MOD_CONTROL | MOD_NOREPEAT, VK_SPACE. No repeat while held down.
        if not self.api.RegisterHotKey(None, 1, 0x0002 | 0x4000, 0x20):
            raise RuntimeError(
                f"Control+Space is unavailable (Windows error {ctypes.get_last_error()}). Close the other app using it and restart Brodi."
            )

    def poll(self) -> bool:
        return (
            bool(self.api.PeekMessageW(ctypes.byref(self.message), None, 0x0312, 0x0312, 1))
            and self.message.wParam == 1
        )

    def close(self):
        self.api.UnregisterHotKey(None, 1)


class EventType(ctypes.Structure):
    _fields_ = [("event_class", ctypes.c_uint32), ("kind", ctypes.c_uint32)]


class HotkeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class MacHotkey:
    def __init__(self):
        self.api = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        pointer, uint = ctypes.c_void_p, ctypes.c_uint32
        self.api.GetApplicationEventTarget.argtypes = []
        self.api.GetApplicationEventTarget.restype = pointer
        self.api.RegisterEventHotKey.argtypes = [uint, uint, HotkeyID, pointer, uint, ctypes.POINTER(pointer)]
        self.api.RegisterEventHotKey.restype = ctypes.c_int32
        self.api.UnregisterEventHotKey.argtypes = [pointer]
        self.api.UnregisterEventHotKey.restype = ctypes.c_int32
        self.api.ReceiveNextEvent.argtypes = [
            uint,
            ctypes.POINTER(EventType),
            ctypes.c_double,
            ctypes.c_bool,
            ctypes.POINTER(pointer),
        ]
        self.api.ReceiveNextEvent.restype = ctypes.c_int32
        self.api.GetEventKind.argtypes = [pointer]
        self.api.GetEventKind.restype = uint
        self.api.ReleaseEvent.argtypes = [pointer]
        self.api.ReleaseEvent.restype = None
        keyboard = int.from_bytes(b"keyb", "big")
        self.events = (EventType * 2)(EventType(keyboard, 5), EventType(keyboard, 6))
        self.reference = pointer()
        self.pressed = False
        # kVK_Space, controlKey, kEventHotKeyExclusive. Control, not Command.
        status = self.api.RegisterEventHotKey(
            49,
            1 << 12,
            HotkeyID(int.from_bytes(b"Brdi", "big"), 1),
            self.api.GetApplicationEventTarget(),
            1,
            ctypes.byref(self.reference),
        )
        if status:
            if status != -9878:
                raise RuntimeError(
                    f"macOS could not register Control+Space ({status}). Run Brodi in a logged-in desktop session and restart it."
                )
            raise RuntimeError(
                f"Control+Space is unavailable (macOS error {status}). Reassign it under System Settings > Keyboard > Keyboard Shortcuts > Input Sources, or close the other app using it, then restart Brodi."
            )

    def poll(self) -> bool:
        event = ctypes.c_void_p()
        status = self.api.ReceiveNextEvent(2, self.events, 0.01, True, ctypes.byref(event))
        if status == -9875:  # eventLoopTimedOutErr
            return False
        if status:
            raise RuntimeError(f"macOS shortcut event loop failed ({status})")
        try:
            pressed = self.api.GetEventKind(event) == 5
            activate = pressed and not self.pressed
            self.pressed = pressed
            return activate
        finally:
            self.api.ReleaseEvent(event)

    def close(self):
        self.api.UnregisterEventHotKey(self.reference)


class GlobalMicHotkey:
    """Deliver native shortcut events on the caller's asyncio loop."""

    def __init__(self, on_toggle: Callable[[], None], on_error: Callable[[str], None]):
        self.on_toggle, self.on_error = on_toggle, on_error
        self.process = None
        self.reader = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "brodi.hotkey",
            cwd=Path(__file__).resolve().parent.parent,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),
        )
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=5)
            if line != b"ready\n":
                raise RuntimeError(
                    line.decode(errors="replace").removeprefix("error:").strip()
                    or "Microphone shortcut helper did not start"
                )
        except BaseException:
            await self.aclose()
            raise
        self.reader = asyncio.create_task(self._read(), name="brodi-mic-hotkey")

    async def _read(self):
        while line := await self.process.stdout.readline():
            if line == b"toggle\n":
                self.on_toggle()
            elif line.startswith(b"error:"):
                self.on_error(line.decode(errors="replace").removeprefix("error:").strip())
                return
        self.on_error("Microphone shortcut stopped. Restart Brodi to restore Control+Space.")

    async def aclose(self):
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
            self.reader = None
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
            self.process = None


def main() -> int:
    stopped = threading.Event()

    def watch_parent():
        # Parent writes no data; EOF means the voice session has exited.
        while os.read(sys.stdin.fileno(), 1):
            pass
        stopped.set()

    backend = None
    try:
        if sys.platform == "win32":
            backend = WindowsHotkey()
        elif sys.platform == "darwin":
            backend = MacHotkey()
        else:
            raise RuntimeError("The microphone shortcut supports Windows and macOS.")
        threading.Thread(target=watch_parent, daemon=True, name="brodi-hotkey-parent").start()
        print("ready", flush=True)
        while not stopped.is_set():
            if backend.poll():
                print("toggle", flush=True)
            else:
                stopped.wait(0.01)
        return 0
    except Exception as exc:
        print("error:" + str(exc), flush=True)
        return 1
    finally:
        if backend:
            backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
