"""Native macOS menu-bar and Windows tray controls for this checkout."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path

import psutil

from .activity import JournalReader, clean, default_path
from .config import flag, load_environment
from .mic_control import read_state
from .paths import user_data_dir

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("brodi.tray")
ACTIONS = {"start", "stop", "restart", "show_activity", "show_log", "toggle_mic", "quit"}


def checkout_id(root: Path) -> str:
    path = str(root.resolve())
    return hashlib.sha256((path.casefold() if sys.platform == "win32" else path).encode()).hexdigest()[:20]


def short_text(value: object, limit: int = 72) -> str:
    text = " ".join(str(clean(value)).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def owns_agent(command: list[str], cwd: str, root: Path = ROOT) -> bool:
    """Match an exact entrypoint and checkout, not a substring in another app."""
    for index, argument in enumerate(command[:-1]):
        if Path(argument).name.casefold() == "agent.py" and command[index + 1] == "console":
            path = Path(argument)
            if not path.is_absolute():
                path = Path(cwd) / path
            return path.resolve() == (root / "agent.py").resolve()
        if argument == "-m" and command[index + 1] in {"brodi.wake_runtime", "brodi.console_service"}:
            return Path(cwd).resolve() == root.resolve()
    return False


class AgentController:
    """Manage this checkout's voice processes; leave launched user apps open."""

    def __init__(self, root: Path = ROOT):
        self.root = root
        log_name = f"brody-{os.getuid()}.log" if sys.platform == "darwin" else "brodi-console.log"
        self.log_path = Path(os.environ.get("BRODI_LOG", Path(tempfile.gettempdir()) / log_name)).expanduser()
        self._child: subprocess.Popen | None = None

    @property
    def python(self) -> str:
        executable = Path(sys.executable)
        console = executable.with_name("python.exe")
        return str(console if sys.platform == "win32" and console.exists() else executable)

    def processes(self) -> list:
        if self._child is not None and self._child.poll() is not None:
            self._child = None
        found = []
        for process in psutil.process_iter(["name", "cmdline"]):
            try:
                info = process.info
                if not (info.get("name") or "").lower().startswith(("python", "uv")):
                    continue
                if owns_agent(info.get("cmdline") or [], process.cwd(), self.root):
                    found.append(process)
            except (psutil.Error, OSError):
                continue
        return found

    @staticmethod
    def leaf_pid(processes: list) -> int | None:
        parents = set()
        for process in processes:
            try:
                parents.add(process.ppid())
            except psutil.Error:
                pass
        return next((process.pid for process in processes if process.pid not in parents), None)

    def start(self, *, manage_background: bool = True, wake: bool = False) -> None:
        if manage_background:
            from .background import installed, start_service

            if installed(self.root):
                start_service(self.root)
                return
        if self.processes():
            return
        load_environment(self.root)
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("Set OPENAI_API_KEY in .env.local before starting Brodi.")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        if wake:
            environment.update(
                BRODI_BACKGROUND="true",
                BRODI_WAKE_REQUIRED="true",
                BRODI_STARTUP_GREETING="false",
                BRODI_VOICE_MODE="pipeline",
            )
        if sys.platform == "darwin":
            # LiveKit's detached console needs a TTY and an absolute Python at login.
            command = [self.python, "-m", "brodi.console_service", str(self.log_path), "--python", self.python]
            platform_options = {"start_new_session": True}
        else:
            command = [self.python, str(self.root / "agent.py"), "console"]
            for kind in ("INPUT", "OUTPUT"):
                if device := environment.get(f"BRODI_{kind}_DEVICE"):
                    command.extend([f"--{kind.lower()}-device", device])
            platform_options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        with self.log_path.open("ab", buffering=0) as output:
            self._child = subprocess.Popen(
                command,
                cwd=self.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                **platform_options,
            )

    def stop(self, *, manage_background: bool = True) -> None:
        if manage_background:
            from .background import stop_service

            stop_service(self.root)
        targets = {process.pid: process for process in self.processes()}
        for process in list(targets.values()):
            try:
                for child in process.children(recursive=True):
                    command = child.cmdline()
                    if any(
                        command[index] == "-m" and command[index + 1] in {"brodi.windows_uia", "brodi.hotkey"}
                        for index in range(len(command) - 1)
                    ):
                        targets[child.pid] = child
            except psutil.Error:
                pass
        for process in reversed(list(targets.values())):
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(list(targets.values()), timeout=2)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(alive, timeout=1)
        if alive:
            raise RuntimeError("Brodi could not stop. Open the log before restarting.")

    def restart(self) -> None:
        self.stop()
        self.start()

    def set_mic_muted(self, pid: int, muted: bool) -> None:
        from .mic_control import request_mute

        if pid not in {process.pid for process in self.processes()}:
            raise RuntimeError("The voice session ended. Start Brodi and try again.")
        request_mute(pid, muted)

    def show_activity(self) -> None:
        arguments = [self.python, "-m", "brodi.activity", "--path", str(default_path())]
        if pid := self.leaf_pid(self.processes()):
            arguments.extend(["--pid", str(pid)])
        else:
            arguments.append("--once")
        if sys.platform == "darwin":
            folder = user_data_dir()
            folder.mkdir(parents=True, exist_ok=True)
            command = folder / f"activity-{checkout_id(self.root)}.command"
            command.write_text(
                "#!/bin/zsh\ncd -- " + shlex.quote(str(self.root)) + "\nexec " + shlex.join(arguments) + "\n",
                encoding="utf-8",
            )
            command.chmod(0o700)
            subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(command)], cwd=self.root)
        else:
            subprocess.Popen(arguments, cwd=self.root, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))

    def show_log(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)
        arguments = (
            ["/usr/bin/open", "-a", "TextEdit", str(self.log_path)]
            if sys.platform == "darwin"
            else ["notepad.exe", str(self.log_path)]
        )
        subprocess.Popen(arguments, cwd=self.root)


def tray_image(state: str, root: Path = ROOT):
    """A template microphone on macOS; the official artwork with a Windows badge."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    if sys.platform == "darwin":
        ink = (0, 0, 0, 255)
        draw.rounded_rectangle((25, 9, 39, 36), radius=7, fill=ink)
        draw.arc((18, 20, 46, 46), 0, 180, fill=ink, width=4)
        draw.line((32, 46, 32, 54), fill=ink, width=4)
        draw.line((23, 54, 41, 54), fill=ink, width=4)
        if state in {"muted", "stopped", "attention"}:
            draw.line((13, 13, 51, 51), fill=(0, 0, 0, 0), width=10)
            draw.line((13, 13, 51, 51), fill=ink, width=4)
        if state in {"stopped", "starting"}:
            image.putalpha(image.getchannel("A").point(lambda alpha: alpha // 2))
    else:
        with Image.open(root / "assets" / "branding" / "brodi.ico") as source:
            image = source.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(image)
        color = {"listening": "#16a36a", "muted": "#d98b18", "attention": "#d64444", "starting": "#5086d9"}.get(
            state, "#777d88"
        )
        draw.ellipse((39, 39, 63, 63), fill=color, outline="white", width=3)
        if state in {"muted", "stopped"}:
            draw.line((46, 51, 56, 51), fill="white", width=3)
        elif state == "attention":
            draw.line((51, 45, 51, 52), fill="white", width=3)
            draw.ellipse((50, 55, 52, 57), fill="white")
    return image


class TrayApp:
    def __init__(self, controller: AgentController | None = None):
        self.controller = controller or AgentController()
        self.status = "Stopped"
        self.running = False
        self.pid: int | None = None
        self.mic_muted: bool | None = None
        self.mic_available = False
        self.hotkey_available = False
        self.error = ""
        self.action = ""
        self.busy = threading.Lock()
        self.closing = threading.Event()
        self.reader = JournalReader(default_path())
        self.states: dict[int, str] = {}
        self.mic_states: dict[int, tuple[bool, bool]] = {}
        self.hotkey_states: dict[int, bool] = {}
        self.icon = None
        self._last_display = None
        self._images = {}

    @property
    def summary(self) -> str:
        if self.action:
            return {
                "start": "Starting…",
                "stop": "Stopping…",
                "restart": "Restarting…",
                "quit": "Quitting…",
                "toggle_mic": "Updating microphone…",
            }.get(self.action, self.status)
        if not self.running:
            return "Stopped"
        if self.mic_muted is not None:
            return "Mic muted" if self.mic_muted else "Listening"
        return "Starting…"

    @property
    def visual_state(self) -> str:
        if self.error:
            return "attention"
        if not self.running:
            return "stopped"
        if self.mic_muted is None or self.action in {"start", "restart"}:
            return "starting"
        return "muted" if self.mic_muted else "listening"

    def refresh(self) -> None:
        processes = self.controller.processes()
        active = {process.pid for process in processes}
        pid = self.controller.leaf_pid(processes) if processes else None
        if pid != self.pid:
            self.error = ""
        self.pid, self.running = pid, bool(processes)
        for event in self.reader.poll():
            event_pid = event.get("pid")
            if event_pid not in active:
                continue
            details = event.get("details") or {}
            if isinstance(details, dict) and isinstance(details.get("mic_muted"), bool):
                self.mic_states[event_pid] = (details["mic_muted"], bool(details.get("mic_available", True)))
                self.hotkey_states[event_pid] = bool(details.get("hotkey_available", False))
            if event["kind"] in {"status", "error"}:
                self.states[event_pid] = short_text(event["message"])
                if event_pid == pid:
                    self.error = short_text(event["message"]) if event["kind"] == "error" else ""
        self.states = {pid: state for pid, state in self.states.items() if pid in active}
        self.mic_states = {pid: state for pid, state in self.mic_states.items() if pid in active}
        self.hotkey_states = {pid: state for pid, state in self.hotkey_states.items() if pid in active}
        if pid is not None and (snapshot := read_state(pid)) and isinstance(snapshot.get("mic_muted"), bool):
            self.mic_states[pid] = (snapshot["mic_muted"], bool(snapshot.get("mic_available")))
            self.hotkey_states[pid] = bool(snapshot.get("hotkey_available"))
        self.status = self.states.get(pid, "Starting…") if self.running else "Stopped"
        self.mic_muted, self.mic_available = self.mic_states.get(pid, (None, False))
        self.hotkey_available = self.hotkey_states.get(pid, False)
        self.update_ui()

    def dispatch_ui(self, callback) -> None:
        # Cocoa menus, icons and their lifecycle must stay on the main thread.
        if sys.platform == "darwin" and threading.current_thread() is not threading.main_thread():
            from PyObjCTools.AppHelper import callAfter

            callAfter(callback)
        else:
            callback()

    def update_ui(self) -> None:
        if self.icon is None or self.closing.is_set():
            return

        def update():
            if self.closing.is_set():
                return
            display = (
                self.summary,
                self.status,
                self.error,
                self.visual_state,
                self.mic_available,
                self.hotkey_available,
                self.busy.locked(),
            )
            if display == self._last_display:
                return
            self.icon.title = short_text(
                f"Brodi — {self.summary}" + (f" · {self.status}" if self.status != self.summary else ""), 127
            )
            if self.visual_state in self._images:
                self.icon.icon = self._images[self.visual_state]
            self.icon.update_menu()
            self._last_display = display

        self.dispatch_ui(update)

    def perform(self, action: str) -> bool:
        if action not in ACTIONS or not self.busy.acquire(blocking=False):
            return False
        return self._perform(action)

    def _perform(self, action: str) -> bool:
        self.action, self.error = action, ""
        self.update_ui()
        try:
            if action == "quit":
                self.controller.stop()
                self.closing.set()
                if self.icon is not None:
                    self.dispatch_ui(self.icon.stop)
            elif action == "toggle_mic":
                if self.pid is None or not self.mic_available or self.mic_muted is None:
                    raise RuntimeError("Microphone controls are not ready. Start or restart Brodi.")
                self.controller.set_mic_muted(self.pid, not self.mic_muted)
            else:
                getattr(self.controller, action)()
            return True
        except Exception as exc:
            self.error = short_text(exc)
            log.error("Tray action %s failed: %s", action, self.error)
            # The persistent menu row still explains errors if notifications are unavailable.
            if self.icon is not None and self.icon.HAS_NOTIFICATION:
                try:
                    self.icon.notify(self.error, "Brodi needs attention")
                except Exception:
                    log.warning("Could not display tray notification", exc_info=True)
            return False
        finally:
            self.action = ""
            self.busy.release()
            self.update_ui()

    def callback(self, action: str):
        def invoke(icon, item):
            # Reserve before starting the worker: rapid clicks cannot queue conflicting actions.
            if action in ACTIONS and self.busy.acquire(blocking=False):
                threading.Thread(target=self._perform, args=(action,), daemon=True, name="brodi-tray-action").start()

        return invoke

    def menu(self, pystray):
        item = pystray.MenuItem
        shortcut = "Control+Space" if sys.platform == "darwin" else "Ctrl+Space"

        def ready(_):
            return not self.busy.locked()

        return pystray.Menu(
            item(lambda _: "Brodi — " + self.summary, None, enabled=False),
            item(
                lambda _: short_text(self.error or self.status),
                None,
                enabled=False,
                visible=lambda _: bool(self.error) or self.status not in {self.summary, "Stopped", "Starting…"},
            ),
            pystray.Menu.SEPARATOR,
            item(
                lambda _: (
                    ("Unmute microphone" if self.mic_muted else "Mute microphone")
                    + ("  ·  " + shortcut if self.hotkey_available else "")
                ),
                self.callback("toggle_mic"),
                enabled=lambda _: ready(_) and self.running and self.mic_available,
            ),
            item(
                "Shortcut unavailable — use the microphone control",
                None,
                enabled=False,
                visible=lambda _: self.running and self.mic_available and not self.hotkey_available,
            ),
            pystray.Menu.SEPARATOR,
            item("Start assistant", self.callback("start"), enabled=lambda _: ready(_) and not self.running),
            item("Stop assistant", self.callback("stop"), enabled=lambda _: ready(_) and self.running),
            item("Restart assistant", self.callback("restart"), enabled=lambda _: ready(_) and self.running),
            pystray.Menu.SEPARATOR,
            item("Show activity", self.callback("show_activity"), default=True, enabled=ready),
            item("Open log", self.callback("show_log"), enabled=ready),
            pystray.Menu.SEPARATOR,
            item("Quit Brodi", self.callback("quit"), enabled=ready),
        )

    def run(self) -> None:
        import pystray

        icon_class = pystray.Icon
        if sys.platform == "darwin":
            import AppKit

            application = AppKit.NSApplication.sharedApplication()
            application.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

            class TemplateIcon(pystray.Icon):
                def _assert_image(self):
                    # pystray exposes no template option; mark its generated NSImage so
                    # macOS supplies the correct contrast in light and dark menu bars.
                    super()._assert_image()
                    self._icon_image.setTemplate_(True)

            icon_class = TemplateIcon
        self._images = {
            state: tray_image(state, self.controller.root)
            for state in ("stopped", "starting", "listening", "muted", "attention")
        }
        self.refresh()
        self.icon = icon_class("brodi", self._images[self.visual_state], "Brodi — " + self.summary, self.menu(pystray))

        def monitor(icon):
            self.dispatch_ui(lambda: setattr(icon, "visible", True))
            while not self.closing.is_set():
                try:
                    self.refresh()
                except Exception as exc:
                    log.error("Tray refresh failed: %s", clean(exc))
                    self.error = "Status unavailable — open the log"
                    self.update_ui()
                self.closing.wait(0.5)

        try:
            self.icon.run(setup=monitor)
        finally:
            self.closing.set()


def acquire_instance(root: Path = ROOT):
    """Hold one tray per checkout; OS releases the lock after a crash, too."""
    name = checkout_id(root)
    if sys.platform == "darwin":
        import fcntl

        folder = user_data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stream = (folder / f"tray-{name}.lock").open("a")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return None
        return stream
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, "Local\\BrodiTray-" + name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return None
    instance = ExitStack()
    instance.callback(kernel.CloseHandle, handle)
    return instance


def main() -> None:
    parser = argparse.ArgumentParser(description="Brodi macOS menu-bar and Windows tray controls")
    parser.add_argument("--force", action="store_true", help="show even if automatic tray is disabled")
    parser.add_argument(
        "--action",
        choices=["start", "stop", "restart", "status", "show_activity", "show_log"],
        help="run a desktop control without opening another tray",
    )
    options = parser.parse_args()
    if sys.platform not in {"win32", "darwin"}:
        parser.error("Desktop tray controls are available on macOS and Windows.")
    load_environment()
    if options.action:
        controller = AgentController()
        try:
            if options.action == "status":
                print("Brodi is running." if controller.processes() else "Brodi is stopped.")
            else:
                getattr(controller, options.action)()
        except Exception as exc:
            parser.exit(1, f"Brodi: {clean(exc)}\n")
        return
    if not options.force and not flag("BRODI_TRAY"):
        return
    data = user_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=data / "tray.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    instance = acquire_instance()
    if instance is None:
        return
    try:
        TrayApp().run()
    except Exception:
        log.exception("Tray stopped unexpectedly")
        raise
    finally:
        instance.close()


if __name__ == "__main__":
    main()
